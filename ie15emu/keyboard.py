"""Клавиатура 15ВВВ-97-006 (ёмкостная матрица) и её ПЗУ 15bbb.rt5.

ПЗУ КР556РТ5 (512 байт) содержит микропрограмму обхода матрицы и
коды клавиш. Эмулятор использует файл как источник карты символов;
базовая раскладка — QWERTY/ЙЦУКЕН с КОИ7-выдачей (как у терминала).

Соответствие ПК-клавиш → коды терминала:
  обычные символы  — как есть (7 бит)
  Backspace        — 08 (ВК, «authbs»)
  Tab              — 09 (ТАБ), Ctrl-G — 07 (ЗВН), Esc — ESC
  Enter            — 0D 0A (ПР ПС)
  Стрелки          — ESC A / B / C / D  (набор команд №2)
  Home             — ESC H  (курсор в «дом»)
  End              — ESC K  (стереть до конца строки)
  PgUp / PgDn      — листание экрана вверх/вниз (служебный подвал целиком;
                     приём от ЭВМ возвращает окно к курсору)
  Ctrl+L           — УДЕРЖАНИЕ экрана: выдача ЭВМ останавливается (в линию
                     уходит команда 15ИЭ «Сброс», 0x0C); повторное
                     нажатие — снять удержание
  Ctrl+Q           — снять удержание / вернуть окно к живой выдаче
                     («Восстановить передачу»)
  Insert / Delete  — ESC b / ESC c (инверсное / нормальное видео)
  Ctrl+← / Ctrl+→  — курсор на начало текущего (следующего) слова;
                     разделители слова — пробел и точка (локальная навигация)
  Ctrl+↑ / Ctrl+↓  — курсор в начало текущей / нижней строки (локально)
  F8               — НАБОР: набор команд №1 ↔ №2 (VT52) (служебная)
  F5               — ОЧИСТКА: очистить экран (кадр ЭВМ) с возвратом в
                     начало первой строки; сошедший экран сохраняется в
                     «истории» (PgUp), история не стирается
  F6               — ЭХО: подавить вывод набираемого на экран (знаки
                     уходят в линию, но не печатаются — защита паролей,
                     как снятая ламель печатающего контура Э-60)
  F7               — УПР.СИМВ: показывать/скрыть образные знаки
                     управляющих символов (режим blink Видеотона-340);
                     по умолчанию выключен (служебная)
  F12              — СПРАВКА: показать/убрать ряды-легенды подвала в ходе
                     сеанса (состояние и «ПОСЛ» остаются всегда). Основная
                     клавиша F12: F1 в ряде окружений занят ОС (и на
                     консоли переключает виртуальную консоль). Формы F1
                     тоже разбираются — везде, где они доходят, работают.
                     Переключается и любой другой клавишей через [keys]
  F9               — СЕТЬ: сеанс АВТОНОМНО ↔ С ЭВМ (служебная)
  F10              — ПЕРЕДАЧА (SEND): отдаёт буфер (при незакрытой
                     строке добавляет ПР ПС) и переводит сеанс в С ЭВМ
  (F11 не используется — занят окружением рабочего стола)

Русская раскладка включается --layout positional|phonetic (или --koi7 —
позиционная по умолчанию): клавиши без Shift печатают русские буквы
(QWERTY2KOI7_POS — по положению клавиш ЙЦУКЕН, QWERTY2KOI7 — фонетика),
а «регистр» (Shift) переключает алфавит на английский, как на 15ВВВ.
Работает при любой раскладке ОС: EN-раскладка присылает «W» (проходит
как есть), RU-раскладка — «Ц» (возвращается английской буквой своего
назначения в выбранной таблице).
В наборе Н2 (раскладка выключена) печатаются заглавные обоих
алфавитов (русская раскладка — русские, латинская — английские), и
регистр ввода НЕ важен: «Д» и Shift+«Д» дают один и тот же код ЭВМ
(строка ЭВМ одно-регистровая, сами заглавные — от клавиатуры 15ВВВ);
Shift+латинская остаётся входом русской (W→Shift+Ц), а правило
«русская+Shift→латиница» (Ц→W) действует только в Н1 (задан --layout),
как на 15ВВВ.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from .charset import ALPHA_BIT, RUS7, encode_koi7

KBD_ROM = "15bbb.rt5"

# QWERTY → КОИ7-буквы (псевдо-ЙЦУКЕН для выдачи с ПК)
QWERTY2KOI7 = {
    "q": 0x11, "w": 0x16, "e": 0x05, "r": 0x12, "t": 0x14, "y": 0x19,
    "u": 0x15, "i": 0x09, "o": 0x0F, "p": 0x10, "[": 0x18, "]": 0x1D,
    "a": 0x01, "s": 0x13, "d": 0x04, "f": 0x06, "g": 0x07, "h": 0x08,
    "j": 0x0A, "k": 0x0B, "l": 0x0C, ";": 0x1C, "'": 0x1E,
    "z": 0x1A, "x": 0x17, "c": 0x03, "v": 0x16, "b": 0x02, "n": 0x0E,
    "m": 0x0D, ",": 0x00, ".": 0x1B,
}

# Позиционная раскладка: клавиша QWERTY → русская буква того же
# положения на клавиатуре ЙЦУКЕН (q→Й, w→Ц, …, ;→Ж, '→Э). Ъ (клавиша
# «]») и Ё (клавиша «\») в КОИ7 Н1 отсутствуют и в таблицу не входят;
# в Н2 они отдают знаки «]» и «|» той же клавишей (см. N2_SYMBOLS).
QWERTY2KOI7_POS = {
    "q": 0x0A, "w": 0x03, "e": 0x15, "r": 0x0B, "t": 0x05, "y": 0x0E,
    "u": 0x07, "i": 0x1B, "o": 0x1D, "p": 0x1A, "[": 0x08,
    "a": 0x06, "s": 0x19, "d": 0x17, "f": 0x01, "g": 0x10, "h": 0x12,
    "j": 0x0F, "k": 0x0C, "l": 0x04, ";": 0x16, "'": 0x1C,
    "z": 0x11, "x": 0x1E, "c": 0x13, "v": 0x0D, "b": 0x09, "n": 0x14,
    "m": 0x18, ",": 0x02, ".": 0x00,
}

# Выбор раскладки по имени (None — выключена)
KOI7_TABLES = {"phonetic": QWERTY2KOI7, "positional": QWERTY2KOI7_POS}

# Н2: знаки, которые RU-раскладка отдаёт клавишей того же физического
# положения, что EN-раскладка — карта по самОму знаку, поэтому результат
# не зависит ни от раскладки ОС, ни от положения CapsLock:
#   "  (RU Shift+2)  → @   :  (RU Shift+6) → ^
#   ё/Ё (клавиша \\) → |   ъ/Ъ (клавиша ]) → ]
# «№» (RU клавиша 3) — вне карты: у него нет кода на линии ВТ, он идёт
# внутренним 0x9F, а to_line кладёт на линию позицию 2/3 («#»).
# «#», «?», «%», «;», «!», «<», «>», «[», «]», «'» приходят с тех же
# клавиш обеими раскладками сами собой (см. README, «Клавиши»).
N2_SYMBOLS = {'"': "@", ":": "^", "ё": "|", "Ё": "|",
              "ъ": "]", "Ъ": "]"}

# Обратные таблицы: код КОИ7-буквы → клавиша QWERTY. «Регистр» (Shift)
# в русской раскладке переключает алфавит: заглавная кириллица с
# клавиатуры (RU-раскладка ОС шлёт «Ц» на Shift+ц) печатается английской
# буквой своего назначения. Коллизии (фонетич. w/v→Ж) разрешаются
# в пользу первой записи таблицы.
KOI7_TO_QWERTY = {}
for _name, _table in KOI7_TABLES.items():
    _inv = {}
    for _key, _code in _table.items():
        _inv.setdefault(_code, _key)
    KOI7_TO_QWERTY[_name] = _inv


def unescape_key(s: str) -> str:
    """Значение из конфига [keys]: «\\xNN» — символ с кодом NN (УП от
    Ctrl+клавиша), остальное читается как есть."""
    head, *rest = s.split("\\x")
    out = head
    for part in rest:
        hex2 = part[:2]
        if len(hex2) == 2 and all(c in "0123456789abcdefABCDEF" for c in hex2):
            out += chr(int(hex2, 16)) + part[2:]
        else:
            out += "\\x" + part
    return out


def load_kbd_rom(rom_dir: str | Path) -> bytes | None:
    p = Path(rom_dir) / KBD_ROM
    return p.read_bytes() if p.exists() else None


def lookup_key(kbd_rom: bytes | None, col: int, row: int) -> int | None:
    """Прочитать код клавиши из ПЗУ клавиатуры (адрес = col*32+row)."""
    if not kbd_rom:
        return None
    addr = (col * 32 + row) & 0x1FF
    code = kbd_rom[addr]
    return None if code == 0xFF else code


DEFAULT_LAYOUT = "positional"   # русская раскладка по умолчанию — по позиции ЙЦУКЕН

def ctrl_code(key: str) -> bytes | None:
    """Управляющий символ по имени нажатия «ctrl+буква».

    Имя приходит от терминала как «ctrl+e» (CSI-u, modifyOtherKeys) или
    «ctrl+shift+E» — Shift на управляющий символ не влияет, код тот же.
    Кириллица и прочие не-ASCII — не управляющие символы (None): с ними
    такого нажатия на 15ВВВ не бывает.
    """
    parts = key.split("+")
    if len(parts) < 2:
        return None
    ch = parts[-1]
    if {p.lower() for p in parts[:-1]} not in ({"ctrl"}, {"ctrl", "shift"}):
        return None
    if len(ch) != 1 or not ch.isascii() or not ch.isalpha():
        return None
    return bytes([ord(ch.lower()) & 0x1F])


def key_to_bytes(key: str, layout: str | None = None,
                 shift_to_rus: bool = False,
                 latin_up: bool = False) -> bytes:
    """Нажатие ПК-клавиши → байты внутреннего КОИ7-потока терминала.

    layout: None — выключена; "positional" (по умолчанию для --koi7) —
    клавиша даёт русскую букву своего положения (q→Й); "phonetic" —
    фонетическая таблица (w→В, q→Я). Заглавные русские — с битом
    алфавита (0x80); в байты линии их переводит `charset.to_line`.
    shift_to_rus (режим Н2 при layout=None): набор одно-регистрный —
    русская раскладка даёт русские заглавные (внутренний верх 0x80+код)
    НЕЗАВИСИМО от регистра ввода: «д» и Shift+«Д» — один код ЭВМ,
    латинская — английские заглавные; Shift+латинская (W→Ц) остаётся
    входом русской с EN-раскладки, а правило «русская+Shift→латиница»
    (Ц→W) действует только в Н1 — когда задан --layout. Знаки
    ^ @ # № [ ] ' | ? % ; ! > < набираются одним и тем же знаком
    с обеих раскладок и при любом CapsLock (N2_SYMBOLS + проброс
    ASCII как есть).
    latin_up: строка 0x60.. линии занята кириллицей ЭВМ, поэтому строчную
    латиницу поднимаем в верх (в Н2 — всегда; в Н1 — когда раскладка
    выключена). С русской раскладкой (shift_to_rus) это уже сделано.
    """
    if key in ("KEY_ENTER", "\n", "\r"):
        return b"\r"
    # Ctrl+буква — управляющий символ (Ctrl+A=01 … Ctrl+Z=1A) вне зависимости
    # от раскладки: нажатие с Ctrl всегда даёт код ИТ 0x60+знак (как на
    # 15ВВВ), независимо от того, что клавиша набрала бы без модификатора.
    ctrl = ctrl_code(key)
    if ctrl is not None:
        return ctrl
    if key in ("KEY_BACKSPACE", "\x7f"):
        return b"\x08"
    if key == "KEY_UP":
        return b"\x1bA"
    if key == "KEY_DOWN":
        return b"\x1bB"
    if key == "KEY_RIGHT":
        return b"\x1bC"
    if key == "KEY_LEFT":
        return b"\x1bD"
    if key == "KEY_HOME":
        return b"\x1bH"        # как на клавиатуре 15ИЭ: ESC H
    if key == "KEY_END":
        return b"\x1bK"        # стереть до конца строки
    # PgUp/PgDn — локальное листание экрана (см. TerminalSession.feed_key),
    # в линию не уходит
    if key == "KEY_INSERT":
        return b"\x1bb"        # инверсное видео
    if key == "KEY_DELETE":
        return b"\x1bc"        # нормальное видео
    if key == "KEY_ESC" or key == "\x1b":
        return b"\x1b"
    # Несколько знаков подряд (--feed "строка", вставка из буфера обмена) —
    # кодируем целиком в КОИ7 Н1 (в т.ч. кириллицу; ascii-кодирование съедало
    # русские буквы). Регистр латиницы здесь сохраняется: вставка должна
    # уйти в линию такой, какой была (как на настоящем терминале).
    if len(key) > 1 and not key.startswith("KEY_"):
        return encode_koi7(key)
    ch = key if len(key) == 1 else ""
    if not ch:
        return b""
    if shift_to_rus and layout is None:
        # Н2: знак RU-раскладки → тот же знак общего физического
        # положения (карта по знаку — CapsLock не влияет)
        if ch == "№":
            return bytes([ALPHA_BIT | 0x1F])   # на линию — 0x23 (to_line)
        if ch in N2_SYMBOLS:
            return N2_SYMBOLS[ch].encode("ascii")
    # Русский символ уходит в линию как код КОИ7 (с битом алфавита).
    # При включённой русской раскладке (Н1) «регистр» (Shift) переключает
    # алфавит: заглавная кириллица печатается английской буквой своего
    # назначения по обратной таблице выбранной раскладки — только для Н1;
    # в Н2 регистр ввода не важен (ветка RUS7 ниже).
    table = KOI7_TABLES.get(layout or "")
    up = ch.upper().replace("Ё", "Е")
    if up in RUS7:
        if ch.isupper() and layout and not shift_to_rus:
            # Н1 (раскладка --layout включена): Shift меняет алфавит по
            # позиционной таблице — русская заглавная → латинская
            # положения (Ц→W). В Н2 (shift_to_rus) этого НЕТ: строка ЭВМ
            # одно-регистровая, «Д» и «д» дают один код (см. ниже)
            lat = KOI7_TO_QWERTY[layout].get(RUS7.index(up))
            if lat:
                return lat.upper().encode("ascii")
        if shift_to_rus and layout is None:
            # Н2: кириллица одно-регистрная — русская буква кладётся
            # сразу внутренним верхом (0x80+код), как её отдаёт ЭВМ.
            # Регистр ввода не важен: и «д», и Shift+«Д» дают один и тот
            # же код ЭВМ (для набора/латинизации в Н2 — см. Н1 выше)
            return bytes([RUS7.index(up) | ALPHA_BIT])
        return encode_koi7(ch)
    # Латиница без Shift — русская буква выбранной раскладки; с Shift
    # (заглавная) — английский набор, переназначения нет.
    if table and ch in table:
        return bytes([table[ch] | ALPHA_BIT | 0x60])
    if shift_to_rus and layout is None and ch.isalpha():
        # Н2 при выключенной русской раскладке: Shift+латинская — русская
        # буква положения клавиши (W→Ц): единственный вход русской с
        # EN-раскладки (русская+Shift в Н2 даёт ту же русскую, что и
        # без Shift — регистр ввода не важен)
        if ch.isupper():
            pos = KOI7_TABLES["positional"].get(ch.lower())
            if pos is not None:
                return bytes([pos | ALPHA_BIT])
        # строчная латинская без Shift — верх международной строки:
        # на линии Н2 строка 0x60.. занята кириллицей ЭВМ, строчной
        # латиницы в одно-регистрном наборе нет
        if ch.upper().isascii():
            return ch.upper().encode("ascii")
        # не-ASCII вне кириллицы (AltGr-акценты и т.п.) — ниже в «?»
    if latin_up and ch.isalpha() and ch.upper().isascii():
        # Н2 с выключенной русской раскладкой: латиница уходит в линию как
        # есть (только регистр поднимается — строка 0x60.. занята кириллицей
        # ЭВМ). Это режим набора команд ДИСПАК: «HOM» значит «НОМ».
        return ch.upper().encode("ascii")
    return ch.encode("ascii", errors="replace")


# Служебные клавиши сеанса (команды эмулятора, а не коды линии).
KEY_SEND = "SEND"       # «передать накопленное» (аналог клавиши SEND)
KEY_UNDO = "UNDO"       # «отмена»: вернуть буфер набора на шаг назад
                        # (АВТОНОМНО; глубина — [terminal] undo)
CTRL_UNDO = 0x15        # Ctrl+U (УП 15) — клавиша отмены правки набора
KEY_MODE = "MODE"       # переключение АВТОНОМНО ↔ С ЭВМ
KEY_CMDSET = "CMDSET"   # клавиша «НАБОР»: набор №1 ↔ набор №2 (VT52)
KEY_BLINK = "BLINK"     # клавиша «УПР.СИМВ»: показ/скрытие знаков УП
KEY_ECHO = "ECHO"       # клавиша «ЭХО»: подавить вывод набора на экран
KEY_CLEAR = "CLEAR"     # «ОЧИСТКА»: экран в историю, кадр — в начало
KEY_WHEELUP = "KEY_WHEELUP"       # колесо мыши вверх: листание «истории»
KEY_WHEELDOWN = "KEY_WHEELDOWN"   # колесо мыши вниз: к кадру ЭВМ
KEY_SELCLEAR = "SELCLEAR"   # снять всё выделение мышью (F3)
KEY_SELTAKE = "SELTAKE"     # взять выделенное в буфер набора (F4)
# Выделение с клавиатуры (Shift+стрелки): построчное; Shift+Ctrl+стрелки —
# поколонное. Стрелки 15ВВВ с такими кодами не работали, свободны.
KEY_SELRIGHT = "SELRIGHT"
KEY_SELLEFT = "SELLEFT"
KEY_SELUP = "SELUP"
KEY_SELDOWN = "SELDOWN"
KEY_CTRLSELRIGHT = "CTRLSELRIGHT"
KEY_CTRLSELLEFT = "CTRLSELLEFT"
KEY_CTRLSELUP = "CTRLSELUP"
KEY_CTRLSELDOWN = "CTRLSELDOWN"
KEY_CTRLRIGHT = "KEY_CTRLRIGHT"     # прыжок по словам: начало следующего
KEY_CTRLLEFT = "KEY_CTRLLEFT"       # …и текущего слова
KEY_CTRLUP = "KEY_CTRLUP"           # начало текущей строки
KEY_CTRLDOWN = "KEY_CTRLDOWN"       # начало строки ниже
KEY_SELHOME = "SELHOME"             # выделить до начала строки (Ctrl+Home)
KEY_SELEND = "SELEND"               # …до конца строки (Ctrl+End)
KEY_SELBSPACE = "SELBSPACE"         # Shift+Backspace — удалить выделенное
KEY_HELP = "HELP"                   # F12 — показать/убрать справку подвала
KEY_BEGIN = "KEY_BEGIN"             # середина цифровой клавиатуры без
                                    # NumLock («ESC [ E»): на 15ИЭ такой
                                    # клавиши нет — гасим нажатие

SPECIAL_KEYS = {"KEY_UNDO": KEY_UNDO, "KEY_SEND": KEY_SEND, "KEY_MODE": KEY_MODE,
                "KEY_CMDSET": KEY_CMDSET, "KEY_BLINK": KEY_BLINK,
                "KEY_ECHO": KEY_ECHO, "KEY_CLEAR": KEY_CLEAR,
                "KEY_SELCLEAR": KEY_SELCLEAR, "KEY_SELTAKE": KEY_SELTAKE,
                "F3": KEY_SELCLEAR, "F4": KEY_SELTAKE,
                "F5": KEY_CLEAR, "F6": KEY_ECHO, "F7": KEY_BLINK,
                "F8": KEY_CMDSET, "F9": KEY_MODE, "F10": KEY_SEND}

# Последовательности клавиатуры ПК (терминал в режиме cbreak) → ключи сеанса.
# F1 и F11 не разбираются принципиально: перехватываются окружением.
SEQ_KEYS = {
    b"\x1b[A": "KEY_UP", b"\x1bOA": "KEY_UP",
    b"\x1b[B": "KEY_DOWN", b"\x1bOB": "KEY_DOWN",
    b"\x1b[C": "KEY_RIGHT", b"\x1bOC": "KEY_RIGHT",
    b"\x1b[D": "KEY_LEFT", b"\x1bOD": "KEY_LEFT",
    b"\x1b[1;5C": KEY_CTRLRIGHT, b"\x1b[1;5D": KEY_CTRLLEFT,
    b"\x1b[1;5A": KEY_CTRLUP, b"\x1b[1;5B": KEY_CTRLDOWN,
    # Shift+стрелки — выделение с клавиатуры (построчное; с Ctrl —
    # поколонное: ESC[1;6A и т.п.). Имена без «KEY_»: в линию не уходят
    b"\x1b[1;2C": KEY_SELRIGHT, b"\x1b[1;2D": KEY_SELLEFT,
    b"\x1b[1;2A": KEY_SELUP, b"\x1b[1;2B": KEY_SELDOWN,
    b"\x1b[1;6C": KEY_CTRLSELRIGHT, b"\x1b[1;6D": KEY_CTRLSELLEFT,
    b"\x1b[1;6A": KEY_CTRLSELUP, b"\x1b[1;6B": KEY_CTRLSELDOWN,
    # Shift+Home/End СОЗНАТЕЛЬНО не привязываются: ОС перехватывает их
    # на листание вывода и в приложение они не доходят; где доходят —
    # гасит _parse_mod_key. Выделение строки — за Ctrl+Home/Ctrl+End
    b"\x1b[H": "KEY_HOME", b"\x1b[1~": "KEY_HOME", b"\x1bOH": "KEY_HOME",
    b"\x1b[4~": "KEY_END", b"\x1b[F": "KEY_END", b"\x1bOF": "KEY_END",
    b"\x1b[2~": "KEY_INSERT", b"\x1b[3~": "KEY_DELETE",
    b"\x1b[5~": "KEY_PAGEUP", b"\x1b[6~": "KEY_PAGEDOWN",
    # Цифровая клавиатура без NumLock — навигационный кластер: 7→Home,
    # 1→End, 9→PgUp, 3→PgDn, 0→Insert, .→Delete, 8/2/4/6→стрелки (они
    # уже разобраны выше как «ESC [ A…D»). Осталась середина «5» — на
    # настоящей 15ИЭ такой клавиши нет, гасим: раньше «ESC [ E» падал
    # в разбор как Esc+«[»+«E», а Esc попадал в буфер набора и при
    # перерисовке эха съедал весь текст (исчезал шрифт на экране)
    b"\x1b[E": "KEY_BEGIN",
    b"\x1bOR": KEY_SELCLEAR, b"\x1b[13~": KEY_SELCLEAR,   # F3 — снять выделение
    b"\x1bOS": KEY_SELTAKE, b"\x1b[14~": KEY_SELTAKE,     # F4 — взять в буфер
    b"\x1b[15~": KEY_CLEAR,                         # F5 — ОЧИСТКА экрана
    b"\x1b[[E": KEY_CLEAR,                          # F5 консоли Linux (ESC[[A…E)
    b"\x1b[[C": KEY_SELCLEAR,                       # F3 консоли Linux
    b"\x1b[[D": KEY_SELTAKE,                        # F4 консоли Linux
    # F1 — справка подвала: три формы (xterm, PF1 в application-режиме,
    # консоль Linux). Окружение может перехватывать F1 — тогда переключить
    # справку можно привязкой в [keys] на любую другую клавишу
    # СПРАВКА подвала — F12 (в консоли/окружении F1 может быть занят ОС,
    # см. шапку): три формы — xterm/console, PF1 в application-режиме и
    # сам F1 как запасная, если терминал его пропускает
    b"\x1b[24~": KEY_HELP, b"\x1b[11~": KEY_HELP, b"\x1bOP": KEY_HELP,
    b"\x1b[[A": KEY_HELP,
    b"\x1b[17~": KEY_ECHO,                          # F6 — ЭХО (подавить набор)
    b"\x1b[18~": KEY_BLINK,                         # F7 — УПР.СИМВ (показ УП)
    b"\x1b[19~": KEY_CMDSET,                        # F8 — НАБОР (набор 1↔2)
    b"\x1b[20~": KEY_MODE,                          # F9 — АВТОНОМНО↔С ЭВМ
    b"\x1b[21~": KEY_SEND,                          # F10 — SEND
    b"\x1b\x1b": "KEY_ESC",
}
_SEQ_LEN = sorted({len(seq) for seq in SEQ_KEYS}, reverse=True)


# Имена модификаторов в привязках [keys] (канонический порядок имён)
_COMBO_MODS = ("ctrl", "alt", "shift")


def _combo_name(code: int, mask: int) -> str | None:
    """Имя нажатия «модификаторы+знак» по кодовому знаку и маске
    (kitty: shift=1 alt=2 ctrl=4; xterm modifyOtherKeys: shift=1 ctrl=4
    alt=8 — маска уже приведена вызывающим). Без модификаторов — сам
    знак; служебные коды (<0x20) не именуются (их ведут старые пути)."""
    if code < 0x20 or 0xD800 <= code <= 0xDFFF or code >= 0xE000:
        return None            # УП, суррогаты и служебные keysym kitty —
                               # ведут старые пути (стрелки, клавиши редактора)
    parts = [m for m, bit in (("ctrl", 4), ("alt", 2), ("shift", 1))
             if mask & bit]
    parts.append(chr(code))
    return "+".join(parts) if len(parts) > 1 else chr(code)


def _parse_csi_u(data: bytes, i: int):
    """Разбор клавишных последовательностей с модификаторами:
    kitty/foot/wezterm `ESC [ код[:доп] ; модификатор u` и
    xterm modifyOtherKeys `ESC [ 27 ; модификатор ; код ~`.

    Возвращает (имя,consumed) или (None, 0)."""
    j = data.find(b"[", i)
    if j != i + 1:
        return None, 0
    ends = [p for p in (data.find(b"u", j + 1), data.find(b"~", j + 1)) if p >= 0]
    k = min(ends) if ends else -1
    if k < 0 or k - j > 16:
        return None, 0
    term = data[k]
    body = data[j + 1:k]
    try:
        toks = [t.split(b":")[0] for t in body.split(b";")]
        nums = [int(t) if t else 1 for t in toks]
    except ValueError:
        return None, 0
    if term == 0x7E:                       # '~': только форма xterm 27;м;код
        if len(nums) != 3 or nums[0] != 27:
            return None, 0
        mask = (nums[1] - 1) & 0b1101        # xterm: shift=1 ctrl=4 alt=8
        mask = (mask & 1) | (mask & 4) | ((mask & 8) >> 2)
        if nums[2] == 127 and mask & 1:    # Shift+Backspace (xterm)
            return KEY_SELBSPACE, k + 1 - i
        return _combo_name(nums[2], mask), k + 1 - i
    if len(nums) == 1:
        # управляющие в CSI-u (kitty/foot/wezterm с включёнными флагами):
        # Esc=27, Enter=13, Tab=9, Backspace=8/127 — в сыром виде их
        # «CSI 27 u» разбор бы рассыпал в знаки «[27u»
        if nums[0] in _CSI_U_CTRL:
            return _CSI_U_CTRL[nums[0]], k + 1 - i
        return _combo_name(nums[0], 0), k + 1 - i
    if len(nums) == 2:                     # kitty: код;модификаторы
        mod = (nums[1] - 1) & 7
        if nums[0] in (8, 127) and mod & 1:
            # Shift+Backspace: 127 (DEL) и 8 (BS) — терминалы шлют оба
            return KEY_SELBSPACE, k + 1 - i
        if nums[0] in _CSI_U_CTRL and not (mod & 2):
            # Enter/Esc/Tab с Shift — те же клавиши (Alt гасим ниже)
            return _CSI_U_CTRL[nums[0]], k + 1 - i
        return _combo_name(nums[0], mod), k + 1 - i
    return None, 0


# Управляющие клавиши в кодировке CSI-u (по номеру УП): имя, которое
# дальше ведёт обычный путь (decode_key_bytes / key_to_bytes)
_CSI_U_CTRL = {9: "\t", 10: "KEY_ENTER", 13: "KEY_ENTER",
               27: "KEY_ESC", 8: "KEY_BACKSPACE", 127: "KEY_BACKSPACE"}


# Стрелки и функциональные клавиши с модификатором (xterm):
# ESC [ 1 ; mod A…Z, ESC [ n ; mod ~, где mod = 1+shift(1)+alt(2)+ctrl(4).
# Клавиши 15ИЭ (стрелки ВКДа и Ф1–Ф10) без модификаторов, поэтому такие
# нажатия в линию ничего не отправляют: иначе escape-мусор ушёл бы в
# ЭВМ («ESC [ 1 ; 2 A»), а её эхо забивало бы кадр — «циклическая прокрутка».
# Ctrl+буква — исключение: это управляющий символ (Ctrl+E → 05), его ведёт
# key_to_bytes. Буквы разбираем все, а не только A…D стрелок: иначе
# «ESC [ 1 ; 5 E» (старая форма modifyOtherKeys) рассыпалась бы в знаки
# и в набор уходила бы «[» — ровно то, что видно на терминале без CSI-u.
_MOD_KEY = re.compile(rb"\x1b\[(?:1;(\d+)([A-Za-z])|(\d+);(\d+)~)")


# Функциональные клавиши xterm по букве: их тоже можно нажать
# с модификатором — и тогда имя собирается как «модификаторы+клавиша».
_MOD_LETTER = {"A": KEY_CTRLUP, "B": KEY_CTRLDOWN,          # стрелки: Ctrl
               "C": KEY_CTRLRIGHT, "D": KEY_CTRLLEFT,
               "H": KEY_SELHOME, "F": KEY_SELEND}           # Shift+Home/End


def _parse_mod_key(data: bytes, i: int):
    """Нажатие клавиши с модификатором (Shift+↑, Ctrl+Home, Ctrl+E и т. п.).

    Shift+стрелки отдаём выделению с клавиатуры (Shift — построчное,
    Shift+Ctrl — поколонное), Ctrl+стрелки — прыжкам по словам/строкам.
    Ctrl+буква даёт управляющий символ (имя «ctrl+буква» → key_to_bytes).
    Остальные комбинации (Alt+…, Ctrl+F5 и т. п.) гасим: таких клавиш
    у 15ВВВ нет, в линию мусор не идёт.

    Возвращает (имя клавиши или «», сколько байт съели) либо (None, 0),
    если это не такая последовательность.
    """
    m = _MOD_KEY.match(data, i)
    if not m:
        return None, 0
    mod = int(m.group(1) or m.group(4))
    if mod < 2:                       # без модификаторов — разбирают ниже
        return None, 0
    mask = mod - 1                    # параметр xterm = 1+shift(1)+alt(2)+ctrl(4)
    letter = m.group(2)               # «A»…«Z» — ESC [ 1 ; m X
    if letter:
        ch = letter.decode("ascii")
        base = _MOD_LETTER.get(ch)
        shift, ctrl = bool(mask & 1), bool(mask & 4)
        if base in (KEY_SELHOME, KEY_SELEND):
            # Home/End с модификатором. Shift+Home/End блокируем: ОС
            # перехватывает их на листание вывода и в приложение они не
            # доходят, а где доходят — гасим, чтобы поведение не зависело
            # от терминала. Выделение строки набора — за Ctrl+Home/End
            # (та же клавиша в форме «ESC [ 1 ; 5 H»).
            if shift or mask & 2:
                return "", m.end() - i
            return base, m.end() - i
        if mask & 2:                  # Alt — таких клавиш у 15ВВВ нет
            return "", m.end() - i
        if base is None:              # не стрелка — буква с Ctrl/Shift
            # сдвинутая буква приходит заглавной и с битом shift; без сдвига
            # терминалы шлют и строчную, и заглавную — имя приводим к виду
            # «ctrl+буква», как его даёт CSI-u (имя пишут в [keys])
            name = _combo_name(ord(ch if shift else ch.lower()),
                               mask if shift else mask & ~1)
            return name or "", m.end() - i
        if shift and ctrl:
            return {KEY_CTRLUP: KEY_CTRLSELUP, KEY_CTRLDOWN: KEY_CTRLSELDOWN,
                    KEY_CTRLRIGHT: KEY_CTRLSELRIGHT,
                    KEY_CTRLLEFT: KEY_CTRLSELLEFT}[base], m.end() - i
        if shift:
            return {KEY_CTRLUP: KEY_SELUP, KEY_CTRLDOWN: KEY_SELDOWN,
                    KEY_CTRLRIGHT: KEY_SELRIGHT,
                    KEY_CTRLLEFT: KEY_SELLEFT}.get(base, base), m.end() - i
        return base, m.end() - i      # Ctrl без Shift — слова/строки
    # «~»-форма: ESC [ код ; модификатор ~. Часть терминалов (консоль Linux,
    # старый xterm) шлёт Home/End именно так: 1 — Home, 4 — End. С Shift —
    # блокируем (см. выше), с Ctrl — выделение строки набора.
    code = int(m.group(3))
    if code in (1, 4):
        if mask == 4:                 # Ctrl+Home / Ctrl+End
            return KEY_SELHOME if code == 1 else KEY_SELEND, m.end() - i
        return "", m.end() - i        # Shift/прочие — блок
    return "", m.end() - i            # Alt/прочие «~»-формы — гасим


def _parse_bracket_paste(data: bytes, i: int):
    """Вставка в квадратных скобках (bracketed paste): «ESC [ 200 ~ текст
    ESC [ 201 ~» → весь текст одним ключом.

    Эмулятор включает режим терминала 2004 (см. run_line), и терминал
    оборачивает вставку из буфера обмена (Ctrl+Shift+V) этими метками:
    границы вставки видны точно, без тайминговой догадки group_paste
    (та остаётся запасным путём для терминалов без поддержки).

    Управляющие из буфера обмена не заводим: ESC сломал бы разбор эха,
    «\r\n»/«\r» приводим к одному разделителю (как в OSC52).

    Возвращает (текст, сколько байт съели); метки нет — (None, 0).
    """
    if data[i:i + 6] == b"\x1b[201~":
        return "", 6                     # один конец без начала — гасим
    if data[i:i + 6] != b"\x1b[200~":
        return None, 0
    j = data.find(b"\x1b[201~", i + 6)
    if j < 0:
        return None, 0                  # хвост доберёт split_incomplete
    text = data[i + 6:j].decode("utf-8", "replace")
    # управляющие не заводим: ESC сломал бы разбор эха, перевод строки —
    # ПР (0x0D), как у ВК: это разделитель строк и буфера, и линии
    text = (text.replace("\x1b", "").replace("\r\n", "\n")
            .replace("\n", "\r"))
    return text, (j + 6) - i


def _parse_osc52(data: bytes, i: int):
    """Ответ терминала на запрос буфер обмена (OSC 52): base64 → текст.

    Вставка из внешнего источника (Ctrl+Shift+V): эмулятор шлёт
    «ESC ] 52 ; c ; ? », терминал отвечает «ESC ] 52 ; c ; <base64> BEL»
    (или ST). Текст возвращается как нажатие — дальше он проходит ту же
    перекодировку, что и ручной набор (encode_koi7 → линия/буфер).

    Возвращает (текст, сколько байт съели); текст «» — ответ съеден,
    но пуст (терминал буфер отдать не захотел), None/0 — это не он.
    """
    for term in (b"\x07", b"\x1b\\"):         # BEL или ST
        m = re.match(rb"\x1b\]52;[^;\x07\x1b]*;"
                     rb"([^\x07\x1b]*)" + re.escape(term), data[i:])
        if m:
            payload = m.group(1)
            if not payload:
                return "", m.end() - i        # терминал не отдал буфер
            try:
                text = base64.b64decode(payload, validate=True).decode("utf-8")
            except Exception:
                return "", m.end() - i        # мусор — гасим молча
            # управляющие из буфера обмена не заводим: ESC сломал бы
            # разбор эха; перевод строки — ПР (0x0D), как у ВК
            text = (text.replace("\x1b", "").replace("\r\n", "\n")
                    .replace("\n", "\r"))
            return text, m.end() - i
    return None, 0


def _parse_mouse(data: bytes, i: int):
    """Мышь терминала, отданная приложению.

    Понимает три кодировки, чтобы работать на любом терминале:
    SGR (1006) — `ESC [ < b ; x ; y M/m`, urxvt (1015) — `ESC [ b ; x ; y M`
    и старую X10 — `ESC [ M` + три байта (код+32).

    Возвращает (имя нажатия или «», сколько байт съели). Колесо —
    листание «истории», левая кнопка — выделение текста (нажатие,
    протягивание, отпускание; координаты окна в знаках после «@»).
    С нажатым Ctrl имя начинается с «ctrl+»: так выбирается выделение
    колонкой вместо строчного. Остальные события (правая кнопка,
    движение без кнопки) съедаются молча, чтобы они не попали в линию
    как мусор.
    """
    if data[i + 2:i + 3] == b"M":                 # X10: три байта кода
        if len(data) < i + 6:
            return None, 0
        btn, x, y = (b - 32 for b in data[i + 3:i + 6])
        press, end = True, i + 6
    else:
        j = i + 2
        body = data[j:j + 24]
        sgr = body[:1] == b"<"                    # SGR
        k = body.find(b"M")
        m = body.find(b"m")
        ends = [p for p in (k, m) if p >= 0]
        if not ends:
            return None, 0
        k = min(ends)
        try:
            if sgr:
                btn, x, y = (int(v) for v in body[1:k].split(b";"))
                press = body[k:k + 1] == b"M"
            else:                                  # urxvt: без '<' и 'm'
                btn, x, y = (int(v) for v in body[:k].split(b";"))
                btn -= 32
                press = True
        except ValueError:
            return None, 0
        end = j + k + 1
    # 16 — Ctrl: по нему выбирается вид выделения (протяжка с Ctrl —
    # колонкой), поэтому запоминаем его до снятия модификаторов
    ctrl = "ctrl+" if btn & 16 else ""
    btn &= ~0b10100                              # без shift/meta/ctrl
    moving = bool(btn & 0b110000)                # 32 — движение, 64 — drag
    btn &= ~0b110000
    if btn == 3:                                  # X10: отпускание — код 3
        press = False                          # (в SGR такого кода не бывает)
    if not press:                                 # отпускание
        # кнопка 0 без бита движения — обычное отпускание; X10-код 3
        # (в том числе с битом движения при отпускании протяжки) — тоже
        if (btn == 0 and not moving) or btn == 3:
            return f"{ctrl}MOUSEUP@{x},{y}", end - i
        return "", end - i                       # колесо/движение — тихо
    if btn == 64:                                 # колесо вверх
        return KEY_WHEELUP, end - i
    if btn == 65:                                 # колесо вниз
        return KEY_WHEELDOWN, end - i
    if btn == 0:                                  # левая кнопка
        kind = "MOUSEDRAG" if moving else "MOUSEDOWN"
    else:                                         # правая/средняя — тихо
        return "", end - i
    return f"{ctrl}{kind}@{x},{y}", end - i


def is_key_combo(key: str) -> bool:
    """Имя нажатия «ctrl/alt/shift+знак» (из _combo_name)."""
    parts = key.split("+")
    return len(parts) >= 2 and all(p in _COMBO_MODS for p in parts[:-1]) \
        and len(parts[-1]) == 1


def normalize_combo(s: str) -> str:
    """Привязка из конфига к каноническому имени: модификаторы в любом
    порядке и регистре («Б+Ctrl» и «Ctrl+Б» → «ctrl+Б»); сам знак
    сохраняется как есть — терминал присылает сдвинутую клавишу своим
    кодовым знаком, точное имя незакрытого нажатия видно в подвале."""
    parts = s.split("+")
    mods = {p.lower() for p in parts if p.lower() in _COMBO_MODS}
    rest = "+".join(p for p in parts if p.lower() not in _COMBO_MODS)
    if not mods or not rest:
        return s
    return "+".join([m for m in _COMBO_MODS if m in mods] + [rest])


def _utf8_tail(data: bytes) -> bytes:
    """Недобор многобайтового символа UTF-8 в конце куска (если есть).

    Вставка из внешнего источника (буфер обмена) — UTF-8, и `os.read`
    рвёт её где угодно: без удержания хвоста вторая половина буквы
    уезжала бы в набор отдельными байтами («Ð¼» вместо «м»).
    """
    for back in range(1, 4):
        if back > len(data):
            break
        b = data[-back]
        if 0xC2 <= b <= 0xF4:               # начало 2..4-байтовой формы
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            if back < need:                 # продолжений не хватает — ждём
                return data[-back:]
    return b""


def split_incomplete(data: bytes) -> tuple[bytes, bytes]:
    """Разделить данные на разобранное и недобранный хвост escape-последовательности.

    Терминал отдаёт нажатие несколькими байтами («ESC [ 27 ; 5 ; 101 ~»), и
    `os.read` возвращает их НЕ обязательно целиком. Если первый кусок
    кончается на «ESC» или «ESC [ 27 ;», разбирать его нельзя: ESC погаснет,
    а «[ 2 7 ;» уйдёт в набор обычными знаками — ровно тот «[», что
    наблюдался вместо Ctrl+E. Поэтому хвост, который ещё может стать
    последовательностью, ждёт следующих байтов.

    Правило: у CSI финальный байт — 0x40…0x7E, а всё до него параметры
    (0x20…0x3F). Пока параметры идут и финального байта нет — ждём. Отдельно
    «ESC [ M»: это старая мышь X10, за ней обязательно ещё три байта.
    Аналогично ждём завершителя OSC (BEL/ST) и недобор многобайтовой
    буквы UTF-8 (вставка из буфера обмена).

    Возвращает (полная часть, хвост).
    """
    hold = _utf8_tail(data)                 # недобор UTF-8-буквы в конце
    core = data[:len(data) - len(hold)] if hold else data
    i = core.rfind(b"\x1b")
    if i < 0:
        return core, hold
    tail = core[i:]
    n = len(tail)
    if n == 1:                          # ждём знака после ESC
        return core[:-1], tail + hold
    if tail.startswith(b"\x1b[200~"):
        # bracketed paste: держим до метки конца «ESC [ 201 ~» — вставка
        # приходит чтениями по 256 байт и целиком разобрать её нельзя
        if core[i + 6:].find(b"\x1b[201~") >= 0:
            return core, hold           # метка есть — разбираем целиком
        return core[:-n], tail + hold
    if tail[:2] == b"\x1b[":
        rest = tail[2:]
        if not rest or all(0x20 <= c <= 0x3F for c in rest):
            return core[:-n], tail + hold   # параметры идут, финального нет
        if rest[0] == 0x4D and n < 6:    # «ESC [ M» + код,x,y — мышь X10
            return core[:-n], tail + hold
        return core, hold                 # финальный байт есть — разбираем
    if tail[:2] == b"\x1b]":
        # OSC-ответ (буфер обмена OSC52): ждём завершителя BEL или ST
        body = tail[2:]
        if b"\x07" in body or b"\x1b\\" in body:
            return core, hold
        return core[:-n], tail + hold
    if tail[:2] == b"\x1bO":
        return core, hold                 # SS3 («ESC O A») приходит целиком
    if n < 8 and any(seq.startswith(tail) for seq in SEQ_KEYS):
        return core[:-n], tail + hold       # префикс известной последовательности
    return core, hold


def decode_keys(data: bytes, pending: bytes = b"") -> tuple[list[str], bytes]:
    """Разобрать нажатия с учётом недобранного хвоста (состоятельный вход).

    Возвращает (имена нажатий, хвост для следующего вызова). Обычный
    `decode_key_bytes` годится там, где данные приходят целиком (--feed,
    тесты); для живой клавиатуры нужен этот: он копит хвост между чтениями.
    """
    whole, tail = split_incomplete(pending + data)
    return decode_key_bytes(whole), tail


# Вставка из буфера обмена: хвост, пришедший отдельным чтением, продолжает
# ту же вставку (буфер > 256 байт не влезает в одно чтение)
PASTE_GAP = 0.15         # сек: чтения впритык — продолжение той же вставки


def group_paste(keys: list[str], now: float, paste_until: float,
                gap: float = PASTE_GAP) -> tuple[list[str], float]:
    """Отметить вставку из буфера обмена среди нажатий.

    Настоящую вставку терминал присылает одним куском: с метками
    «ESC [ 200 ~ … ESC [ 201 ~» (bracketed paste) или ответом на запрос
    буфера обмена (OSC 52) — их разбирает `decode_key_bytes`, и текст
    приходит одним ключом. Он и есть вставка: уходит в линию целиком
    (feed_key → key_to_bytes → encode_koi7 — верная перекодировка; в Н2
    посимвольный набор иначе превратил бы «ВНИМАНИЕ» в «DYBVFYBT»).

    Взрыв знаков одного чтения вставкой НЕ считается: так приходит
    быстрый набор — терминал отдаёт всё, что накопилось с прошлого
    чтения, а не только что нажато. Раньше такой взрыв принимался за
    вставку, и путь вставки переписывал пробелы (лишний пробел между
    фрагментами, двойные пробелы схлопывались), а набранное появлялось
    на экране с задержкой.

    Хвост настоящей вставки, пришедший отдельным чтением, продолжает её
    (короткий, но в промежутке gap — состояние передаётся вызывающему
    через paste_until).

    KEY_*/мыши/управляющие — не знаки: они рвут строку и уходят сами
    (ВК посреди вставки — это нажатие, а не её часть).

    Возвращает (ключи, paste_until) — момент, до которого действует
    признак вставки.
    """
    out: list[str] = []
    run: list[str] = []
    active = now < paste_until

    def flush() -> None:
        nonlocal active, paste_until
        if not run:
            return
        if active:
            # продолжение вставки — один ключ (перевод строки из буфера
            # обмена — ПР (0x0D), как у ВК, а не LF)
            out.append("".join(run).replace("\n", "\r"))
            paste_until = now + gap         # хвост чтения тоже вставка
        else:
            out.extend(run)                # обычный набор — посимвольно
        run.clear()

    for key in keys:
        if (len(key) > 1 and not is_key_combo(key)
                and not key.startswith(("KEY_", "MOUSE", "ctrl+MOUSE"))):
            # настоящая вставка: текст пришёл одним куском
            flush()
            out.append(key)
            active = True
            paste_until = now + gap
            continue
        if (len(key) == 1
                and (key.isprintable() or key in "\n\t")):
            run.append(key)
            continue
        flush()
        out.append(key)
    flush()
    return out, (paste_until if active else 0.0)


def decode_key_bytes(data: bytes) -> list[str]:
    """Распарсить байты нажатий в имена клавиш/символы для feed_key().

    Многобайтовые UTF-8-символы (русские буквы с клавиатуры ПК) собираются
    в один символ, чтобы дальше корректно кодироваться в КОИ7.
    """
    out: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x0D:                      # Enter (cbreak-режим даёт CR)
            out.append("KEY_ENTER")
            i += 1
            continue
        if b in (0x08, 0x7F):              # Backspace/DEL
            out.append("KEY_BACKSPACE")
            i += 1
            continue
        if b == CTRL_UNDO:                 # Ctrl+U — отмена правки набора
            out.append(KEY_UNDO)
            i += 1
            continue
        if b == 0x1B:
            name, adv = _parse_bracket_paste(data, i)
            if name is not None:               # вставка в квадратных скобках
                if name:
                    out.append(name)           # весь текст одним ключом
                i += adv
                continue
            name, adv = _parse_mouse(data, i)
            if name is not None:               # событие мыши (SGR)
                if name:
                    out.append(name)
                i += adv
                continue
            name, adv = _parse_osc52(data, i)
            if name is not None:               # ответ на запрос буфера ОС
                if name:
                    out.append(name)           # текст вставки → перекодировка
                i += adv
                continue
            name, adv = _parse_csi_u(data, i)
            if name:                       # нажатие с модификаторами (CSI-u)
                out.append(name)
                i += adv
                continue
            matched = False
            for ln in _SEQ_LEN:
                seq = data[i:i + ln]
                if ln <= n - i and seq in SEQ_KEYS:
                    out.append(SEQ_KEYS[seq])
                    i += ln
                    matched = True
                    break
            if not matched:
                # Стрелка/функциональная с Shift или Alt: гасим нажатие —
                # таких клавиш на 15ВВВ нет, в линию мусор не идёт
                # (иначе Shift+↑ уезжал бы в ЭВМ как «ESC [ 1 ; 2 A»)
                name, adv = _parse_mod_key(data, i)
                if name is not None:
                    if name:
                        out.append(name)
                    i += adv
                    continue
                # Alt+знак: терминалы (в т.ч. VTE без CSI-u) присылают
                # Alt как ESC-префикс перед знаком; ESC перед «[»/«O»
                # и одиночный ESC остаются клавишей Esc как раньше
                nxt = data[i + 1:i + 2]
                ln = 0
                if nxt:
                    c = nxt[0]
                    if 0x20 <= c <= 0x7E and c not in (0x5B, 0x4F):
                        ln = 1
                    elif 0xC2 <= c < 0xE0:
                        ln = 2
                    elif 0xE0 <= c < 0xF0:
                        ln = 3
                    elif 0xF0 <= c < 0xF8:
                        ln = 4
                if ln and i + 1 + ln <= n:
                    chunk = data[i + 1:i + 1 + ln]
                    try:
                        ch = chunk.decode("utf-8")
                    except UnicodeDecodeError:
                        ch = ""
                    if len(ch) == 1 and ord(ch) >= 0x20:
                        out.append("alt+" + ch)
                        i += 1 + ln
                        continue
                out.append("KEY_ESC")
                i += 1
            continue
        if b >= 0x80:                      # UTF-8 (русская буква с ПК)
            ln = (2 if b < 0xE0 else 3 if b < 0xF0 else 4)
            chunk = data[i:i + ln]
            try:
                out.append(chunk.decode("utf-8"))
                i += ln
                continue
            except UnicodeDecodeError:
                out.append(chr(b))
                i += 1
                continue
        out.append(chr(b))
        i += 1
    return out