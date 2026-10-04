"""Запуск: python3 tests/test_charset.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import (Charset, decode_koi7, encode_koi7,
                             normalize_line_bytes)

ROM = Path(__file__).resolve().parent.parent / "rom" / "chargen-15ie.bin"


def make_charset() -> Charset:
    return Charset(ROM)


def test_encode_lower_and_upper():
    b = encode_koi7("аА")
    assert b == bytes([0x61, 0x01])      # «а» = 0x60+1, «А» = 0x01
    assert decode_koi7(b) == "аА"


def test_yo_normalized_to_e():
    assert encode_koi7("ё") == encode_koi7("е")
    assert encode_koi7("Ё") == encode_koi7("Е")


def test_roundtrip_word():
    for word in ("Привет", "Жёлтый", "ЭВМ", "готов к работе"):
        assert decode_koi7(encode_koi7(word)) == word.replace("ё", "е").replace("Ё", "Е")


def test_ascii_passthrough():
    b = encode_koi7("hello 123")
    assert b == b"hello 123"


def test_unknown_char_maps_to_block():
    assert encode_koi7("№") == b"\x7f"
    assert decode_koi7(b"\x7f") == "\u2588"


def test_normalize_line_bytes_utf8():
    # UTF-8 вход (как с линии SIMH БЭСМ-6) → 7-разрядные коды КОИ7
    src = "\u0411\u042d\u0421\u041c-6".encode("utf-8")
    out = normalize_line_bytes(src)
    assert all(b < 0x80 for b in out)
    assert decode_koi7(out) == "\u0411\u042d\u0421\u041c-6"


def test_normalize_line_bytes_raw7bit_passes_unchanged():
    raw = encode_koi7("\u043f\u0440\u0438\u0432\u0435\u0442")   # уже 7 бит
    assert normalize_line_bytes(raw) == raw


def test_label_block_char():
    c = make_charset()
    assert c.label(0x7F) == "\u2588"


def test_glyph_block_is_filled():
    c = make_charset()
    assert all(all(r) for r in c.bitmap(0x7F))   # сплошная заливка


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")