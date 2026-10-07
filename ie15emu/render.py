"""Рендер экрана 15ИЭ-00-013: ANSI (зелёный люминофор) и PNG.

Изображение строится из настоящих глифов ПЗУ знакогенератора:
каждая точка глифа → пиксель, с монохромной зелёной схемой (P3)
и инверсией атрибутов.
"""
from __future__ import annotations

import struct
import zlib

from . import COLS, ROWS
from .charset import Charset
from .screen import ATTR_BLINK, ATTR_CTRL, ATTR_INV, Screen

GREEN = (0x33, 0xFF, 0x66)
DARK = (0x08, 0x14, 0x0C)
SCALE = 4  # масштаб точки для PNG (7×8 → 28×32 на символ)


def to_ansi(screen: Screen, charset: Charset,
            green: bool = True, cursor: bool = True) -> str:
    out = []
    if green:
        out.append("\x1b[32;40m\x1b[2J\x1b[H")
    for y in range(ROWS):
        for x in range(COLS):
            code = screen.glyph(y, x)
            a = screen.attr[y][x]
            if a & ATTR_CTRL and not screen.show_ctrl:
                a = 0
            inv = a & (ATTR_INV | ATTR_BLINK)
            is_cur = cursor and (x, y) == (screen.x, screen.y)
            bits = charset.rows(code, screen.display_set)
            for r, row in enumerate(bits):
                if r:
                    out.append(f"\x1b[{y * 8 + r + 1};{x * 7 + 1}H")
                line = "".join(
                    "█" if ((row >> (6 - c)) & 1) ^ inv ^ (is_cur and r >= 6)
                    else " "
                    for c in range(7)
                )
                out.append(line)
    if green:
        out.append("\x1b[0m")
    return "".join(out)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def to_png_bytes(screen: Screen, charset: Charset, path: str) -> None:
    w, h = COLS * 7 * SCALE, ROWS * 8 * SCALE
    rows = []
    for y in range(ROWS):
        glyph_rows = [b"" for _ in range(8)]
        for x in range(COLS):
            code = screen.glyph(y, x)
            a = screen.attr[y][x]
            if a & ATTR_CTRL and not screen.show_ctrl:
                a = 0
            inv = bool(a & (ATTR_INV | ATTR_BLINK))
            for r, row in enumerate(charset.rows(code, screen.display_set)):
                for c in range(7):
                    on = ((row >> (6 - c)) & 1) ^ inv
                    glyph_rows[r] += bytes(GREEN if on else DARK) * SCALE
        for r in range(8):
            line = b"\x00" + glyph_rows[r]   # пиксели уже масштабированы по X
            for _ in range(SCALE):
                rows.append(line)
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" +
           _png_chunk(b"IHDR", ihdr) +
           _png_chunk(b"IDAT", zlib.compress(raw, 9)) +
           _png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def to_text(screen: Screen, charset: Charset | None = None) -> str:
    """Текстовый дамп экрана (для README/логов)."""
    if charset is None:
        return screen.dump()
    sc = screen
    lines = []
    for y in range(ROWS):
        lines.append("".join(charset.label(c) for c in sc.cells[y]))
    out = ["+" + "-" * COLS + "+"]
    for i, line in enumerate(lines):
        marker = "*" if i == sc.y else " "
        out.append(f"|{line}|{marker}")
    out.append("+" + "-" * COLS + "+")
    out.append("|" + "".join(sc.service) + f"|  (служебная, стр. 25)")
    for extra in sc.service_more:
        out.append("|" + extra + "|")
    out.append(f"курсор: x={sc.x} y={sc.y} inverse={sc.inverse} bell={sc.bell}")
    return "\n".join(out)