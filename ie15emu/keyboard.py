"""Клавиатура 15ВВВ-97-006 (ёмкостная матрица) и её ПЗУ 15bbb.rt5.

ПЗУ КР556РТ5 (512 байт) содержит микропрограмму обхода матрицы и
коды клавиш. Эмулятор использует файл как источник карты символов;
базовая раскладка — QWERTY/ЙЦУКЕН с КОИ7-выдачей (как у терминала).

Соответствие ПК-клавиш → коды терминала:
  обычные символы  — как есть (7 бит)
  Backspace        — 08 (курсор влево, «authbs»)
  Enter            — 0D 0A (ПР ПС)
  Стрелки          — ESC A / B / C / D  (набор команд №2)
  Home/End/PgUp    — ESC H / ESC K / ESC Y
"""
from __future__ import annotations

from pathlib import Path

from .charset import RUS7

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


def key_to_bytes(key: str, koi7: bool = False) -> bytes:
    """Нажатие ПК-клавиши → байты для линии токовой петли/SSH."""
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
        return b"\x1bH"
    if key == "KEY_ESC" or key == "\x1b":
        return b"\x1b"
    ch = key if len(key) == 1 else ""
    if not ch:
        return b""
    if koi7 and ch.lower() in QWERTY2KOI7:
        code = QWERTY2KOI7[ch.lower()]
        return bytes([code | 0x60 if ch.islower() else code])
    return ch.encode("ascii", errors="replace")


# Служебные клавиши сеанса (команды эмулятора, а не коды линии).
KEY_SEND = "SEND"     # «передать накопленное» (аналог клавиши SEND)
KEY_MODE = "MODE"     # переключение АВТОНОМНО ↔ С ЭВМ

SPECIAL_KEYS = {"KEY_SEND": KEY_SEND, "KEY_MODE": KEY_MODE,
                "F10": KEY_SEND, "F11": KEY_MODE}

# Последовательности клавиатуры ПК (терминал в режиме cbreak) → ключи сеанса.
SEQ_KEYS = {
    b"\x1b[A": "KEY_UP", b"\x1bOA": "KEY_UP",
    b"\x1b[B": "KEY_DOWN", b"\x1bOB": "KEY_DOWN",
    b"\x1b[C": "KEY_RIGHT", b"\x1bOC": "KEY_RIGHT",
    b"\x1b[D": "KEY_LEFT", b"\x1bOD": "KEY_LEFT",
    b"\x1b[H": "KEY_HOME", b"\x1b[1~": "KEY_HOME",
    b"\x1b[4~": "KEY_END",
    b"\x1b[21~": KEY_SEND, b"\x1b[20~": KEY_SEND,   # F10 / F9 — SEND
    b"\x1b[23~": KEY_MODE, b"\x1b[24~": KEY_MODE,   # F11 / F12 — режим
    b"\x1b\x1b": "KEY_ESC",
}
_SEQ_LEN = sorted({len(seq) for seq in SEQ_KEYS}, reverse=True)


def decode_key_bytes(data: bytes) -> list[str]:
    """Распарсить байты нажатий в имена клавиш/символы для feed_key()."""
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
        out.append(chr(b))
        i += 1
    return out