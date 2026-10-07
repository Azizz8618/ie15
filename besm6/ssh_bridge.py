#!/usr/bin/env python3
"""SSH-мост «терминал 15ИЭ ↔ линия TTY эмулятора БЭСМ-6».

Настоящий терминал включался в токовую петлю 20 мА (или С2). Здесь мост
даёт ту же роль поверх SSH: SSH-exec-сессия «terminal» прозрачно
прокидывается в telnet-линию `attach ttyN <порт>` эмулятора БЭСМ-6.

Запуск (на машине с БЭСМ-6):
    python3 ssh_bridge.py --port 2222 --target 127.0.0.1:4202 \\
        --hostkey host_ed25519_key --user ie15 --password ie15

Целью можно задать несколько линий — списком или диапазоном; каждая
сессия берёт первую свободную (занятая линия отвечает
«Line connection busy» и закрывается, мост переходит к следующей):
    python3 ssh_bridge.py --target 127.0.0.1:4199-4223 ...

Подключение эмулятора 15ИЭ:
    python3 -m ie15emu --line ssh://ie15@<host>:2222 --password ie15
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading

try:
    import paramiko
except ImportError:
    sys.exit("нужен paramiko: pip install paramiko")


def pump(src, dst, strip_iac: bool = False, filt=None) -> None:
    try:
        filt = filt if filt is not None else (IacFilter() if strip_iac else None)
        while True:
            data = src.recv(4096)
            if not data:
                break
            if filt:
                data = filt.feed(data)
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for closer in (src, dst):
            try:
                closer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


IAC, DONT, DO, WONT, WILL, SB, SE = 0xFF, 254, 253, 252, 251, 250, 240

# ответ SIMH на вход в уже занятую линию (mux/DKS) — за ним сокет закрывается
BUSY_MARKERS = (b"connection busy", b"connection not available",
                b"line busy", b"no free terminal")


def parse_ports(spec: str) -> list[int]:
    """«4202», «4202,4210» или «4202-4223» → упорядоченный список портов."""
    ports: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            ports.extend(range(int(a), int(b) + 1))
        else:
            ports.append(int(part))
    return list(dict.fromkeys(ports))


def connect_free_target(host: str, port_spec: str, timeout: float = 10.0,
                        probe: float = 2.0):
    """Первое свободное telnet-линия БЭСМ-6 из host:port_spec.

    Обходит порты по порядку: недоступный ( refused/timeout ) и занятый
    (SIMH отвечает «Line connection busy» и закрывает сокет) пропускаются.
    Возвращает (socket, первые_прочитанные_байты); байты — баннер линии,
    их надо переслать терминалу, иначе «Encoding is …» потеряется.
    """
    last_err = None
    for p in parse_ports(port_spec):
        try:
            s = socket.create_connection((host, p), timeout)
        except OSError as e:
            last_err = e
            continue
        first = b""
        try:
            s.settimeout(probe)
            first = s.recv(4096)
        except socket.timeout:
            first = b""
        except OSError as e:
            s.close()
            last_err = e
            continue
        low = first.lower()
        if any(m in low for m in BUSY_MARKERS):
            s.close()
            last_err = OSError(f"порт {p}: линия занята")
            continue
        return s, first
    raise OSError(f"нет свободной линии на {host}:{port_spec} ({last_err})")


class IacFilter:
    """Убрать telnet-IAC (RFC 854): мост работает на уровне терминала.

    Состояние переживает границы recv(): последовательность IAC может
    быть разрезана между кусками.
    """

    def __init__(self) -> None:
        self.state = "data"          # data | cmd | opt | sb | sb_iac

    def feed(self, buf: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if self.state == "data":
                if b == IAC:
                    self.state = "cmd"
                else:
                    out.append(b)
            elif self.state == "cmd":
                if b in (WILL, WONT, DO, DONT):
                    self.state = "opt"
                elif b == SB:
                    self.state = "sb"
                elif b == IAC:
                    self.state = "data"
                else:
                    self.state = "data"
            elif self.state == "opt":
                self.state = "data"
            elif self.state == "sb":
                if b == IAC:
                    self.state = "sb_iac"
            else:                    # sb_iac
                self.state = "data" if b == SE else "sb"
            i += 1
        return bytes(out)


class BridgeServer(paramiko.ServerInterface):
    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self.event = threading.Event()

    def check_auth_password(self, username, password):
        if username == self.user and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == self.user else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_channel_exec_request(self, channel, command):
        # paramiko передаёт команду как bytes (Message.get_string)
        if isinstance(command, bytes):
            command = command.decode("utf-8", "replace")
        if command.strip() in ("terminal", "ie15", ""):
            self.event.set()
            return True
        return False

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def handle_client(client: socket.socket, args) -> None:
    transport = paramiko.Transport(client)
    transport.add_server_key(paramiko.RSAKey(filename=args.hostkey)
                             if args.hostkey.endswith("rsa")
                             else paramiko.Ed25519Key(filename=args.hostkey))
    server = BridgeServer(args.user, args.password)
    try:
        transport.start_server(server=server)
    except paramiko.SSHException:
        client.close()
        return
    channel = transport.accept(30)
    if channel is None:
        client.close()
        return
    server.event.wait(30)
    host, _, port_spec = args.target.rpartition(":")
    try:
        upstream, first = connect_free_target(host, port_spec)
    except OSError as e:
        channel.send(f"БЭСМ-6 линия недоступна: {e}\r\n".encode())
        channel.close()
        transport.close()
        return
    print(f"[мост] сессия от {client.getpeername()} → {host}:{upstream.getpeername()[1]}",
          flush=True)
    # IAC-неговацию шлёт SIMH (upstream), её и вырезаем; терминал→SIMH идёт
    # чистый КОИ7/UTF-8 без 0xFF. Тот же фильтр продолжает уже прочитанное
    # при разведке начало баннера.
    up_filter = IacFilter()
    if first:
        head = up_filter.feed(first)
        if head:
            channel.sendall(head)
    t1 = threading.Thread(target=pump, args=(channel, upstream, False), daemon=True)
    t2 = threading.Thread(target=pump, args=(upstream, channel, True, up_filter),
                          daemon=True)
    t1.start()
    t2.start()
    t2.join()
    channel.close()
    transport.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--target", default="127.0.0.1:4202",
                    help="telnet-линия БЭСМ-6 (host:port), порт — число, "
                         "список «4202,4210» или диапазон «4202-4223»; "
                         "на каждой сессии берётся первая свободная линия")
    ap.add_argument("--hostkey", required=True, help="файл ключа SSH-сервера")
    ap.add_argument("--user", default="ie15")
    ap.add_argument("--password", default="ie15")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.listen, args.port))
    sock.listen(5)
    print(f"[мост] SSH {args.listen}:{args.port} → {args.target} "
          f"(пользователь {args.user})", flush=True)
    while True:
        client, addr = sock.accept()
        threading.Thread(target=handle_client, args=(client, args),
                         daemon=True).start()


if __name__ == "__main__":
    main()