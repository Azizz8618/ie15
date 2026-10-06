"""Линия связи терминала 15ИЭ-00-013.

Настоящий терминал подключался токовой петлёй 20 мА (75…9600 бод)
или интерфейсом С2. Здесь:

  TCPLink   — «токовая петля» поверх TCP (совместимо с `attach tty` SIMH)
  SSHLink   — та же линия поверх SSH (paramiko), канал «exec»
"""
from __future__ import annotations

import socket
import time


class LinkError(RuntimeError):
    pass


IAC, DONT, DO, WONT, WILL, SB, SE = 0xFF, 254, 253, 252, 251, 250, 240


class IacStripper:
    """Stateful-срезка telnet-IAC (RFC 854) из потока ЭВМ → терминал.

    Состояние сохраняется между вызовами: последовательность IAC может
    оказаться разрезанной границей recv(). Байты данных линии (КОИ7/UTF-8)
    не содержат 0xFF, поэтому эсквейп IAC IAC сюда не приходит и просто
    отбрасывается.
    """

    def __init__(self) -> None:
        self.state = "data"      # data | cmd | opt | sb | sb_iac

    def feed(self, buf: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if self.state == "data":
                if b == IAC:
                    self.state = "cmd"
                    i += 1
                else:
                    out.append(b)
                    i += 1
            elif self.state == "cmd":
                if b in (WILL, WONT, DO, DONT):
                    self.state = "opt"
                elif b == SB:
                    self.state = "sb"
                elif b == IAC:
                    self.state = "data"      # IAC IAC — эсквейп, отбрасываем
                else:
                    self.state = "data"      # IAC <одиночная команда>
                i += 1
            elif self.state == "opt":
                self.state = "data"
                i += 1
            elif self.state == "sb":
                if b == IAC:
                    self.state = "sb_iac"
                i += 1
            else:                            # sb_iac
                self.state = "data" if b == SE else "sb"
                i += 1
        return bytes(out)


class TCPLink:
    """Прямое TCP-подключение к telnet-линии SIMH (IAC коды отбрасываются)."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(0.2)
        self._iac = IacStripper()

    def recv(self, size: int = 4096) -> bytes:
        try:
            chunk = self.sock.recv(size)
        except socket.timeout:
            return b""
        except OSError as e:
            raise LinkError(f"линия закрыта: {e}") from e
        if not chunk:                 # пустой recv = ЭВМ закрыла соединение
            raise LinkError("линия закрыта ЭВМ")
        return self._iac.feed(chunk)

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise LinkError(f"линия закрыта: {e}") from e

    def close(self) -> None:
        self.sock.close()


class SSHLink:
    """Линия терминала через SSH-сессию (нужен sshd или ssh_bridge.py)."""

    def __init__(self, host: str, port: int, user: str,
                 password: str | None = None, keyfile: str | None = None,
                 timeout: float = 10.0):
        try:
            import paramiko
        except ImportError as e:
            raise LinkError("SSH-линия требует paramiko (pip install paramiko)") from e
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = {"hostname": host, "port": port, "username": user,
                        "timeout": timeout}
        if keyfile:
            kwargs["key_filename"] = keyfile
        else:
            kwargs["password"] = password or ""
            kwargs["look_for_keys"] = False
        self.client.connect(**kwargs)
        self.chan = self.client.get_transport().open_session()
        self.chan.settimeout(0.2)
        # PTY не запрашивается: линия прозрачна для байтов (КОИ7/UTF-8), а
        # pty-режим sshd переврал бы УП-коды (INLCR/IXON/ISTRIP).
        self.chan.exec_command("terminal")
        time.sleep(0.3)
        self._iac = IacStripper()

    def recv(self, size: int = 4096) -> bytes:
        try:
            if self.chan.recv_ready():
                chunk = self.chan.recv(size)
                if not chunk and self.chan.exit_status_ready():
                    raise LinkError("SSH-канал закрыт ЭВМ")
                return self._iac.feed(chunk)
            if not self.chan.get_transport().active or self.chan.closed:
                raise LinkError("SSH-соединение разорвано")
        except socket.timeout:
            pass
        return b""

    def send(self, data: bytes) -> None:
        try:
            self.chan.sendall(data)
        except OSError as e:          # линия сбросилась во время набора
            raise LinkError(f"SSH-канал закрыт: {e}") from e

    def close(self) -> None:
        self.chan.close()
        self.client.close()


class StdioLink:
    """Локальная линия: поток stdin/stdout (для отладки без сети)."""

    def __init__(self):
        import sys
        self._in = sys.stdin.buffer
        self._out = sys.stdout.buffer

    def recv(self, size: int = 4096) -> bytes:
        return self._in.read1(size) if hasattr(self._in, "read1") else b""

    def send(self, data: bytes) -> None:
        self._out.write(data)
        self._out.flush()

    def close(self) -> None:
        pass


def open_link(url: str, **kw):
    """open_link('tcp://host:port') | open_link('ssh://user@host:port') | 'stdio://'"""
    if url.startswith("tcp://"):
        rest = url[6:]
        host, _, port = rest.partition(":")
        return TCPLink(host or "127.0.0.1", int(port or 4202))
    if url.startswith("ssh://"):
        rest = url[6:]
        auth, _, hostport = rest.rpartition("@")
        host, _, port = hostport.partition(":")
        return SSHLink(host or "127.0.0.1", int(port or 22),
                       user=auth or kw.get("user", "ie15"),
                       password=kw.get("password"),
                       keyfile=kw.get("keyfile"))
    if url.startswith("stdio://"):
        return StdioLink()
    raise LinkError(f"неизвестная схема линии: {url}")