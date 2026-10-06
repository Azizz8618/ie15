"""Разбор потока ЭВМ -> терминал: УП-коды и «набор команд №2» (VT52).

Запуск: python3 tests/test_parser.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu import COLS, ROWS
from ie15emu.charset import decode_koi7, encode_koi7
from ie15emu.parser import BS, CR, ESC, FF, HT, LF, BEL, VT, Parser


def feed(parser: Parser, *chunks: bytes) -> str | None:
    out = ""
    for ch in chunks:
        out += parser.feed(ch) or ""
    return out or None


class TestUpKody(unittest.TestCase):
    def test_pr_ps_perevod_stroki(self):
        p = Parser()
        feed(p, b"ab", bytes([CR]), b"c", bytes([LF]), b"d")
        sc = p.screen
        self.assertEqual(sc.cells[0][0], ord("c"))   # ПР: «c» поверх «a»
        self.assertEqual(sc.cells[0][1], ord("b"))
        self.assertEqual(sc.cells[1][0], 0x20)       # ПС: без возврата каретки
        self.assertEqual(sc.cells[1][1], ord("d"))

    def test_vk_kursor_vlevo(self):
        p = Parser()
        feed(p, b"ab", bytes([BS]), b"X")
        self.assertEqual(p.screen.cells[0][1], ord("X"))

    def test_tab_vosem_kolonok(self):
        p = Parser()
        feed(p, b"a", bytes([HT]), b"b")
        self.assertEqual(p.screen.cells[0][8], ord("b"))

    def test_zvn_flag_zvonka(self):
        p = Parser()
        p.feed(bytes([BEL]))
        self.assertTrue(p.screen.bell)

    def test_pu_stb_stirayut_do_konca(self):
        p = Parser()
        feed(p, b"aaaa", bytes([CR, LF]), b"bbbb")
        p.screen.x = p.screen.y = 0
        p.feed(bytes([VT]))
        self.assertEqual(p.screen.cells[0][0], 0x20)
        self.assertEqual(p.screen.cells[1][0], 0x20)
        p.feed(bytes([FF]))
        self.assertTrue(all(c == 0x20 for row in p.screen.cells for c in row))


class TestKomandyNabora2(unittest.TestCase):
    def test_strelki(self):
        p = Parser()
        p.screen.x, p.screen.y = 5, 5
        feed(p, bytes([ESC]), b"A")
        self.assertEqual((p.screen.x, p.screen.y), (5, 4))
        feed(p, bytes([ESC]), b"B")
        self.assertEqual((p.screen.x, p.screen.y), (5, 5))
        feed(p, bytes([ESC]), b"C")
        self.assertEqual((p.screen.x, p.screen.y), (6, 5))
        feed(p, bytes([ESC]), b"D")
        self.assertEqual((p.screen.x, p.screen.y), (5, 5))

    def test_strelki_ne_vyhodyat_za_ekran(self):
        p = Parser()
        feed(p, bytes([ESC]), b"A", bytes([ESC]), b"D")
        self.assertEqual((p.screen.x, p.screen.y), (0, 0))
        for _ in range(COLS + 5):
            p.feed(bytes([ESC, ord("C")]))
        self.assertEqual(p.screen.x, COLS - 1)
        for _ in range(ROWS + 5):
            p.feed(bytes([ESC, ord("B")]))
        self.assertEqual(p.screen.y, ROWS - 1)

    def test_H_dom(self):
        p = Parser()
        p.screen.x, p.screen.y = 10, 10
        feed(p, bytes([ESC]), b"H")
        self.assertEqual((p.screen.x, p.screen.y), (0, 0))

    def test_I_obratnyy_perevod(self):
        p = Parser()
        p.screen.y = 3
        feed(p, bytes([ESC]), b"I")
        self.assertEqual(p.screen.y, 2)
        feed(p, bytes([ESC]), b"I", bytes([ESC]), b"I", bytes([ESC]), b"I")
        self.assertEqual(p.screen.y, 0)
        feed(p, bytes([ESC]), b"I")   # RI в нуле — скролл вниз
        self.assertEqual(p.screen.y, 0)

    def test_J_stiranie_do_konca_ekrana(self):
        p = Parser()
        feed(p, b"a" * 10, bytes([CR, LF]), b"b" * 10, bytes([CR, LF, CR]))
        feed(p, bytes([ESC]), b"J")
        self.assertEqual(p.screen.cells[2][0], 0x20)
        self.assertEqual(p.screen.cells[0][0], ord("a"))

    def test_K_stiranie_do_konca_stroki(self):
        p = Parser()
        feed(p, b"abcdef", bytes([CR]))
        p.screen.x = 2
        feed(p, bytes([ESC]), b"K")
        self.assertEqual(p.screen.cells[0][1], ord("b"))
        self.assertEqual(p.screen.cells[0][2], 0x20)
        self.assertEqual(p.screen.cells[0][COLS - 1], 0x20)

    def test_E_stiranie_ekrana_i_dom(self):
        p = Parser()
        feed(p, b"abc")
        feed(p, bytes([ESC]), b"E")
        self.assertTrue(all(c == 0x20 for row in p.screen.cells for c in row))
        self.assertEqual((p.screen.x, p.screen.y), (0, 0))

    def test_Y_pozitsiya_kursora(self):
        p = Parser()
        feed(p, bytes([ESC]), b"Y", bytes([0x20 + 3, 0x20 + 7]))  # (3;7)
        self.assertEqual((p.screen.y, p.screen.x), (3, 7))

    def test_Y_pozitsiya_ogranichena_ekranom(self):
        p = Parser()
        feed(p, bytes([ESC]), b"Y", bytes([0x7F, 0x7F]))
        self.assertEqual(p.screen.y, ROWS - 1)
        self.assertEqual(p.screen.x, COLS - 1)

    def test_b_c_inversiya(self):
        p = Parser()
        feed(p, bytes([ESC]), b"b", b"X", bytes([ESC]), b"c", b"Y")
        self.assertEqual(p.screen.attr[0][0], 0x80)   # X — инверсный
        self.assertEqual(p.screen.attr[0][1], 0)      # Y — нормальный

    def test_Z_identifikatsiya(self):
        p = Parser()
        self.assertEqual(feed(p, bytes([ESC]), b"Z"), "\x1b/")

    def test_esc_esc_sbroso(self):
        p = Parser()
        out = feed(p, bytes([ESC, ESC]), b"Z")   # ESC ESC сбрасывает, Z — печать
        self.assertIsNone(out)
        self.assertEqual(p.screen.cells[0][0], ord("Z"))
        self.assertEqual(p.state, "raw")

    def test_sostoyaniya_posle_esc(self):
        p = Parser()
        p.feed(bytes([ESC]))
        self.assertEqual(p.state, "esc")
        p.feed(b"Y")
        self.assertEqual(p.state, "esc_y")
        p.feed(b"\x21")
        self.assertEqual(p.state, "esc_y2")


class TestRusLetters(unittest.TestCase):
    """КОИ7 Н1: буквы печатаются только в форме бита алфавита (0x80+i)."""

    def test_bukvy_godyat_na_ekran(self):
        p = Parser()
        p.feed(encode_koi7("Год"))     # «Г»=0x87, а не Голый 0x07=ЗВН
        self.assertEqual(p.screen.cells[0][0], 0x07)
        self.assertFalse(p.screen.bell)

    def test_up_sohranyayutsya_vsegda(self):
        # 08/09/0A/0D — ВК/ТАБ/ПС/ПР всегда УП (буквы приходят с битом)
        p = Parser()
        p.feed(bytes([CR, LF, BS, HT]))
        self.assertEqual((p.screen.x, p.screen.y), (8, 1))

    def test_up_bez_funkcii_ne_pechataetsya(self):
        p = Parser()
        p.feed(bytes([0x07]))          # ЗВН, не «Г»
        self.assertTrue(p.screen.bell)
        self.assertEqual(p.screen.cells[0][0], 0x20)
        p.feed(b"\x00\x03")            # НУС, ЕОТ — не «Ю» и не «Ц»
        self.assertEqual(p.screen.cells[0][:2], [0x20, 0x20])
        self.assertEqual(p.screen.x, 0)

    def test_sedmoj_razryad_ignoriruetsya(self):
        p = Parser()
        p.feed(bytes([0xE1]))          # 0x61 | 0x80
        self.assertEqual(p.screen.cells[0][0], 0x61)


class TestRezhimyNaborov(unittest.TestCase):
    """Режим 2 (VT52, по умолчанию) разбирает ESC; режим 1 — только УП."""

    def test_rezhim2_po_umolchaniyu(self):
        self.assertEqual(Parser().mode, 2)

    def test_rezhim2_esc_komandy(self):
        p = Parser()
        p.feed(b"x" * 10 + bytes([CR]))        # строка + возврат каретки
        p.feed(b"abc")                          # перезатирает начало, x=3
        p.feed(bytes([ESC]) + b"K")            # стереть от курсора до конца
        self.assertEqual(p.screen.cells[0][:5],
                         [ord("a"), ord("b"), ord("c"), 0x20, 0x20])

    def test_rezhim1_esc_ne_komanda(self):
        # ESC гасится, а следующий знак — обычная печать (команд нет)
        p = Parser(mode=1)
        p.feed(b"ab")
        p.feed(bytes([ESC]) + b"K")
        self.assertEqual(p.screen.cells[0][:3],
                         [ord("a"), ord("b"), ord("K")])
        self.assertEqual(p.screen.x, 3)
        p.feed(bytes([ESC]) + b"H")            # и «дом» не двигает курсор
        self.assertEqual((p.screen.x, p.screen.y), (4, 0))


class TestPerenosStroki(unittest.TestCase):
    def test_avtoperenos_80(self):
        p = Parser()
        p.feed(b"x" * (COLS + 1))
        self.assertEqual(p.screen.cells[0][COLS - 1], ord("x"))
        self.assertEqual(p.screen.cells[1][0], ord("x"))

    def test_skroll_vnizu(self):
        p = Parser()
        for i in range(ROWS + 1):               # 25 строк на экран на 24
            p.feed(bytes([CR, ord("0") + i % 10, LF]))
        self.assertEqual(p.screen.cells[0][0], ord("2"))          # 2 скролла
        self.assertEqual(p.screen.cells[ROWS - 2][0], ord("4"))   # последняя
        self.assertEqual(p.screen.cells[ROWS - 1][0], 0x20)
        self.assertEqual(p.screen.y, ROWS - 1)

    def test_csi_sgr_glossitsya(self):
        # линии SIMH VT340 рисуют УП через «ESC [ 2 m <символ> ESC [ m»
        # (SGR «dim») — терминал обязан проглотить последовательность
        # целиком и напечатать только сам символ
        p = Parser()
        p.feed(b"OK\x1b[2m@\x1b[m!")
        self.assertEqual(decode_koi7(bytes(p.screen.cells[0][:4])), "OK@!")

    def test_csi_strelki_kak_esc(self):
        # ANSI-стрелки «ESC [ A» = «ESC A» (курсор вверх)
        p = Parser()
        p.feed(b"\x1bY\x38\x21")                # строка min(24,23)=23, столбец 1
        p.feed(b"\x1b[A")
        self.assertEqual((p.screen.y, p.screen.x), (22, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
