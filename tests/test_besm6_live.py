"""Живой тест линии БЭСМ-6/ДИСПАК (SIMH, tcp/4202).

Тип линии определяется по баннеру и поддерживаются оба:
  serial-RAW («Encoding is RAW», без «Connected to DKS»):
      вход HYC<ETX>; приглашение «ЭВМ-3,TNNN» по пустой строке;
      ответы команд оканчиваются парой NUL (0x00 0x00);
      ВЫД -> «ВАМ НЕЛЬЗЯ», ЗАД/ГОД -> «ОШИБ» (уровень ЭВМ-3).
  Э-60 через ДКС («Connected to DKS», линия в raw-кодировке):
      вход HYC<ПР>; ОС подтверждает терминал видом «NNNN)» и эхом
      возвращает набранные команды («NNNN) ВЫД»).

Русские буквы на обеих линиях — внутренние КОИ7 (строка 0x60..):
клавиатурный эталон «выд» → b"wyd" (совпадает с живой таблицей приказов).

Наблюдалось на живых линиях 2026-10-04…07; первая строка после входа
машина иногда «съедает»; частые переподключения линию дёргают —
одна сессия на весь класс. Если слот Э-60 залип (клиент убит налету),
помогает перезапуск besm6; `set dks disabled/enabled` без ребута гостя
лукавит: память Э-60 обнулится, а СВЯЗЬ7 в Диспаке — нет.

Запуск: python3 tests/test_besm6_live.py
"""
from __future__ import annotations

import os
import select
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import Charset, normalize_line_bytes
from ie15emu.link import IacStripper
from ie15emu.link import LinkError, TCPLink
from ie15emu.parser import Parser
from ie15emu.render import to_text

HOST = "127.0.0.1"
# перебор ДКС-линий (dispak.ini: 4199=tty2 … 4223=tty24): занятые линии
# TCPLink пропускает сам — тесту не мешает чужая сессия на дефолтном 4202
PORT = os.environ.get("IE15_TEST_PORT", "4203-4223")
ROM = Path(__file__).resolve().parent.parent / "rom" / "chargen-15ie.bin"

# «выд зад год» → внутренние КОИ7 линии (строка 0x60..)
KOI7_TABLE = str.maketrans("выдзадгод", "wydzadgod")

HOMOGLYPH = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
                           "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
                           "X": "Х", "Y": "У"})


def fold(s: str) -> str:
    """Свести латинские омоглифы к русским (визуальное сравнение)."""
    return s.upper().translate(HOMOGLYPH)


class Besm6Live(unittest.TestCase):
    """Одна сессия на весь класс: линия не любит частых переподключений."""

    dks = False
    eol = b"\x03"

    @classmethod
    def setUpClass(cls):
        # PORT — спецификация («4202-4223»): TCPLink сам обойдёт занятые
        # линии («Line connection busy») и возьмёт первую свободную
        try:
            link = TCPLink(HOST, PORT, timeout=3.0, probe=2.0)
        except LinkError as e:
            raise unittest.SkipTest(f"БЭСМ-6 недоступна на {HOST}:{PORT}: {e}")
        cls.link = link
        cls.sock = link.sock
        cls.buf = link._pending      # баннер разведки не теряем
        cls.sock.setblocking(False)
        cls.parser = Parser()
        cls.parser.charset = Charset(ROM)
        cls.raw = False
        cls.buf = b""
        # ждём весь баннер: у ДКС-линии «Connected to DKS» приходит после
        # tmxr-строк (даже если те говорят «Encoding is UTF-8» — soctty
        # сериала + dks)
        cls._wait_any((b"Connected to DKS", b"Encoding is RAW"), 25)
        banner = cls._wait_any((b"Connected to DKS", b"Connected to DKS"), 3)
        banner = cls.buf
        cls.dks = b"Connected to DKS" in banner
        # ДКС-линии dispak.ini — «dks,raw»: сырые Н1-коды; serial-RAW — тоже
        cls.raw = b"Encoding is RAW" in banner or cls.dks
        if cls.raw:
            # telnet-IAC вырезаем stateful-но: else 0xFF-байты уходят в
            # N1-разбор и портят эталонный приём (тест проверяет поток «как
            # есть», без сессии)
            cls.iac = IacStripper()
        cls.eol = b"\r" if cls.dks else b"\x03"
        mark = len(cls.buf)
        cls.sock.sendall(b"HYC" + cls.eol)          # «НУС» — подключение нового терминала
        if cls.dks:
            cls._wait_any((b"HYC",), 15, mark)      # Э-60 эхо входа (после mark)
        else:
            cls._wait_any((b"117",), 20)            # «…ДИСПАК 117 ВЕРСИЯ»
            cls._wait_any((b"\x00\x00",), 10)
        time.sleep(2)                               # «съеденный» первый ввод

    @classmethod
    def tearDownClass(cls):
        cls.link.close()
        # СВЯЗЬ7 должна обработать отключение Э-60: слот освобождается не
        # сразу, иначе следующий тест/сессия упрётся в занятую линию
        time.sleep(10)

    @classmethod
    def _pump(cls, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([cls.sock], [], [], 0.3)
            if not r:
                return
            try:
                c = cls.sock.recv(4096)
            except BlockingIOError:
                continue
            if not c:
                return
            if getattr(cls, "raw", False):
                c = cls.iac.feed(c)
            cls.buf += c
            cls.parser.feed(c if getattr(cls, "raw", False)
                            else normalize_line_bytes(c))

    @classmethod
    def _wait_any(cls, pats, timeout: float, mark: int = 0) -> bytes:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(p in cls.buf[mark:] for p in pats):
                break
            cls._pump(0.5)
        return cls.buf[mark:]

    @classmethod
    def _cmd(cls, rus: str) -> str:
        """Отослать приказ (КОИ7) и вернуть свёрнутый экран после ответа."""
        cls._pump(0.6)
        mark = len(cls.buf)
        wire = rus.translate(KOI7_TABLE).encode("ascii")
        cls.sock.sendall(wire + cls.eol)
        if cls.dks:
            cls._wait_any((wire,), 12, mark)   # Э-60: подтверждение — эхо
            cls._pump(1.0)                     # добрать строку «NNNN) приказ»
        else:
            cls._wait_any((b"\x00\x00",), 12, mark)   # serial: пара NUL
            cls._pump(0.6)
        return fold(to_text(cls.parser.screen, cls.parser.charset))

    def test_1_text_roundtrip(self):
        """Русский текст дошёл до ЭВМ и вернулся на экран (эхо/ответ)."""
        screen = self._cmd("выд")
        self.assertIn(fold("ВЫД"), screen)
        if not self.dks:
            self.assertIn(fold("ВАМ НЕЛЬЗЯ"), screen)   # нет прав — проверено

    def test_2_prikaz_zad(self):
        screen = self._cmd("зад")
        self.assertIn(fold("ЗАД"), screen)              # приказ вернулся
        if not self.dks:
            self.assertIn(fold("ОШИБ"), screen)         # serial-диалект

    def test_3_glyfy_na_ekrane_ne_pustye(self):
        self._cmd("год")
        sc = self.parser.screen
        codes = [c for row in sc.cells for c in row if c not in (0x20, 0)]
        self.assertTrue(codes, "экран пуст")
        for code in codes:
            self.assertTrue(any(self.parser.charset.rows(code)), hex(code))

    def test_4_serial_priglashenie(self):
        if self.dks:
            self.skipTest("приглашение «ЭВМ-3,TNNN» — диалект serial-линии")
        for _ in range(2):                 # приглашение стабильно
            self._pump(0.6)
            mark = len(self.buf)
            self.sock.sendall(self.eol)    # пустая строка
            tail = self._wait_any((b"3,T0",), 15, mark)
            self.assertIn(b"3,T0", tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
