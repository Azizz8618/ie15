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
  PgUp / PgDn      — ESC J / ESC E (стереть до конца экрана / очистить)
  Insert / Delete  — ESC b / ESC c (инверсное / нормальное видео)
  Ctrl+← / Ctrl+→  — курсор на начало текущего (следующего) слова;
                     разделители слова — пробел и точка (локальная навигация)
  Ctrl+↑ / Ctrl+↓  — курсор в начало текущей / нижней строки (локально)
  F8               — НАБОР: набор команд №1 ↔ №2 (VT52) (служебная)
  F7               — БЛИНК: показывать/скрыть образные знаки УП (как
                     режим blink на Видеотоне-340) (служебная)
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
# положения на клавиатуре ЙЦУКЕН (q→Й, w→Ц, …). Ъ (клавиша «]») в Н1
# отсутствует и не отображается; «'» (Ё) даёт Е, как везде в КОИ7 Н1.
QWERTY2KOI7_POS = {
    "q": 0x0A, "w": 0x03, "e": 0x15, "r": 0x0B, "t": 0x05, "y": 0x0E,
    "u": 0x07, "i": 0x1B, "o": 0x1D, "p": 0x1A, "[": 0x08,
    "a": 0x06, "s": 0x19, "d": 0x17, "f": 0x01, "g": 0x10, "h": 0x12,
    "j": 0x0F, "k": 0x0C, "l": 0x04, ";": 0x1C, "'": 0x05,
    "z": 0x11, "x": 0x1E, "c": 0x13, "v": 0x0D, "b": 0x09, "n": 0x14,
    "m": 0x18, ",": 0x02, ".": 0x00,
}

# Выбор раскладки по имени (None — выключена)
KOI7_TABLES = {"phonetic": QWERTY2KOI7, "positional": QWERTY2KOI7_POS}

# Обратные таблицы: код КОИ7-буквы → клавиша QWERTY. «Регистр» (Shift)
# в русской раскладке переключает алфавит: заглавная кириллица с
# клавиатуры (RU-раскладка ОС шлёт «Ц» на Shift+ц) печатается английской
# буквой своего назначения. Коллизии (фонетич. w/v→Ж, позиц. t/'→Е)
# разрешаются в пользу первой записи таблицы.
KOI7_TO_QWERTY = {}
for _name, _table in KOI7_TABLES.items():
    _inv = {}
    for _key, _code in _table.items():
        _inv.setdefault(_code, _key)
    KOI7_TO_QWERTY[_name] = _inv


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

def key_to_bytes(key: str, layout: str | None = None) -> bytes:
    """Нажатие ПК-клавиши → байты внутреннего КОИ7-потока терминала.

    layout: None — выключена; "positional" (по умолчанию для --koi7) —
    клавиша даёт русскую букву своего положения (q→Й); "phonetic" —
    фонетическая таблица (w→В, q→Я). Заглавные русские — с битом
    алфавита (0x80); в байты линии их переводит `charset.to_line`.
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
    if key == "KEY_PAGEUP":
        return b"\x1bJ"        # стереть до конца экрана
    if key == "KEY_PAGEDOWN":
        return b"\x1bE"        # очистить экран, курсор в «дом»
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
    # Русский символ уходит в линию как код КОИ7 Н1 (с битом алфавита).
    # При включённой русской раскладке «регистр» (Shift) переключает
    # алфавит: заглавная кириллица печатается английской буквой своего
    # назначения по обратной таблице выбранной раскладки.
    table = KOI7_TABLES.get(layout or "")
    up = ch.upper().replace("Ё", "Е")
    if up in RUS7:
        if layout and ch.isupper():
            lat = KOI7_TO_QWERTY[layout].get(RUS7.index(up))
            if lat:
                return lat.upper().encode("ascii")
        return encode_koi7(ch)
    # Латиница без Shift — русская буква выбранной раскладки; с Shift
    # (заглавная) — английский набор, переназначения нет.
    if table and ch in table:
        return bytes([table[ch] | ALPHA_BIT | 0x60])
    return ch.encode("ascii", errors="replace")


# Служебные клавиши сеанса (команды эмулятора, а не коды линии).
KEY_SEND = "SEND"       # «передать накопленное» (аналог клавиши SEND)
KEY_MODE = "MODE"       # переключение АВТОНОМНО ↔ С ЭВМ
KEY_CMDSET = "CMDSET"   # клавиша «НАБОР»: набор №1 ↔ набор №2 (VT52)
KEY_BLINK = "BLINK"     # клавиша «БЛИНК»: показ/скрытие образных знаков УП

SPECIAL_KEYS = {"KEY_SEND": KEY_SEND, "KEY_MODE": KEY_MODE,
                "KEY_CMDSET": KEY_CMDSET, "KEY_BLINK": KEY_BLINK,
                "F7": KEY_BLINK, "F8": KEY_CMDSET, "F9": KEY_MODE,
                "F10": KEY_SEND}

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
    b"\x1b[18~": KEY_BLINK,                         # F7 — БЛИНК (показ УП)
    b"\x1b[19~": KEY_CMDSET,                        # F8 — НАБОР (набор 1↔2)
    b"\x1b[20~": KEY_MODE,                          # F9 — АВТОНОМНО↔С ЭВМ
    b"\x1b[21~": KEY_SEND,                          # F10 — SEND
    b"\x1b\x1b": "KEY_ESC",
}
_SEQ_LEN = sorted({len(seq) for seq in SEQ_KEYS}, reverse=True)


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
            matched = False
            for ln in _SEQ_LEN:
                seq = data[i:i + ln]
                if ln <= n - i and seq in SEQ_KEYS:
                    out.append(SEQ_KEYS[seq])
                    i += ln
                    matched = True
                    break
            if not matched:                # одиночный ESC
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