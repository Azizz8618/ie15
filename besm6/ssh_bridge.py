#!/usr/bin/env python3
"""SSH-мост «терминал 15ИЭ ↔ линия TTY эмулятора БЭСМ-6».

Настоящий терминал включался в токовую петлю 20 мА (или С2). Здесь мост
даёт ту же роль поверх SSH: SSH-exec-сессия «terminal» прозрачно
прокидывается в telnet-линию `attach ttyN <порт>` эмулятора БЭСМ-6.

Запуск (на машине с БЭСМ-6):
    python3 ssh_bridge.py --port 2222 --target 127.0.0.1:4202 \\
        --hostkey host_ed25519_key --user ie15 --password ie15

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


def pump(src, dst, strip_iac: bool = False) -> None:
    try:
        filt = IacFilter() if strip_iac else None
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
    host, _, port = args.target.rpartition(":")
    try:
        upstream = socket.create_connection((host, int(port)), 10)
    except OSError as e:
        channel.send(f"БЭСМ-6 линия недоступна: {e}\r\n".encode())
        channel.close()
        transport.close()
        return
    print(f"[мост] сессия от {client.getpeername()} → {args.target}", flush=True)
    # IAC-неговацию шлёт SIMH (upstream), её и вырезаем; терминал→SIMH идёт
    # чистый КОИ7/UTF-8 без 0xFF.
    t1 = threading.Thread(target=pump, args=(channel, upstream, False), daemon=True)
    t2 = threading.Thread(target=pump, args=(upstream, channel, True), daemon=True)
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
                    help="telnet-линия БЭСМ-6 (host:port)")
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