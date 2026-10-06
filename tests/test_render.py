"""Рендер экрана: текстовый дамп, ANSI и PNG из глифов ПЗУ.

Запуск: python3 tests/test_render.py
"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu import COLS, ROWS
from ie15emu.charset import Charset, encode_koi7
from ie15emu.parser import Parser
from ie15emu.render import SCALE, to_ansi, to_png_bytes, to_text
from ie15emu.screen import ATTR_INV

ROM = Path(__file__).resolve().parent.parent / "rom" / "chargen-15ie.bin"


def make_parser(text: str = "", inverse: bool = False) -> Parser:
    p = Parser()
    p.charset = Charset(ROM)
    if inverse:
        p.feed(b"\x1bb")
    p.feed(encode_koi7(text))
    if inverse:
        p.feed(b"\x1bc")
    return p


class TestTextDump(unittest.TestCase):
    def test_bez_znakogeneratora(self):
        p = make_parser("ВЫД")
        out = to_text(p.screen)
        self.assertIn("служебная", out)
        self.assertIn("курсор", out)

    def test_s_znakogeneratorom(self):
        p = make_parser("ВЫД")
        out = to_text(p.screen, p.charset)
        self.assertIn("ВЫД", out.replace("\n", ""))   # label() — русские буквы
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("+-"))
        self.assertEqual(len(lines), ROWS + 4)   # шапка + строки + 3 подписи

    def test_metka_stroki_kursora(self):
        p = make_parser("ab")
        p.screen.y = 1
        out = to_text(p.screen, p.charset)
        self.assertIn("|*", out)                     # текущая строка помечена


class TestAnsi(unittest.TestCase):
    def test_zelenyy_lyuminofor(self):
        p = make_parser("В")
        out = to_ansi(p.screen, p.charset, cursor=False)
        self.assertTrue(out.startswith("\x1b[32;40m"))
        self.assertTrue(out.endswith("\x1b[0m"))
        self.assertIn("█", out)

    def test_inversiya_menyaet_tochki(self):
        p = make_parser("В")
        normal = to_ansi(p.screen, p.charset, cursor=False)
        p2 = make_parser("В", inverse=True)
        self.assertEqual(p2.screen.attr[0][0], ATTR_INV)
        inv = to_ansi(p2.screen, p2.charset, cursor=False)
        self.assertNotEqual(normal, inv)

    def test_kursor_podchyorkivanie(self):
        p = make_parser(" ")
        with_cur = to_ansi(p.screen, p.charset, cursor=True)
        without = to_ansi(p.screen, p.charset, cursor=False)
        self.assertNotEqual(with_cur, without)

    def test_razmer_setki(self):
        p = make_parser()
        out = to_ansi(p.screen, p.charset, green=False, cursor=False)
        # позиционирование на строки 2..8 глифа (1-я — с текущей позиции)
        self.assertIn("\x1b[2;1H", out)
        self.assertIn("\x1b[8;1H", out)
        self.assertIn(f"\x1b[2;{(COLS - 1) * 7 + 1}H", out)


class TestPng(unittest.TestCase):
    def _png(self, p: Parser) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            to_png_bytes(p.screen, p.charset, path)
            return Path(path).read_bytes()
        finally:
            Path(path).unlink(missing_ok=True)

    def test_podpis_i_razmer(self):
        p = make_parser("ВЫД")
        data = self._png(p)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h, depth, color = struct.unpack(">IIBB", data[16:26])
        self.assertEqual(w, COLS * 7 * SCALE)
        self.assertEqual(h, ROWS * 8 * SCALE)
        self.assertEqual(depth, 8)       # 8 бит на канал
        self.assertEqual(color, 2)       # RGB

    def test_unk_chunks_prohodyat(self):
        p = make_parser("ЭВМ")
        data = self._png(p)
        idat = data[data.index(b"IDAT") - 4:]
        ln = struct.unpack(">I", idat[:4])[0]
        raw = zlib.decompress(idat[8:8 + ln])
        self.assertEqual(len(raw), ROWS * 8 * SCALE *
                         (1 + COLS * 7 * SCALE * 3))
        self.assertIn(b"IEND", data)

    def test_pustoy_i_zapolnennyy_raznye(self):
        empty = self._png(make_parser())
        full = self._png(make_parser("МИН"))
        self.assertNotEqual(empty, full)


if __name__ == "__main__":
    unittest.main(verbosity=2)