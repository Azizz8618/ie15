"""Запуск: python3 tests/test_keyboard.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import encode_koi7
from ie15emu.keyboard import (DEFAULT_LAYOUT, KEY_BLINK, KEY_CMDSET,
                              KEY_MODE, KEY_SEND, KEY_WHEELDOWN, KEY_WHEELUP,
                              decode_key_bytes, is_key_combo, key_to_bytes,
                              normalize_combo)


def test_mouse_sgr_wheel():
    # мышь отдана приложению (SGR 1006): колесо — листание «истории»,
    # клики и движение молча съедаются, в линию мусор не идёт
    assert decode_key_bytes(b"\x1b[<64;10;5M") == [KEY_WHEELUP]
    assert decode_key_bytes(b"\x1b[<65;10;5M") == [KEY_WHEELDOWN]
    assert decode_key_bytes(b"\x1b[<64;10;5m") == []        # отпускание
    assert decode_key_bytes(b"\x1b[<68;1;1M") == [KEY_WHEELUP]   # +shift
    assert decode_key_bytes(b"\x1b[<81;1;1M") == [KEY_WHEELDOWN]  # +ctrl
    assert decode_key_bytes(b"\x1b[<0;3;4M\x1b[<0;3;4m") == []    # клик
    assert decode_key_bytes(b"\x1b[<32;1;1M") == []          # движение
    assert decode_key_bytes(b"a\x1b[<64;1;1Mb") == ["a", KEY_WHEELUP, "b"]
    # стрелки/функциональные после мышиных событий не пострадали
    assert decode_key_bytes(b"\x1b[<64;1;1M\x1b[A") == [KEY_WHEELUP, "KEY_UP"]


def test_csi_u_modifiers():
    # kitty/foot/wezterm: «ESC [ код;мод u»; xterm modifyOtherKeys:
    # «ESC [ 27;мод;код ~» — нажатие с Ctrl/Alt именуется и не дублирует
    # обычный знак; без модификаторов — тот же знак, что и раньше
    from ie15emu.keyboard import is_key_combo, normalize_combo
    assert decode_key_bytes(b"\x1b[1073;5u") == ["ctrl+б"]
    assert decode_key_bytes(b"\x1b[1073;3u") == ["alt+б"]
    assert decode_key_bytes(b"\x1b[27;5;91~") == ["ctrl+["]
    assert decode_key_bytes(b"\x1b[1073u") == ["б"]
    assert decode_key_bytes(b"a\x1b[1041;5uc") == ["a", "ctrl+Б", "c"]
    # старые пути не тронуты; служебные keysym kitty (>=0xE000) — тоже
    assert decode_key_bytes(b"\x1b[A\x1b[15~\x1b[3~") == ["KEY_UP", "CLEAR",
                                                           "KEY_DELETE"]
    assert not is_key_combo("KEY_UP") and not is_key_combo("1+1")
    assert normalize_combo("Б+Ctrl") == normalize_combo("Ctrl+Б") == "ctrl+Б"


def test_alt_prefix():
    # Alt+знак как ESC-префикс (VTE и другие без CSI-u): именуется
    # «alt+…», привязывается в [keys]; Esc перед «[»/«O» и одиночный
    # Esc остаются как раньше
    assert decode_key_bytes(b"\x1b\xd0\xb1") == ["alt+б"]
    assert decode_key_bytes(b"\x1bA") == ["alt+A"]
    assert decode_key_bytes(b"\x1b[") == ["KEY_ESC", "["]
    assert decode_key_bytes(b"\x1bO") == ["KEY_ESC", "O"]
    assert decode_key_bytes(b"\x1b\x1b") == ["KEY_ESC"]
    assert decode_key_bytes(b"\x1b") == ["KEY_ESC"]
    assert decode_key_bytes(b"\x1bd") == ["alt+d"]
    # экранные команды клавиатурой отдаёт ключами (KEY_HOME и т.п.),
    # поэтому слияние ESC+знак в alt+… их не ломает
    assert decode_key_bytes(b"\x1b[A\x1b[H\x1b[3~") == ["KEY_UP", "KEY_HOME",
                                                         "KEY_DELETE"]


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


def test_h2_symbols_same_keys_both_layouts():
    # Н2: RU-раскладка отдаёт знак клавишей того же физического положения,
    # что EN; карта по самому знаку — CapsLock/Shift-состояние не важны
    for rus, lat in (('"', "@"), (":", "^"), ("ё", "|"),
                     ("Ё", "|"), ("ъ", "]"), ("Ъ", "]")):
        assert key_to_bytes(rus, None, shift_to_rus=True) == lat.encode("ascii")
    # «№» (RU клавиша 3) — внутренний номерной знак 0x9F, на линию — 0x23
    assert key_to_bytes("№", None, shift_to_rus=True) == bytes([0x80 | 0x1F])
    # русская буква+Shift — латинская положения (те же [ ] ' ; на RU)
    assert key_to_bytes("Х", None, shift_to_rus=True) == b"["
    assert key_to_bytes("Э", None, shift_to_rus=True) == b"'"
    assert key_to_bytes("Ж", None, shift_to_rus=True) == b";"
    assert key_to_bytes("Щ", None, shift_to_rus=True) == b"O"   # o→Щ
    # ASCII-знаки проходят как есть — с обеих раскладок одним и тем же
    for s in "@#^[]'|?%;!<>":
        assert key_to_bytes(s, None, shift_to_rus=True) == s.encode("ascii")
    # вне Н2 карта знаков не работает (Н0/Н1 — историческое поведение)
    assert key_to_bytes('"', None) == b'"'
    assert key_to_bytes('"', "positional") == b'"'


def test_host_mode_key_mapping():
    assert key_to_bytes("KEY_ENTER") == b"\r\n"
    assert key_to_bytes("KEY_ESC") == b"\x1b"


def test_shift_to_rus_h2():
    # Н2, латинская раскладка без Shift — английские заглавные
    # (одно-регистрный набор: строка 0x60.. линии — кириллица ЭВМ)
    assert key_to_bytes("w", None, shift_to_rus=True) == b"W"
    # Н2, Shift+латинская клавиша — русская буква положения
    assert key_to_bytes("W", None, shift_to_rus=True) == bytes([0x03 | 0x80])
    assert key_to_bytes("Q", None, shift_to_rus=True) == bytes([0x0A | 0x80])
    # Н2, русская раскладка: без Shift — русские заглавные (внутренний
    # верх 0x80+код), с Shift — симметрично латинская буква положения
    assert key_to_bytes("ц", None, shift_to_rus=True) == bytes([0x03 | 0x80])
    assert key_to_bytes("Ц", None, shift_to_rus=True) == b"W"
    assert key_to_bytes("Й", None, shift_to_rus=True) == b"Q"
    assert key_to_bytes("Е", None, shift_to_rus=True) == b"T"
    # «Ё» в Н2 — не буква: её клавиша (общая с EN «\|») даёт вертикальную черту
    assert key_to_bytes("Ё", None, shift_to_rus=True) == b"|"
    # без флага (Н0/Н1 вне раскладки) — как раньше
    assert key_to_bytes("W", None) == b"W"
    assert key_to_bytes("w", None) == b"w"

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
