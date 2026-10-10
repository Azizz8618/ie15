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

    banner = True       # ждём баннер tmxr («CONNECTED TO …»)

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
        raise _exhausted(host, ports, busy, refused, kind="линии")

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


def _exhausted(host: str, ports, busy, refused, kind="SSH-линии") -> LinkError:
    parts = []
    if busy:
        parts.append(f"заняты {spec_hint(busy)}")
    if refused:
        parts.append(f"нет соединения с {spec_hint(refused)}")
    if not parts:
        parts.append("каналы не отвечают")
    return LinkError(f"нет свободной {kind} на {host} "
                     f"({spec_hint(ports)}): {'; '.join(parts)}")


class SSHLink:
    """Линия терминала через SSH-сессию.

    Два режима, выбираются сами:

    * прямой туннель (direct-tcpip) к telnet-линиям SIMH на 127.0.0.1
      сервера — работает на обычном sshd (OpenSSH), мост не нужен;
      line_spec («4202-4223») — перебор линий, занятые пропускаются;
    * exec «terminal [spec]» — к ssh_bridge.py (порт моста), когда
      direct-tcpip запрещён сервером.
    """

    banner = True       # ждём баннер tmxr («CONNECTED TO …»)

    def __init__(self, host: str, port, user: str,
                 password: str | None = None, keyfile: str | None = None,
                 timeout: float = 10.0, probe: float = 3.0,
                 line_spec: str | None = None):
        try:
            import paramiko
        except ImportError as e:
            raise LinkError("SSH-линия требует paramiko (pip install paramiko)") from e
        # port — число, список или спецификация («22», «2222,2223»):
        # перебор SSH-портов сервера/моста
        self.port = None
        self.line_port = None
        self._pending = b""
        self._iac = IacStripper()
        busy, refused = [], []
        last_err: Exception | None = None
        for p in ports:
            kwargs: dict = {"hostname": host, "port": p, "username": user,
                            "timeout": timeout}
            if keyfile:
                kwargs["key_filename"] = keyfile
            else:
                kwargs["password"] = password or ""
                kwargs["look_for_keys"] = False
            client = None
            # sshd на перегрузке бросает незавершённые рукопожатия
            # (MaxStartups): «Error reading SSH protocol banner» — transient,
            # пробуем несколько раз с нарастающей паузой
            for attempt in range(4):
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    client.connect(**kwargs)
                    break
                except paramiko.SSHException:
                    client.close()
                    client = None
                    if attempt < 3:
                        time.sleep(1.0 + 1.5 * attempt)
                except OSError:
                    client.close()
                    client = None
                    break            # refused/сеть — повторять бессмысленно
            if client is None:
                refused.append(p)
                continue
            chan = first = None
            if line_spec:
                # 1) прямой туннель sshd к линиям SIMH на хосте сервера;
                # first — маркер «линия найдена и обстреляна баннером»
                chan, first, last_err = self._forward_scan(
                    paramiko, client, client.get_transport(), line_spec,
                    timeout, probe, busy)
                if chan is None and first == b"":
                    # туннель не открылся ни на одну линию (занято/нет
                    # слушателя) — мост тут не поможет, сообщаем причину
                    client.close()
                    continue
            if chan is None:
                # 2) exec на сервере: мост ssh_bridge.py («terminal …») или,
                # если его нет, netcat/socat к линии — SSH-сервер может
                # запрещать direct-tcpip.  PTY не запрашивается: линия
                # прозрачна для байтов, pty-режим переврал бы УП-коды
                # каждую линию сервера пробуем своими exec-каналами:
                # мост «terminal», а на голом sshd — netcat к 127.0.0.1:порт
                for lp in (parse_port_spec(line_spec) if line_spec else [None]):
                    cmds = (["terminal"] if lp is None else
                            [f"terminal {lp}", "terminal",
                             f"nc -q1 127.0.0.1 {lp}", f"nc 127.0.0.1 {lp}"])
                    for cmd in cmds:
                        try:
                            chan = client.get_transport().open_session(
                                timeout=timeout)
                            chan.settimeout(probe)
                            chan.exec_command(cmd)
                            try:
                                first = chan.recv(4096)
                            except socket.timeout:
                                first = b""
                        except (paramiko.SSHException, OSError) as e:
                            client.close()
                            refused.append(p)
                            last_err = e
                            chan = None
                            cmds = []
                            break
                        if busy_reply(first):
                            chan.close()
                            chan = None
                            busy.append(lp or p)
                            last_err = LinkError("линия Э-60 занята")
                            continue
                        if (not first and chan.exit_status_ready()
                                and chan.recv_exit_status() != 0):
                            # команды нет в PATH — пробуем следующий вариант
                            chan.close()
                            chan = None
                            last_err = LinkError(f"exec «{cmd}» не удалось")
                            continue
                        self.line_port = lp
                        break
                    if chan is not None:
                        break
                    if not cmds:        # транспорт/сессия умерли — этот
                        break           # ssh-порт уже занесён в refused
                if chan is None:
                    client.close()
                    if isinstance(last_err, LinkError) and "занята" in str(
                            last_err):
                        busy.append(p)
                    else:
                        refused.append(p)
                    continue
            chan.settimeout(0.2)
            self.client, self.chan, self.port = client, chan, p
            if first:
                self._pending = self._iac.feed(first)
            time.sleep(0.3)
            return
        if busy and not refused:
            raise LinkError(f"все линии заняты ЭВМ ({spec_hint(ports)})"
                            + (f": {last_err}" if last_err else ""))
        raise _exhausted(host, ports, busy, refused)

    def _forward_scan(self, paramiko, client, transport, line_spec,
                      timeout, probe, busy):
        """Перебор direct-tcpip каналов к 127.0.0.1:<порт> линии.

        Возврат (chan, first, err): chan открыт; first — баннер; err —
        причина. first == b"" — маркер «туннель разрешён, но свободных
        линий нет» (на этот SSH-порт повторять бессмысленно); first is
        None с err == None — туннель запрещён, нужен откат на exec."""
        refused = opened_busy = opened_refused = False
        for lp in parse_port_spec(line_spec):
            try:
                chan = transport.open_channel(
                    "direct-tcpip", ("127.0.0.1", lp), ("127.0.0.1", 0),
                    timeout=timeout)
            except paramiko.SSHException as e:
                # «administratively prohibited» — туннель запрещён сервером
                # (или это ssh_bridge.py, принимающий только session) →
                # откат на exec; «connect failed/refused» — на линии нет
                # слушателя, пробуем следующую
                low = str(e).lower()
                if "prohib" in low or " refused)" in low:
                    refused = True
                    break
                opened_refused = True
                continue
            except OSError:
                opened_refused = True
                continue
            try:
                chan.settimeout(probe)
                first = chan.recv(4096)
            except socket.timeout:
                first = b""
            except OSError as e:
                chan.close()
                opened_busy = True
                continue
            if busy_reply(first):
                chan.close()
                busy.append(lp)
                opened_busy = True
                continue
            self.line_port = lp
            return chan, first, None
        if refused:                     # туннель запрещён — на мост через exec
            return None, None, None
        if opened_busy:
            return None, None, LinkError("линии Э-60 заняты ЭВМ")
        if opened_refused:
            return None, b"", LinkError("нет слушателя на линиях")
        return None, b"", LinkError("туннель не открылся ни на одну линию")

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

    banner = False      # баннера tmxr тут нет — «ПРОВЕРЬ ЛИНИЮ!» не показываем

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
        line_spec = ""
        if "?" in rest:
            rest, _, query = rest.partition("?")
            for kv in query.split("&"):
                k, _, v = kv.partition("=")
                if k == "port":
                    line_spec = v
        auth, _, hostport = rest.rpartition("@")
        host, _, port = hostport.partition(":")
        return SSHLink(host or "127.0.0.1", port or 22,
                       user=auth or kw.get("user", "ie15"),
                       password=kw.get("password"),
                       keyfile=kw.get("keyfile"),
                       line_spec=line_spec or None)
    if url.startswith("stdio://"):
        return StdioLink()
    raise LinkError(f"неизвестная схема линии: {url}")