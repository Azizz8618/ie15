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

СОСТОЯНИЕ МОСТА. Кроме канала линии мост отвечает на exec-команду
«status» (он же «ping»): краткий отчёт и закрытие канала. Отчёт нужен
терминалу — он спрашивает состояние регулярно (см. ie15emu/link.py,
SSHLink.probe): ответ доказывает, что мост жив, а строка
«ЛИНИЯ=<порт>:МЕРТВА» — что линия Э-60 умерла, хотя SSH-соединение
ещё держится. Без такой проверки связь может пропасть «молча», а в
строке состояния так и будет гореть «ЛИНИЯ=ДКС».

Формат отчёта — строки «КЛЮЧ=ЗНАЧЕНИЕ» (CRLF, UTF-8):
    МОСТ=ЖИВ
    СЕССИЙ=1
    ЛИНИЯ=4202:ЖИВА
Запрос состояния обслуживается и отдельным соединением, и вторым
каналом того же соединения (терминал спрашивает по второму — дешевле).
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

try:
    import paramiko
except ImportError:
    sys.exit("нужен paramiko: pip install paramiko")


def pump(src, dst, strip_iac: bool = False, filt=None) -> None:
    try:
        filt = filt if filt is not None else (IacFilter() if strip_iac else None)
        while True:
            try:
                data = src.recv(4096)
            except socket.timeout:
                continue          # тишина в линии — ждём дальше
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

# команды exec-канала: «terminal» — линия Э-60, «status» — состояние моста
CMD_TERMINAL = "terminal"
CMD_STATUS = "status"


def parse_ports(spec: str) -> list[int]:
    """«4202», «4202,4210» или «4202-4223» → упорядоченный список портов."""
    ports: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if lo > hi:
                continue
            ports.extend(range(lo, hi + 1))
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
        # после разведки сокет оставляем с коротким таймаутом: насос
        # (pump) переживает тишину в линии и ждёт следующих байтов
        s.settimeout(1.0)
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


# --- состояние моста ----------------------------------------------------
class BridgeConn:
    """Состояние одного SSH-соединения с мостом.

    line_port — порт линии Э-60, за которой стоит соединение; alive —
    жива ли сама линия (мост её видит и может за ней следить).
    """

    def __init__(self, peer: str = ""):
        self.peer = peer
        self.line_port: int | None = None
        self.alive = False
        self.since = 0.0

    def age(self) -> float:
        """Сколько секунд жива линия этого соединения."""
        return max(0.0, time.time() - self.since) if self.since else 0.0


_CONNS: list[BridgeConn] = []
_CONNS_LOCK = threading.Lock()


def conn_add(conn: BridgeConn) -> None:
    with _CONNS_LOCK:
        _CONNS.append(conn)


def conn_del(conn: BridgeConn) -> None:
    with _CONNS_LOCK:
        if conn in _CONNS:
            _CONNS.remove(conn)


def status_report(conn: BridgeConn | None = None) -> bytes:
    """Отчёт о состоянии моста (ответ на «status»).

    Строки «КЛЮЧ=ЗНАЧЕНИЕ»: МОСТ=ЖИВ, СЕССИЙ=N и по строке на линию —
    «ЛИНИЯ=<порт>:ЖИВА/МЕРТВА».
    """
    with _CONNS_LOCK:
        conns = list(_CONNS)
    lines = ["МОСТ=ЖИВ", f"СЕССИЙ={len(conns)}"]
    for c in conns:
        if c.line_port is None:
            continue
        state = "ЖИВА" if c.alive else "МЕРТВА"
        mine = " (моя)" if c is conn else ""
        lines.append(f"ЛИНИЯ={c.line_port}:{state}{mine}")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


class BridgeServer(paramiko.ServerInterface):
    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self.event = threading.Event()
        self.line_spec = None       # «terminal 4202-4223» от терминала
        # команда exec-запроса по каналу: канал → «terminal»/«status»
        self.cmds: dict[int, str] = {}

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
        words = command.strip().split()
        head = words[0].lower() if words else ""
        # «terminal», «ie15», «» — цель по умолчанию; «terminal <порт>»
        # (число, список или диапазон) — перебирать эти ДКС-линии
        if head in (CMD_TERMINAL, "ie15"):
            self.line_spec = words[1] if len(words) > 1 else None
            self.cmds[channel.get_id()] = CMD_TERMINAL
            self.event.set()
            return True
        # «status» (он же «ping») — отчёт о состоянии моста; линии не касаемся
        if head in (CMD_STATUS, "ping", "мост"):
            self.cmds[channel.get_id()] = CMD_STATUS
            self.event.set()
            return True
        if command.strip() == "":
            self.line_spec = None
            self.cmds[channel.get_id()] = CMD_TERMINAL
            self.event.set()
            return True
        return False

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def wait_cmd(server: BridgeServer, channel, timeout: float = 10.0) -> str:
    """Команда exec-запроса канала («terminal»/«status»).

    Запрос приходит отдельным вызовом (check_channel_exec_request) сразу
    после открытия канала, но не одновременно с ним — ждём появления.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        kind = server.cmds.pop(channel.get_id(), None)
        if kind:
            return kind
        time.sleep(0.05)
    return ""


def answer_status(channel, conn: BridgeConn | None) -> None:
    """Ответить на запрос состояния и закрыть канал."""
    try:
        channel.sendall(status_report(conn))
        channel.send_exit_status(0)
    except OSError:
        pass
    finally:
        try:
            channel.close()
        except OSError:
            pass


def status_loop(transport, server: BridgeServer, conn: BridgeConn) -> None:
    """Обслуживать запросы состояния — вторыми каналами соединения.

    Терминал спрашивает мост по ходу сеанса: канал линии уже занят,
    состояние спрашивается отдельным каналом того же SSH-соединения.
    """
    while True:
        try:
            channel = transport.accept(3600)
        except Exception:
            return
        if channel is None:
            return
        kind = wait_cmd(server, channel, timeout=10)
        if kind == CMD_STATUS:
            answer_status(channel, conn)
            continue
        # не status — линия уже обслуживается, лишний канал закрываем
        try:
            channel.close()
        except OSError:
            pass


def handle_client(client: socket.socket, args) -> None:
    transport = paramiko.Transport(client)
    transport.add_server_key(paramiko.RSAKey(filename=args.hostkey)
                             if args.hostkey.endswith("rsa")
                             else paramiko.Ed25519Key(filename=args.hostkey))
    server = BridgeServer(args.user, args.password)
    conn = BridgeConn(peer=str(client.getpeername()))
    conn_add(conn)
    try:
        transport.start_server(server=server)
    except paramiko.SSHException:
        conn_del(conn)
        client.close()
        return
    try:
        _serve(transport, server, conn, args, client)
    finally:
        conn_del(conn)
        try:
            transport.close()
        except Exception:
            pass
        client.close()


def _serve(transport, server, conn, args, client) -> None:
    """Канал линии (и запросы состояния) одного SSH-соединения."""
    while True:
        channel = transport.accept(30)
        if channel is None:
            return
        kind = wait_cmd(server, channel, timeout=10)
        if kind == CMD_STATUS:
            # соединение только для спроса: отвечаем и ждём следующего канала
            answer_status(channel, conn)
            continue
        if kind != CMD_TERMINAL:
            return                      # неизвестная команда — соединение закрываем
        host, _, port_spec = args.target.rpartition(":")
        if server.line_spec:                   # терминал сам задал канал(ы)
            port_spec = server.line_spec
        try:
            upstream, first = connect_free_target(host, port_spec)
        except OSError as e:
            # линии за этим каналом нет (занята/недоступна) — сообщаем и
            # ждём следующего канала: терминал попросит другую линию, а
            # SSH-соединение для этого рвать нельзя
            channel.send(f"БЭСМ-6 линия недоступна: {e}\r\n".encode())
            channel.close()
            continue
        conn.line_port = upstream.getpeername()[1]
        conn.alive = True
        conn.since = time.time()
        print(f"[мост] сессия от {client.getpeername()} → "
              f"{host}:{conn.line_port}", flush=True)
        # IAC-неговацию шлёт SIMH (upstream), её и вырезаем; терминал→SIMH идёт
        # чистый КОИ7/UTF-8 без 0xFF. Тот же фильтр продолжает уже прочитанное
        # при разведке начало баннера.
        up_filter = IacFilter()
        if first:
            head = up_filter.feed(first)
            if head:
                channel.sendall(head)
        t1 = threading.Thread(target=pump, args=(channel, upstream, False),
                              daemon=True)
        t2 = threading.Thread(target=pump, args=(upstream, channel, True, up_filter),
                              daemon=True)
        t1.start()
        t2.start()
        # запросы состояния — вторыми каналами того же соединения
        threading.Thread(target=status_loop, args=(transport, server, conn),
                         daemon=True).start()
        t2.join()
        conn.alive = False
        channel.close()
        return


def stat_loop(args) -> None:
    """Периодическая сводка состояния моста в его журнал.

    Мост виден «глазами» оператора в своей tmux-сессии: сводка показывает,
    жив ли он и какие линии за ним стоят, даже когда терминалов нет.
    """
    while True:
        time.sleep(max(1.0, args.stat))
        with _CONNS_LOCK:
            conns = list(_CONNS)
        lines = []
        for c in conns:
            if c.line_port is None:
                continue
            state = "жива" if c.alive else "мертва"
            lines.append(f"линия {c.line_port} {state} ({int(c.age())} с)")
        print(f"[мост] состояние: сессий {len(conns)}"
              + ("; " + "; ".join(lines) if lines else ""), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--target", default="127.0.0.1:4202-4223",
                    help="telnet-линия БЭСМ-6 (host:port), порт — число, "
                         "список «4202,4210» или диапазон «4202-4223»; "
                         "на каждой сессии берётся первая свободная линия")
    ap.add_argument("--hostkey", required=True, help="файл ключа SSH-сервера")
    ap.add_argument("--user", default="ie15")
    ap.add_argument("--password", default="ie15")
    ap.add_argument("--stat", type=float, default=300.0,
                    help="раз в сколько секунд печатать сводку состояния "
                         "моста в журнал; 0 — не печатать")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.listen, args.port))
    sock.listen(5)
    print(f"[мост] SSH {args.listen}:{args.port} → {args.target} "
          f"(пользователь {args.user})", flush=True)
    if args.stat > 0:
        threading.Thread(target=stat_loop, args=(args,), daemon=True).start()
    while True:
        client, addr = sock.accept()
        threading.Thread(target=handle_client, args=(client, args),
                         daemon=True).start()


if __name__ == "__main__":
    main()
