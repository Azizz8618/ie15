"""Запуск: python3 tests/test_charset.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import (Charset, RUS7, apply_set, decode_koi7, encode_koi7,
                             normalize_incremental, normalize_line_bytes,
                             to_line)

ROM = Path(__file__).resolve().parent.parent / "rom" / "chargen-15ie.bin"


def make_charset() -> Charset:
    return Charset(ROM)


def test_encode_lower_and_upper():
    b = encode_koi7("аА")
    # Внутренний поток: бит алфавита 0x80; строчная ещё и 0x60.
    assert b == bytes([0xE1, 0x81])      # «а» = 0xE0+1, «А» = 0x80+1
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


def test_control_passthrough():
    # ПР/ПС/ВК/ESC в тексте не должны становиться «█» (дефект --feed)
    assert encode_koi7("выд\r\n") == encode_koi7("выд") + b"\r\n"


def test_unknown_char_maps_to_block():
    assert encode_koi7("№") == b"\x7f"
    assert decode_koi7(b"\x7f") == "█"


def test_normalize_line_bytes_utf8():
    # UTF-8 вход (как с линии SIMH БЭСМ-6) → внутренний поток КОИ7 Н1:
    # заглавные помечаются битом алфавита (0x8D «М» ≠ 0x0D ПР!),
    # перевод строки не превращается в «█».
    src = "БЭСМ-6М\r\n".encode("utf-8")
    out = normalize_line_bytes(src)
    assert decode_koi7(out[:-2]) == "БЭСМ-6М"   # ПР ПС — не буквы!
    assert out[6] == 0x8D                  # «М» заглавная = Н1+0x80
    assert out[-2:] == b"\r\n"


def test_normalize_line_bytes_raw7bit_passes_unchanged():
    # линия RAW: машина отдаёт внутренние КОИ7-коды (< 0x80) — как есть
    raw = bytes(0x60 + RUS7.index(c) for c in "ПРИВЕТ")
    assert normalize_line_bytes(raw) == raw


def test_koi7_display_upper():
    # локальное эхо АВТОНОМНО: строчные Н1 (0xE0..) → заглавные (0x80..),
    # латиница и УП не тронуты
    from ie15emu.charset import koi7_display_upper
    assert koi7_display_upper(encode_koi7("ва")) == encode_koi7("ВА")
    assert koi7_display_upper(b"HELLO\r\n") == b"HELLO\r\n"


def test_koi7_raw_upper():
    # линия RAW: строка 0x60.. машины — заглавные (алфавит одно-регистрный)
    from ie15emu.charset import koi7_raw_upper
    raw = bytes(0x60 + RUS7.index(c) for c in "ВЫД")     # b"wyd"
    out = koi7_raw_upper(raw)
    assert out == encode_koi7("ВЫД")                     # форма 0x80+i
    assert decode_koi7(out) == "ВЫД"
    assert koi7_raw_upper(b"HYC\r\n") == b"HYC\r\n"      # латиница/УП не тронуты


def test_to_line_raw_matches_besm_internal():
    # линия RAW/KOI-7: буква уходит во внутреннюю строку БЭСМ-6 0x60.. —
    # ровно как в живом тесте: «ВЫД» → b"wyd"; конец строки — ETX
    assert to_line(encode_koi7("ВЫД"), "raw") == b"wyd"
    assert to_line(encode_koi7("выд"), "raw") == b"wyd"
    assert to_line(encode_koi7("МАМА\r\n"), "raw") == b"mama\x03"


def test_to_line_utf8_sends_cyrillic_as_utf8():
    # линия UTF-8: кириллица уходит многобайтово (unicode_to_koi7 в SIMH
    # положит её в ту же строку 0x60..); ПР ПС сворачивается в один ПР,
    # иначе vt_fix прочитал бы два конца строки; латиница — как есть
    assert to_line(encode_koi7("МАМА"), "utf8") == "МАМА".encode("utf-8")
    assert to_line(encode_koi7("ма"), "utf8") == "ма".encode("utf-8")
    assert to_line(b"HYC\r\n", "utf8") == b"HYC\r"
    assert to_line(b"work", "utf8") == b"work"
    assert to_line(encode_koi7("ВЫД\r\n"), "utf8") \
        == "ВЫД\r".encode("utf-8")


def test_normalize_incremental_splits_utf8():
    # разрез многобайтового символа на границе recv() не даёт «█»-мусор
    src = to_line(encode_koi7("ПРИВЕТ"), "utf8")   # UTF-8 с заглавными
    out = b""
    carry = b""
    for i in range(len(src)):                      # по одному байту
        part, carry = normalize_incremental(src[i:i + 1], carry)
        out += part
    assert carry == b""
    assert decode_koi7(out) == "ПРИВЕТ"


def test_label_block_char():
    c = make_charset()
    assert c.label(0x7F) == "█"


def test_glyph_block_is_filled():
    c = make_charset()
    assert all(all(r) for r in c.bitmap(0x7F))   # сплошная заливка


def test_apply_set_n1_uppercase_row():
    # Н1: 0x40…0x5E — русские заглавные (ЮАБЦ…Ч), 0x5F — «Ъ»
    assert apply_set(0x40, "n1") == 0x00            # Ю
    assert apply_set(0x42, "n1") == 0x02            # Б
    assert apply_set(0x5E, "n1") == 0x1E            # Ч
    assert apply_set(0x5F, "n1") == 0x1F            # Ъ
    # строчная строка 0x60… — уже Н1 (внутренние строчные), не трогаем
    assert apply_set(0x62, "n1") == 0x62            # б
    # Н0 — то, что есть
    assert apply_set(0x42, "n0") == 0x42
    assert decode_koi7(bytes([apply_set(0x42, "n1")])) == "Б"
    assert decode_koi7(b"B") == "B"


def test_n0_special_signs():
    # Н0 (ISO 646 IRV): 0x24 = ¤ (дензнак), 0x5E = ¬ — не $ и ^
    assert decode_koi7(b"$") == "¤"
    assert decode_koi7(b"^") == "¬"


def test_screen_display_set_toggle():
    from ie15emu.parser import Parser
    p = Parser()
    p.feed(b"KX-")
    assert p.screen.text().splitlines()[0][:3] == "KX-"
    p.screen.display_set = "n1"     # Н1: 0x4B=К, 0x58=Ь, 0x2D тире везде
    assert p.screen.text().splitlines()[0][:3] == "КЬ-"
    p.screen.display_set = "n0"
    assert p.screen.text().splitlines()[0][:3] == "KX-"


def test_display_char_matrix():
    from ie15emu.charset import display_char
    # Н0 — ASCII целиком, кириллических знаков нет
    assert display_char(0x41, "n0") == "A" and display_char(0x61, "n0") == "a"
    assert display_char(0x01, "n0") == ""
    # Н1 — русские два регистра: 0x40-я строка — верхний, 0x60-я — нижний
    assert display_char(0x42, "n1") == "Б" and display_char(0x62, "n1") == "б"
    assert display_char(0x02, "n1") == "Б"          # и внутренние коды ЭВМ
    # Н2 — международная строка + кириллица кодов ЭВМ (два регистра строк
    # 0x60.. — по-прежнему русские: так их отдаёт utf8/ДКС-приём)
    assert display_char(0x42, "n2") == "B" and display_char(0x62, "n2") == "б"
    assert display_char(0x02, "n2") == "Б"


def test_nabor_h1_echo_two_cases():
    from ie15emu.parser import Parser as P
    from ie15emu.session import TerminalSession, MODE_LOCAL
    class L:
        sent = []
        def send(self, d): pass
    ses = TerminalSession(P(), link=L(), mode=MODE_LOCAL)
    ses.nabor = "n1"
    for ch in "пП":
        ses.feed_key(ch)
    txt = ses.parser.screen.text().splitlines()[0]
    assert "п" in txt and "П" in txt        # Н1: оба регистра кириллицы живые

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
