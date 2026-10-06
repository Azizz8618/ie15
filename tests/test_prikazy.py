"""Отображение настоящих приказов пользователя ДИСПАК (bonbot.be / prikaz.be).

Приказы и статусы взяты из словарей КОНК (re_dispak/bonbot.be) и таблицы
разбора приказов (re_dispak/prikaz.be, прик1..прик8). Проверяется
правильность отображения: каждая буква обязана попадать в заполненный глиф
ПЗУ знакогенератора (слоты 0x00..0x1E в ПЗУ пусты — заглавные обязаны
идти в блок 0xE0..0xFE, строчные — в 0xC0..0xDE).

Эталоны глифов сняты с настоящего дампа rom/chargen-15ie.bin.
Ответ «ЭВМ-3,TNNN» — подтверждён расшифровкой РЭП п.2.5.15.14
(TNNN — номер терминала).

Запуск: python3 tests/test_prikazy.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import (Charset, RUS7, decode_koi7, encode_koi7,
                             normalize_line_bytes, rom_addr)
from ie15emu.parser import Parser

ROM = Path(__file__).resolve().parent.parent / "rom" / "chargen-15ie.bin"

# Приказы пользователя ДИСПАК (bonbot.be, КОНК; prikaz.be, прик1..прик8)
PRIKAZY = (
    "ВЫД", "ЗАД", "ЗАП", "СНЯ", "ОТМ", "ПОВТ", "ВРЕМ", "ГОД", "ЛИСТ",
    "СЧЕТ", "ПОВ", "ВРЕ", "СМЕ", "ДАЙ", "БОБ", "ВЦП", "ВВД", "ПРТ",
    "АДР", "ВМГ", "ВКН", "СБВ", "СВМ", "СПЕ", "ПУЛ", "ПРО", "ВЫБ",
    "НОМ", "СКВ", "ЗРА", "РЕЖ", "ПОР", "ДАТ", "РАЗ", "КЛК", "СБО",
    "УСТ", "НОТ", "ОСТ", "НЕТ", "КТО", "НУС", "ВМД", "СВД", "ПАК",
    "РЗД", "МИН", "КВА", "ПОД", "НАЙ", "ПУС", "ИСК", "ВКЛ", "ОПР",
    "УПР", "КТЧ", "ВОС", "ЕСМ", "РЗЕ", "ФС5", "ФС8",
)

# Статусы/ответы системы (prikaz.be)
STATUSY = (
    "ГЛАВ", "ЖДУ", "ТРАКТ", "АРХ.", "Э70А", "ОБМЕН", "ВИСП", "ENQ",
    "ПАУЗА", "Э71", "КАЧКА", "ОТЛ.", "ЭК", "ЗАПР.", "ЗУПР", "ЕСТЬ",
    "ЗНЕТ", "ДАЙ0",
)

# Ответ на передачу символа конца строки: «ЭВМ-3,TNNN», TNNN — номер терминала
PROMPT_TMPL = "ЭВМ-3,T{n:03d}"

# Эталонные глифы ПЗУ (rom/chargen-15ie.bin, адреса 0xE0+индекс по RUS7)
GLYPHS_ZAGLAVNYE = {
    "В": ["######.", "#.....#", "#.....#", "######.",
          "#.....#", "#.....#", "#.....#", "######."],
    "Ы": ["#.....#", "#.....#", "#.....#", "####..#",
          "#...#.#", "#...#.#", "#...#.#", "####..#"],
    "Д": ["...###.", "..#..#.", ".#...#.", ".#...#.",
          ".#...#.", ".#...#.", "#######", "#.....#"],
    "Э": [".####..", "#....#.", "......#", "..#####",
          "......#", "......#", "#....#.", ".####.."],
    "М": ["#.....#", "##...##", "#.#.#.#", "#..#..#",
          "#..#..#", "#.....#", "#.....#", "#.....#"],
}

_cs: Charset | None = None


def charset() -> Charset:
    global _cs
    if _cs is None:
        _cs = Charset(ROM)
    return _cs


def glyph_rows(ch: str) -> list[str]:
    """8 строк глифа буквы как текст из точек (7 колонок)."""
    code = encode_koi7(ch)[0]
    return ["".join("#" if (r >> (6 - i)) & 1 else "."
                    for i in range(7))
            for r in charset().rows(code)]


class TestPrikazyKodirovanie(unittest.TestCase):
    """Приказы кодируются и декодируются без потерь."""

    def test_obratnoe_preobrazovanie(self):
        for word in PRIKAZY + STATUSY + tuple(
                PROMPT_TMPL.format(n=n) for n in (1, 2, 15)):
            self.assertEqual(decode_koi7(encode_koi7(word)), word, word)

    def test_stroka_dialoga(self):
        # «ВЫД 5,СМЕ 30/56,ВРЕ» — типовой диалог с ДИСПАК
        line = "ВЫД 5,СМЕ 30/56,ВРЕ"
        self.assertEqual(decode_koi7(encode_koi7(line)), line)


class TestPrikazyOtobrazhenie(unittest.TestCase):
    """Каждая буква приказа обязана попадать в заполненный глиф ПЗУ."""

    def test_glyfy_ne_pustye(self):
        c = charset()
        for word in PRIKAZY + STATUSY + ("ЭВМ-3,T002",):
            for ch in word:
                code = encode_koi7(ch)[0]
                self.assertTrue(any(c.rows(code)),
                                f"{word!r}: {ch!r} → пустой глиф {code:#04x}")

    def test_zaglavnye_v_bloke_E0_FE(self):
        # слоты 0x00..0x1E ПЗУ пусты — заглавные обязаны идти в 0xE0..0xFE
        for ch in RUS7:
            addr = rom_addr(encode_koi7(ch)[0])
            self.assertGreaterEqual(addr, 0xE0, ch)
            self.assertLessEqual(addr, 0xFE, ch)

    def test_strochnye_v_bloke_C0_DE(self):
        for ch in RUS7.lower():
            addr = rom_addr(encode_koi7(ch)[0])
            self.assertGreaterEqual(addr, 0xC0, ch)
            self.assertLessEqual(addr, 0xDE, ch)

    def test_reyestry_razlichimy(self):
        c = charset()
        for ch in RUS7:
            verh = c.rows(encode_koi7(ch)[0])
            niz = c.rows(encode_koi7(ch.lower())[0])
            self.assertNotEqual(verh, niz,
                                f"{ch}/{ch.lower()}: один глиф на два регистра")

    def test_bukvy_v_prikazah_razlichimy(self):
        c = charset()
        for word in PRIKAZY:
            seen = {}
            for ch in word:
                if ch.isalpha():
                    rows = tuple(c.rows(encode_koi7(ch)[0]))
                    if ch in seen:
                        self.assertEqual(seen[ch], rows, word)
                    seen[ch] = rows
            self.assertEqual(len({r for r in seen.values()}), len(seen), word)

    def test_glyfy_vyderzhki_iz_pzu(self):
        # сверка с реальными глифами дампа: ВЫД → В Ы Д, ЭВМ-3 → Э В М
        for ch, expected in GLYPHS_ZAGLAVNYE.items():
            self.assertEqual(glyph_rows(ch), expected, ch)

    def test_vyvod_cherez_parser(self):
        # полный тракт: коды КОИ7 → ОЗУ экрана → глифы ПЗУ
        p = Parser()
        p.charset = charset()
        p.feed(encode_koi7("ВЫД"))
        codes = [p.screen.cells[0][x] for x in range(3)]
        self.assertEqual(decode_koi7(bytes(codes)), "ВЫД")
        for code in codes:
            self.assertTrue(any(p.charset.rows(code)), hex(code))


class TestPromptEVM3(unittest.TestCase):
    """«ЭВМ-3,TNNN» — ответ на передачу символа конца строки.

    TNNN — номер терминала (РЭП п.2.5.15.14).
    """

    def test_format(self):
        for n in (1, 2, 15, 120):
            s = PROMPT_TMPL.format(n=n)
            self.assertTrue(s.startswith("ЭВМ-3,T"), s)
            self.assertEqual(s[6:], f"T{n:03d}", s)

    def test_otobrazhenie(self):
        c = charset()
        for n in (1, 2, 15):
            s = PROMPT_TMPL.format(n=n)
            self.assertEqual(decode_koi7(encode_koi7(s)), s, s)
            for ch in "ЭВМ":
                self.assertTrue(any(c.rows(encode_koi7(ch)[0])), ch)

    def test_iz_linii_simh_utf8(self):
        # строка из линии SIMH (UTF-8) → внутренний КОИ7 с битом
        # алфавита (заглавные ≥0x80 — иначе «М» сливается с ПР) → тот же
        # текст после декодирования
        for n in (1, 2):
            s = PROMPT_TMPL.format(n=n)
            out = normalize_line_bytes(s.encode("utf-8"))
            self.assertTrue(any(b >= 0x80 for b in out))
            self.assertEqual(decode_koi7(out), s)
            # через парсер: заглавные обязаны лечь буквами, а не УП
            p = Parser()
            p.feed(out)
            self.assertEqual(decode_koi7(
                bytes(p.screen.cells[0][:len(s)])), s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
