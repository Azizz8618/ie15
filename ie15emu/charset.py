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
        if code == 0x7F:
            return "█"   # сплошная заливка (полный непрозрачный блок)
        return f"<{code:02X}>"


# --- КОИ7 Н1: кодирование/декодирование (ГОСТ 27463-87) -----------------
# Буквы: заглавные 0x00..0x1E, строчные 0x60..0x7E (порядок как в RUS7).
# Ё/ё в КОИ7 Н1 нет — нормализуется в Е/е.

_LOWER_OFFSET = 0x60


def encode_koi7(text: str) -> bytes:
    """Unicode-строка → 7-разрядные коды ВЗУ терминала (КОИ7 Н1).

    Не-русские печатные ASCII-символы проходят как есть (старший бит
    сбрасывается); неизвестные символы заменяются заполнителем `0x7F`.
    """
    out = bytearray()
    for ch in text:
        up = ch.upper().replace("Ё", "Е")
        if up in RUS7:
            code = RUS7.index(up)
            out.append(code | _LOWER_OFFSET if ch.islower() else code)
            continue
        b = ord(ch) & 0x7F
        out.append(b if 0x20 <= b < 0x7F else 0x7F)
    return bytes(out)


def decode_koi7(data: bytes) -> str:
    """7-разрядные коды КОИ7 Н1 → Unicode (строчные/заглавные русские)."""
    chars = []
    labels = {i: RUS7[i] for i in range(len(RUS7))}
    labels.update({i | _LOWER_OFFSET: RUS7[i].lower()
                   for i in range(len(RUS7))})
    for b in data:
        b &= 0x7F
        if b in labels:
            chars.append(labels[b])
        elif 0x20 <= b < 0x7F:
            chars.append(chr(b))
        elif b == 0x7F:
            chars.append("█")
        else:
            chars.append("")
    return "".join(chars)


def normalize_line_bytes(data: bytes) -> bytes:
    """Нормализовать вход из линии (ЭВМ → терминал) к 7-разрядным кодам КОИ7.

    Линии SIMH БЭСМ-6 (`attach ttyN <порт>`) работают в UTF-8 (см. пометку
    `Encoding is UTF-8` в `BESM6/OSZAGR/dispak.ini`): русский текст приходит
    многобайтовыми последовательностями UTF-8, а не сырыми кодами КОИ7.
    Терминал же хранит и показывает позиции как 7-разрядные коды КОИ7 Н1,
    поэтому перед разбором декодируем UTF-8 и перекодируем в КОИ7.

    Уже готовые 7-разрядные коды КОИ7 (байты < 0x80) проходят без изменений.
    """
    if not data:
        return data
    if not any(b >= 0x80 for b in data):
        return data                      # уже 7-разрядный КОИ7
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return bytes(b & 0x7F for b in data)   # сырой 8-разрядный поток
    return encode_koi7(text)
