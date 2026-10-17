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

from . import COLS, ROWS, __version__
from .charset import (encode_koi7, koi7_display_upper, koi7_raw_upper,
                      normalize_incremental, to_line)
from .link import LinkError
from .keyboard import (DEFAULT_LAYOUT, ctrl_code, is_key_combo, key_to_bytes,
                       KEY_CTRLSELDOWN, KEY_CTRLSELLEFT, KEY_CTRLSELRIGHT,
                       KEY_CTRLSELUP, KEY_SELDOWN, KEY_SELLEFT,
                       KEY_SELRIGHT, KEY_SELUP,
                       KEY_SELHOME, KEY_SELEND, KEY_SELBSPACE)
from .screen import HELP, RULE, mk
from .undo import DEFAULT_UNDO, UndoStack

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
    "SELHOME": "ВЫД=^ДОМ", "SELEND": "ВЫД=^КОНЕЦ",
    "SELBSPACE": "ВК-ВЫДЕЛ",
    "SEND": "SEND", "MODE": "СЕАНС", "CMDSET": "НАБОР", "BLINK": "УПР.СИМВ",
    "ECHO": "ЭХО", "CLEAR": "ОЧИСТКА",
}


LAYOUT_LABELS = {"phonetic": "ФОН", "positional": "ПОЗ"}

WHEEL_STEP = 3       # рядов выдачи на щелчок колеса мыши

# разделители слова для прыжков Ctrl+←/→ по набору (АВТОНОМНО)
WORD_DELIMS = b" ."


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
                 keymap: dict[str, str] | None = None,
                 undo_depth: int = DEFAULT_UNDO) -> None:
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
        self.mouse_on = False   # мышь терминала отдана эмулятору (F-подвал)
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
        def st(label, value, style=None):  # «МЕТКА=значение» с цветом значения
            return mk(label, (value, style or self._flag_style(value)))

        line = LINE_LABELS.get(self.encoding, f"ЛИНИЯ={self.encoding}")
        if self.line_lost:
            line = "ЛИНИЯ=НЕТ"        # таймаут/разрыв — сеанс продолжается
        last = mk("ПОСЛ: ", (self.last_key, "last"))
        state = [
            # «АВТОНОМНО» — красным и подчёркнутым: набор идёт в буфер
            # терминала, а не в ЭВМ, и потерять его легко
            st("СЕТЬ=", "С ЭВМ" if host else "АВТОНОМНО",
               "state" if host else "local"),
            
            st("НАБОР=", self.NABOR_LABELS[self.nabor]),
            st("ЛИНИЯ=", line.split("=", 1)[-1],
               "off" if self.line_lost else None),
            st("РАСК=", lay),
            st("ЭХО=", "ВКЛ" if self.echo else "ВЫКЛ"),
            st("ВИДЕО=", "ИНВ" if sc.inverse else "НОРМ"),
            st("БУФЕР=", str(len(self.buffer)) if self.buffer else "ПУСТО"),
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
        Клавиши F3/F4 снимают и забирают выделение (см. `feed_key`).
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
        if key == "SELCLEAR":                # F3 — снять всё выделение
            self._buf_sel_reset()
            self.parser.screen.sel_clear()
            self._update_status()           # «ВЫД» в строке состояния
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
                    self.buffer.extend(data)
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
            # до последней правки. В С ЭВМ буфера нет — отменять нечего
            if self.mode == MODE_HOST or not self.buffer:
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
            return data        # очередь эхо-ответа ведёт emit()
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
            self.buffer[pos:pos] = b"\r"
            self._buf_pos = pos + 1
        elif self.insert_mode:
            self.buffer[pos:pos] = data      # вставка: хвост уходит вправо
            self._buf_pos = pos + len(data)
        elif pos + len(data) <= len(self.buffer):
            self.buffer[pos:pos + len(data)] = data    # замена поверх
            self._buf_pos = pos + len(data)
        else:                                # замена в конце строки = добавление
            self.buffer[pos:pos] = data
            self._buf_pos = pos + len(data)
        if self.echo:
            if not buffered:
                self._echo_top = self.parser.screen.y
            self._redraw_echo()
        self._update_service()
        self._update_status()       # счётчик отмены «ОТМЕНА=» в строке
        return None

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
        знаков. После печати курсор ставится в позицию `_buf_pos`: по числу
        ПР до неё — ряд, по остатку — знак.
        """
        sc = self.parser.screen
        self._erase_local_echo()          # стирает и ставит курсор в начало
        self._echo_top = sc.y
        buf = bytes(self.buffer)
        if buf:
            # ПР в буфере — разделитель строк; на экране это ПР ПС
            self._echo_feed(buf.replace(b"\r", b"\r\n"))
        head = buf[:self._buf_pos]
        nl = head.count(b"\r")            # сколько строк выше курсора
        col = len(head) - (head.rfind(b"\r") + 1)
        # перенос по COLS: строка эха длиннее 80 знаков идёт на следующий ряд,
        # иначе курсор после 80-го знака уезжал бы в конец своей строки
        sc.y = min(sc.rows - 1, self._echo_top + nl + col // COLS)
        sc.x = col % COLS
        if self._buf_sel:
            # эхо перепечатано — снимки текста рядов в подсветке выделения
            # устарели, пересобираем её по новому содержимому кадра
            self._buf_sel_show()

    # --- выделение в буфере набора (АВТОНОМНО) ------------------------
    def _buf_pos_to_screen(self, i: int) -> tuple[int, int]:
        """Позиция в буфере набора → (ряд кадра, знак) на экране.

        Эхо печатается ПР ПС на каждый ПР буфера и переносится по COLS,
        поэтому экранные координаты считаем по тому же правилу, что и
        курсор в `_redraw_echo`.
        """
        sc = self.parser.screen
        head = bytes(self.buffer[:i]).replace(b"\r", b"\r\n")
        nl = head.count(b"\n")                # строк эха выше
        col = len(head) - (head.rfind(b"\n") + 1)
        y = min(sc.rows - 1, self._echo_top + nl + col // COLS)
        return y, col % COLS

    def _buf_rows_up_to(self, i: int) -> int:
        """Сколько рядов кадра занимает эхо буфера до позиции i.

        Точно повторяет печать `_redraw_echo`: ПР/ПС дают новый ряд,
        каждые COLS знаков — перенос (строка в COLS знаков занимает
        два ряда: ряд печати и ряд после переноса).
        """
        rows = run = 0
        for b in bytes(self.buffer[:i]):
            if b in (0x0D, 0x0A):
                rows += run // COLS + 1      # переносы законченной строки + ПС
                run = 0
            else:
                run += 1
        return rows + run // COLS

    def _buf_page_span(self) -> tuple[int, int] | None:
        """Позиции буфера, отображённые на кадре (видимая страница набора).

        Кадр показывает хвост печати: если строк больше, чем рядов под
        эхом, кадр при печати смещается вверх и верхние строки уходят
        в историю. Возвращает (от, до) — что именно сейчас видно, либо
        None, если печатать нечего (пустой буфер).
        """
        buf = bytes(self.buffer)
        if not buf:
            return None
        sc = self.parser.screen
        top = 0 if self._echo_top is None \
            else max(0, min(self._echo_top, sc.rows - 1))
        available = sc.rows - top            # рядов под эхом
        if available <= 0:
            return None
        total = self._buf_rows_up_to(len(buf))
        # печать дотягивает последний ряд эха (там стоит курсор) до нижней
        # кромки кадра — при переполнении кадр смещается вверх на s рядов
        scrolled = max(0, top + total - (sc.rows - 1))
        j_start = max(0, scrolled - top)     # первый видимый ряд печати
        if j_start <= 0:
            return (0, len(buf))             # влезло целиком
        row = run = 0
        for idx, b in enumerate(buf):
            if row >= j_start:
                return (idx, len(buf))
            if b in (0x0D, 0x0A):
                row += run // COLS + 1
                run = 0
            else:
                run += 1
        return (len(buf), len(buf))           # удалять нечего

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
        y_end = min(sc.y, sc.rows - 1)
        top = min(self._echo_top, y_end)   # блок мог убежать при скролле
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
        # мышь терминала отдана эмулятору? (выделение текста, колесо)
        cells.append(mk(("МЫШЬ=", "label"),
                        ("ВКЛ" if self.mouse_on else "ВЫКЛ",
                         "on" if self.mouse_on else "off")))
        # размер выделения мышью: видно сразу, выбрана одна строка или,
        # скажем, пол-экрана. Строка состояния — вне кадра, места в кадре
        # это не занимает.
        frags, rows_n, chars = self.parser.screen.sel_stats()
        if frags:
            cells.append(mk(("ВЫД=", "label"),
                            (f"{self.parser.screen.sel_kind} "
                             f"{frags}фр {rows_n}с {chars}зн", "state")))
        # отмена правок набора (АВТОНОМНО): «ОТМЕНА=Ctrl+U» — клавиша, а рядом
        # число — сколько шагов ещё можно вернуть. В подвале для этого
        # нет места (каждая группа уже занята, лишний ряд съел бы кадр),
        # поэтому подсказка живёт здесь — как и «ВЫД» выше. Показываем
        # только когда отменять есть что: на пустом буфере строка состояния
        # не должна лишней мигать
        if self.undo.depth and self.undo.available:
            cells.append(mk(("ОТМЕНА=", "label"),
                            (f"Ctrl+U ({len(self.undo)})", "state")))
        # версия эмулятора — видно, чем именно работаем (та же цифра в
        # --version и в строке запуска); перед датой, дата остаётся последней
        cells.append(mk(("ВЕРСИЯ=", "label"), (__version__, "state")))
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
        if self.link is not None and not self.line_lost:
            try:
                self.link.send(wire)
            except (OSError, LinkError) as e:
                self.mark_line_lost(str(e))
                return None
        return wire