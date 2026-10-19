"""Состояние SSH-моста: отчёт по запросу «status» и проверка линии.

Мост (besm6/ssh_bridge.py) отвечает на exec-команду «status»: сколько
сессий, какие за ним линии и живы ли они. Терминал спрашивает состояние
регулярно (Link.probe): ответ доказывает, что связь жива — без такой
проверки обрыв «молча» (тишина сети, убитый мост) не виден, и в строке
состояния так и горит «ЛИНИЯ=КВУ» при давно пропавшей связи.

Тест поднимает мост и поддельную линию Э-60 (свой telnet-порт с баннером
tmxr) — живой БЭСМ-6 не нужен.

Запуск: python3 -m pytest tests/test_bridge_status.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ie15emu.link import LinkError, SSHLink
from ie15emu.parser import Parser
from ie15emu.session import MODE_HOST, TerminalSession


def free_port() -> int:
    """Свободный TCP-порт (мост и линия поднимаются на разных)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class FakeLine:
    """Поддельная линия SIMH: баннер tmxr и эхо (как tty-порт)."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.banner = (b"CONNECTED TO THE PDP-11/70 SIMULATOR TTY DEVICE, "
                       b"LINE 3\r\nENCODING IS RAW\r\n")
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,),
                             daemon=True).start()

    def _session(self, conn) -> None:
        try:
            conn.sendall(self.banner)
            while True:
                if not conn.recv(4096):
                    break
                conn.sendall(b"")            # пустое эхо — линия жива
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class Bridge:
    """Процесс моста ssh_bridge.py на свободном порту."""

    def __init__(self, target_port: int) -> None:
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "besm6" / "ssh_bridge.py"),
             "--listen", "127.0.0.1", "--port", str(self.port),
             "--target", f"127.0.0.1:{target_port}",
             "--hostkey", str(ROOT / "besm6" / "host_ed25519_key"),
             "--user", "ie15", "--password", "ie15", "--stat", "0"],
            cwd=str(ROOT), stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 1).close()
                return
            except OSError:
                time.sleep(0.2)
        raise AssertionError("мост не поднялся")

    def kill(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


def open_link(bridge: Bridge, line: FakeLine) -> SSHLink:
    """Линия терминала через мост (exec «terminal <порт>»)."""
    return SSHLink("127.0.0.1", bridge.port, "ie15", password="ie15",
                   timeout=10, line_spec=str(line.port))


def test_probe_reports_bridge_state():
    """Мост отвечает на «status»: мост жив, наша линия жива."""
    line = FakeLine()
    bridge = Bridge(line.port)
    link = None
    try:
        link = open_link(bridge, line)
        info = link.probe(timeout=10)
        assert "МОСТ=ЖИВ" in info, info
        assert f"ЛИНИЯ={line.port}:ЖИВА" in info, info
    finally:
        if link is not None:
            link.close()
        bridge.kill()
        line.close()


def test_status_on_separate_connection():
    """Запрос состояния отдельным соединением — тоже работает."""
    import paramiko

    line = FakeLine()
    bridge = Bridge(line.port)
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", bridge.port, username="ie15",
                       password="ie15", timeout=10)
        chan = client.get_transport().open_session(timeout=10)
        chan.settimeout(10)
        chan.exec_command("status")
        data = b""
        while True:
            chunk = chan.recv(4096)
            if not chunk:
                break
            data += chunk
        text = data.decode("utf-8")
        assert "МОСТ=ЖИВ" in text, text
        assert "СЕССИЙ=" in text, text
    finally:
        if client is not None:
            client.close()
        bridge.kill()
        line.close()


def test_probe_fails_when_bridge_dies():
    """Мост убит — проверка обязана признать связь потерянной."""
    line = FakeLine()
    bridge = Bridge(line.port)
    link = None
    try:
        link = open_link(bridge, line)
        assert "МОСТ=ЖИВ" in link.probe(timeout=10)
        bridge.kill()
        deadline = time.time() + 10
        dead = False
        while time.time() < deadline and not dead:
            try:
                link.probe(timeout=1.0)
            except LinkError:
                dead = True
                break
            time.sleep(0.2)
        assert dead, "мёртвый мост не признан мёртвым"
    finally:
        if link is not None:
            link.close()
        line.close()


def test_session_marks_line_lost_when_bridge_dies():
    """Терминал: мост молчит — в подвале «ЛИНИЯ=НЕТ», а не «ЛИНИЯ=КВУ»."""
    line = FakeLine()
    bridge = Bridge(line.port)
    link = None
    try:
        link = open_link(bridge, line)
        session = TerminalSession(Parser(), link=link, mode=MODE_HOST)
        # баннер линии (КВУ: без «Connected to DKS») — тип линии в подвале
        session.handle_line_reply(line.banner)
        session._check_next = 0.0
        assert session.link_check_tick() is True
        assert session.check_state == "ЖИВ"
        panel = "".join(session.parser.screen.panel_cells())
        assert "ЛИНИЯ=КВУ" in panel
        bridge.kill()
        deadline = time.time() + 10
        while time.time() < deadline and not session.line_lost:
            session._check_next = 0.0
            session.link_check_tick()
            time.sleep(0.2)
        assert session.line_lost is True
        panel = "".join(session.parser.screen.panel_cells())
        assert "ЛИНИЯ=НЕТ" in panel
    finally:
        if link is not None:
            link.close()
        line.close()


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
