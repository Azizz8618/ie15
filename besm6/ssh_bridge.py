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
        while True:
            data = src.recv(4096)
            if not data:
                break
            if strip_iac:
                data = drop_iac(data)
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for closer in (src, dst):
            try:
                closer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def drop_iac(buf: bytes) -> bytes:
    """Убрать telnet-IAC (RFC 854): мост работает на уровне терминала."""
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
    try:
        upstream = socket.create_connection(
            tuple(args.target.rsplit(":", 1)[0:1] + (int(args.target.rsplit(":", 1)[1]),)), 10)
    except OSError as e:
        channel.send(f"БЭСМ-6 линия недоступна: {e}\r\n".encode())
        channel.close()
        transport.close()
        return
    print(f"[мост] сессия от {client.getpeername()} → {args.target}", flush=True)
    t1 = threading.Thread(target=pump, args=(channel, upstream, True), daemon=True)
    t2 = threading.Thread(target=pump, args=(upstream, channel, False), daemon=True)
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