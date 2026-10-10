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

import re
import time

from . import COLS, ROWS
from .charset import (koi7_display_upper, koi7_raw_upper,
                      normalize_incremental, to_line)
from .keyboard import DEFAULT_LAYOUT, is_key_combo, key_to_bytes
from .screen import HELP, RULE, mk

MODE_LOCAL = "local"      # «АВТОНОМНО»
MODE_HOST = "host"        # «С ЭВМ»

# Подписи установленных управляющих разрядов (служебная строка 25);
# единый пробел — вся строка должна умещаться в 80 знаков
LINE_LABELS = {"utf8": "ЛИНИЯ=UTF-8", "raw": "ЛИНИЯ=RAW", "dks": "ЛИНИЯ=ДКС"}

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
    "SEND": "SEND", "MODE": "СЕАНС", "CMDSET": "НАБОР", "BLINK": "УПР.СИМВ",
    "ECHO": "ЭХО", "CLEAR": "ОЧИСТКА",
}


LAYOUT_LABELS = {"phonetic": "ФОН", "positional": "ПОЗ"}

WHEEL_STEP = 3       # рядов выдачи на щелчок колеса мыши


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


def _banner_open(line: bytes) -> bool:
    """Похоже ли начало строки на начало строки баннера."""
    low = line.strip().lower()
    return not low or any(h.startswith(low) for h in BANNER_HEADS)


class TerminalSession:
    """Состояние терминала: режим работы + накопленный буфер автономного набора."""

    def __init__(self, parser, link=None, mode: str = MODE_LOCAL,
                 koi7: bool = False, encoding: str = "utf8",
                 layout: str | None = None,
                 keymap: dict[str, str] | None = None) -> None:
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
        self.echo = True             # клавиша «ЭХО» (F6): вывод набираемого
                                     # на экран; снята — пароли не печатаются
        self._echo_pending = bytearray()   # эхо-ответ линии, ожидаемый при
                                     # выключенном эхе (срезается с приёма)
        self._echo_top = None        # строка, с которой началось локальное эхо блока
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
        self._banner_tail = b""      # недобранная строка баннера
        self._banner_done = False    # баннер закончился (пошли данные ЭВМ)
        self._update_service()
        self._update_status()

    # --- режим ------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        self.mode = mode
        self._update_service()

    def toggle_mode(self) -> str:
        self.mode = MODE_HOST if self.mode == MODE_LOCAL else MODE_LOCAL
        self._update_service()
        return self.mode

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
        def st(label, value):              # «МЕТКА=значение» с цветом значения
            return mk(label, (value, self._flag_style(value)))

        line = LINE_LABELS.get(self.encoding, f"ЛИНИЯ={self.encoding}")
        state = [
            st("СЕТЬ=", "С ЭВМ" if host else "АВТОНОМНО"),
            st("НАБОР=", self.NABOR_LABELS[self.nabor]),
            st("ЛИНИЯ=", line.split("=", 1)[-1]),
            st("РАСК=", lay),
            st("ЭХО=", "ВКЛ" if self.echo else "ВЫКЛ"),
            st("ВИДЕО=", "ИНВ" if sc.inverse else "НОРМ"),
            st("БУФЕР=", str(len(self.buffer)) if self.buffer else "ПУСТО"),
            st("УПР.СИМВ=", "ВКЛ" if self.parser.show_ctrl else "ВЫКЛ"),
            mk("ПОСЛ: ", (self.last_key, "last")),
            RULE,                                 # состояние от справки
            HELP,                                 # дальше — справка (можно убрать)
            # F-клавиши — верхним рядом справки, по порядку F5…F10
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
            "",                                   # конец группы Ctrl
            # правка текста — отдельной группой
            mk(("ДОМ=Home", "edit")), mk(("СТЕРСТР=End", "edit")),
            mk(("ИНВЕРС=Ins", "edit")), mk(("НОРМ=Del", "edit")),
        ]
        sc.set_panel(state, wide=[state[-9]])   # «ПОСЛ» — длинная, свой ряд

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
        if key in self.keymap:
            key = self.keymap[key]
            if not key:
                self.last_key = f"{orig}→гашение"
                self._update_service()
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
            # подвал, его и пишут ключом в [keys]; в линию не уходит
            self.last_key = f"{key} U+{ord(key.rsplit('+', 1)[-1]):04X}"
            self._update_service()
            return None
        elif len(key) == 1:
            self.last_key = arrow + f"{key} U+{ord(key):04X}"
            self._update_service()
        if key == "KEY_PAGEUP":
            self.parser.screen.scroll_view(-self.parser.screen._view_h + 1)
            self._update_service()
            return None
        if key == "KEY_PAGEDOWN":
            self.parser.screen.scroll_view(self.parser.screen._view_h - 1)
            self._update_service()
            return None
        if key in ("KEY_WHEELUP", "KEY_WHEELDOWN"):
            # колесо мыши — то же листание «истории» выдачи, по три ряда
            # за щелчок; вверх так же запрещено, пока не выдано больше
            # одной страницы кадра (циклической прокрутки нет)
            step = WHEEL_STEP * (-1 if key == "KEY_WHEELUP" else 1)
            self.parser.screen.scroll_view(step)
            self._update_service()
            return None
        if key == "ECHO":
            self.echo = not self.echo
            if not self.echo:
                self._echo_pending = bytearray()
            self._update_service()
            return None
        if key == "CLEAR":
            # экран — в историю, кадр в начало; локальное эхо буфера там же
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
        move = {"KEY_CTRLRIGHT": "word_right", "KEY_CTRLLEFT": "word_left",
                "KEY_CTRLUP": "line_start", "KEY_CTRLDOWN": "line_below"}.get(key)
        if move:
            # локальная навигация 15ВВВ: курсор двигается по экрану,
            # в линию ничего не уходит (таких кодов у ЭВМ нет)
            getattr(self.parser.screen, move)()
            self._update_service()
            return None
        data = key_to_bytes(key, layout=self.layout,
                            shift_to_rus=self.nabor == "n2")
        if not data:
            return None
        if self.mode == MODE_HOST:
            return data        # очередь эхо-ответа ведёт emit()
        # АВТОНОМНО: накапливаем в буфере и печатаем на экране,
        # держим линию свободной (как на незаполненном PLAN-символ «Р»).
        # Эхо — заглавными (Н1 не различает регистр, так же отвечает ЭВМ);
        # в буфере код остаётся как дана раскладка.
        buffered = bool(self.buffer)
        self.buffer += data
        if self.echo:
            if not buffered:
                self._echo_top = self.parser.screen.y
            if self.nabor in ("n0", "n1"):
                # Н0: ASCII как есть; Н1: русские оба регистра как есть
                # (строчные коды 0x60.. — русские строчные, верхние —
                # 0x40..0x5F и внутренние 0x00..0x1E)
                self.parser.feed(data)
            else:
                # Н2 (VT-52/линия ЭВМ): эхо заглавными — одно-регистная
                # традиция кодов ЭВМ; латинскую строчную показываем верхним
                # регистром, т.к. строчные коды 0x60.. заняты русскими
                latin = bytes((b - 0x20) if 0x61 <= b <= 0x7A else b
                              for b in data)
                self.parser.feed(koi7_display_upper(latin))
        self._update_service()
        return None

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
        y_end = min(self.parser.screen.y, self.parser.screen.rows - 1)
        top = min(self._echo_top, y_end)   # блок мог убежать при скролле
        cmd = bytearray()
        for y in range(top, y_end + 1):
            cmd += b"\x1bY" + bytes([0x20 + y, 0x20]) + b"\x1bK"
        cmd += b"\x1bY" + bytes([0x20 + top, 0x20])
        self._echo_top = None
        self.parser.feed(bytes(cmd))

    # --- передача буфера --------------------------------------------
    def send(self) -> bytes:
        """Клавиша SEND: выдать накопленное в линию и очистить буфер."""
        data = bytes(self.buffer)
        self.buffer.clear()
        self._update_service()         # «ПЕРЕДАЧА=ПУСТО»
        return data

    def pending(self) -> bytes:
        """Ожидает ли буфер передачи (что на реальном терминале гасит РТ)."""
        return bytes(self.buffer)

    def emit(self, data: bytes | None) -> None:
        """Фактическая передача в линию: внутренний КОИ7-поток → байты линии.

        При выключенном эхе (защита пароля) в очередь срезаемого эхо-ответа
        попадают и знаки, ушедшие в режиме С ЭВМ нажатием клавиши.
        """
        if data:
            self._note_sent(bytes(data))
        if data and self.link is not None:
            self.link.send(to_line(bytes(data), self.encoding))

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
            if self.line_type != "ДКС":
                self.line_type = "ДКС"
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
            self.line_type = "КВУ"        # tty без «Connected to DKS» — КВУ
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
        port = getattr(link, "line_port", None) or getattr(link, "port", None)
        when = time.localtime()
        cells = []
        if getattr(link, "banner", False) and not self.banner_seen:
            # сообщение о подключении так и не пришло — линия не «отошла»:
            # показываем прямо в строке состоянии, в кадр не пишем
            cells.append(mk(("ПРОВЕРЬ ЛИНИЮ!", "off")))
        else:
            cells.append(mk((self.line_type or "КВУ", "state")))
        if self.machine:
            cells.append(mk("ЭВМ=", (self.machine, "state")))
        if self.line_no is not None:
            cells.append(mk("ТЕРМ=", (str(self.line_no), "state")))
        if port:
            cells.append(mk("КАНАЛ=", (str(port), "state")))
        if self.line_mode:
            cells.append(mk("РЕЖИМ=", (self.line_mode, "state")))
        cells.append(mk(f"{when.tm_mday:02d}.{when.tm_mon:02d}.{when.tm_year} "
                        f"{when.tm_hour:02d}:{when.tm_min:02d}:"
                        f"{when.tm_sec:02d}"))
        self.parser.screen.set_status(cells)

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
            # RAW/ДКС-линия — байтовый канал ВТ-340: коды Н1 приходят как
            # есть (русская буква — 0x60.. или уже с битом алфавита 0x80..);
            # UTF-8-разбор здесь портил кадры, tmxr-баннер — единственная
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
        reply = self.parser.feed(stream)
        self._update_service()         # ИНВ/ЗВН могли измениться разбором
        if not reply:
            return None
        wire = to_line(reply.encode("latin-1"), self.encoding)
        if self.link is not None:
            self.link.send(wire)
        return wire