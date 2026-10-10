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
  F9               — СЕТЬ: сеанс АВТОНОМНО ↔ С ЭВМ (служебная)
  F10              — ПЕРЕДАЧА (SEND): отдаёт буфер (при незакрытой
                     строке добавляет ПР ПС) и переводит сеанс в С ЭВМ
  (F1 и F11 не используются — заняты окружением рабочего стола)

Русская раскладка включается --layout positional|phonetic (или --koi7 —
позиционная по умолчанию): клавиши без Shift печатают русские буквы
(QWERTY2KOI7_POS — по положению клавиш ЙЦУКЕН, QWERTY2KOI7 — фонетика),
а «регистр» (Shift) переключает алфавит на английский, как на 15ВВВ.
Работает при любой раскладке ОС: EN-раскладка присылает «W» (проходит
как есть), RU-раскладка — «Ц» (возвращается английской буквой своего
назначения в выбранной таблице).
В наборе Н2 (раскладка выключена) без Shift печатаются заглавные обоих
алфавитов (русская раскладка — русские, латинская — английские), а Shift
меняет алфавит по позиционной таблице в обе стороны: ru+Shift→W, W+Shift→Ц.
"""
from __future__ import annotations

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

def key_to_bytes(key: str, layout: str | None = None,
                 shift_to_rus: bool = False) -> bytes:
    """Нажатие ПК-клавиши → байты внутреннего КОИ7-потока терминала.

    layout: None — выключена; "positional" (по умолчанию для --koi7) —
    клавиша даёт русскую букву своего положения (q→Й); "phonetic" —
    фонетическая таблица (w→В, q→Я). Заглавные русские — с битом
    алфавита (0x80); в байты линии их переводит `charset.to_line`.
    shift_to_rus (режим Н2 при layout=None): набор одно-регистрный —
    без Shift русская раскладка даёт русские заглавные (внутренний
    верх 0x80+код), латинская — английские заглавные; Shift меняет
    алфавит по позиционной таблице в обе стороны: W→Ц, Ц→W. Знаки
    ^ @ # № [ ] ' | ? % ; ! > < набираются одним и тем же знаком
    с обеих раскладок и при любом CapsLock (N2_SYMBOLS + проброс
    ASCII как есть).
    """
    if key in ("KEY_ENTER", "\n", "\r"):
        return b"\r\n"
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
    # Несколько знаков подряд (--feed "строка") — кодируем целиком в КОИ7 Н1
    # (в т.ч. кириллицу; ascii-кодирование съедало русские буквы).
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
    # Русский символ уходит в линию как код КОИ7 Н1 (с битом алфавита).
    # При включённой русской раскладке «регистр» (Shift) переключает
    # алфавит: заглавная кириллица печатается английской буквой своего
    # назначения по обратной таблице выбранной раскладки.
    table = KOI7_TABLES.get(layout or "")
    up = ch.upper().replace("Ё", "Е")
    if up in RUS7:
        if ch.isupper() and (layout or shift_to_rus):
            # та же смена алфавита и в Н2 (раскладка выключена) — по
            # позиционной таблице: Shift+русская → латинская положения
            lat = KOI7_TO_QWERTY[layout or "positional"].get(RUS7.index(up))
            if lat:
                return lat.upper().encode("ascii")
        if shift_to_rus and layout is None:
            # Н2: кириллица одно-регистрная — русская буква кладётся
            # сразу внутренним верхом (0x80+код), как её отдаёт ЭВМ
            return bytes([RUS7.index(up) | ALPHA_BIT])
        return encode_koi7(ch)
    # Латиница без Shift — русская буква выбранной раскладки; с Shift
    # (заглавная) — английский набор, переназначения нет.
    if table and ch in table:
        return bytes([table[ch] | ALPHA_BIT | 0x60])
    if shift_to_rus and layout is None and ch.isalpha():
        # Н2 при выключенной русской раскладке: Shift+клавиша — русская
        # буква положения клавиши (W→Ц), симметрично «русская+Shift→лат.»
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
    return ch.encode("ascii", errors="replace")


# Служебные клавиши сеанса (команды эмулятора, а не коды линии).
KEY_SEND = "SEND"       # «передать накопленное» (аналог клавиши SEND)
KEY_MODE = "MODE"       # переключение АВТОНОМНО ↔ С ЭВМ
KEY_CMDSET = "CMDSET"   # клавиша «НАБОР»: набор №1 ↔ набор №2 (VT52)
KEY_BLINK = "BLINK"     # клавиша «УПР.СИМВ»: показ/скрытие знаков УП
KEY_ECHO = "ECHO"       # клавиша «ЭХО»: подавить вывод набора на экран
KEY_CLEAR = "CLEAR"     # «ОЧИСТКА»: экран в историю, кадр — в начало
KEY_WHEELUP = "KEY_WHEELUP"       # колесо мыши вверх: листание «истории»
KEY_WHEELDOWN = "KEY_WHEELDOWN"   # колесо мыши вниз: к кадру ЭВМ

SPECIAL_KEYS = {"KEY_SEND": KEY_SEND, "KEY_MODE": KEY_MODE,
                "KEY_CMDSET": KEY_CMDSET, "KEY_BLINK": KEY_BLINK,
                "KEY_ECHO": KEY_ECHO, "KEY_CLEAR": KEY_CLEAR,
                "F5": KEY_CLEAR, "F6": KEY_ECHO, "F7": KEY_BLINK,
                "F8": KEY_CMDSET, "F9": KEY_MODE, "F10": KEY_SEND}

# Последовательности клавиатуры ПК (терминал в режиме cbreak) → ключи сеанса.
# F1 и F11 не разбираются принципиально: перехватываются окружением.
SEQ_KEYS = {
    b"\x1b[A": "KEY_UP", b"\x1bOA": "KEY_UP",
    b"\x1b[B": "KEY_DOWN", b"\x1bOB": "KEY_DOWN",
    b"\x1b[C": "KEY_RIGHT", b"\x1bOC": "KEY_RIGHT",
    b"\x1b[D": "KEY_LEFT", b"\x1bOD": "KEY_LEFT",
    b"\x1b[1;5C": "KEY_CTRLRIGHT", b"\x1b[1;5D": "KEY_CTRLLEFT",
    b"\x1b[1;5A": "KEY_CTRLUP", b"\x1b[1;5B": "KEY_CTRLDOWN",
    b"\x1b[H": "KEY_HOME", b"\x1b[1~": "KEY_HOME", b"\x1bOH": "KEY_HOME",
    b"\x1b[4~": "KEY_END", b"\x1b[F": "KEY_END", b"\x1bOF": "KEY_END",
    b"\x1b[2~": "KEY_INSERT", b"\x1b[3~": "KEY_DELETE",
    b"\x1b[5~": "KEY_PAGEUP", b"\x1b[6~": "KEY_PAGEDOWN",
    b"\x1b[15~": KEY_CLEAR,                         # F5 — ОЧИСТКА экрана
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
        return _combo_name(nums[2], mask), k + 1 - i
    if len(nums) == 1:
        return _combo_name(nums[0], 0), k + 1 - i
    if len(nums) == 2:                     # kitty: код;модификаторы
        return _combo_name(nums[0], (nums[1] - 1) & 7), k + 1 - i
    return None, 0


def _parse_mouse(data: bytes, i: int):
    """Мышь терминала в режиме SGR (мышь отдана приложению).

    Возвращает (имя клавиши или «», сколько байт съели). Колесо вверх/вниз
    (кнопки 64/65) — листание «истории» выдачи, остальные события (клики,
    движение) эмулятору не нужны: съедаем молча, чтобы они не попали в
    линию как мусор.
    """
    j = data.find(b"<", i)
    if j != i + 2:
        return None, 0
    k = data.find(b"M", j)
    m = data.find(b"m", j)                      # отпускание — без колеба
    ends = [p for p in (k, m) if p >= 0 and p - j < 24]
    if not ends:
        return None, 0
    k = min(ends)
    try:
        btn = int(data[j + 1:k].split(b";")[0])
    except ValueError:
        return None, 0
    if data[k:k + 1] != b"M":                  # отпускание колеса — тихо
        return "", k + 1 - i
    base = btn & ~0b10100                     # без shift/meta/ctrl (4|8|16)
    if base == 64:
        return KEY_WHEELUP, k + 1 - i
    if base == 65:
        return KEY_WHEELDOWN, k + 1 - i
    return "", k + 1 - i


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
        if b == 0x1B:
            name, adv = _parse_mouse(data, i)
            if name is not None:               # событие мыши (SGR)
                if name:
                    out.append(name)
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