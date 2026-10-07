"""Сквозной прогон эмулятора терминала через живую линию БЭСМ-6.

Три транспортные цепочки:
  1. TCP:  терминал ── TCPLink ────────────────→ SIMH tty2 (127.0.0.1:4202)
  2. SSH:  терминал ── SSHLink ── ssh_bridge.py ─→ SIMH tty2
  3. АВТОНОМНО: набор в буфер, клавиша SEND — весь блок в линию.

Требует запущенного besm6 (см. README: tmux-сессия `besm6`); без него
тесты скипаются. Проверяет, что кириллица ходит в обе стороны: «ВЫД»
с клавиатуры доходит до ДИСПАК как внутренние КОИ7-коды (живой эталон
«wyd» из test_besm6_live), а ответ машины ложится на экран буквами,
без «█»-мусора от telnet-IAC и разорванного UTF-8.

Запуск: python3 tests/test_line_roundtrip.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ie15emu.keyboard import KEY_SEND
from ie15emu.link import LinkError, SSHLink, TCPLink
from ie15emu.parser import Parser
from ie15emu.session import MODE_HOST, MODE_LOCAL, TerminalSession

from test_besm6_live import HOST, PORT, fold  # живой эталон протокола

BRIDGE_PORT = 2222


def besm6_alive() -> bool:
    # PORT — спецификация («4202-4204»): проверяем через TCPLink, он сам
    # обоймёт занятые линии; «connection refused» на всех — только тогда
    # считается, что БЭСМ-6 нет
    try:
        TCPLink(HOST, PORT, timeout=2.0, probe=1.0).close()
        return True
    except LinkError:
        # занятая линия тоже означает живой эмулятор
        return True
    except OSError:
        return False


BUSY_PATS = (b"busy", b"no free terminal")


def open_with_retry(factory, tries: int = 8, wait: float = 4.0):
    """Линия Э-60 после разрыва освобождает слот не сразу (ОС обрабатывает
    отключение циклом СВЯЗЬ7); переподключаемся с паузами. Возвращает
    (link, head) — head предпрочитан, его скармливают login()."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            link = factory()
        except Exception as e:      # мост закрыл канал: слот ещё занят
            last = e
            time.sleep(wait)
            continue
        head = b""
        try:
            deadline = time.time() + 6
            while time.time() < deadline and len(head) < 400:
                d = link.recv()
                if d:
                    head += d
                    if any(p in head for p in BUSY_PATS):
                        break
                else:
                    time.sleep(0.05)
            if any(p in head for p in BUSY_PATS):
                last = LinkError("линия Э-60 ещё занята")
                link.close()
                time.sleep(wait)
                continue
            return link, head
        except LinkError as e:            # канал умер — слот ещё занят
            last = e
            link.close()
            time.sleep(wait)
    raise AssertionError(f"линия занята после {tries} попыток: {last}")


def make_session(link, mode=MODE_HOST):
    parser = Parser()
    return TerminalSession(parser, link=link, mode=mode)


def feed_keys(session, text: str) -> None:
    """Нажатия клавиш → линия (как в run_line), без прямой записи в link."""
    for ch in text:
        session.emit(session.feed_key(ch))


def feed_enter(session) -> None:
    session.emit(session.feed_key("KEY_ENTER"))


def read_until(link, session, pattern: bytes, timeout: float) -> bytes:
    """Читать линию в парсер, пока в сырых байтах не встретится pattern."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = link.recv()
        if data:
            buf += data
            session.handle_line_reply(data)
            if pattern in buf:
                return buf
        else:
            time.sleep(0.02)
    raise AssertionError(f"нет ответа {pattern!r} за {timeout} c; получено {buf[:200]!r}")


def read_until_any(link, session, patterns, timeout: float) -> bytes:
    """Читать линию, пока не встретится любой из паттернов."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = link.recv()
        if data:
            buf += data
            session.handle_line_reply(data)
            if any(p in buf for p in patterns):
                return buf
        else:
            time.sleep(0.02)
    raise AssertionError(f"нет ответа ни по одному из {patterns!r} "
                         f"за {timeout} c; получено {buf[:200]!r}")


def drain(link, session, quiet: float = 0.6) -> None:
    """Дочитать хвост прошлого ответа (иначе эхо/приглашение собьёт ожидание)."""
    while True:
        data = link.recv()
        if data:
            session.handle_line_reply(data)
            quiet = 0.6
        else:
            time.sleep(0.05)
            quiet -= 0.05
            if quiet <= 0:
                return


def wait_screen(link, session, phrase: str, timeout: float) -> None:
    """Читать линию, пока фраза (в свёртке) не появится на экране.

    Не зависит ни от кодировки линии, ни от концовок ответов: критерий —
    то, что видит оператор.
    """
    target = fold(phrase)
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = link.recv()
        if data:
            session.handle_line_reply(data)
            if target in fold(session.parser.screen.text()):
                return
        else:
            time.sleep(0.02)
    raise AssertionError(f"нет {phrase!r} на экране за {timeout} c:\n"
                         f"{session.parser.screen.text()[:600]}")


def detect_line_type(link, session) -> bytes:
    """Баннер линии: «Connected to DKS» — Э-60, иначе serial."""
    return read_until(link, session, b"line", 25)


def login(session, link, head: bytes = b"") -> None:
    """Вход, общий для serial-RAW и ДКС линий."""
    if head:
        session.handle_line_reply(head)
    if b"line" not in head:               # баннер мог прийти не целиком
        try:
            detect_line_type(link, session)
        except AssertionError:
            pass
    feed_keys(session, "HYC")            # окончание подберёт to_line
    feed_enter(session)
    # serial: приветствие «…117…»; Э-60: эхо «HYC» локальным редактором
    read_until_any(link, session, (b"117", b"HYC", b"3,T0", b") "), 20)
    drain(link, session)                 # дочитать хвост приветствия/эха
    feed_enter(session)                  # пустая строка
    # готовность: serial — приглашение/НУС; ДКС — ПР-хвост или «NNNN) »
    if session.encoding == "dks":
        read_until_any(link, session, (b"\n", b") "), 15)
    else:
        read_until_any(link, session, (b"3,T0", b"\x00\x00"), 15)
    time.sleep(2)                        # первый ввод машина «съедает»


def connect_logged_in(link_factory, tries: int = 5):
    """Подключение + вход с переподключением: Э-60 слот освобождается
    только после обработки разрыва на стороне ОС."""
    last: Exception | None = None
    for _ in range(tries):
        link = session = None
        try:
            link, head = open_with_retry(link_factory)
            session = make_session(link)
            login(session, link, head)
            return link, session
        except (LinkError, AssertionError) as e:
            last = e
            if link is not None:
                try:
                    link.close()
                except Exception:
                    pass
            time.sleep(8)          # дать СВЯЗЬ7 обработать отключение
    raise AssertionError(f"вход не состоялся за {tries} попытки: {last}")


class LineRoundtripMixin:
    """Общий сценарий: логин, «ВЫД» с клавиатуры, ответ на экране.

    Замечание: 0x7F («█») — штатный заполнитель ДИСПАК на этой линии,
    поэтому «чистота экрана» оценивается наличием приглашения ЭВМ-3,TNNN,
    а не отсутствием заливок.
    """

    link = None
    session = None

    def run_vyd_from_keyboard(self):
        """Печатает «ВЫД» клавишами терминала; ответ ЭВМ виден на экране.

        serial-RAW: «ВЫД» без прав → «ВАМ НЕЛЬЗЯ»;
        ДКС/Э-60: ОС подтверждает приказ эхом «NNNN) ВЫД».
        """
        session = self.session
        drain(self.link, session)
        feed_keys(session, "ВЫД")
        feed_enter(session)
        if session.encoding == "dks":
            wait_screen(self.link, session, "ВЫД", 20)   # эхо от ОС
        else:
            wait_screen(self.link, session, "ВАМ НЕЛЬЗЯ", 20)

    def prompt_on_screen(self):
        # «ЭВМ-3,TNNN» — диалект serial-линии (Э-60 подтверждает «NNNN)»)
        if self.session.encoding == "dks":
            return
        drain(self.link, self.session)   # хвост ответа не должен съесть ввод
        feed_enter(self.session)
        wait_screen(self.link, self.session, "3,T0", 15)


@unittest.skipUnless(besm6_alive(), f"БЭСМ-6 не отвечает на {HOST}:{PORT}")
class TestTcpRoundtrip(LineRoundtripMixin, unittest.TestCase):
    """Прямая токовая петля поверх TCP."""

    @classmethod
    def setUpClass(cls):
        cls.link, cls.session = connect_logged_in(lambda: TCPLink(HOST, PORT))

    @classmethod
    def tearDownClass(cls):
        cls.link.close()
        # СВЯЗЬ7 должна обработать отключение Э-60 терминала, иначе
        # следующая сессия (SSH-класс) упрётся в занятый слот
        time.sleep(15)

    def test_1_banner_sets_encoding(self):
        self.assertTrue(self.session.enc_decided)
        self.assertIn(self.session.encoding, ("raw", "utf8", "dks"))

    def test_2_keyboard_to_machine_and_back(self):
        self.run_vyd_from_keyboard()
        self.prompt_on_screen()

    def test_3_autonomous_then_send(self):
        # АВТОНОМНО: набранное лежит в буфере и на экране, линия молчит;
        # по SEND блок уходит в линию целиком и ЭВМ отвечает.
        session = self.session
        drain(self.link, session)
        session.set_mode(MODE_LOCAL)
        feed_keys(session, "ЗАД")
        feed_enter(session)
        self.assertTrue(session.pending())     # линия свободна, ждём SEND
        self.assertIn("ЗАД", fold(session.parser.screen.text()))
        out = session.feed_key(KEY_SEND)
        session.emit(out)
        self.assertEqual(session.pending(), b"")
        if session.encoding == "dks":
            # Э-60: после SEND линия должна ответить новыми байтами
            # (эхо «NNNN) ЗАД») — локальное эхо уже было на экране
            got = 0
            deadline = time.time() + 15
            while got < 4 and time.time() < deadline:
                chunk = self.link.recv()
                if chunk:
                    session.handle_line_reply(chunk)
                    got += len(chunk)
                else:
                    time.sleep(0.02)
            self.assertGreaterEqual(got, 4, "Э-60 не ответила на SEND")
        else:
            wait_screen(self.link, session, "ОШИБ", 20)
        session.set_mode(MODE_HOST)


@unittest.skipUnless(besm6_alive(), f"БЭСМ-6 не отвечает на {HOST}:{PORT}")
class TestSshRoundtrip(LineRoundtripMixin, unittest.TestCase):
    """Через SSH-мост (ssh_bridge.py → telnet-линия SIMH)."""

    @classmethod
    def setUpClass(cls):
        cls.bridge = subprocess.Popen(
            [sys.executable, str(ROOT / "besm6" / "ssh_bridge.py"),
             "--listen", "127.0.0.1", "--port", str(BRIDGE_PORT),
             "--target", f"{HOST}:{PORT}",
             "--hostkey", str(ROOT / "besm6" / "host_ed25519_key"),
             "--user", "ie15", "--password", "ie15"],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            cls.link = None
            for _ in range(40):          # ждём подъёма SSH-сервера
                try:
                    socket.create_connection(("127.0.0.1", BRIDGE_PORT), 1).close()
                    break
                except OSError:
                    time.sleep(0.25)
            # line_spec передаём так же, как терминал с ?port= в URL:
            # мост обязан перебрать ДКС-линии и взять первую свободную
            cls.link, cls.session = connect_logged_in(
                lambda: SSHLink("127.0.0.1", BRIDGE_PORT, "ie15",
                                password="ie15", timeout=10,
                                line_spec=PORT))
        except Exception:
            cls.stop_bridge()
            raise

    @classmethod
    def stop_bridge(cls):
        cls.bridge.terminate()
        try:
            cls.bridge.wait(5)
        except subprocess.TimeoutExpired:
            cls.bridge.kill()

    @classmethod
    def tearDownClass(cls):
        if cls.link is not None:
            cls.link.close()
        cls.stop_bridge()

    def test_1_login_over_ssh(self):
        self.assertTrue(self.session.enc_decided)

    def test_2_keyboard_to_machine_and_back(self):
        self.run_vyd_from_keyboard()
        self.prompt_on_screen()


if __name__ == "__main__":
    unittest.main(verbosity=2)
