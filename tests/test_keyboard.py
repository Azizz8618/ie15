"""Запуск: python3 tests/test_keyboard.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import encode_koi7
from ie15emu.keyboard import (DEFAULT_LAYOUT, KEY_BLINK, KEY_CMDSET,
                              KEY_MODE, KEY_SEND,
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
    assert decode_key_bytes(b"\x1b[18~") == [KEY_BLINK]   # F7 — БЛИНК
    assert decode_key_bytes(b"\x1b[19~") == [KEY_CMDSET]  # F8 — НАБОР
    assert decode_key_bytes(b"\x1b[20~") == [KEY_MODE]    # F9 — СЕТЬ
    assert decode_key_bytes(b"\x1b[21~") == [KEY_SEND]    # F10 — ПЕРЕДАЧА


def test_send_then_mode():
    assert decode_key_bytes(b"\x1b[20~\x1b[21~") == [KEY_MODE, KEY_SEND]


def test_f1_f11_ne_razbirayutsya():
    # F1 (\x1bOP) и F11 (\x1b[23~) не служат клавишами терминала: не должны
    # давать ни служебных ключей, ни экранных кодов
    assert KEY_MODE not in decode_key_bytes(b"\x1b[23~")
    assert KEY_SEND not in decode_key_bytes(b"\x1b[23~")
    assert KEY_CMDSET not in decode_key_bytes(b"\x1bOP")


def test_positional_layout_default():
    # Позиционная (по умолчанию): клавиша даёт русскую букву своего места
    assert DEFAULT_LAYOUT == "positional"
    assert key_to_bytes("q", layout="positional") == bytes([0x0A | 0xE0])  # Й
    assert key_to_bytes("w", layout="positional") == bytes([0x03 | 0xE0])  # Ц
    assert key_to_bytes("Q", layout="positional") == b"Q"          # Shift — англ.
    assert key_to_bytes("<", layout="positional") == b"<"
    assert key_to_bytes("1", layout="positional") == b"1"


def test_phonetic_layout():
    # Фонетическая: w→В, q→Я, x→Ч (историческая таблица проекта)
    assert key_to_bytes("w", layout="phonetic") == bytes([0x16 | 0xE0])  # Ж
    assert key_to_bytes("q", layout="phonetic") == bytes([0x11 | 0xE0])  # Я
    assert key_to_bytes("W", layout="phonetic") == b"W"


def test_shift_switches_alphabet_ru_os():
    # RU-раскладка ОС: Shift+ц присылает «Ц» — «регистр» переключает
    # алфавит: буква уходит английской по обратной таблице выбранной раскладки
    assert key_to_bytes("Ц", layout="positional") == b"W"          # pos: w→Ц
    assert key_to_bytes("Ц", layout="phonetic") == b"C"            # phon: c→Ц
    assert key_to_bytes("ц", layout="positional") == encode_koi7("ц")   # без Shift — русская
    assert key_to_bytes("Ж", layout="phonetic") == b"W"            # w/v→Ж: первая w


def test_layout_off_passes_through():
    # Выключенная раскладка: латиница как есть, заглавная кириллица не тронется
    assert key_to_bytes("q", layout=None) == b"q"
    assert key_to_bytes("Ц", layout=None) == encode_koi7("Ц")


def test_control_keys_send_esc():
    assert key_to_bytes("KEY_END") == b"\x1bK"
    # PgUp/PgDn — локальное листание экрана, в линию ничего
    assert key_to_bytes("KEY_PAGEUP") == b""
    assert key_to_bytes("KEY_PAGEDOWN") == b""
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
