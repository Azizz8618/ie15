"""Линия связи: keep-alive и поведение при обрыве.

Запуск: python3 -m pytest tests/test_link.py
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.link import (KEEPALIVE, LinkError, SSHLink, _enable_ssh_keepalive,
                          _set_tcp_keepalive)
from ie15emu.parser import Parser
from ie15emu.session import MODE_HOST, MODE_LOCAL, TerminalSession


def test_tcp_socket_gets_keepalive():
    # Без трафика сервер (sshd/шлюз/NAT) по своему таймауту гасит
    # соединение — TCP-сокет должен сам поддерживать линию живой
    s = socket.socket()
    try:
        _set_tcp_keepalive(s)
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        # если стек поддерживает — выставлены и тайминги пробы
        idle = getattr(socket, "TCP_KEEPIDLE", None)
        if idle is not None:
            got = s.getsockopt(socket.IPPROTO_TCP, idle)
            assert got == KEEPALIVE
    finally:
        s.close()


class _FakeTransport:
    """Заглушка paramiko-транспорта: только записывает вызовы."""

    def __init__(self) -> None:
        self.keepalive: int | None = None

    def set_keepalive(self, seconds: int) -> None:
        self.keepalive = seconds


def test_ssh_transport_gets_keepalive():
    t = _FakeTransport()
    assert _enable_ssh_keepalive(t) is True
    assert t.keepalive == KEEPALIVE


def test_ssh_keepalive_tolerates_broken_transport():
    class _Dead:
        def set_keepalive(self, seconds):
            raise RuntimeError("transport уже закрыт")

    assert _enable_ssh_keepalive(_Dead()) is False
    assert _enable_ssh_keepalive(None) is False


class _BrokenChan:
    """SSH-канал мёртв: то, что бросает paramiko при обрыве/таймауте."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def sendall(self, data):            # noqa: ARG002 — как у paramiko
        raise self._exc


def _ssh_link_with(chan) -> SSHLink:
    link = SSHLink.__new__(SSHLink)     # без подключения
    link.chan = chan
    link._pending = b""
    link._iac = None
    link.client = None
    return link


def test_ssh_send_converts_paramiko_errors_to_linkerror():
    # Исключение paramiko НЕ является OSError: раньше оно улетало мимо
    # «except (OSError, LinkError)» в главном цикле и роняло эмулятор.
    # Важен именно родной класс из link.py (он же и приезжает от paramiko)
    from ie15emu.link import _SSHException

    for exc in (_SSHException("SSH session not active"), EOFError(),
                OSError("Broken pipe")):
        link = _ssh_link_with(_BrokenChan(exc))
        try:
            link.send(b"data")
        except LinkError:
            pass
        else:
            raise AssertionError(f"{type(exc).__name__} не переведён в LinkError")


def test_session_survives_line_death():
    """Обрыв линии не завершает сеанс: отправка гасится, флаг поднят."""

    class DeadLink:
        def send(self, data):                # noqa: ARG002
            raise LinkError("линия потеряна: SSH-канал закрыт")

        def recv(self):
            raise LinkError("линия потеряна")

        def close(self):
            pass

    # С ЭВМ: каждый знак уходит в линию — обрыв ловится, сеанс жив
    # (как в главном цикле: session.emit(session.feed_key(key)))
    s = TerminalSession(Parser(), link=DeadLink(), mode=MODE_HOST)
    s.emit(s.feed_key("A"))
    assert s.line_lost is True
    # причина обрыва видна в подвале — ровно в момент обрыва
    assert s.last_key.startswith("ЛИНИЯ")
    # дальнейший набор не падает (отправки подавлены), флаг не сбрасывается
    for ch in "BC":
        assert s.emit(s.feed_key(ch)) is None
    assert s.line_lost is True
    assert "НЕТ" in "".join(s.parser.screen.panel_cells())  # строка состояния

    # АВТОНОМНО: набор линию не трогает — флаг не поднимается и буфер
    # спокойно работает (именно ради этого режима сеанс и не завершаем)
    s = TerminalSession(Parser(), link=DeadLink(), mode=MODE_LOCAL)
    for ch in "AB":
        assert s.feed_key(ch) is None
    assert s.line_lost is False
    assert len(s.pending()) == 2               # буфер набора полон (Н2)
    # ПЕРЕДАЧА (SEND) уже уходит в линию — здесь обрыв и поднимает флаг
    s.emit(s.feed_key("SEND"))
    assert s.line_lost is True
    assert s.last_key.startswith("ЛИНИЯ")


def test_line_lost_flag_flips_once():
    class Once:
        def __init__(self):
            self.n = 0

        def send(self, data):                # noqa: ARG002
            self.n += 1
            raise LinkError("обрыв")

    link = Once()
    s = TerminalSession(Parser(), link=link, mode=MODE_HOST)
    s.emit(b"x")
    first = s.last_key
    s.emit(b"x")                             # линия уже отмечена — тихо
    assert s.line_lost is True
    assert s.last_key == first
    assert link.n == 1                       # дальше отправок нет
