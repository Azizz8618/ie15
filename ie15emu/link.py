"""Линия связи терминала 15ИЭ-00-013.

Настоящий терминал подключался токовой петлёй 20 мА (75…9600 бод)
или интерфейсом С2. Здесь:

  TCPLink   — «токовая петля» поверх TCP (совместимо с `attach tty` SIMH)
  SSHLink   — та же линия поверх SSH (paramiko), канал «exec»

Порт можно задать одним числом, списком («4202,4210») или диапазоном
(«4202-4223»): подключение идёт по очереди ко всем портам — если линия
занята (SIMH отвечает «Line connection busy» и закрывает сокет) или порт
не отвечает, пробуется следующий.
"""
from __future__ import annotations

import socket
import time


class LinkError(RuntimeError):
    pass


# чем SIMH отвечает на вход в уже занятую линию (tmxr/DKS) — после этих
# строк сокет закрывается; такие порты пропускаем при переборе
BUSY_MARKERS = (b"connection busy", b"connection not available",
                b"line busy", b"no free terminal",
                # мост сообщает, что не смог дойти до линии (UTF-8)
                "линия недоступна".encode("utf-8"))


def parse_port_spec(spec) -> list[int]:
    """Спецификация портов → упорядоченный список без повторов.

    «4202», «4202,4210», «4202-4223», «4202-4204,4210» — как в конфигурации
    терминала (поле port) и в ключe --port.
    """
    if isinstance(spec, int):
        specs = [str(spec)]
    elif isinstance(spec, (list, tuple)):
        specs = [str(s) for s in spec]
    else:
        specs = str(spec).split(",")
    ports: list[int] = []
    for part in specs:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if lo > hi:
                raise LinkError(f"неверный диапазон портов: {part}")
            ports.extend(range(lo, hi + 1))
        else:
            ports.append(int(part))
    for p in ports:
        if not 0 < p < 65536:
            raise LinkError(f"порт вне диапазона: {p}")
    if not ports:
        raise LinkError("не задано ни одного порта")
    if len(ports) > 512:
        raise LinkError("слишком длинный список портов (максимум 512)")
    return list(dict.fromkeys(ports))


def busy_reply(data: bytes) -> bool:
    return any(m in data.lower() for m in BUSY_MARKERS)


def spec_hint(ports) -> str:
    """«4202,4204-4206» из списка — компактнее длинных диапазонов."""
    ports = sorted(set(ports))
    if not ports:
        return "нет"
    out, run, prev = [], None, None
    for p in ports:
        if prev is not None and p == prev + 1:
            run[1] = p
        else:
            if run:
                out.append(run)
            run = [p, p]
        prev = p
    if run:
        out.append(run)
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


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
    """Прямое TCP-подключение к telnet-линии SIMH (IAC коды отбрасываются).

    port — одно число, список или спецификация («4202-4223»): к портам
    идут по порядку; занятая линя (SIMH шлёт «Line connection busy» и
    закрывает сокет) и недоступный порт пропускаются, берётся следующий.
    """

    def __init__(self, host: str, port, timeout: float = 5.0,
                 probe: float = 2.0):
        ports = parse_port_spec(port)
        self.port = None
        self._pending = b""
        self._iac = IacStripper()
        refused = []
        busy = []
        for p in ports:
            try:
                sock = socket.create_connection((host, p), timeout)
            except OSError:
                refused.append(p)
                continue
            first = b""
            try:
                sock.settimeout(probe)
                first = sock.recv(4096)
            except socket.timeout:
                first = b""
            except OSError:
                sock.close()
                refused.append(p)
                continue
            if busy_reply(first):
                sock.close()
                busy.append(p)
                continue
            sock.settimeout(0.2)
            self.sock = sock
            self.port = p
            if first:
                self._pending = self._iac.feed(first)
            return
        if busy and not refused:
            raise LinkError(f"все линии заняты ЭВМ ({spec_hint(ports)})")
        raise LinkError(
            f"нет свободной линии на {host} ({spec_hint(ports)}): "
            f"заняты {spec_hint(busy)}, недоступны {spec_hint(refused)}")

    def recv(self, size: int = 4096) -> bytes:
        if self._pending:
            out, self._pending = self._pending, b""
            return out
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

    def __init__(self, host: str, port, user: str,
                 password: str | None = None, keyfile: str | None = None,
                 timeout: float = 10.0, probe: float = 3.0):
        try:
            import paramiko
        except ImportError as e:
            raise LinkError("SSH-линия требует paramiko (pip install paramiko)") from e
        # port — число, список или спецификация («2222,2223» / «2222-2225»):
        # перебор мостов; порт, где линия ЭВМ уже занята (мост прокидывает
        # «Line connection busy»), тоже пропускаем и идём дальше
        ports = parse_port_spec(port)
        self.port = None
        self._pending = b""
        self._iac = IacStripper()
        busy, refused = [], []
        for p in ports:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs: dict = {"hostname": host, "port": p, "username": user,
                            "timeout": timeout}
            if keyfile:
                kwargs["key_filename"] = keyfile
            else:
                kwargs["password"] = password or ""
                kwargs["look_for_keys"] = False
            try:
                client.connect(**kwargs)
            except (paramiko.SSHException, OSError):
                client.close()
                refused.append(p)
                continue
            chan = client.get_transport().open_session()
            chan.settimeout(probe)
            # PTY не запрашивается: линия прозрачна для байтов (КОИ7/UTF-8), а
            # pty-режим sshd переврал бы УП-коды (INLCR/IXON/ISTRIP).
            chan.exec_command("terminal")
            try:
                first = chan.recv(4096)
            except socket.timeout:
                first = b""
            if busy_reply(first):
                chan.close()
                client.close()
                busy.append(p)
                continue
            chan.settimeout(0.2)
            self.client, self.chan, self.port = client, chan, p
            if first:
                self._pending = self._iac.feed(first)
            time.sleep(0.3)
            return
        if busy and not refused:
            raise LinkError(f"все SSH-линии заняты ЭВМ ({spec_hint(ports)})")
        raise LinkError(
            f"нет свободной SSH-линии на {host} ({spec_hint(ports)}): "
            f"заняты {spec_hint(busy)}, недоступны {spec_hint(refused)}")

    def recv(self, size: int = 4096) -> bytes:
        if self._pending:
            out, self._pending = self._pending, b""
            return out
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
    """open_link('tcp://host:port') | open_link('ssh://user@host:port') | 'stdio://'

    port — одно число, список или спецификация («4202-4223», «4202,4210»);
    при диапазоне/списке идёт перебор по порядку до свободной линии.
    """
    if url.startswith("tcp://"):
        rest = url[6:]
        host, _, port = rest.partition(":")
        return TCPLink(host or "127.0.0.1", port or 4202)
    if url.startswith("ssh://"):
        rest = url[6:]
        auth, _, hostport = rest.rpartition("@")
        host, _, port = hostport.partition(":")
        return SSHLink(host or "127.0.0.1", port or 22,
                       user=auth or kw.get("user", "ie15"),
                       password=kw.get("password"),
                       keyfile=kw.get("keyfile"))
    if url.startswith("stdio://"):
        return StdioLink()
    raise LinkError(f"неизвестная схема линии: {url}")