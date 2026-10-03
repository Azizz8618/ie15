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


class TCPLink:
    """Прямое TCP-подключение к telnet-линии SIMH (IAC коды отбрасываются)."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(0.2)

    @staticmethod
    def _strip_iac(buf: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(buf)
        while i < n:
            if buf[i] == 0xFF and i + 1 < n:
                cmd = buf[i + 1]
                if 0xFB <= cmd <= 0xFE:
                    i += 3
                elif cmd == 0xFA:
                    j = buf.find(b"\xff\xf0", i)
                    i = (j + 2) if j >= 0 else n
                else:
                    i += 2
            else:
                out.append(buf[i])
                i += 1
        return bytes(out)

    def recv(self, size: int = 4096) -> bytes:
        try:
            return self._strip_iac(self.sock.recv(size))
        except socket.timeout:
            return b""

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

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
        self.chan.get_pty(term="vt52", width=80, height=25)
        self.chan.exec_command("terminal")
        time.sleep(0.3)

    def recv(self, size: int = 4096) -> bytes:
        try:
            if self.chan.recv_ready():
                return self.chan.recv(size)
        except socket.timeout:
            pass
        return b""

    def send(self, data: bytes) -> None:
        self.chan.sendall(data)

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