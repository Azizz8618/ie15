"""Запуск: python3 tests/test_keyboard.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.keyboard import KEY_MODE, KEY_SEND, decode_key_bytes, key_to_bytes


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
    assert decode_key_bytes(b"\x1b[23~") == [KEY_MODE]    # F11


def test_send_then_mode():
    assert decode_key_bytes(b"\x1b[23~\x1b[21~") == [KEY_MODE, KEY_SEND]


def test_host_mode_key_mapping():
    assert key_to_bytes("KEY_ENTER") == b"\r\n"
    assert key_to_bytes("KEY_ESC") == b"\x1b"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
