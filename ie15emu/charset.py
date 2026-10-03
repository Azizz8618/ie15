"""Знакогенератор 15ИЭ-00-013 из ПЗУ КР556РТ5 (rom/chargen-15ie.bin).

Организация (по тех. описанию): 256 глифов × 8 строк × 7 точек.
Адрес глифа (код ВЗУ) — старшие разряды, строка — младшие.
Каждый байт строки: младшие 7 битов = точки слева направо (старший — левая).
"""
from __future__ import annotations

from pathlib import Path

GLYPHS = 256
GLYPH_ROWS = 8
GLYPH_COLS = 7

# КОИ7-алфавит: коды 0x00..0x1E = ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ
RUS7 = "ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ"


class Charset:
    def __init__(self, rom_path: str | Path):
        data = Path(rom_path).read_bytes()
        if len(data) != GLYPHS * GLYPH_ROWS:
            raise ValueError(
                f"{rom_path}: ожидалось {GLYPHS * GLYPH_ROWS} байт, "
                f"получено {len(data)}"
            )
        self.rom = data

    def rows(self, code: int) -> list[int]:
        """8 байт-строк глифа (7 точек в старших разрядах 7…1)."""
        base = (code & 0xFF) * GLYPH_ROWS
        return [self.rom[base + r] >> 1 for r in range(GLYPH_ROWS)]

    def bitmap(self, code: int) -> list[list[int]]:
        """Глиф как матрица 8×7 из 0/1."""
        out = []
        for r in self.rows(code):
            out.append([(r >> (6 - c)) & 1 for c in range(GLYPH_COLS)])
        return out

    def label(self, code: int) -> str:
        """ASCII/КИИ7-подпись кода для отладочного вывода (КОИ7 Н1)."""
        if code < 0x1F:
            return RUS7[code]
        if 0x60 <= code < 0x7F:
            return RUS7[code - 0x60].lower()
        if 0x20 <= code < 0x60:
            return chr(code)
        return f"<{code:02X}>"
