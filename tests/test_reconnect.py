"""Повторное соединение по ВВОДУ: попытки повторяются, сеанс не падает.

Запуск: python3 -m pytest tests/test_reconnect.py
"""
from __future__ import annotations

import os
import pty
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ie15emu.__main__ import link_sock, reopen_link, select_ready
from ie15emu.link import LinkError
from ie15emu.parser import Parser
from ie15emu.session import MODE_HOST, TerminalSession


# --- дескриптор мёртвой линии -----------------------------------------
def test_link_sock_of_closed_socket_is_none():
    # Закрытый сокет отдаёт fileno() == -1, а такой дескриптор в select
    # нельзя (ValueError) — сеанс падал с трейсбеком после неудачного
    # переподключения. Дескриптор закрытой линии — это «нет дескриптора».
    a, b = socket.socketpair()
    b.close()
    a.close()
    assert link_sock(type("L", (), {"sock": a})()) is None


def test_select_ready_skips_dead_descriptors():
    # Мёртвый дескриптор в select — ValueError; отбрасываем и ждём
    a, b = socket.socketpair()
    try:
        ready = select_ready([-1, a.fileno()], 0.05)
        assert a.fileno() not in ready      # данных нет — пусто
        b.send(b"x")
        ready = select_ready([-1, a.fileno()], 0.5)
        assert ready == [a.fileno()]
    finally:
        a.close()
        b.close()


def test_select_ready_of_only_dead_descriptor():
    # Ни одного живого дескриптора — пусто, без исключения
    assert select_ready([-1], 0.05) == []
    assert select_ready([], 0.05) == []


# --- само переподключение ----------------------------------------------
class _FakeLink:
    """Линия-заглушка: close() считается, исключения — по указанию."""

    def __init__(self, exc=None):
        self.closed = 0
        self.exc = exc

    def close(self):
        self.closed += 1
        if self.exc is not None:
            raise self.exc


def test_reopen_link_closes_old_and_opens_new(monkeypatch):
    from ie15emu import __main__ as m

    old = _FakeLink()
    new = _FakeLink()
    monkeypatch.setattr(m, "open_link", lambda url, **kw: new)
    link, err = m.reopen_link("tcp://127.0.0.1:4202", {}, old)
    assert link is new and err == ""
    assert old.closed == 1


def test_reopen_link_survives_broken_old_link(monkeypatch):
    # close() мёртвой линии может бросить что угодно — попытка не падает
    from ie15emu import __main__ as m

    old = _FakeLink(exc=OSError("сокет уже мёртв"))
    monkeypatch.setattr(m, "open_link", lambda url, **kw: _FakeLink())
    link, err = m.reopen_link("tcp://127.0.0.1:4202", {}, old)
    assert link is not None and err == ""


def test_reopen_link_reports_failure_without_raising(monkeypatch):
    from ie15emu import __main__ as m

    def boom(url, **kw):
        raise LinkError("нет свободной линии")

    monkeypatch.setattr(m, "open_link", boom)
    old = _FakeLink()
    link, err = m.reopen_link("tcp://127.0.0.1:4202", {}, old)
    assert link is None
    assert "нет свободной линии" in err
    assert old.closed == 1            # старая линия закрыта


def test_reopen_link_swallows_any_error(monkeypatch):
    # Исключения SSH-библиотеки не являются OSError — раньше они
    # вылетали наружу и роняли сеанс
    from ie15emu import __main__ as m

    class Weird(Exception):
        pass

    def boom(url, **kw):
        raise Weird("SSH-канал закрыт")

    monkeypatch.setattr(m, "open_link", boom)
    link, err = m.reopen_link("ssh://u@h:22", {}, None)
    assert link is None
    assert "SSH-канал закрыт" in err


# --- попытки повторяются ------------------------------------------------
def test_enter_retries_reconnect_again_and_again():
    # Каждое нажатие ВВОД при обрыве — новая попытка; ограничений нет
    session = TerminalSession(Parser(), mode=MODE_HOST)
    session.mark_line_lost("нет ответа")
    tries = []
    session.reconnect = lambda: tries.append(1)
    for _ in range(5):
        session.feed_key("KEY_ENTER")
        session.feed_key("\n")
    assert len(tries) == 10            # пять нажатий, по две формы ВВОД
    assert session.line_lost is True   # флаг обрыва не сбрасывается


# --- живая проверка: сеанс не вылетает --------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_until(fd, want: bytes, timeout: float) -> bytes:
    """Читать из pty, пока не встретится нужная строка (или выйдет время)."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if want and want in buf:
            break
    return buf


def _drain(fd, timeout: float) -> bytes:
    """Забрать из pty всё, что набежало за timeout."""
    return _read_until(fd, b"", timeout)


def test_live_session_survives_failed_reconnect():
    """Живой сеанс: линия рвётся, ВВОД даёт неудачные попытки — вылета нет.

    Раньше первая же неудачная попытка закрывала сокет, и главный цикл
    падал с «ValueError: file descriptor cannot be a negative integer».
    """
    port = _free_port()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)

    def break_line():
        conn, _ = srv.accept()          # эмулятор подключился
        conn.close()                    # «разрыв сети»
        time.sleep(0.3)
        srv.close()                     # порт закрыт — попытки неудачны

    breaker = threading.Thread(target=break_line, daemon=True)
    breaker.start()

    master, slave = pty.openpty()
    env = dict(os.environ, TERM="xterm", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ie15emu", "--line", f"tcp://127.0.0.1:{port}",
         "--seconds", "12"],
        stdin=slave, stdout=slave, stderr=slave, cwd=str(ROOT),
        env=env, close_fds=True)
    os.close(slave)
    try:
        buf = _read_until(master, "сеанс продолжается".encode("utf-8"), 8.0)
        time.sleep(0.3)
        # три нажатия ВВОД — три попытки, ни одна не роняет сеанс
        for _ in range(3):
            os.write(master, b"\n")
            time.sleep(0.6)
            buf += _drain(master, 0.3)
        assert proc.poll() is None, "сеанс вылетел после неудачного переподключения"
        text = buf.decode("utf-8", "replace")
        assert "Traceback" not in text
        # попытки повторяются: каждое нажатие ВВОД — новая попытка
        assert text.count("повторное соединение") >= 3, text[-500:]
        assert text.count("не подключилось") >= 3, text[-500:]
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except OSError:
            pass
        os.close(master)
