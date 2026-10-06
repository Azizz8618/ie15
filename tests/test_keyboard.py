"""Запуск: python3 tests/test_keyboard.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import encode_koi7
from ie15emu.keyboard import (KEY_CMDSET, KEY_MODE, KEY_SEND,
                              decode_key_bytes, key_to_bytes)


def test_plain_chars():
    assert decode_key_bytes(b"abc") == ["a", "b", "c"]


def test_enter_and_backspace():
    assert decode_key_bytes(b"\r") == ["KEY_ENTER"]
    assert decode_key_bytes(b"\x7f") == ["KEY_BACKSPACE"]


def test_arrow_sequences():
    assert decode_key_bytes(b"\x1b[A\x1b[B\x1b[C\x1b[D") == [
        "KEY_UP", "KEY_DOWN", "KEY_RIGHT", "KEY_LEFT"]


def test_send_mode_keys():
    assert decode_key_bytes(b"\x1b[21~") == [KEY_SEND]    # F10
    assert decode_key_bytes(b"\x1b[20~") == [KEY_MODE]    # F9
    assert decode_key_bytes(b"\x1b[19~") == [KEY_CMDSET]  # F8 — РЕЖИМ наборов


def test_send_then_mode():
    assert decode_key_bytes(b"\x1b[20~\x1b[21~") == [KEY_MODE, KEY_SEND]


def test_f1_f11_ne_razbirayutsya():
    # F1 (\x1bOP) и F11 (\x1b[23~) не служат клавишами терминала: не должны
    # давать служебных ключей (экранные коды из них не делает и parсер-режим)
    assert KEY_MODE not in decode_key_bytes(b"\x1b[23~")
    assert KEY_SEND not in decode_key_bytes(b"\x1b[23~")
    assert KEY_CMDSET not in decode_key_bytes(b"\x1bOP")


def test_koi7_shift_prints_english():
    # Русская раскладка (--koi7): без Shift — русская буква,
    # с Shift — английская (таблица Key-ов нижнего регистра)
    assert key_to_bytes("w", koi7=True) == bytes([0xF6])     # «в» = 0xE0|0x16
    assert key_to_bytes("W", koi7=True) == b"W"              # Shift → английский
    assert key_to_bytes("Q", koi7=True) == b"Q"
    assert key_to_bytes("<", koi7=True) == b"<"              # Shift+«,” — англ.
    assert key_to_bytes("1", koi7=True) == b"1"
    assert key_to_bytes("ж", koi7=True) == encode_koi7("ж")  # прямая кириллица — как была
    # RU-раскладка ОС: Shift+ц присылает «Ц» — «регистр» переключает
    # алфавит: буква уходит английской, какой назначена в QWERTY2KOI7
    assert key_to_bytes("Ц", koi7=True) == b"C"              # «c»→Ц
    assert key_to_bytes("В", koi7=True) == b"X"              # «x»→В
    assert key_to_bytes("Ж", koi7=True) == b"V"              # w/v→Ж: берётся v
    assert key_to_bytes("Й", koi7=True) == b"J"              # «j»→Й
    assert key_to_bytes("ц", koi7=True) == encode_koi7("ц")  # без Shift — русская
    assert key_to_bytes("Ц", koi7=False) == encode_koi7("Ц")  # вне --koi7 — без переключения


def test_control_keys_send_esc():
    assert key_to_bytes("KEY_END") == b"\x1bK"
    assert key_to_bytes("KEY_PAGEUP") == b"\x1bJ"
    assert key_to_bytes("KEY_PAGEDOWN") == b"\x1bE"
    assert key_to_bytes("KEY_INSERT") == b"\x1bb"
    assert key_to_bytes("KEY_DELETE") == b"\x1bc"
    assert key_to_bytes("\t") == b"\x09"          # ТАБ
    assert key_to_bytes("\x07") == b"\x07"        # ЗВН (Ctrl-G)


def test_host_mode_key_mapping():
    assert key_to_bytes("KEY_ENTER") == b"\r\n"
    assert key_to_bytes("KEY_ESC") == b"\x1b"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
