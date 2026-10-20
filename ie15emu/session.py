"""Режимы работы терминала 15ИЭ-00-013 (аналог клавиши SEND).

  АВТОНОМНО (LOCAL) — набор идёт в локальный буфер и сразу на экран
                      терминала, обмена с линией нет;
  С ЭВМ (HOST)     — каждый знак немедленно уходит в линию
                      (токовая петля / SSH), эхо приходит от ЭВМ;
  SEND             — «передать накопленное»: байты буфера уходят в линию,
                      буфер очищается. Так же работает клавиша SEND
                      на реальной клавиатуре 15ВВВ-97-006.

Сеанс (`TerminalSession`) не знает о транспорте: он возвращает байты для
линии, а отправку делает вызывающая сторона через `emit()` / сам session
через `link` (если подключён).
"""
from __future__ import annotations

import base64
import re
import time

from . import COLS, ROWS, __version__
from .charset import (decode_koi7, encode_koi7, koi7_display_upper,
                      koi7_raw_upper,
                      normalize_incremental, to_line)
from .link import LinkError, parse_probe
from .keyboard import (DEFAULT_LAYOUT, ctrl_code, is_key_combo, key_to_bytes,
                       KEY_CTRLSELDOWN, KEY_CTRLSELLEFT, KEY_CTRLSELRIGHT,
                       KEY_CTRLSELUP, KEY_SELDOWN, KEY_SELLEFT,
                       KEY_SELRIGHT, KEY_SELUP,
                       KEY_SELHOME, KEY_SELEND, KEY_SELBSPACE, KEY_HELP)
from .screen import HELP, RULE, cell_text, mk
from .undo import DEFAULT_UNDO, UndoStack

MODE_LOCAL = "local"      # «АВТОНОМНО»
MODE_HOST = "host"        # «С ЭВМ»

# Тип линии по баннеру SIMH: «Connected to DKS» — ДКС, иначе КВУ.
# В подвале показывается как «ЛИНИЯ=КВУ» / «ЛИНИЯ=ДКС» (обрыв — «НЕТ»).
LINE_TYPE_KVU = "КВУ"
LINE_TYPE_DKS = "ДКС"

# имена клавиш-команд (не знаки и не вставка — ветки feed_key ниже)
COMMAND_KEYS = frozenset((
    "UNDO", "SEND", "MODE", "CMDSET", "BLINK", "ECHO", "CLEAR",
    "SELCLEAR", "SELTAKE", "SELBSPACE", "HELP",
))

# Индикация последней нажатой управляющей клавиши: клавиша=отданный код
CONTROL_HINTS = {
    "KEY_ENTER": "ENTER=0D0A", "KEY_BACKSPACE": "BKSP=08", "\x7f": "BKSP=08",
    "\t": "TAB=09", "\x07": "BEL=07", "KEY_ESC": "ESC=1B", "\x1b": "ESC=1B",
    "KEY_UP": "UP=ESCA", "KEY_DOWN": "DOWN=ESCB", "KEY_RIGHT": "RGHT=ESCC",
    "KEY_LEFT": "LEFT=ESCD", "KEY_HOME": "HOME=ESCH", "KEY_END": "END=ESCK",
    "KEY_PAGEUP": "ЭКРАН↑", "KEY_PAGEDOWN": "ЭКРАН↓",
    "KEY_WHEELUP": "КОЛЕСО↑", "KEY_WHEELDOWN": "КОЛЕСО↓",
    "KEY_INSERT": "INS=ESCb", "KEY_DELETE": "DEL=ESCc",
    "KEY_CTRLRIGHT": "СЛОВО=^→", "KEY_CTRLLEFT": "СЛОВО=^←",
    "KEY_CTRLUP": "СТРОКА=^↑", "KEY_CTRLDOWN": "СТРОКА=^↓",
    "SELHOME": "ВЫД=^ДОМ", "SELEND": "ВЫД=^КОНЕЦ",
    "SELBSPACE": "ВК-ВЫДЕЛ",
    "SEND": "SEND", "MODE": "СЕАНС", "CMDSET": "НАБОР", "BLINK": "УПР.СИМВ",
    "ECHO": "ЭХО", "CLEAR": "ОЧИСТКА", "HELP": "СПРАВКА=F12",
}


LAYOUT_LABELS = {"phonetic": "ФОН", "positional": "ПОЗ"}

# Предел буфера набора в АВТОНОМНО (знаков). 0 — не ограничивать.
#
# Настоящий 15ИЭ ограничен строкой: ДИСПАК принимает в строку ввода
# ограниченное число знаков, дальше — «ПЕРЕПОЛНЕН БУФЕР ВВОДА» (boek1.be:
# счётчик НОМСИМВ, начальное значение 60₈). Здесь предел задаёт конфиг
# ([terminal] buffer): при переполнении лишние знаки не принимаются,
# подвал показывает «БУФЕР=ПОЛОН», и оператор отдаёт накопленное
# (ПЕРЕДАЧА, F10) либо снимает его (F3).
DEFAULT_BUFFER = 4096


def _find_break(buf, pos: int, end: int) -> int:
    """Позиция ближайшего разрыва строки (ПР или ПС) в буфере [pos, end).

    -1 — разрыва нет. Поиск байта идёт в C: буфер набора бывает большой,
    и просмотр каждого знака делал нажатие клавиши дорогим.
    """
    a = buf.find(b"\r", pos, end)
    b = buf.find(b"\n", pos, end)
    if a < 0:
        return b
    if b < 0:
        return a
    return a if a < b else b

WHEEL_STEP = 3       # рядов выдачи на щелчок колеса мыши

# УДЕРЖАНИЕ ЭКРАНА (пауза выдачи ЭВМ) — клавиша Ctrl+L.
#
# Это команда 15ИЭ «Сброс» (код 014₈ = 0x0C). Если линия объявлена ОС
# терминалом «Э» (приказ НУС <N> 1 — «ТИП ТЕРМ: 1 → ЭЛЕКТРОНИКА»), ДИСПАК
# трактует 0x0C как DEL/ERASE и отменяет идущую выдачу: boek1.be (диспетчер
# «упркод» для нестандартных терминалов, код 12) → swcv1 → mott.be
# «DELERA» → ЗАЧЕМ → ОТМВД — «ОТМЕНА ВЫДАЧИ»: сброс буфера приёма и
# признак BREAK. На линиях типа «T» (Видеотон) код до ОС доходит, но
# игнорируется — там поток останавливает сам эмулятор.
#
# Независимо от типа линии удержание останавливает поток на стороне
# эмулятора: приём из линии прекращается, выдача копится в буфере сокета и
# машина встаёт — защита от зациклившихся программ, которые «заливают»
# экран. Снятие удержания (Ctrl+L ещё раз или Ctrl+Q) возвращает окно к
# курсору: накопленное дочитывается и показывается.
HOLD_KEYS = ("ctrl+l", "ctrl+shift+l", "\x0c")   # что приходит с терминала
HOLD_CODE = b"\x0c"          # команда «Сброс» — уходит в линию
RESUME_KEYS = ("ctrl+q", "ctrl+shift+q", "\x11")  # снятие удержания

# Движения курсора не являются шагом отмены: навигация буфер не правит,
# и откат по слову через неё не прерывается (набор до и после движения
# откатывается одним шагом)
CURSOR_MOVE_KEYS = frozenset((
    "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT",
    "KEY_CTRLLEFT", "KEY_CTRLRIGHT", "KEY_CTRLUP", "KEY_CTRLDOWN",
    "KEY_HOME", "KEY_END",
    "SELUP", "SELDOWN", "SELLEFT", "SELRIGHT",
    "CTRLSELUP", "CTRLSELDOWN", "CTRLSELLEFT", "CTRLSELRIGHT",
    "SELHOME", "SELEND",
    "KEY_PAGEUP", "KEY_PAGEDOWN",
    "KEY_WHEELUP", "KEY_WHEELDOWN",
))

# разделители слова для прыжков Ctrl+←/→ по набору (АВТОНОМНО)
WORD_DELIMS = b" ."

# «Пробел поддержки»: программы БЭСМ-6, ждущие ввода с терминала, по
# тишине на линии считают сеанс уснувшим и сбрасывают его. Раз в минуту
# шлём в линию пробел — линия не молчит, а на экран он не попадает:
# эхо пробела снимается до разбора (см. _strip_pulse_echo).
PULSE_CHAR = b" "          # что шлём
PULSE_INTERVAL = 60.0      # секунд между пробелами (0 — выключено)
PASTE_UNDO_GAP = 0.5       # сек: чтения подряд = продолжение одной
                           # вставки — тогда отмена удаляет её целиком
PULSE_ECHO_TTL = 5.0       # с: сколько ждём эхо своего пробела; позже —
                           # не гасим (чтобы не съесть чужой пробел в выводе)

# Проверка живости линии: раз в сколько секунд спрашиваем у линии
# (моста SSH) состояние (см. TerminalSession.link_check_tick). Без неё
# связь может пропасть «молча» — SSH-соединение не рвётся, данных нет,
# а в строке состояния так и горит «ЛИНИЯ=ДКС» при давно ушедшей связи.
CHECK_INTERVAL = 60.0      # секунд между проверками (0 — выключено)


# Баннер tmxr/telnet SIMH: «CONNECTED TO THE PDP-11/70 SIMULATOR TTY
# DEVICE, LINE 3», «ENCODING IS RAW», «WRU …», дата-время, «From 10.0.0.1».
# Он идёт в строку состояния над кадром, а не в кадр ЭВМ.
BANNER_LINE = re.compile(
    rb"simulator\s+tty\s+device"           # … TTY DEVICE, LINE N
    rb"|encoding\s+is\s+\S+"               # … RAW / UTF-8 / KOI-7
    rb"|connected\s+to\s+dks"              # тип линии ДКС
    rb"|prompt\s+character"                # приглашение терминала
    rb"|^\s*wru\b"                          # ответ на WRU
    rb"|^\s*\w{3}\s+\w{3}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4}"   # дата
    rb"|\bfrom\s+\d{1,3}(?:\.\d{1,3}){3}",                       # From IP
    re.IGNORECASE)
BANNER_LINE_NO = re.compile(rb"TTY\s+DEVICE,\s*LINE\s+(\d+)", re.IGNORECASE)
BANNER_MODE = ((rb"raw", "RAW"), (rb"utf-?8", "UNICODE"),
               (rb"koi-?7", "JCUKEN"), (rb"jcuken", "JCUKEN"))
# начала служебных строк баннера: по ним решаем, стоит ли ждать перевода
# строки (если не начинается на них — это уже выдача ЭВМ, отдаём сразу)
BANNER_HEADS = (b"connected", b"encoding", b"wru", b"prompt", b"from",
                b"sun", b"mon", b"tue", b"wed", b"thu", b"fri", b"sat")


# --- unicode-вывод поверх raw-линии (оболочка SIMH) ---------------------
# Приглашение оболочки SIMH в трёх формах, в каких оно приходит:
#   «set>»/«SET>» — ASCII (и КОИ7 в позиции 0x60.. — тот же ASCII),
#   «СЕТ>» — unicode (SIMH печатает utf8),
#   «СЕТ>» — кодами КОИ7 с битом алфавита (0x93 0x85 0x94).
# Ищем по сырым данным (до перекодировок): `_sniff` — окно последних
# 200 байт приёма, оно же переживает разрыв приглашения между чтениями.
_SIMH_PROMPT = (b"\xd0\xa1\xd0\x95\xd0\xa2>", b"\x93\x85\x94>")


def _simh_prompt(buf: bytes) -> bool:
    """Есть ли в куске приглашение оболочки SIMH («set>»/«СЕТ>»)."""
    return re.search(rb"(?i)set>", buf) is not None \
        or any(p in buf for p in _SIMH_PROMPT)


def _looks_like_utf8(data: bytes) -> bool:
    """Кусок похож на unicode-текст: кириллица U+0400…U+04FF в UTF-8
    (пары 0xD0/0xD1 + 0x80..0xBF). В сырых 7-разрядных кодах линии и во
    внутреннем потоке КОИ7 (0x00..0x1F/0x60..0x7E/0x80..0x9E/0xE0..0xFE)
    таких байтов не бывает — ложное срабатывание исключено. Хвост пары
    в конце куска считается признаком: дописывается следующим чтением.
    """
    for i, b in enumerate(data):
        if b in (0xD0, 0xD1):
            if i + 1 == len(data):
                return True            # неполная пара — ждём продолжения
            if 0x80 <= data[i + 1] <= 0xBF:
                return True
    return False


def _banner_open(line: bytes) -> bool:
    """Похоже ли начало строки на начало строки баннера."""
    low = line.strip().lower()
    return not low or any(h.startswith(low) for h in BANNER_HEADS)


class TerminalSession:
    """Состояние терминала: режим работы + накопленный буфер автономного набора."""

    def __init__(self, parser, link=None, mode: str = MODE_LOCAL,
                 koi7: bool = False, encoding: str = "utf8",
                 layout: str | None = None,
                 keymap: dict[str, str] | None = None,
                 undo_depth: int = DEFAULT_UNDO,
                 buffer_limit: int = DEFAULT_BUFFER) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        self.parser = parser
        self.link = link
        self.mode = mode
        # layout — выбранная русская раскладка; koi7 (старый флаг) включает
        # раскладку по умолчанию (позиционную).
        if layout is None and koi7:
            layout = DEFAULT_LAYOUT
        # инвариант клавиатуры Н1 (тот же, что у сеттера nabor): раскладка
        # живёт только в Н1 — в Н0/Н2 её нет, иначе позиционная таблица
        # съедала бы знаки международной строки при старте с config
        if getattr(parser.screen, "display_set", "n2") != "n1":
            layout = None
        self.layout = layout
        # переназначение из [keys] конфига: клавиша → знак/строка/KEY_имя
        self.keymap = keymap or {}
        self.default_layout = layout     # раскладка, включаемая набором Н1
        self.koi7 = layout is not None
        self.buffer = bytearray()
        # предел буфера набора (знаков): 0 — не ограничивать. Задаётся
        # конфигом ([terminal] buffer) — по умолчанию DEFAULT_BUFFER.
        self.buffer_limit = max(0, int(buffer_limit))
        # отмена правок набора (АВТОНОМНО): снимки буфера, глубина — из
        # конфига ([terminal] undo), 0 — выключена
        self.undo = UndoStack(undo_depth)
        self.echo = True             # клавиша «ЭХО» (F6): вывод набираемого
                                     # на экран; снята — пароли не печатаются
        self.insert_mode = True      # Insert: вставка (True) / замена (False)
        self._buf_pos = 0            # позиция курсора в буфере набора
        # выделение в буфере набора (Shift+Home/End, Shift+стрелки):
        # пара (начало, конец) и подсвечивающие его фрагменты кадра
        self._buf_sel: tuple[int, int] | None = None
        self._buf_sel_frags: list = []
        self._echo_pending = bytearray()   # эхо-ответ линии, ожидаемый при
                                     # выключенном эхе (срезается с приёма)
        self._echo_top = None        # строка, с которой началось локальное эхо блока
        self._echo_from = 0          # индекс буфера, с которого начата печать
        self._echo_to = 0            # индекс буфера, на котором печать кончилась
        self._echo_bottom = None     # нижний ряд напечатанного блока
        self.encoding = encoding     # «utf8» | «raw» | «dks» — передача
        self.rx_encoding = encoding  # приём (у ДКС-линии — raw без NUL-преобразований)
        self.enc_decided = False     # баннер «Encoding is …» обработан
        self.last_key = ""           # последняя управляющая клавиша (панель)
        self._carry = b""            # незавершённый хвост UTF-8 из линии
        self._sniff = b""            # окно поиска баннера
        # строка состояния линии (над кадром, в кадр не входит)
        self.line_type = ""          # КВУ | ДКС — по баннеру «Connected to DKS»
        self.line_no: int | None = None      # «TTY DEVICE, LINE N»
        self.line_mode = ""          # RAW | UNICODE | JCUKEN
        self.machine = ""            # номер ЭВМ (из конфига, пусто — не показываем)
        self.peer = ""               # адрес, с которого пришли («From …»)
        self.banner_seen = False     # баннер линии («CONNECTED TO …») пришёл
        self.line_lost = False       # линия потеряна (таймаут/разрыв SSH):
                                     # сеанс продолжается, отправки подавлены
        # сообщение от ЭВМ в АВТОНОМНО: кадр занят набором — рисовать его
        # нельзя (эхо перетрёт), поэтому индикатор в строке состояния:
        # текст (последние 120 знаков) и сколько их пришло
        self.incoming = ""
        self.incoming_n = 0
        self._simh_shell = False   # «set>»: оболочка SIMH, набор уже Н0
        # «пробел поддержки»: интервал (сек), когда следующий уйдёт и до
        # какого срока ждём его эхо, чтобы снять с экрана
        self.pulse_interval = PULSE_INTERVAL
        self._pulse_next = time.time() + PULSE_INTERVAL
        self._pulse_echo_until = 0.0
        # проверка живости линии (моста): интервал (сек) и состояние
        # последней проверки («ЖИВ»/«НЕТ»/«» — ещё не проверяли)
        self.check_interval = CHECK_INTERVAL
        self._check_next = time.time() + CHECK_INTERVAL
        self.check_state = ""
        self.check_info = ""       # отчёт моста (последний)
        self._last_paste = 0.0     # монотонное время последней части вставки
        self._paste_pending = bytearray()   # хвост вставки без ПР: ждёт
                                            # продолжения (см. _wrap_paste)
        # запись управляющих последовательностей НАПРЯМУЮ терминалу
        # (буфер обмена OSC52: копирование/вставка); ставит run_line,
        # None — писать некуда (тесты, headless)
        self.term_write = None
        # callback повторного соединения: ВВОД в С ЭВМ при «ЛИНИЯ=НЕТ»
        # (ставит run_line; None — переподключаться некуда)
        self.reconnect = None
        self.mouse_on = False   # мышь терминала отдана эмулятору (F-подвал)
        # удержание экрана: выдача ЭВМ остановлена, эмулятор линию не
        # читает (см. HOLD_KEYS и главный цикл сеанса)
        self.hold = False
        # ждём эхо нашей команды «Сброс» (0x0C) — его надо срезать,
        # иначе кадр увидит «ДОМ» и курсор уйдёт в начало
        self._hold_echo = False
        self._banner_tail = b""      # недобранная строка баннера
        self._banner_done = False    # баннер закончился (пошли данные ЭВМ)
        self._update_service()
        self._update_status()

    # --- режим ------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        was_host = self.mode == MODE_HOST
        self.flush_paste()                 # отложенный хвост вставки — в буфер
        self.mode = mode
        # смена режима закрывает индикатор «ОТ ЭВМ»: в С ЭВМ сообщения
        # идут в кадр по-старому, накопленное в АВТОНОМНО больше не нужно
        self.incoming = ""
        self.incoming_n = 0
        self.parser.screen.notice = ""     # и выпадающую строку
        if mode == MODE_LOCAL and was_host:
            # переход С ЭВМ → АВТОНОМНО: кадр становится началом набора
            self._frame_to_buffer()
        self._update_service()
        self._update_status()

    def toggle_mode(self) -> str:
        self.set_mode(MODE_HOST if self.mode == MODE_LOCAL else MODE_LOCAL)
        return self.mode

    def _frame_to_buffer(self) -> None:
        """Переход С ЭВМ → АВТОНОМНО: текст кадра добавляется в буфер набора.

        Кадр ЭВМ переходит к оператору: набор продолжает текст экрана,
        ПЕРЕДАЧА отдаёт и его. Копируются ряды ниже собственного эха
        набора (если эхо видно на кадре), иначе весь кадр; пустые ряды
        сверху и снизу текста не переносятся, пустые ряды внутри —
        сохраняются. Сам кадр не перерисовывается: текст уже на нём, а
        `_echo_top` получает ряд кадра, с которого начинается буфер, —
        следующий знак правит буфер и перерисовывает эхо с учётом
        перенесённого текста (кадр становится окном на буфер, как при
        обычном наборе больше экрана). Ничего не стирается: строки ЭВМ,
        ушедшие при печати за край кадра, остаются в буфере, а не
        пропадают.

        Ряд кадра — одна строка буфера (разделитель ПР, как у Enter).
        Перенос отменяем: Ctrl+U возвращает буфер к состоянию до
        перехода. Закреплённый просмотр истории (PgUp) снимается —
        набор должен быть виден.
        """
        sc = self.parser.screen
        had_echo = bool(self.buffer) and self._echo_top is not None
        if had_echo:
            # эхо набора на кадре — кадр добавляем ниже него
            echo_end = min(self._echo_to, len(self.buffer))
            copy_from = max(0, self._echo_top) + self._buf_rows_up_to(echo_end)
        else:
            copy_from = 0
        copy_from = max(0, min(copy_from, sc.rows - 1))
        bottom = -1               # последний непустой ряд кадра ниже copy_from
        for y in range(sc.rows - 1, copy_from - 1, -1):
            if any(c != 0x20 for c in sc.cells[y]):
                bottom = y
                break
        if bottom < copy_from:
            return                # ниже эха кадр пуст — добавлять нечего
        while copy_from <= bottom and \
                not any(c != 0x20 for c in sc.cells[copy_from]):
            copy_from += 1        # пустые ряды сверху текста не переносим
        self.undo.snapshot(self.buffer)     # перенос кадра — отменяемый шаг
        tail = bytes(self.buffer[self.buffer.rfind(b"\r") + 1:])
        data = bytearray()
        for y in range(copy_from, bottom + 1):
            chunk = self._row_to_buffer(sc.cells[y]).rstrip(b" ")
            if had_echo and tail and y == copy_from and chunk.startswith(tail):
                # незакрытая строка набора продолжена ЭВМ на том же ряду:
                # хвост эха уже в буфере — переносим только продолжение
                chunk = chunk[len(tail):]
            data += chunk + b"\r"
        # предел буфера набора: кадр (до 25×80 знаков) может не влезть —
        # переносим сколько влезет
        data = bytearray(self._buf_clip(bytes(data)))
        self.buffer.extend(data)
        self.undo.step()
        if not had_echo:
            # буфер начался с ряда `copy_from` кадра
            self._echo_top = copy_from
            self._echo_from = 0
        self._echo_to = len(self.buffer)
        self._echo_bottom = min(
            sc.rows - 1,
            max(0, self._echo_top or 0) + self._buf_rows_up_to(self._echo_to))
        self._buf_pos = len(self.buffer)    # набор продолжается ниже текста
        # курсор — куда ляжет следующий знак; у нижнего края прижимается,
        # печать следующего знака сама сдвинет окно, если места нет
        rows, col = self._buf_rowcol(self._buf_pos)
        sc.y = max(0, min(sc.rows - 1,
                          max(0, self._echo_top or 0) + rows))
        sc.x = col
        sc.view_top = None        # набор должен быть виден, а не история

    @staticmethod
    def _row_to_buffer(row: list[int]) -> bytes:
        """Ряд кадра (коды ЭВМ) → коды внутреннего потока буфера набора.

        В кадре русские заглавные — 0x00…0x1E (коды ЭВМ), в буфере —
        с «битом алфавита» 0x80+: иначе «М»=0x0D сливался бы с ПР, «К»=0x0B
        — с УП, и печать эха исполняла бы их как команды. Русские строчные
        кадра (0x60…) получают 0xE0+, латиница и знаки проходят как есть.
        """
        out = bytearray()
        for c in row:
            if 0x00 <= c <= 0x1F:      # русская заглавная (коды ЭВМ)
                out.append(0x80 | c)
            elif 0x60 <= c <= 0x7E:    # русская строчная
                out.append(0xE0 | (c - 0x60))
            else:
                out.append(c)
        return bytes(out)

    def _wrap_paste(self, data: bytes) -> bytes:
        """Строки вставки длиннее COLS знаков ломаются по пробелам.

        Печать рвёт длинную строку по краю кадра как придётся — середина
        слова. Перелом по пробелу: слово идёт на следующий ряд целиком,
        пробел на месте перелома заменяется разрывом строки. Слово
        длиннее ряда (пробелов нет) рвётся по краю кадра — делить его
        нечем. Первый ряд может быть продолжением строки набора (вставка
        в конец набранного) — ширина считается от текущей колонки.

        Вставка приходит кусками (чтения по 256 байт), и граница куска
        режет слово посередине. Поэтому ломаются только ЗАВЕРШЁННЫЕ
        строки (с ПР), а хвост без ПР откладывается (`_paste_pending`) —
        его продолжит следующий кусок; отдать отложенное до того —
        `flush_paste` (любая клавиша, кроме продолжения вставки,
        ПЕРЕДАЧА, смена режима, пауза в кусках).
        """
        seg = bytes(self._paste_pending) + bytes(data)
        self._paste_pending = bytearray()
        out = bytearray()
        col = self._buf_rowcol(self._buf_pos)[1]   # колонка вставки
        line = bytearray()
        for b in seg:
            if b in (0x0D, 0x0A):                  # ПР/ПС вставки — разрыв
                col = self._paste_emit(bytes(line), out, col)
                out += b"\r"
                col = 0
                line = bytearray()
            else:
                line.append(b)
        self._paste_pending += line                # хвост без ПР — в отложку
        return bytes(out)

    def flush_paste(self) -> None:
        """Отдать хвост вставки, ждущий продолжения (см. _wrap_paste).

        Продолжение не пришло (другая клавиша, ПЕРЕДАЧА, смена режима,
        пауза в кусках) — хвост печатается сейчас: перелом по пробелам,
        слово длиннее ряда рвётся по краю кадра. Встаёт в текущую позицию
        курсора и отменяется вместе со своей вставкой (шаг открыт).
        """
        if not self._paste_pending:
            return
        seg = bytes(self._paste_pending)
        self._paste_pending = bytearray()
        out = bytearray()
        col = self._buf_rowcol(self._buf_pos)[1]
        col = self._paste_emit(seg, out, col)
        # предел буфера набора: хвост вставки подрезаем по месту (и говорим,
        # сколько взято — молча обрезать нельзя)
        clipped = self._buf_clip(bytes(out))
        if len(clipped) != len(out):
            self.last_key = (f"ВСТАВКА ОБРЕЗАНА ({len(clipped)} зн.)"
                             if clipped else "ВСТАВКА НЕ ПРИНЯТА (БУФЕР ПОЛОН)")
            out = bytearray(clipped)
        self.undo.snapshot(self.buffer)    # хвост — шаг своей вставки
        self.buffer[self._buf_pos:self._buf_pos] = bytes(out)
        self._buf_pos += len(out)
        if self.echo:
            self._redraw_echo()
        self._update_service()

    def paste_tick(self) -> None:
        """Пауза в кусках вставки (PASTE_UNDO_GAP): хвост больше не
        продолжится — отдать. Вызывается из цикла сеанса на каждой
        итерации (в тестах отдача происходит на следующей клавише).
        """
        if self._paste_pending and \
                time.monotonic() - self._last_paste > PASTE_UNDO_GAP:
            self.flush_paste()

    def _paste_emit(self, seg: bytes, out: bytearray, col: int) -> int:
        """Строка вставки в `out` с переломом по пробелам.

        Возвращает колонку после последнего знака. Соединительный пробел
        не дублируется: перед точкой вставки может уже стоять пробел
        (набранное «НАБРАНО » + вставка «СЛОВО…» — один пробел).
        """

        def need_space() -> bool:
            if col == 0:
                return False
            if out:
                return out[-1] != 0x20
            return not (self._buf_pos and
                        self.buffer[self._buf_pos - 1] == 0x20)

        for w in seg.split(b" "):
            if col and col + 1 + len(w) > COLS:
                out += b"\r"                   # перелом по пробелу
                col = 0
            while len(w) > COLS - col:         # слово шире ряда
                out += w[:COLS - col] + b"\r"
                w = w[COLS - col:]
                col = 0
            if w:
                if need_space():
                    out += b" "
                    col += 1
                out += w
                col += len(w)
        return col

    # --- НАБОР: три режима терминала 15ИЭ (клавиша F8) -------------------
    #   Н0 — знаки ASCII (ISO 646 IRV: ¤ ¬ ¯), команды — только УП;
    #   Н1 — русские знаки верхнего и нижнего регистра (КОИ-7 Н1:
    #        0x40…0x5F — заглавные, 0x60…0x7E — строчные), команды — УП;
    #   Н2 — набор команд №2 (VT-52) — по умолчанию, с ним терминаль
    #        и работает с БЭСМ-6; знаки — международная строка, русские
    #        приходят кодами ЭВМ (как их отдаёт SIMH/ДКС).
    NABORS = ("n0", "n1", "n2")
    NABOR_LABELS = {"n0": "Н0", "n1": "Н1", "n2": "Н2"}   # кириллица на панель

    @property
    def nabor(self) -> str:
        return self.parser.screen.display_set

    @nabor.setter
    def nabor(self, value: str) -> None:
        if value not in self.NABORS:
            raise ValueError(f"неизвестный набор: {value!r}")
        self.parser.mode = 2 if value == "n2" else 1
        self.parser.screen.display_set = value
        # Н1 — национальный алфавит: русская раскладка включена;
        # Н0/Н2 — ASCII/VT-52: клавиши печатают латиницу как есть
        self.layout = self.default_layout if value == "n1" else None
        self.koi7 = value == "n1"

    def _flag_style(self, value: str) -> str:
        """Цвет значения состояния: включено — зелёный, выключено — красный."""
        if value == "ВКЛ":
            return "on"
        if value == "ВЫКЛ":
            return "off"
        return "state"

    def _update_service(self) -> None:
        # Подвал — ячейки «метка=значение» в ровных колонках; между группами
        # пустая ячейка (конец группы → перенос и черта). Значения состояния
        # выделены цветом (ВКЛ — зелёный, ВЫКЛ — красный), группы легенды —
        # своим цветом: F-клавиши в верхнем ряду по порядку F5…F10, сочетания
        # с Ctrl («УПР») и правка (ДОМ/КОНЕЦ/ВСТАВКА/УДАЛИТЬ) — отдельно.
        # Подвал занимает минимум рядов, остальное место окна — кадру ЭВМ.
        sc = self.parser.screen
        host = self.mode == MODE_HOST
        lay = ("ВЫКЛ" if not self.layout
               else LAYOUT_LABELS.get(self.layout, self.layout.upper()))
        def st(label, value, style=None):  # «МЕТКА=значение» с цветом значения
            return mk(label, (value, style or self._flag_style(value)))

        # «ЛИНИЯ=» — единственный индикатор состояния линии: её тип
        # (КВУ | ДКС), а не кодировка (кодировку объявляет сама линия, и она
        # видна в строке состояния над кадром — «РЕЖИМ=RAW»). Пока баннер
        # SIMH не пришёл — тип неизвестен («?»); обрыв связи (в т.ч. мост
        # не отвечает — см. link_check_tick) — «НЕТ»: сеанс продолжается,
        # линия не работает.
        if self.line_lost:
            line = "НЕТ"
        elif self.line_type:
            line = self.line_type           # КВУ | ДКС
        elif getattr(self.link, "banner", False):
            line = "?"                       # баннер ещё не пришёл
        else:
            line = "ЛОК"                     # локальная линия (stdio://)
        last = mk("ПОСЛ: ", (self.last_key, "last"))
        state = [
            # «АВТОНОМНО» — красным и подчёркнутым: набор идёт в буфер
            # терминала, а не в ЭВМ, и потерять его легко
            st("СЕТЬ=", "С ЭВМ" if host else "АВТОНОМНО",
               "state" if host else "local"),
            
            st("НАБОР=", self.NABOR_LABELS[self.nabor]),
            st("ЛИНИЯ=", line,
               "off" if (self.line_lost or line == "?") else None),
            st("РАСК=", lay),
            st("ЭХО=", "ВКЛ" if self.echo else "ВЫКЛ"),
            st("ВИДЕО=", "ИНВ" if sc.inverse else "НОРМ"),
            # «БУФЕР=» не прячем: в режиме удержания оператор может
            # перейти в АВТОНОМНО и набирать — счётчик нужен и там.
            # Буфер полон — показываем это вместо счётчика
            (mk("БУФЕР=", ("ПОЛОН", "off"))
             if self.buffer_limit > 0
             and len(self.buffer) >= self.buffer_limit
             else st("БУФЕР=", str(len(self.buffer)) if self.buffer else "ПУСТО")),
            st("УПР.СИМВ=", "ВКЛ" if self.parser.show_ctrl else "ВЫКЛ"),
            last,                                   # «ПОСЛ» — свой ряд
            RULE,                                 # состояние от справки
            HELP,                                 # дальше — справка (можно убрать)
            # F-клавиши — верхним рядом справки, по порядку F5…F10
            mk(("СБРОС=F3", "fkey")), mk(("ВЗЯТЬ=F4", "fkey")),
            mk(("ОЧИСТКА=F5", "fkey")), mk(("ЭХО=F6", "fkey")),
            mk(("УПР.СИМВ=F7", "fkey")), mk(("НАБОР=F8", "fkey")),
            mk(("СЕТЬ=F9", "fkey")), mk(("ПЕРЕДАЧА=F10", "fkey")),
            "",                                   # конец группы F-клавиш
            # прочая клавиатура терминала
            mk("ВК=Bksp"), mk("ТАБ=Tab"), mk("ЗВН=Ctrl-G"), mk("ПРПС=Enter"),
            mk("ESC=Esc"), mk("КУРСОР=Стрелки"),
            mk("ЭКРАН↑=PgUp"), mk("ЭКРАН↓=PgDn"),
            "",                                   # конец группы клавиатуры
            # сочетания с Ctrl («УПР») — отдельной группой
            mk(("СЛОВО=Ctrl+→", "ctrl")), mk(("СЛОВО=Ctrl+←", "ctrl")),
            mk(("НАЧСТР=Ctrl+↑", "ctrl")), mk(("НИЖСТР=Ctrl+↓", "ctrl")),
            mk(("ВЫД=Shift+Стрелки", "edit")),    # выделение с клавиатуры
            mk(("ВЫДКОЛ=Shift+Ctrl", "edit")),
            # правка текста — отдельной группой
            mk(("ДОМ=Home", "edit")), mk(("СТЕРСТР=End", "edit")),
            mk(("ИНВЕРС=Ins", "edit")), mk(("НОРМ=Del", "edit")),
        ]
        # «АВТОНОМНО» длиннее «С ЭВМ»: ширина колонки сетки не должна
        # зависеть от режима сеанса, иначе на F9 (СЕТЬ↔АВТОНОМНО) все
        # колонки подвала съезжают. Пол «СЕТЬ=АВТОНОМНО» держит колонку
        # (см. Screen._grid) одинаковой в обоих режимах; ставится ДО
        # set_panel — первая же сборка идёт уже с этим минимумом.
        sc.panel_colw_min = len("СЕТЬ=АВТОНОМНО")
        # «ПОСЛ» — длинная ячейка: всегда свой ряд и не влияет на ширину
        # колонок, иначе сетка подвала скачет на каждом нажатии
        sc.set_panel(state, wide=[last])

    # --- выделение текста мышью -------------------------------------
    def _mouse(self, key: str) -> None:
        """Выделение текста мышью (левая кнопка) и колесо.

        Протягивание по кадру или по истории даёт фрагмент; фрагментов
        может быть несколько — каждый следующий протяг добавляет свой.
        С нажатым Ctrl выделение идёт колонкой (прямоугольник знаков),
        без Ctrl — строчное, от нажатой строки до отпущенной.
        Клавиши F3/F4: F3 снимает выделение, а без выделения — очищает
        буфер набора (экран не трогая); F4 забирает выделенное
        (см. `feed_key`).
        Ничего не уходит в линию: выделение — только чтение экрана.
        """
        block = key.startswith("ctrl+")
        if block:
            key = key[5:]
        kind, _, coords = key.partition("@")
        try:
            col, row = (int(v) for v in coords.split(","))
        except ValueError:
            return None
        sc = self.parser.screen
        v = row - sc.view_top_row             # ряд видимой части окна
        if not 0 <= v < len(sc.view_map) or sc.view_map[v] is None:
            if kind == "MOUSEDOWN":
                # клик мимо текста — протяжку отменяем; уже выбранные
                # фрагменты при этом остаются
                sc.sel_cancel()
            elif kind == "MOUSEUP":
                # отпустили над подвалом/за краем — протяжку надо закончить,
                # иначе она повисла бы до следующего щелчка
                sc.sel_end()
                self._update_status()
            # движение мимо текста игнорируем: начало и конец не теряются
            return None
        kind_row, kind_idx = sc.view_map[v]
        x = max(0, min(col - 1, COLS - 1))   # координаты терминала с 1
        if kind == "MOUSEDOWN":
            sc.sel_begin(kind_row, kind_idx, x, block)
        elif kind == "MOUSEUP":
            # отпускание — тоже конец протяжки: быстрый протяг не всегда
            # сопровождается событиями движения, иначе выделялся бы один знак
            sc.sel_drag(kind_row, kind_idx, x)
            sc.sel_end()
            self._update_status()           # «ВЫД» в строке состояния
        else:                                 # MOUSEDRAG
            sc.sel_drag(kind_row, kind_idx, x)
        # подвал не пересобираем: события мыши идут десятками в секунду,
        # а состояние сеанса от них не меняется
        return None

    # --- клавиатура -------------------------------------------------
    def feed_key(self, key: str) -> bytes | None:
        """Нажатие клавиши оператора.

        Возвращает байты, которые нужно немедленно передать в линию
        (в режиме С ЭВМ — сам знак, по SEND — весь накопленный буфер),
        либо None, если передавать нечего.

        keymap (секция [keys] конфига) применяется до правил набора:
        нажатие заменяется знаком/строкой/KEY-именем, пустое значение
        гасит клавишу. В подвале показывается ВСЁ нажатие: знак с
        кодом («б U+0431»), УП («УП 18»), имя с модификатором
        («ctrl+б U+0431») и результат переназначения («ctrl+б→<») —
        по этим строкам и пишут привязки в [keys].
        """
        orig = key
        # отложенный хвост прошлой вставки: продолжение вставки его
        # дописывает, любая другая клавиша — отдает его в буфер первым
        if not (len(key) > 1 and not is_key_combo(key)
                and not key.startswith(("KEY_", "MOUSE", "ctrl+MOUSE"))
                and key not in COMMAND_KEYS) and self._paste_pending:
            self.flush_paste()
        # выпадающая строка сообщения ЭВМ гасится первой же клавишей
        # (или движением мыши) — оператор увидел, пора убирать
        if self.parser.screen.notice:
            self.parser.screen.notice = ""
        # незаконченная протяжка с клавиатуры (Shift+стрелки) живёт, пока
        # не завершат: любой другой знак/клавиша фиксирует фрагмент — как
        # отпускание кнопки мыши при протяжке указателем. Саму мышь
        # (MOUSEDOWN/DRAG/UP) и селект-клавиши это не трогает: у мыши
        # свой жизненный цикл протяжки, селект-клавиши её продолжают.
        if self.parser.screen._drag is not None and \
                key not in (KEY_SELRIGHT, KEY_SELLEFT, KEY_SELUP, KEY_SELDOWN,
                            KEY_CTRLSELRIGHT, KEY_CTRLSELLEFT,
                            KEY_CTRLSELUP, KEY_CTRLSELDOWN) and \
                "MOUSE" not in key:
            self.parser.screen.sel_end()
            self._update_status()           # «ВЫД» в строке состояния
        if key in self.keymap:
            key = self.keymap[key]
            if not key:
                self.last_key = f"{orig}→гашение"
                self._update_service()
                return None
        # УДЕРЖАНИЕ ЭКРАНА: Ctrl+L — команда 15ИЭ «Сброс» (0x0C).
        # Терминал без CSI-u присылает сырой байт 0x0C — ловим и его.
        # Работает только в С ЭВМ: в АВТОНОМНО выдачи машины в кадре нет,
        # а 0x0C — обычный знак набора.
        if key in HOLD_KEYS and self.mode == MODE_HOST:
            self._set_hold(not self.hold)
            return None
        if key in RESUME_KEYS:
            # «Восстановить передачу»: снять удержание, а если его не было —
            # вернуть окно к живой выдаче (следящий режим)
            if self.hold:
                self._set_hold(False)
                return None
            self.parser.screen.view_top = None
            self.last_key = "К ВЫДАЧЕ"
            self._update_service()
            return None
        if len(key) != 1:
            # Многосимвольный текст — вставка (из буфера ОС или --feed).
            # Пока части идут подряд (терминал без меток 200~ шлёт её
            # чтениями по 256 байт), это ОДНА вставка: шаг отмены между
            # частями не ставится и промежуточный снимок не кладётся —
            # цель отката остаётся «состояние до вставки», и Ctrl+U
            # удаляет весь объём целиком, а не по частям
            is_paste_text = (len(key) > 1 and not is_key_combo(key)
                             and not key.startswith(
                                 ("KEY_", "MOUSE", "ctrl+MOUSE")))
            if is_paste_text:
                continuing = (time.monotonic() - self._last_paste
                              <= PASTE_UNDO_GAP)
                self._last_paste = time.monotonic()
            else:
                continuing = False
            if not continuing and key not in CURSOR_MOVE_KEYS:
                # откат по слову: любая не-знаковая клавиша (Enter, ВК,
                # F-клавиши…) заканчивает текущий шаг отмены — следующая
                # правка начнёт новый, а Ctrl+U откатит до этой границы.
                # Движения курсора — не шаг: буфер не правят
                self.undo.step()
        if key.lower() in ("ctrl+shift+c", "ctrl+shift+v"):
            # копирование/вставка из внешних источников (буфер обмена ОС).
            # Без этого ветки нажатие разбиралось бы как УП Ctrl+C/Ctrl+V
            # (0x03/0x16) и уходило бы в линию мусором; сами по себе
            # терминалы эти клавиши перехватывают (обычно Ctrl+Shift+V и
            # вставляет текст — тогда приходит уже текст, он и
            # перекодируется ниже по обычному пути)
            self._ext_clip(key)
            return None
        arrow = "" if orig == key else f"{orig}→"
        hint = CONTROL_HINTS.get(key)
        if hint:
            self.last_key = arrow + hint
            self._update_service()
        elif len(key) == 1 and ord(key) < 0x20:
            self.last_key = arrow + f"УП {ord(key):02X}"
            self._update_service()
        elif is_key_combo(key):
            # нажатие с Ctrl/Alt/Shift без привязки: точное имя — в
            # подвал, его и пишут ключом в [keys]; в линию не уходит.
            # Исключение — Ctrl+ASCII-буква: это управляющий символ
            # (Ctrl+E → 05), его ведёт key_to_bytes (как на 15ВВВ: Ctrl
            # всегда даёт УП). Кириллица с Ctrl — гасится (нет такого УП).
            last = key.rsplit('+', 1)[-1]
            if ctrl_code(key) is None:
                self.last_key = f"{key} U+{ord(last):04X}"
                self._update_service()
                return None
        elif len(key) == 1:
            self.last_key = arrow + f"{key} U+{ord(key):04X}"
            self._update_service()
        elif is_paste_text:
            # многосимвольное нажатие — вставка из буфера обмена (OSC52)
            # или --feed: идёт обычной перекодировкой, в подвале — размер
            # (имена KEY_/MOUSE сюда не попадают — у них свои подсказки)
            self.last_key = f"ВСТАВКА: {len(key)} зн."
            self._update_service()
        if key == "KEY_PAGEUP":
            # АВТОНОМНО с буфером больше кадра — листаем сам набор (см.
            # _buf_page_move); иначе — «история» выдачи, как раньше
            if self._buf_page_move(-1, max(1, self.parser.screen.rows - 1)):
                self._update_service()
                return None
            self.parser.screen.scroll_view(-self.parser.screen._view_h + 1)
            self._update_service()
            return None
        if key == "KEY_PAGEDOWN":
            if self._buf_page_move(+1, max(1, self.parser.screen.rows - 1)):
                self._update_service()
                return None
            self.parser.screen.scroll_view(self.parser.screen._view_h - 1)
            self._update_service()
            return None
        if key in ("KEY_WHEELUP", "KEY_WHEELDOWN"):
            # колесо мыши — то же листание: в АВТОНОМНО с длинным буфером
            # по три строки набора, иначе — «истории» выдачи по три ряда
            # (вверх так же запрещено, пока не выдано больше одной страницы
            # кадра — циклической прокрутки нет)
            step = WHEEL_STEP * (-1 if key == "KEY_WHEELUP" else 1)
            if self._buf_page_move(
                    -1 if key == "KEY_WHEELUP" else +1, WHEEL_STEP):
                self._update_service()
                return None
            self.parser.screen.scroll_view(step)
            self._update_service()
            return None
        if key.startswith(("MOUSEDOWN@", "MOUSEDRAG@", "MOUSEUP@",
                           "ctrl+MOUSEDOWN@", "ctrl+MOUSEDRAG@",
                           "ctrl+MOUSEUP@")):
            return self._mouse(key)
        if key == "KEY_INSERT":
            # Insert: в АВТОНОМНО переключает режим правки набора
            # (вставка ↔ замена); в С ЭВМ — код терминала (ESC b)
            if self.mode == MODE_LOCAL:
                self.insert_mode = not self.insert_mode
                self.last_key = "ВСТАВКА" if self.insert_mode else "ЗАМЕНА"
                self._update_service()
                return None
        if key == "KEY_HOME":
            # В АВТОНОМНО Home — курсор в начало текущей строки набора
            # (в С ЭВМ остаётся код терминала ESC H — «дом»)
            if self.mode == MODE_LOCAL:
                self._buf_sel_reset()
                self._buf_pos = self.buffer.rfind(b"\r", 0, self._buf_pos) + 1
                if self.echo and self.buffer:
                    self._redraw_echo()
                else:
                    self.parser.screen.x = 0
                self.last_key = "ДОМ"
                self._update_service()
                return None
        if key == "KEY_END":
            # В АВТОНОМНО End — курсор в конец текущей строки набора
            # (в С ЭВМ остаётся код терминала ESC K — «стереть до конца»)
            if self.mode == MODE_LOCAL:
                self._buf_sel_reset()
                nxt = self.buffer.find(b"\r", self._buf_pos)
                self._buf_pos = len(self.buffer) if nxt < 0 else nxt
                if self.echo and self.buffer:
                    self._redraw_echo()
                self.last_key = "КОНЕЦ"
                self._update_service()
                return None
        if key in ("KEY_UP", "KEY_DOWN", "KEY_RIGHT", "KEY_LEFT"):
            # В АВТОНОМНО обычные стрелки двигают курсор по буферу набора
            # (в С ЭВМ остаются кодами терминала ESC A/B/C/D). Без этой
            # обработки стрелки проваливались в key_to_bytes и отправляли
            # escape-последовательности в буфер — курсор «уезжал вправо».
            if self.mode == MODE_LOCAL:
                buf, n = self.buffer, len(self.buffer)
                pos = self._buf_pos
                self._buf_sel_reset()
                if key == "KEY_RIGHT":
                    self._buf_pos = min(pos + 1, n)
                elif key == "KEY_LEFT":
                    self._buf_pos = max(pos - 1, 0)
                else:
                    # Вычисляем текущий столбец
                    line_start = buf.rfind(b"\r", 0, pos) + 1
                    col = pos - line_start
                    if key == "KEY_UP":
                        # Предыдущая строка, тот же столбец
                        prev_end = line_start - 1
                        if prev_end <= 0:
                            self._buf_pos = 0
                        else:
                            prev_start = buf.rfind(b"\r", 0, prev_end) + 1
                            self._buf_pos = min(prev_start + col, prev_end)
                    else:  # KEY_DOWN
                        # Следующая строка, тот же столбец
                        next_start = buf.find(b"\r", pos)
                        if next_start < 0:
                            self._buf_pos = n
                        else:
                            next_end = buf.find(b"\r", next_start + 1)
                            next_line_end = n if next_end < 0 else next_end
                            self._buf_pos = min(next_start + 1 + col, next_line_end)
                if self.echo and self.buffer:
                    self._redraw_echo()
                self.last_key = CONTROL_HINTS.get(key, "")
                self._update_service()
                return None
        if key == "SELCLEAR":                # F3
            had_sel = (bool(self._buf_sel)
                       or bool(self.parser.screen.sel_text()))
            self._buf_sel_reset()
            self.parser.screen.sel_clear()
            if had_sel:
                # первое нажатие — снять выделение (как и раньше)
                self.last_key = "ВЫДЕЛЕНИЕ СНЯТО"
                self._update_service()
                self._update_status()       # «ВЫД» в строке состояния
                return None
            # Повторное нажатие (выделения нет) — очистить буфер набора
            # целиком. Экран НЕ трогаем: текст остаётся как есть (в т.ч.
            # локальное эхо) — следующий набор «спустится» ниже, как после
            # вывода ЭВМ. Очистка отменяема (Ctrl+U вернёт буфер).
            self.undo.snapshot(self.buffer)
            self.buffer.clear()
            self._buf_pos = 0
            self.undo.step()
            self.last_key = "БУФЕР ОЧИЩЕН"
            self._update_service()
            self._update_status()           # БУФЕР=ПУСТО, счётчик отмены
            return None
        if key == "SELTAKE":                  # F4 — взять выделенное в буфер
            sc = self.parser.screen
            if sc._drag is not None:          # открытая протяжка — завершить
                sc.sel_end()
                self._update_status()
            text = sc.sel_text()
            sc.sel_clear()
            if text:
                # в АВТОНОМНО — в буфер набора (ПЕРЕДАЧА его отдаст);
                # в С ЭВМ — сразу в линию, знак за знаком
                data = encode_koi7(text + "\n")
                if self.mode == MODE_HOST:
                    self._note_sent(data)
                    if self.link is not None and not self.line_lost:
                        try:
                            self.link.send(to_line(data, self.encoding))
                        except (OSError, LinkError) as e:
                            self.mark_line_lost(str(e))
                else:
                    self.undo.snapshot(self.buffer)     # отменяемая правка
                    data = self._buf_clip(data)        # предел буфера
                    self.buffer.extend(data)
                    self.undo.step()          # взятое — свой шаг отката
            self._update_service()
            self._update_status()           # «ВЫД» в строке состояния
            return None
        if key == "ECHO":
            self.echo = not self.echo
            if not self.echo:
                self._echo_pending = bytearray()
            self._update_service()
            return None
        if key == "CLEAR":
            # F5 «ОЧИСТКА»: в АВТОНОМНО удаляет страницу — то, что сейчас
            # на кадре. Набор: видимая страница буфера; загруженная в кадр
            # страница истории — из истории. В С ЭВМ: экран в историю.
            if self.mode == MODE_LOCAL:
                sc = self.parser.screen
                if sc.view_top is not None and sc.view_top < len(sc.history):
                    # на кадре страница из истории — удаляем её
                    sc.delete_history_page(sc._view_h)
                    self.last_key = "СТР. УДАЛЕНА"
                    self._buf_sel_reset()
                    self._update_service()
                    self._update_status()
                    return None
                span = self._buf_page_span()
                if span and span[1] > span[0]:
                    # видимая страница набора уходит из буфера: стираем
                    # от первого отображённого знака до конца, курсор —
                    # на место удалённого (в пределах буфера)
                    a, b = span
                    self.undo.snapshot(self.buffer)     # отменяемая правка
                    total_before = self._buf_rows_up_to(len(self.buffer))
                    del self.buffer[a:b]
                    self.undo.step()          # страница — свой шаг отката
                    if self._buf_pos > a:
                        self._buf_pos = a
                    self._buf_sel_reset()
                    if self.echo:
                        # курсор — на низ старого эха, чтобы стёрлись и
                        # ряды ниже новой (короткой) печати
                        top = 0 if self._echo_top is None else self._echo_top
                        sc.y = min(sc.rows - 1, top + total_before - 1)
                        self._redraw_echo()
                    self.last_key = "СТР. УДАЛЕНА"
                else:
                    # печатать нечего — просто чистим кадр
                    sc.clear_all()
                    sc.view_top = None
                    self._echo_top = None
                    self.last_key = "ОЧИСТКА"
                self._update_service()
                self._update_status()       # счётчик отмены «ОТМЕНА=»
                return None
            # С ЭВМ: экран в историю, кадр в начало
            self._buf_sel_reset()
            self.parser.screen.clear_screen_kept()
            self._echo_top = None
            self._update_service()
            return None
        if key == "SEND":
            data = self.send()
            if data:
                # «отдать строку»: если оператора не закрыл её Enter'ом,
                # ПЕРЕДАЧА добавляет ПР ПС — иначе Э-60 доэхомит блок
                # в незакрытую строку
                if data[-1:] not in (b"\r", b"\n"):
                    data += b"\r\n"
                if self.echo:
                    # Э-60 (ДКС) всегда локально эхит отданный блок —
                    # стираем свою копию, чтобы эхо машины встала на её
                    # место одной строкой (на serial-линиях эха может не быть)
                    if self.encoding == "dks":
                        self._erase_local_echo()
                else:
                    # эх подавлен: локального вывода не было, а эхо-ответ
                    # линии срежется с приёма (_strip_pending_echo)
                    self._queue_echo(data)
                # как на настоящем терминале: отдав буфер, терминал
                # переходит в режим С ЭВМ
                self.set_mode(MODE_HOST)
            return data
        if key == "UNDO":
            # отмена правки набора (АВТОНОМНО): вернуть буферу состояние
            # до последней правки. В С ЭВМ буфера нет — отменять нечего.
            # Пустой буфер не помеха: очистка F3 как раз отменяется —
            # Ctrl+U вернёт очищенное содержимое (решает стек, а не то,
            # что сейчас в буфере)
            if self.mode == MODE_HOST or not self.undo.available:
                self.last_key = "ОТМЕНА: пусто"
                self._update_service()
                return None
            self.undo.undo(self.buffer)
            self._buf_sel_reset()
            # эхо на экране относятся к прежнему содержимому: перерисуем
            self._undo_echo()
            self.last_key = "ОТМЕНА"
            self._update_service()
            self._update_status()           # счётчик отмены в строке состояния
            return None
        if key == "MODE":
            self.toggle_mode()
            return None
        if key == "CMDSET":
            nxt = self.NABORS[(self.NABORS.index(self.nabor) + 1)
                              % len(self.NABORS)]
            self.nabor = nxt
            self._update_service()
            return None
        if key == "BLINK":
            self.parser.show_ctrl = not self.parser.show_ctrl
            self._update_service()
            return None
        if key == KEY_HELP:
            # F12 — справка подвала в ходе сеанса: прячет/показывает
            # ряды-легенды (состояние и «ПОСЛ» не трогаются). При скрытой
            # справке подвал сам подсказывает, как её вернуть («СПРАВКА=F12»)
            sc_help = self.parser.screen
            sc_help.set_help(not sc_help.show_help)
            self.last_key = "СПРАВКА ВКЛ" if sc_help.show_help \
                else "СПРАВКА СКРЫТА"
            self._update_service()
            self._update_status()     # подсказка «СПРАВКА=F12» — здесь же
            return None
        move = {"KEY_CTRLRIGHT": "word_right", "KEY_CTRLLEFT": "word_left",
                "KEY_CTRLUP": "line_start", "KEY_CTRLDOWN": "line_below"}.get(key)
        if move:
            # Локальная навигация 15ВВВ: курсор двигается по экрану,
            # в линию ничего не уходит (таких кодов у ЭВМ нет).
            # В АВТОНОМНО набор идёт по позиции курсора в буфере, поэтому
            # навигация ведётся по буферу — иначе курсор на экране разошёлся
            # бы с тем местом, куда встанет следующий знак.
            if self.mode == MODE_LOCAL and self.buffer:
                self._buf_sel_reset()
                self._buf_move(key)
                if self.echo:
                    self._redraw_echo()
            else:
                getattr(self.parser.screen, move)()
            self._update_service()
            return None
        if key in (KEY_SELRIGHT, KEY_SELLEFT, KEY_SELUP, KEY_SELDOWN,
                   KEY_CTRLSELRIGHT, KEY_CTRLSELLEFT, KEY_CTRLSELUP,
                   KEY_CTRLSELDOWN):
            # выделение с клавиатуры: Shift+стрелки — построчное,
            # Shift+Ctrl+стрелки — поколонное (как протяжка мышью с Ctrl).
            # Якорь — курсор 15ИЭ, конец тянется за стрелки; завершённое
            # выделение снимается следующим Shift+стрелка (начало нового).
            # Курсор при протяжке не двигается: его двигают стрелки
            # без Shift (в С ЭВМ — отдают коды ЭВМ).
            self._buf_sel_reset()
            sc = self.parser.screen
            dy = -1 if key in (KEY_SELUP, KEY_CTRLSELUP) else \
                1 if key in (KEY_SELDOWN, KEY_CTRLSELDOWN) else 0
            dx = -1 if key in (KEY_SELLEFT, KEY_CTRLSELLEFT) else \
                1 if key in (KEY_SELRIGHT, KEY_CTRLSELRIGHT) else 0
            if sc._drag is None:
                # первая клавиша лишь открывает протяжку от курсора:
                # вбок выделяется знак под курсором, вверх/вниз — строка
                # от курсора (диапазон протяжки включает обе точки)
                sc.kbd_sel_begin(block=key in (KEY_CTRLSELRIGHT,
                                               KEY_CTRLSELLEFT,
                                               KEY_CTRLSELUP,
                                               KEY_CTRLSELDOWN))
            else:                              # продолжение: тянем конец
                sc.kbd_sel_drag(sc._drag[4] + dy, sc._drag[5] + dx)
            n, lines, chars = sc.sel_stats()
            self.last_key = (f"ВЫД: {lines} стр., {chars} зн. (F4 — взять)"
                             if lines else "ВЫД: пусто")
            self._update_service()
            self._update_status()
            return None
        if key in (KEY_SELHOME, KEY_SELEND):
            # Shift+Home / Shift+End. В АВТОНОМНО — выделение части строки
            # набора от курсора до её начала (Home) или до конца (End):
            # выделение живёт в буфере (_buf_sel), подсвечено на экране и
            # удаляется Shift+Backspace. В С ЭВМ клавиш у 15ВВВ нет —
            # нажатие гасится, в линию слово не уходит.
            if self.mode == MODE_LOCAL:
                buf = self.buffer
                pos = self._buf_pos
                if key == KEY_SELHOME:
                    start = buf.rfind(b"\r", 0, pos) + 1    # начало строки
                    end = pos
                else:
                    start = pos
                    nxt = buf.find(b"\r", pos)             # конец строки
                    end = len(buf) if nxt < 0 else nxt
                if end > start:
                    self._buf_sel = (start, end)
                    chars = self._buf_sel_show() if self.echo \
                        else end - start
                    self.last_key = f"ВЫД: {chars} зн. (Shift+ВК — удалить)"
                    self._update_service()
                    self._update_status()
                    return None
                self._buf_sel_reset()
                self.last_key = "ВЫД: пусто"
                self._update_service()
                return None
            # С ЭВМ: то же выделение, но по кадру — как Shift+стрелки
            # (кадр принадлежит ЭВМ, выделяется его текст, берётся F4)
            sc = self.parser.screen
            self._buf_sel_reset()
            _kind, idx, _x = sc.cursor_anchor()
            if sc._drag is None:
                sc.kbd_sel_begin(block=False)
            sc.kbd_sel_drag(idx, 0 if key == KEY_SELHOME else COLS - 1)
            _fr, lines, chars = sc.sel_stats()
            self.last_key = (f"ВЫД: {lines} стр., {chars} зн. (F4 — взять)"
                             if lines else "ВЫД: пусто")
            self._update_service()
            self._update_status()
            return None
        if key == KEY_SELBSPACE:
            # АВТОНОМНО: Shift+Backspace — удалить выделенный фрагмент
            # набора (Shift+Home/End или Shift+стрелки по буферу).
            # Выделения нет — работает как обычный ВК. В С ЭВМ гасится:
            # кадр принадлежит ЭВМ, в линию слово не уходит.
            if self.mode != MODE_LOCAL:
                self.last_key = "ВЫД: удалить — в АВТОНОМНО"
                self._update_service()
                return None
            if self._buf_del_selection():
                self.last_key = "УДАЛЕНО"
                self._update_service()
                self._update_status()
                return None
            self._buf_sel_reset()
            if self._buf_pos > 0 and self.buffer[self._buf_pos - 1] != 0x0D:
                self.undo.snapshot(self.buffer)
                del self.buffer[self._buf_pos - 1:self._buf_pos]
                self._buf_pos -= 1
                self.undo.step()
                if self.echo:
                    self._redraw_echo()
                self._update_service()
                self._update_status()
            return None
        data = key_to_bytes(key, layout=self.layout,
                            shift_to_rus=self.nabor == "n2")
        if not data:
            return None
        if self.mode == MODE_HOST:
            # ВВОД при фактическом обрыве — повторное соединение. Ввод
            # приходит разными формами: 0x0D (KEY_ENTER/прямой ПР) или
            # 0x0A — стандартный setcbreak не снимает ICRNL, и терминал
            # превращает ВК в \n (шлеть в мёртвую линию нечего)
            if (key in ("KEY_ENTER", "\n", "\r")
                    and self.line_lost and self.reconnect):
                self.reconnect()
                return None
            # Перелом по пробелам здесь НЕ применяется (и не возвращать!):
            # ПР от перелома менял диалог с ЭВМ и ломал чтение выдачи.
            # Эхо машины печатает длинные строки сама — как напечатает.
            return data        # очередь эхо-ответа ведёт emit()
        if len(key) != 1 and is_paste_text:
            # строки вставки длиннее кадра ломаются по пробелам — слова
            # не разделяются (иначе печать рвёт их по краю кадра как
            # придётся)
            data = self._wrap_paste(data)
            # предел буфера набора: вставку подрезаем по месту (и говорим,
            # сколько взято — молча обрезать нельзя)
            clipped = self._buf_clip(data)
            if len(clipped) != len(data):
                self.last_key = (f"ВСТАВКА ОБРЕЗАНА ({len(clipped)} зн.)"
                                 if clipped
                                 else "ВСТАВКА НЕ ПРИНЯТА (БУФЕР ПОЛОН)")
                data = clipped
        # АВТОНОМНО: обычный ВК при открытом выделении удаляет его (как в
        # редакторах). Это же закрывает терминалы, где Shift+Backspace не
        # различим: xterm шлёт для Shift+ВК и обычного ВК один \x7f.
        if key == "KEY_BACKSPACE" and self._buf_del_selection():
            self.last_key = "УДАЛЕНО"
            self._update_service()
            self._update_status()
            return None
        # АВТОНОМНО: набор правится по позиции курсора в буфере — вставка
        # (Insert, по умолчанию) сдвигает хвост вправо, замена пишет поверх
        # знака под курсором; Home/Enter/Bks двигают и экранный курсор.
        # Эхо — заглавными (Н1 не различает регистр, так же отвечает ЭВМ);
        # в буфере код остаётся как дана раскладка.
        buffered = bool(self.buffer)
        self.undo.snapshot(self.buffer)             # отменяемая правка
        pos = self._buf_pos
        self._buf_sel_reset()      # любой знак снимает выделение набора
        if key == "KEY_BACKSPACE":
            # ВК: стираем знак слева от курсора. В начале строки ничего
            # не делаем — ПР (разделитель строк) не стирается, иначе
            # Backspace склеивал бы строки набора
            if pos > 0 and self.buffer[pos - 1] != 0x0D:
                del self.buffer[pos - 1:pos]
                self._buf_pos = pos - 1
        elif key == "KEY_ENTER":
            # Enter: CR (0D) — управляющий символ разделителя строки; курсор
            # (и на экране) — в начало следующей строки
            if self._buf_full(1):
                return None
            self.buffer[pos:pos] = b"\r"
            self._buf_pos = pos + 1
        else:
            # вставка/замена: буфер растёт, если знаков больше, чем до
            # конца набора (замена поверх размера не меняет)
            grow = len(data) if self.insert_mode \
                else max(0, pos + len(data) - len(self.buffer))
            if self._buf_full(grow):
                return None
            if self.insert_mode:
                self.buffer[pos:pos] = data      # вставка: хвост уходит вправо
                self._buf_pos = pos + len(data)
            elif pos + len(data) <= len(self.buffer):
                self.buffer[pos:pos + len(data)] = data    # замена поверх
                self._buf_pos = pos + len(data)
            else:                                # замена в конце строки = добавление
                self.buffer[pos:pos] = data
                self._buf_pos = pos + len(data)
        # откат по слову: граница шага — после разделителя (пробел, точка,
        # ПР) и после ВК — дальше набор идёт новым словом/строкой
        if data in (b" ", b".", b"\r") or key == "KEY_BACKSPACE":
            self.undo.step()
        if self.echo:
            if not buffered:
                # Первая строка набора НЕ стирает уже имеющееся на кадре
                # (вывод ЭВМ из режима С ЭВМ, оставшийся после перехода
                # в АВТОНОМНО): эхо начинается ниже последней ЗАНЯТОЙ
                # строки — «спускается» вниз (пустой кадр — с обычного
                # места курсора). Если место внизу кончилось, кадр
                # сдвигается вверх — ушедшие строки ЭВМ уходят в
                # «историю», а не пропадают.
                sc = self.parser.screen
                bottom = 0
                for y in range(sc.rows - 1, -1, -1):
                    if any(c != 0x20 for c in sc.cells[y]):
                        bottom = y + 1
                        break
                top = max(sc.y, bottom)
                while top >= sc.rows:
                    sc.scroll_up()
                    # содержимое сдвинулось вверх — низ ищем заново
                    bottom = 0
                    for y in range(sc.rows - 1, -1, -1):
                        if any(c != 0x20 for c in sc.cells[y]):
                            bottom = y + 1
                            break
                    top = max(sc.y, bottom)
                self._echo_top = top
            self._redraw_echo()
        self._update_service()
        self._update_status()       # счётчик отмены «ОТМЕНА=» в строке
        return None

    def _set_hold(self, on: bool) -> None:
        """Удержание экрана: остановить выдачу ЭВМ / снять удержание.

        Пока удержание включено, эмулятор линию не читает (главный цикл
        сеанса смотрит `hold`): выдача копится в буфере сокета, машина
        упирается в него и встаёт — экран остаётся тем, что на нём, и
        оператор спокойно читает. Снятие удержания возвращает окно к
        курсору («авто-следование»): накопленное дочитывается и печатается.
        """
        if on == self.hold:
            return
        self.hold = on
        if on:
            # команда «Сброс» в линию: на линии типа «Э» ДИСПАК по ней
            # отменяет идущую выдачу (ОТМЕНА ВЫДАЧИ, признак BREAK)
            self._hold_send(HOLD_CODE)
        else:
            self.parser.screen.view_top = None      # окно — к выдаче
            self._hold_echo = False                 # эхо «Сброса» не ждём
        self.last_key = "УДЕРЖАНИЕ" if on else "ВЫДАЧА ИДЁТ"
        self._update_service()
        self._update_status()

    def _hold_send(self, data: bytes) -> None:
        """Отдать в линию управляющий код терминала (0x0C — «Сброс»)."""
        if self.mode != MODE_HOST or self.link is None or self.line_lost:
            return
        try:
            self.link.send(to_line(data, self.encoding))
        except (OSError, LinkError) as e:
            # линия разорвалась — удержание всё равно оставляем: сеанс
            # продолжается, а экраном можно вернуться к выдаче
            self.mark_line_lost(str(e))
            return
        # линия (Э-60) эхит присланное обратно — «Сброс» в кадр попасть
        # не должен: на 15ИЭ 0x0C — это «ДОМ», курсор ушёл бы в начало,
        # и следующий вывод затёр бы верх кадра. Эхо срежем при приёме.
        self._hold_echo = True

    # --- предел буфера набора -----------------------------------------
    def _buf_room(self, need: int) -> bool:
        """Влезает ли в буфер набора ещё `need` знаков.

        Предел — конфиг ([terminal] buffer), 0 — не ограничивать.
        """
        if self.buffer_limit <= 0:
            return True
        return len(self.buffer) + need <= self.buffer_limit

    def _buf_full(self, need: int) -> bool:
        """Буфер набора переполнен — отказать (с подсказкой в подвале).

        True — не влезает, ввод отвергнут.
        """
        if self._buf_room(need):
            return False
        self.last_key = f"БУФЕР ПОЛОН ({self.buffer_limit})"
        self._update_service()
        self._update_status()
        return True

    def _buf_clip(self, data: bytes) -> bytes:
        """Подрезать данные по месту в буфере набора (по границе слова).

        Вставка длиннее остатка буфера обрезается — молча обрезать нельзя,
        поэтому вызывающий сообщает, сколько взято.
        """
        if self.buffer_limit <= 0:
            return data
        room = self.buffer_limit - len(self.buffer)
        if room <= 0:
            return b""
        if len(data) <= room:
            return data
        cut = data[:room]
        # по границе слова: отступ назад до пробела или ПР
        brk = max(cut.rfind(b" "), cut.rfind(b"\r"), cut.rfind(b"\n"))
        if brk > 0:
            cut = cut[:brk]
        return cut

    def _echo_feed(self, data: bytes) -> None:
        """Напечатать набираемое на экран (АВТОНОМНО).

        Н0: ASCII как есть; Н1: русские оба регистра как есть (строчные
        коды 0x60.. — русские строчные, верхние — 0x40..0x5F и внутренние
        0x00..0x1E). Н2 (VT-52/линия ЭВМ): эхо заглавными — одно-регистровая
        традиция кодов ЭВМ; латинскую строчную показываем верхним
        регистром, т.к. строчные коды 0x60.. заняты русскими.
        """
        if self.nabor in ("n0", "n1"):
            self.parser.feed(data)
        else:
            latin = bytes((b - 0x20) if 0x61 <= b <= 0x7A else b
                          for b in data)
            self.parser.feed(koi7_display_upper(latin))

    def _buf_move(self, key: str) -> None:
        """Передвинуть курсор набора по буферу (АВТОНОМНО).

        Разделители слова — пробел и точка (как у Ctrl+стрелок 15ВВВ),
        конец строки — ПР (0D), им же заканчивается и буфер.
        """
        buf, n = self.buffer, len(self.buffer)
        pos = self._buf_pos
        if key == "KEY_CTRLRIGHT":           # на начало следующего слова
            while pos < n and buf[pos] not in WORD_DELIMS:
                pos += 1                     # через хвост текущего слова
            while pos < n and buf[pos] in WORD_DELIMS:
                pos += 1                     # через разделители
        elif key == "KEY_CTRLLEFT":          # на начало текущего слова
            while pos > 0 and buf[pos - 1] in WORD_DELIMS:
                pos -= 1
            while pos > 0 and buf[pos - 1] not in WORD_DELIMS:
                pos -= 1
        elif key == "KEY_CTRLUP":            # начало текущей строки
            pos = buf.rfind(b"\r", 0, pos) + 1
        elif key == "KEY_CTRLDOWN":          # начало следующей строки
            nxt = buf.find(b"\r", pos)
            if nxt >= 0:
                pos = buf.find(b"\r", nxt + 1) + 1 if buf.find(b"\r", nxt + 1) >= 0 \
                    else nxt + 1
            else:
                pos = n                      # ниже строк нет — в конец
        self._buf_pos = max(0, min(pos, n))

    def _redraw_echo(self) -> None:
        """Перерисовать локальное эхо набора по буферу (АВТОНОМНО).

        Эхо рисуется целиком из буфера: и вставка, и замена двигают хвост
        строки, а перерисовка по кускам оставляла бы «хвост» отменённых
        знаков. Если буфер больше кадра — печатается не всё, а окно
        (`_buf_window`), в котором видны и хвост, и строка курсора:
        печать «с нуля» уезжала вверх, строки над курсором и сам курсор
        уходили за край кадра, и знак вставлялся не под курсором.

        После печати `_echo_top` — ряд нулевого знака буфера: печать могла
        сдвинуть кадр вверх (`scrolled`), и этот сдвиг вычитается. Курсор
        ставится по позиции `_buf_pos` (ряд — от `_echo_top`, прижатый
        к кадру, колонка — по остатку от COLS).
        """
        sc = self.parser.screen
        sc.view_top = None        # печать правит кадр: закреплённый просмотр
                                  # истории (PgUp) снят — набор должен быть
                                  # виден, иначе набор шёл бы «вслепую»
        self._erase_local_echo()          # стирает и ставит курсор в начало
        top = sc.y
        buf = bytes(self.buffer)
        start, end, lo = 0, len(buf), 0
        delta = 0
        page_end = end                # страница для F5 — до последнего ПС
        if buf:
            avail = max(1, sc.rows - top)
            start, end, lo = self._buf_window(buf, avail)
            page_end = end
            if end > start and buf[end - 1] in (0x0D, 0x0A):
                # последний разрыв строки хвоста печати: после него курсор
                # ушёл бы на строку ниже, кадр сдвинулся бы вверх и вся
                # арифметика рядов поехала — сам разрыв на вид ничего
                # не меняет (строка и так последняя), но для F5 страница
                # всё равно кончается последним ПС (см. _echo_to)
                end -= 1
            before = sc.scrolled
            # ПР в буфере — разделитель строк; на экране это ПР ПС. Строка,
            # заполнившая кадр ровно (длина кратна COLS), уже перевела
            # курсор переносом печати — дополнительный ПС дал бы пустой
            # ряд, поэтому её ПР печатается без ПС (как у ЭВМ: «текст ПР»)
            out = bytearray()
            line_start = buf.rfind(b"\r", 0, start) + 1
            for i in range(start, end):
                b = buf[i]
                if b == 0x0D:
                    line_len = i - line_start
                    out += b"\r" if line_len and line_len % COLS == 0 \
                        else b"\r\n"
                    line_start = i + 1
                else:
                    out.append(b)
            self._echo_feed(bytes(out))
            delta = sc.scrolled - before     # страховка: печать всё же уехала
        # ряд нулевого знака буфера: верх печати минус пропущенные сверху
        # ряды (`lo`) минус сдвиг кадра; знак i печатается в строке
        # `_echo_top + rows(i)`
        self._echo_top = top - lo - delta
        self._echo_from = start
        self._echo_to = page_end
        # нижний ряд напечатанного блока — для стирания при следующей правке
        self._echo_bottom = min(sc.rows - 1,
                                self._echo_top + self._buf_rows_up_to(end))
        rows, col = self._buf_rowcol(self._buf_pos)
        sc.y = max(0, min(sc.rows - 1, self._echo_top + rows))
        sc.x = col
        if self._buf_sel:
            # эхо перепечатано — снимки текста рядов в подсветке выделения
            # устарели, пересобираем её по новому содержимому кадра
            self._buf_sel_show()

    # --- выделение в буфере набора (АВТОНОМНО) ------------------------
    def _buf_rowcol(self, i: int) -> tuple[int, int]:
        """(ряд эха, колонка) для позиции i — проходом по разрывам строк.

        Точно повторяет печать: ПР/ПС дают новый ряд, каждые COLS знаков —
        перенос. Строка, заполнившая кадр ровно (длина кратна COLS),
        перевела курсор переносом печати — её ПР нового ряда не даёт
        (иначе перерисовка вставила бы пустой ряд после каждой такой
        строки), колонка — по остатку от COLS.

        Разрывы строк ищутся ПОИСКОМ байта (счёт идёт в C), а не
        просмотром каждого знака: буфер набора бывает большой (вставка,
        макрос), и просмотр по знакам делал нажатие клавиши дорогим тем
        больше, чем длиннее набор.
        """
        buf = self.buffer
        rows = run = 0
        pos = 0
        while pos < i:
            nl = _find_break(buf, pos, i)
            if nl < 0:
                break
            run = nl - pos
            rows += max(1, (run + COLS - 1) // COLS)
            run = 0
            pos = nl + 1
        run = i - pos
        return rows + run // COLS, run % COLS

    def _buf_rows_up_to(self, i: int) -> int:
        """Сколько рядов кадра занимает эхо буфера до позиции i."""
        return self._buf_rowcol(i)[0]

    def _buf_pos_to_screen(self, i: int) -> tuple[int, int]:
        """Позиция в буфере набора → (ряд кадра, знак) на экране.

        Ряд считается от `_echo_top` (ряд нулевого знака буфера после
        последней печати — он может быть и над кадром, если печать
        уезжала вверх) и прижимается к кадру: выше видимой части
        строка уже в истории, ей отдаётся верхний ряд.
        """
        sc = self.parser.screen
        top = 0 if self._echo_top is None else self._echo_top
        rows, col = self._buf_rowcol(i)
        return max(0, min(sc.rows - 1, top + rows)), col

    def _buf_starts(self, buf: bytes) -> tuple[list[int], int]:
        """Начала рядов печати и число рядов: starts[r] — индекс буфера,
        с которого начинается ряд r. Границы считаются как в `_buf_rowcol`:
        ПР/ПС даёт новый ряд, каждые COLS знаков — перенос; строка длиной,
        кратной COLS, занимает один ряд (перенос уже сделан печатью).

        Разрывы строк ищутся поиском байта (счёт в C) — см. `_buf_rowcol`.
        """
        starts = [0]
        rows = 0
        pos = 0
        n = len(buf)
        while pos < n:
            nl = _find_break(buf, pos, n)
            if nl < 0:
                break
            run = nl - pos
            full = max(1, (run + COLS - 1) // COLS)
            for j in range(1, full):              # переносы строки
                rows += 1
                starts.append(pos + j * COLS)
            rows += 1                             # ряд после разрыва
            starts.append(nl + 1)
            pos = nl + 1
        run = n - pos
        for j in range(1, run // COLS + 1):       # хвост незакрытой строки
            rows += 1
            starts.append(pos + j * COLS)
        return starts, rows

    def _buf_pos_at_row(self, row: int, col: int) -> int:
        """Позиция буфера: начало ряда `row` плюс колонка (в границах ряда)."""
        buf = bytes(self.buffer)
        starts, _total = self._buf_starts(buf)
        row = max(0, min(row, len(starts) - 1))
        start = starts[row]
        nxt = starts[row + 1] if row + 1 < len(starts) else len(buf)
        # зажимаемся в границы ряда: до ПС/переноса, у последнего ряда — до конца
        return start if nxt <= start else min(start + col, nxt - 1)

    def _buf_page_move(self, direction: int, rows: int) -> bool:
        """АВТОНОМНО: листание страницы по буферу набора (PgUp/PgDn/колесо).

        Здесь нет «истории» выдачи (эхо в автономном режиме кадр вверх не
        сдвигает — печать влезает в окно), поэтому листать нужно сам набор:
        курсор уходит на `rows` строк вверх (−1) или вниз (+1), а окно
        печати следует за ним — экран показывает предыдущие/следующие
        страницы буфера.

        False — листать нечего (буфер влезает в кадр, либо курсор уже у
        границы): тогда листание уходит в обычную «историю» — она может
        быть от прошлой работы в режиме С ЭВМ.
        """
        if self.mode != MODE_LOCAL:
            return False
        buf = bytes(self.buffer)
        total = self._buf_rows_up_to(len(buf))
        if total <= self.parser.screen.rows - 1:
            return False                      # весь буфер влезает в кадр
        row, col = self._buf_rowcol(self._buf_pos)
        target = min(total, max(0, row + direction * rows))
        if target == row:
            return False                      # у границы — дальше история
        self._buf_sel_reset()
        self._buf_pos = self._buf_pos_at_row(target, col)
        # листание перешло к набору — закреплённый просмотр истории (если
        # был из С ЭВМ) снимается, кадр показывает буфер
        self.parser.screen.view_top = None
        if self.echo:
            self._redraw_echo()
        return True

    def _buf_window(self, buf: bytes, avail: int) -> tuple[int, int, int]:
        """Окно эха, когда буфер больше кадра: (с чего, до куда, рядов сверху).

        Печать «с нуля» и до конца буфера уезжала вверх: строки над
        курсором и сам курсор уходили за край кадра, и знак вставлялся
        не под курсором. Поэтому печатается не больше `avail` рядов:
          * курсор у конца — виден хвост буфера (как раньше);
          * курсор высоко — окно вокруг его строки, хвост не печатается
            (он не нужен: важен знак под курсором).
        Кадр при печати не смещается — печать влезает в окно целиком.
        """
        # starts[r] — индекс буфера, с которого начинается ряд r
        # (границы строк и переносов считаются так же, как в _buf_rowcol)
        starts, total = self._buf_starts(buf)

        def row_of(i: int) -> int:
            # бинарный поиск по монотонной таблице
            lo, hi = 0, len(starts)
            while lo < hi:
                mid = (lo + hi) // 2
                if starts[mid] <= i:
                    lo = mid + 1
                else:
                    hi = mid
            return lo - 1

        pos = self._buf_pos
        cur = row_of(pos)
        if cur >= total - (avail - 1):
            # хвостовое окно: конец буфера и курсор у конца видны
            lo = max(0, total - (avail - 1))
            return starts[lo], len(buf), lo
        # курсор высоко — окно от начала его строки
        line_start_i = buf.rfind(b"\r", 0, pos) + 1
        lo = row_of(line_start_i)
        hi = lo + (avail - 1)
        end = starts[hi + 1] if hi + 1 < len(starts) else len(buf)
        return line_start_i, end, lo

    def _buf_page_span(self) -> tuple[int, int] | None:
        """Позиции буфера, отображённые на кадре (видимая страница набора).

        Берём то, с чего реально началась последняя печать (`_echo_from`)
        — при буфере больше кадра верхние строки не печатаются вовсе и
        «страница» начинается не с нуля. Если эхо не печаталось (выключено
        или буфер пуст) — видимой считается хвостовая страница.
        """
        buf = bytes(self.buffer)
        if not buf:
            return None
        if self._echo_top is not None:
            # что реально напечатано на кадре (при буфере больше кадра
            # хвост может не печататься вовсе — окно вокруг курсора)
            return (min(self._echo_from, len(buf)),
                    min(self._echo_to, len(buf)))
        sc = self.parser.screen
        total = self._buf_rows_up_to(len(buf))
        a = max(0, total - (sc.rows - 1))      # хвостовая страница
        if a <= 0:
            return (0, len(buf))
        rows = run = 0
        for idx, b in enumerate(buf):
            if b in (0x0D, 0x0A):
                rows += max(1, (run + COLS - 1) // COLS)
                run = 0
                if rows > a:                   # конец строки уже ниже верха
                    return (idx + 1, len(buf))
            else:
                run += 1
        return (len(buf), len(buf))            # удалять нечего

    def _buf_sel_hide(self) -> None:
        """Снять подсветку выделения набора (выделение мышью не трогаем)."""
        if not self._buf_sel_frags:
            return
        sc = self.parser.screen
        ours = {id(f) for f in self._buf_sel_frags}
        sc.selections = [f for f in sc.selections if id(f) not in ours]
        self._buf_sel_frags = []
        sc.touch()

    def _buf_sel_reset(self) -> None:
        """Снять выделение набора целиком (подсветка и диапазон)."""
        self._buf_sel_hide()
        self._buf_sel = None

    def _buf_del_selection(self) -> bool:
        """Удалить выделенный фрагмент набора (Shift+Home/End и т.п.).

        True — фрагмент был и удалён. Отдельный снимок для отмены делает
        сама (в общий путь правки её уже вставил вызывающий).
        """
        sel = self._buf_sel
        if not sel or sel[1] <= sel[0]:
            return False
        a, b = sel
        self.undo.snapshot(self.buffer)          # отменяемая правка
        del self.buffer[a:b]
        self.undo.step()                         # удалённое — шаг целиком
        self._buf_pos = a
        self._buf_sel_reset()
        if self.echo:
            self._redraw_echo()
        return True

    def _buf_sel_show(self) -> int:
        """Подсветить выделенный фрагмент набора на кадре.

        Диапазон буфера переносится в координаты кадра (с учётом ПР и
        переноса по COLS) и кладётся в `selections` как фрагменты
        «frame» — их подсветит отрисовка (инверсия, как у выделения
        мышью). Возвращает число выделенных знаков.
        """
        sc = self.parser.screen
        self._buf_sel_hide()
        sel = self._buf_sel
        if not sel or self._echo_top is None:
            return 0
        a, b = sel
        buf = bytes(self.buffer)
        a, b = max(0, a), min(len(buf), b)
        if b <= a:
            return 0
        # экранные точки каждого знака выделения; ряд меняется — новый кусок
        pts = [self._buf_pos_to_screen(i) for i in range(a, b)]
        pieces: list[tuple] = []
        start = 0
        for j in range(1, len(pts) + 1):
            if j < len(pts) and pts[j][0] == pts[j - 1][0]:
                continue
            y, x0 = pts[start]
            x1 = pts[j - 1][1]
            text = sc.row_text("frame", y) or ""
            if text[x0:x1 + 1].strip():       # пустые ряды не подсвечиваем
                pieces.append(("frame", y, x0, x1, text))
            start = j
        if pieces:
            # selections хранит СПИСКИ кусков (один фрагмент = одна протяжка)
            frag = pieces
            sc.selections.append(frag)
            self._buf_sel_frags.append(frag)
            sc.sel_kind = "СТР"
            sc.touch()
        return b - a

    def _undo_echo(self) -> None:
        """Перерисовать набираемое после отмены правки.

        На экране лежит прежнее содержимое (знаки уже напечатаны), а буфер
        после отмены другой. Стираем строки прежнего эха целиком и печатаем
        текущий буфер заново — иначе остался бы «хвост» отменённых знаков.
        При выключенном эхом на экране нет ничего нашего — не трогаем.
        """
        if not self.echo:
            return
        # отмена правки возвращает буфер — курсор в него же (в конец)
        self._buf_pos = len(self.buffer)
        self._redraw_echo()

    def _queue_echo(self, data: bytes) -> None:
        """Что вернёт линия в эхо на отданные байты (в форме приёма) —
        при выключенном эхе это не печатается, а срезается с потока."""
        if self.rx_encoding == "raw" and self.nabor != "n2":
            # Н0/Н1: линия эхит байты как есть
            self._echo_pending += bytes(data)
            return
        stream, _ = normalize_incremental(to_line(bytes(data), self.encoding))
        if self.rx_encoding == "raw":
            stream = koi7_raw_upper(stream)
        self._echo_pending += stream

    def _strip_pending_echo(self, stream: bytes) -> bytes:
        """Пока эхо выключено, съедает из потока эхо-ответ на посланные
        знаки. Переводы строк (ПР/ПС) при этом отрабатываются как usual —
        они двигают каретку, но не показывают; первый несовпавший печатный
        байт — настоящий вывод ЭВМ, он показывается."""
        if not self._echo_pending:
            return stream
        i = 0
        pend = self._echo_pending
        n = len(stream)
        while i < n and pend:
            b = stream[i]
            if b in (0x0D, 0x0A):
                i += 1                       # ПР/ПС — проводим парсеру
                continue
            if b == pend[0]:
                pend.pop(0)
                i += 1
            else:
                break                        # дальше — ответ ЭВМ, не эхо
        while pend and pend[0] in (0x0D, 0x0A):
            pend.pop(0)                      # переводы в очереди не «должны»
        return stream[i:]

    def _erase_local_echo(self) -> None:
        """Стереть строки локального эха отдаваемого блока и вернуть
        курсор в начало: эхо ЭВМ (на Э-60 оно всегда включено) впишет
        текст заново — без дублирования внахлёст."""
        if self._echo_top is None:
            return
        sc = self.parser.screen
        # верх: `_echo_top` может быть и НИЖЕ курсора — первый набор после
        # перехода в АВТОНОМНО начинается под уже имеющимся выводом ЭВМ,
        # прижимать его к курсору (к строке ЭВМ) нельзя — стёрли бы её
        top = max(0, min(self._echo_top, sc.rows - 1))
        # низ стирания: курсор или нижний край напечатанного блока
        # (содержимое могло быть длиннее — уехало при печати)
        y_end = min(max(sc.y, self._echo_bottom or 0), sc.rows - 1)
        if y_end < top:
            y_end = top
        # Стираем ряды ОЗУ напрямую, а не ESC-командами через парсер:
        # в наборе команд №1 (Н0/Н1) ESC в парсер игнорируется, и
        # «ESC Y … ESC K» печатался бы в кадр буквально — «Y  K».
        for y in range(top, y_end + 1):
            sc.cells[y] = [0x20] * COLS
            sc.attr[y] = [0] * COLS
            sc.row_rev[y] = sc.rev + 1
        sc.rev += 1
        sc.x = 0
        sc.y = top
        self._echo_top = None

    # --- передача буфера --------------------------------------------
    def send(self) -> bytes:
        """Клавиша SEND: выдать накопленное в линию и очистить буфер."""
        self.flush_paste()                 # хвост вставки — часть буфера
        data = bytes(self.buffer)
        self.buffer.clear()
        self._buf_pos = 0            # буфер пуст — курсор в начало набора
        self._buf_sel_reset()        # выделение набора вместе с буфером
        # отданное в линию отменять нечего: буфера уже нет, а следующий
        # набор начинается с чистого листа
        self.undo.clear()
        self._update_service()         # «ПЕРЕДАЧА=ПУСТО»
        return data

    def pending(self) -> bytes:
        """Ожидает ли буфер передачи (что на реальном терминале гасит РТ)."""
        return bytes(self.buffer)

    def _ext_clip(self, key: str) -> None:
        """Копирование/вставка с внешним буфером обмена (OSC52).

        Ctrl+Shift+C — выделенное (КОИ7 → Unicode → base64) уходит в буфер
        обмена ОС; Ctrl+Shift+V — запрос в буфер ОС: терминал отвечает
        текстом, ответ разбирает `_parse_osc52`, а дальше текст идёт
        обычным путём вставки — с полной перекодировкой (encode_koi7 →
        линия в С ЭВМ, буфер в АВТОНОМНО).

        Если терминал такие клавиши перехватывает сам (обычно и вставляет
        сам — тогда приходит уже текст) или протокол не поддерживает —
        молчим с подсказкой, в линию ничего не уходит.
        """
        want_paste = key.lower().endswith("+v")
        if not want_paste:
            text = self.parser.screen.sel_text()
            if not text:
                self.last_key = "ВЫД: пусто — копировать нечего"
            elif self.term_write:
                payload = base64.b64encode(
                    text.replace("\r\n", "\n").encode("utf-8")).decode()
                self.term_write(f"\x1b]52;c;{payload}\x07".encode())
                self.last_key = f"В БУФЕРЕ ОС: {len(text)} зн."
            else:
                self.last_key = "КОПИЯ: терминал не поддерживает OSC52"
        else:
            if self.term_write:
                self.term_write(b"\x1b]52;c;?\x07")
                self.last_key = "ЗАПРОС ВСТАВКИ ИЗ ОС"
            else:
                self.last_key = "ВСТАВКА: терминал не поддерживает OSC52"
        self._update_service()
        self._update_status()

    def mark_line_lost(self, why: str = "") -> None:
        """Линия потеряна: сеанс продолжается (АВТОНОМНО работает и без неё).

        Вызывается главным циклом при ошибке приёма/передачи. Флаг гасит
        дальнейшие отправки и переводит строку состояния в «ЛИНИЯ=НЕТ» —
        эмулятор не падает и не завершает сеанс, как этого требует работа
        в автономном режиме.
        """
        if self.line_lost:
            return                       # уже отмечено — сообщение одно
        self.line_lost = True
        self.last_key = f"ЛИНИЯ: {why}" if why else "ЛИНИЯ ПОТЕРЯНА"
        self._update_service()
        self._update_status()

    def pulse_tick(self) -> bool:
        """Раз в минуту — пробел в линию (поддержание программ БЭСМ-6).

        Программа, ждущая ввода с терминала, по тишине считает сеанс
        уснувшим; пробел без ПР не заканчивает строку, поэтому вводом
        для программы не считается, но линия не молчит. Сам пробел на
        экран не попадает — его эхо снимает _strip_pulse_echo.

        Только в АВТОНОМНО: в С ЭВМ терминал и так на связи — линия не
        молчит, а лишний пробел сбивает выдачу (машина печатает, а в
        поток вклинивается знак).

        True — пробел ушёл в линию. Вызывается из главного цикла
        (сама себя держит по интервалу; pulse_interval=0 — выключена).
        """
        if self.pulse_interval <= 0 or self.line_lost or self.link is None:
            return False
        if self.mode != MODE_LOCAL:
            return False            # в С ЭВМ пробел сбивает выдачу
        now = time.time()
        if now < self._pulse_next:
            return False
        self._pulse_next = now + self.pulse_interval
        try:
            # пробел одинаков во всех кодировках линии (utf8/raw/dks) —
            # перекодировка to_line не нужна
            self.link.send(PULSE_CHAR)
        except (OSError, LinkError) as e:
            self.mark_line_lost(str(e))
            return False
        self._pulse_echo_until = now + PULSE_ECHO_TTL
        return True

    def link_check_tick(self) -> bool:
        """Регулярно спрашивать линию (мост SSH) о состоянии.

        Связь пропадает «молча»: SSH-соединение не рвётся, но данных
        больше нет — и подвал продолжает показывать «ЛИНИЯ=КВУ»/«ЛИНИЯ=ДКС»,
        хотя линии давно нет. Раз в check_interval секунд спрашиваем у
        линии состояние (Link.probe): ответа нет — линия потеряна
        («ЛИНИЯ=НЕТ»), сеанс при этом продолжается (АВТОНОМНО доступен,
        ВВОД в С ЭВМ — повторное соединение).

        True — состояние линии изменилось (нужно перерисовать подвал).
        """
        if self.check_interval <= 0 or self.link is None or self.line_lost:
            return False
        if getattr(self.link, "probe_label", None) is None:
            return False            # линия локальная — проверять нечего
        now = time.time()
        if now < self._check_next:
            return False
        self._check_next = now + self.check_interval
        try:
            info = self.link.probe()
        except (OSError, LinkError) as e:
            self.check_info = str(e)
            if self.check_state != "НЕТ":
                self.check_state = "НЕТ"
                self._update_status()
            self.mark_line_lost(str(e))
            return True
        self.check_info = str(info)
        state = parse_probe(info, getattr(self.link, "line_port", None))
        # мост сообщил, на какой линии стоит соединение: терминал, задавший
        # диапазон портов, мог угадать не с того — показываем настоящий
        port = state.get("port")
        if port and port != getattr(self.link, "line_port", None):
            self.link.line_port = port
            self._update_status()
        if state.get("line") == "МЕРТВА":
            # мост жив, а линия за ним умерла — канал ещё держится, связи
            # фактически уже нет
            self.check_state = "НЕТ"
            self.mark_line_lost("линия мертва (мост сообщил)")
            return True
        mark = state.get("bridge") or "ЖИВ"
        if mark != self.check_state:
            self.check_state = mark
            self._update_status()
            return True
        return False

    def mark_line_alive(self) -> None:
        """Линия снова жива (повторное соединение): сбросить обрыв."""
        self.line_lost = False
        self.check_state = ""
        self.check_info = ""
        self._check_next = time.time() + self.check_interval
        self._update_service()
        self._update_status()

    def _strip_pulse_echo(self, stream: bytes) -> bytes:
        """Снять с приёма эхо своего «пробела поддержки».

        Машинное эхо пробела печатало бы знак и двигало курсор — следом
        весь вывод съезжал бы на колонку. Гасим только первый пробел и
        только в окне PULSE_ECHO_TTL: позже пробел в выводе — уже чужой
        (или эхо не пришло вовсе), и трогать его нельзя.
        """
        until = self._pulse_echo_until
        if not until:
            return stream
        if time.time() > until:
            self._pulse_echo_until = 0.0        # опоздали — не гасим чужое
            return stream
        i = stream.find(b" ")
        if i < 0:
            return stream                       # эхо ещё не пришло
        if any(b not in (0x0D, 0x0A) for b in stream[:i]):
            self._pulse_echo_until = 0.0        # перед пробелом — чужой
            return stream                       # текст, не эхо-одиночка
        self._pulse_echo_until = 0.0
        return stream[:i] + stream[i + 1:]

    def emit(self, data: bytes | None) -> None:
        """Фактическая передача в линию: внутренний КОИ7-поток → байты линии.

        При выключенном эхе (защита пароля) в очередь срезаемого эхо-ответа
        попадают и знаки, ушедшие в режиме С ЭВМ нажатием клавиши.
        """
        if data:
            self._note_sent(bytes(data))
        if data and self.link is not None and not self.line_lost:
            try:
                self.link.send(to_line(bytes(data), self.encoding))
            except (OSError, LinkError) as e:
                # Линия разорвалась (TCP/SSH, в т.ч. таймаут) — не падаем:
                # помечаем потерю и продолжаем сеанс. В АВТОНОМНО набор и
                # правка буфера работают и без линии.
                self.mark_line_lost(str(e))

    def _note_sent(self, data: bytes) -> None:
        """При выключенном эхе: записать в очередь ожидаемого эхо-ответа.
        Клавишные байты режима С ЭВМ — внутренний поток (с алфавитным
        битом), их перекладываем в форму линии; SEND отдаёт уже готовые
        байты линии — перекодировка не нужна."""
        if self.echo or not data:
            return
        if self.mode == MODE_HOST:
            self._queue_echo(data)
        else:
            self._echo_pending += data

    def _strip_banner(self, data: bytes) -> bytes:
        """Убрать служебные строки баннера из приёма линии.

        Баннер tmxr/telnet («CONNECTED TO … TTY DEVICE, LINE 3»,
        «ENCODING IS RAW», «WRU …», дата-время, «From …») показывается
        в строке состояния над кадром, а в кадре ЭВМ занимал бы строки и
        сбивал бы «чистый» первый экран.

        Фильтр живёт только в начале сеанса: целые строки проверяются по
        образцам баннера, а недобранная строка ждёт продолжения, только
        если её начало похоже на начало служебной строки. Первая же
        строка выдачи ЭВМ («Cmd> …», ответ ОС и т. п.) выключает фильтр —
        дальше в кадр идёт всё как есть.
        """
        buf = self._banner_tail + data
        self._banner_tail = b""
        head, sep, tail = buf.rpartition(b"\n")
        parts: list[bytes] = []
        if sep:
            kept = []
            for raw in head.split(b"\n"):
                if not raw.rstrip(b"\r"):
                    continue          # пустая строка баннера — не «конец фазы»
                if BANNER_LINE.search(raw.rstrip(b"\r")):
                    continue
                self._banner_done = True
                kept.append(raw)
            if kept:
                parts.append(b"\n".join(kept) + b"\n")
        if tail and (self._banner_done or not _banner_open(tail)):
            parts.append(tail.rstrip(b"\r"))   # началось не «с баннера» — мимо
            self._banner_done = True
        elif tail:
            self._banner_tail = tail            # дописываем следующим куском
        return b"".join(parts)

    def _parse_banner(self, data: bytes) -> None:
        """Разобрать баннер линии: тип линии, номер терминала, режим."""
        buf = self._sniff + data
        self._sniff = buf[-200:]
        changed = False
        if not self.banner_seen and (
                BANNER_LINE_NO.search(buf)
                or re.search(rb"encoding\s+is\s+\S+", buf, re.IGNORECASE)
                or b"Connected to DKS" in buf):
            self.banner_seen = True
            changed = True
        if b"Connected to DKS" in buf:
            if self.line_type != LINE_TYPE_DKS:
                self.line_type = LINE_TYPE_DKS
                changed = True
        m = BANNER_LINE_NO.search(buf)
        if m:
            no = int(m.group(1))
            if no != self.line_no:
                self.line_no = no
                changed = True
        for pat, label in BANNER_MODE:
            if re.search(rb"encoding\s+is\s+" + pat, buf, re.IGNORECASE):
                if self.line_mode != label:
                    self.line_mode = label
                    changed = True
                break
        m = re.search(rb"From\s+(\d{1,3}(?:\.\d{1,3}){3})", buf,
                      re.IGNORECASE)
        if m:
            peer = m.group(1).decode("ascii")
            if peer != self.peer:
                self.peer = peer
                changed = True
        if not self.line_type:
            self.line_type = LINE_TYPE_KVU    # tty без «Connected to DKS» — КВУ
            changed = True                    # подвал: «ЛИНИЯ=КВУ»
        if changed:
            self._update_service()
            self._update_status()

    def _update_status(self) -> None:
        """Собрать строку состояния линии над кадром.

        Тип линии (КВУ/ДКС) и номер терминала — из баннера SIMH, канал —
        фактический telnet-порт подключения, номер ЭВМ — из конфига
        ([line] machine, пусто — не показываем), режим — объявленный
        линией (raw / unicode / jcuken), справа — дата и время.
        """
        link = self.link
        sc = self.parser.screen
        port = getattr(link, "line_port", None) or getattr(link, "port", None)
        when = time.localtime()
        cells = []
        if getattr(link, "banner", False) and not self.banner_seen:
            # сообщение о подключении так и не пришло — линия не «отошла»:
            # показываем прямо в строке состоянии, в кадр не пишем
            cells.append(mk(("ПРОВЕРЬ ЛИНИЮ!", "off")))
        # типа линии (КВУ/ДКС) здесь нет: состояние линии — только в
        # подвале, ячейка «ЛИНИЯ=» (см. _update_service). Здесь номера,
        # канал и режим.
        if self.machine:
            cells.append(mk("ЭВМ=", (self.machine, "state")))
        if self.line_no is not None:
            cells.append(mk("ТЕРМ=", (str(self.line_no), "state")))
        if port:
            cells.append(mk("КАНАЛ=", (str(port), "state")))
        if self.line_mode:
            mode_cell = mk("РЕЖИМ=", (self.line_mode, "state"))
            cells.append(mode_cell)
        # мышь терминала отдана эмулятору? (выделение текста, колесо)
        mouse_cell = mk(("МЫШЬ=", "label"),
                        ("ВКЛ" if self.mouse_on else "ВЫКЛ",
                         "on" if self.mouse_on else "off"))
        cells.append(mouse_cell)
        # удержание экрана: выдача ЭВМ остановлена (Ctrl+L) — видно, что
        # кадр «замёрз» намеренно, а не завис. Ячейка не жертва: признак
        # важнее подсказки в правом углу (тут первым уйдёт «СПРАВКА=F12»)
        if self.hold:
            cells.append(mk(("УДЕРЖ=", "label"), ("ВКЛ", "off")))
        # размер выделения мышью: видно сразу, выбрана одна строка или,
        # скажем, пол-экрана. Строка состояния — вне кадра, места в кадре
        # это не занимает.
        frags, rows_n, chars = self.parser.screen.sel_stats()
        # кого можно пожертвовать ради индикатора «ОТ ЭВМ» и в каком
        # порядке (первая в списке — первая жертва): подсказки, затем
        # справочные МЫШЬ и ВЕРСИЯ. Без индикатора эта очередь не трогается
        victims: list = []
        if frags:
            cell = mk(("ВЫД=", "label"),
                      (f"{self.parser.screen.sel_kind} "
                       f"{frags}фр {rows_n}с {chars}зн", "state"))
            cells.append(cell)
            victims.insert(0, cell)
        # отмена правок набора (АВТОНОМНО): «ОТМЕНА=Ctrl+U» — клавиша, а рядом
        # число — сколько шагов ещё можно вернуть. В подвале для этого
        # нет места (каждая группа уже занята, лишний ряд съел бы кадр),
        # поэтому подсказка живёт здесь — как и «ВЫД» выше. Показываем
        # только когда отменять есть что: на пустом буфере строка состояния
        # не должна лишней мигать
        if self.undo.depth and self.undo.available:
            cell = mk(("ОТМЕНА=", "label"),
                      (f"Ctrl+U ({len(self.undo)})", "state"))
            cells.append(cell)
            victims.insert(0, cell)          # первая жертва — ОТМЕНА
        # версия эмулятора — видно, чем именно работаем (та же цифра в
        # --version и в строке запуска); перед датой, дата остаётся последней
        version_cell = mk(("ВЕРСИЯ=", "label"), (__version__, "state"))
        cells.append(version_cell)
        # жертвы по списку (первая в списке — первая жертва): подсказки,
        # затем мышь. Версия не жертвуется никогда.
        victims.append(mouse_cell)             # мышь
        # индикатор сообщений ЭВМ в АВТОНОМНО (см. _note_incoming): кадр занят
        # набором, поэтому текст показываем здесь — хвост последних знаков,
        # а в скобках — сколько сообщений пришло с начала режима
        ind_txt = None
        if self.mode == MODE_LOCAL and self.incoming:
            tail = self.incoming[-27:]
            ind_txt = "…" + tail if len(self.incoming) > 27 else tail
            if self.incoming_n > 1:
                ind_txt = f"{ind_txt} ({self.incoming_n})"
        date_cell = mk(f"{when.tm_mday:02d}.{when.tm_mon:02d}.{when.tm_year} "
                       f"{when.tm_hour:02d}:{when.tm_min:02d}:"
                       f"{when.tm_sec:02d}")
        # справка скрыта — подсказка «СПРАВКА=F12» в крайнем правом углу
        # верхней строки состояния (см. set_status(right=…))
        right = mk(("СПРАВКА=F12", "edit")) if not sc.show_help else None

        def width(cs) -> int:
            # длина строки состояния с разделителями « | »
            return sum(len(cell_text(c)) for c in cs) + 4 * (len(cs) - 1)

        def grown() -> list:
            tail = [date_cell]
            if ind_txt is not None:
                tail.insert(0, mk(("ОТ ЭВМ=", "label"), (ind_txt, "state")))
            return cells + tail

        def need(cs) -> int:
            # сколько места нужно строке вместе с ячейкой в правом углу
            return width(cs) + (len(" | ") + len(cell_text(right))
                                if right is not None else 0)

        # строка состояния обязана влезть в ширину подвала: длиннее —
        # терминал перенесёт её на второй ряд и сдвинет кадр ЭВМ. Жертвы —
        # по очереди (см. victims) уходят справочные ячейки, потом
        # сокращается индикатор «ОТ ЭВМ», потом снимается подсказка в
        # правом углу. Признак удержания экрана жертвой не считается:
        # выдача остановлена — это важнее подсказки «СПРАВКА=F12».
        # Ужимаем строку всегда, когда есть что ужать (индикатор, подсказка)
        # и при удержании — тогда признак удержания гарантированно влезет.
        panel_w = self.parser.screen.panel_cols
        if ind_txt is not None or right is not None or self.hold:
            while need(grown()) > panel_w:
                if victims:
                    cells.remove(victims.pop(0))
                    continue
                if ind_txt is not None:
                    cut = need(grown()) - panel_w
                    if len(ind_txt) <= cut:
                        ind_txt = None          # места нет вовсе — без него
                        continue
                    ind_txt = "…" + ind_txt[cut + 1:]
                    continue
                right = None                    # подсказке не хватило — молчит
                break
        self.parser.screen.set_status(grown(), right=right)

    def _detect_encoding(self, data: bytes) -> None:
        """Кодировки приёма/передачи по баннеру линии SIMH.

        «Encoding is …» задаёт направления; «Connected to DKS» меняет
        только передачу (окончание строки — ПР, а не ETX): приём остаётся
        тем, что объявил tmxr (RAW-ДКС у dispak.ini — raw; ДКС без raw
        говорит с терминалом UTF-8, и дешифровать его как КОИ7 нельзя).
        Смена типа линии обязывает обновить служебную строку.
        """
        self._sniff = (self._sniff + data)[-200:]
        was = self.encoding
        if b"Connected to DKS" in self._sniff:
            # Э-60 регистрируется ОС не сразу — строка может прийти позже
            # любых serial-баннеров и имеет приоритет для передачи.
            self.encoding = "dks"
            if b"Encoding is RAW" in self._sniff or b"Encoding is KOI-7" \
                    in self._sniff:
                self.rx_encoding = "raw"
                self.enc_decided = True
        elif self.enc_decided:
            return
        elif b"Encoding is UTF-8" in self._sniff:
            self.encoding = self.rx_encoding = "utf8"
            # decided не ставим: «Connected to DKS» способно опоздать
        elif (b"Encoding is RAW" in self._sniff
              or b"Encoding is KOI-7" in self._sniff):
            self.encoding = self.rx_encoding = "raw"
            self.enc_decided = True
        if self.encoding != was:
            self._update_service()   # табло разрядов следует за конфигурацией

    def _note_incoming(self, stream: bytes) -> None:
        """Сообщение ЭВМ в АВТОНОМНО — в индикатор и выпадающую строку.

        Кадр занят локальным набором: строка сообщения легла бы в строки
        эха и исчезла бы при следующей правке (`_redraw_echo` перетирает
        блок эха целиком). Поэтому:
          * текст копится в поле «ОТ ЭВМ=…» строки состояния (хвост
            последних 120 знаков);
          * и показывается ПОЛНОСТЬЮ в выпадающей строке под состоянием
            (`sc.notice`) — до первого нажатия клавиши (см. feed_key).
        Поток бывает двух форм: внутренняя (с битом алфавита — управляющие
        отдельно от букв) и сырая 7-разрядная КОИ7 (заглавные в 0x00..0x1E,
        где ПР/ПС неотличимы от «М»/«Й»): в первой управляющие схлопываем
        в пробелы, во второй трогаем только разрывы строк.
        """
        if any(b >= 0x80 for b in stream):
            clean = bytes(b if b >= 0x20 else 0x20 for b in stream)
        else:
            clean = bytes(b if b not in (0x0D, 0x0A) else 0x20
                          for b in stream)
        text = " ".join(decode_koi7(clean).split())
        if not text:
            return
        # сообщения склеиваются пробелом — иначе слипались бы в одно слово
        self.incoming = (self.incoming + " " + text)[-120:] \
            if self.incoming else text
        self.incoming_n += 1
        self.parser.screen.notice = self.incoming   # гасится первой клавишей
        self._update_status()

    def handle_line_reply(self, data: bytes) -> bytes | None:
        """Приём из линии (ответ ЭВМ); возможный ответ терминала — ESC/ и т.п.

        Ответ сразу уходит в линию (например, идентификация по ESC Z), как
        это делал настоящий терминал, и возвращается для вызывающей стороны.
        """
        if not data:
            return None
        self._detect_encoding(data)
        self._parse_banner(data)
        if not self._banner_done:          # баннер — в строку состояния
            data = self._strip_banner(data)
            if not data:
                return None
        if self.rx_encoding == "raw":
            if self._carry or _looks_like_utf8(data):
                # Unicode-вывод поверх raw-линии (оболочка SIMH после
                # «set>» печатает utf8): байты читаем как UTF-8, иначе
                # кириллица рассыпалась бы по одному байту в мусор —
                # 0xD0/0xD1 в сырых кодах линии не бывает (см. детектор)
                stream, self._carry = normalize_incremental(data, self._carry)
            elif _simh_prompt(self._sniff):
                # чанк с приглашением оболочки SIMH — это латиница, а не
                # строка 0x60.. кириллицы ЭВМ: карту koi7_raw_upper не
                # применяем, иначе «set>» превратилось бы в коды 0x93..,
                # которые в Н0 (переключение — следом) не отображаются
                stream = data
                stream, self._carry = stream, b""
            else:
                # RAW/ДКС-линия — байтовый канал ВТ-340: коды Н1 приходят как
                # есть (русская буква — 0x60.. или уже с битом алфавита 0x80..);
                # UTF-8-разбор здесь портил бы кадры, tmxr-баннер — единственная
                # UTF-8-строка, ей жертвуем. Верхний регистр (одно-регистная
                # традиция ЭВМ) применяется только в Н2 — в Н0/Н1 линия
                # печатает оба регистра как есть
                stream = koi7_raw_upper(data) if self.nabor == "n2" else data
                stream, self._carry = stream, b""
        else:
            # utf8-приём читается инкрементально: незавершённая на границе
            # recv() многобайтовая кириллица доносит остатком
            stream, self._carry = normalize_incremental(data, self._carry)
        if not self.echo and stream:
            stream = self._strip_pending_echo(stream)
        if stream:
            # эхо «пробела поддержки» (см. pulse_tick) — на экран не выводим
            stream = self._strip_pulse_echo(stream)
        if self._hold_echo and b"\x0c" in stream:
            # эхо нашей команды «Сброс» (см. _hold_send): в кадр ему нельзя —
            # 0x0C на 15ИЭ это «ДОМ», курсор ушёл бы в начало кадра
            stream = stream.replace(b"\x0c", b"", 1)
            self._hold_echo = False
        if self.mode == MODE_LOCAL:
            # АВТОНОМНО: кадр занят набором — сообщение ЭВМ в него не
            # рисуем (эхо перетёрло бы его при первой же правке), а
            # показываем индикатором «ОТ ЭВМ=…» в строке состояния.
            # Служебный ответ (ESC Z — идентификация) уходит в линию как
            # у настоящего терминала: без него ОС сочла бы сеанс мёртвым.
            self._note_incoming(stream)
            reply = "\x1b/" if (b"\x1bZ" in stream or b"\x1b[Z" in stream) \
                else None
        else:
            reply = self.parser.feed(stream)
            self._update_service()     # ИНВ/ЗВН могли измениться разбором
        # Оболочка SIMH («set>»/«СЕТ>»): печатает unicode и обычный ASCII —
        # набор переключается на Н0 сам, дальше латиница идёт как есть (в Н1
        # строчная латиница показывалась бы русскими буквами). ПОСЛЕ вывода
        # текущей строки: само приглашение ещё печатается прежним набором
        # (в Н2 видно «СЕТ>»), Н0 действует для следующей выдачи.
        # Однократно; вернуть набор — клавиша F8. Ищем в сыром окне
        # приёма (_sniff) — сам поток к этому моменту перекодирован
        if stream and not self._simh_shell and _simh_prompt(self._sniff):
            self._simh_shell = True
            if self.nabor != "n0":
                self.nabor = "n0"
                self.last_key = "НАБОР=Н0 (set>)"
                self._update_service()
                self._update_status()
        if not reply:
            return None
        wire = to_line(reply.encode("latin-1"), self.encoding)
        if self.link is not None and not self.line_lost:
            try:
                self.link.send(wire)
            except (OSError, LinkError) as e:
                self.mark_line_lost(str(e))
                return None
        return wire