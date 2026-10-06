"""CLI эмулятора терминала «Электроника 15ИЭ-00-013».

Примеры:
  # отладка экрана без сети (скрипт событий)
  python3 -m ie15emu --script demo --png shots/demo.png

  # терминал к линии tty SIMH БЭСМ-6 напрямую (токовая петля поверх TCP)
  python3 -m ie15emu --line tcp://127.0.0.1:4202 --png shots/besm6.png

  # тот же терминал, но через SSH-канал (см. besm6/ssh_bridge.py)
  python3 -m ie15emu --line ssh://ie15@127.0.0.1:2222 --password ie15
"""
from __future__ import annotations

import argparse
import configparser
import sys
import time
from pathlib import Path

from . import COLS, ROWS, __version__
from .charset import Charset, encode_koi7
from .keyboard import key_to_bytes
from .link import LinkError, open_link
from .parser import Parser
from .render import to_png_bytes, to_text
from .session import TerminalSession

ROM_DEFAULT = Path(__file__).resolve().parent.parent / "rom"
CONF_DEFAULT = ROM_DEFAULT.parent / "ie15.conf"


def load_conf(path: str | None = None) -> configparser.ConfigParser:
    """Читает ie15.conf (рядом с пакетом, если путь не указан)."""
    cp = configparser.ConfigParser()
    p = Path(path).expanduser() if path else CONF_DEFAULT
    if p.is_file():
        cp.read(p, encoding="utf-8")
    return cp


def line_url_from_conf(cp: configparser.ConfigParser) -> str | None:
    """URL линии из секции [line]: ssh://user@server:ssh_port | tcp://server:port."""
    server = cp.get("line", "server", fallback="") if cp.has_section("line") else ""
    if not server:
        return None
    port = cp.get("line", "port", fallback="4202")
    if cp.get("line", "type", fallback="tcp").lower() == "ssh":
        user = cp.get("ssh", "user", fallback="ie15")
        sport = cp.get("line", "ssh_port", fallback="2222")
        return f"ssh://{user}@{server}:{sport}"
    return f"tcp://{server}:{port}"


def apply_conf(ns, cp: configparser.ConfigParser):
    """Значения из конфига туда, где CLI-аргумент не задан (None/false)."""
    if ns.line is None and not getattr(ns, "script", None):
        ns.line = line_url_from_conf(cp)
    if ns.mode is None:
        ns.mode = cp.get("terminal", "mode", fallback="host") or "host"
    if ns.sets is None:
        ns.sets = cp.get("terminal", "sets", fallback="2") or "2"
    if not ns.koi7:
        ns.koi7 = cp.getboolean("terminal", "koi7", fallback=False)
    if ns.password is None:
        ns.password = cp.get("ssh", "password", fallback="") or None
    if ns.keyfile is None:
        kf = cp.get("ssh", "keyfile", fallback="")
        kf = str(Path(kf).expanduser()) if kf else ""
        # закрытый ключ по умолчанию — только если файл существует
        ns.keyfile = kf if kf and Path(kf).is_file() else None
    return ns


def koi7(text: str) -> bytes:
    """Unicode-строка → внутренний КОИ7 Н1 поток (см. charset.encode_koi7)."""
    return encode_koi7(text)


DEMO_SCRIPT = (
    b"\x1bE",                                   # стирание + курсор в «дом»
    koi7("электроника 15ие-00-013\n\r"),
    koi7("фрязинский дисплей 80х25\n\r"),
    b"\x1bY5\x28" + koi7(">> "),                # курсор в позицию (5;8)
    koi7("готов к работе\n\r"),
)


def run_script(parser: Parser, png: str | None) -> None:
    sc = parser.screen
    sc.set_service("АВТОНОМНО РЕЖИМ 2=VT52 ЛИНИЯ=ДЕМО АЛФ=Н1"
                   " ВИДЕО=НОРМ ПЕРЕДАЧА=ПУСТО")
    for chunk in DEMO_SCRIPT:
        parser.feed(chunk)
    parser.feed(b"\x1bY8\x20")  # курсор к служебной строке (поз. 25)
    print(to_text(sc, parser.charset))
    if png:
        _write_png(png, parser)


def _write_png(path: str, parser: Parser) -> None:
    from .render import to_png_bytes
    charset = parser.charset
    to_png_bytes(parser.screen, charset, path)
    print(f"[png] {path}")


def _drain_banner(session, timeout: float = 2.0) -> None:
    """Считать вступление линии (баннер «Encoding is …») до первого SEND.

    Только для сетевых линий: StdioLink.recv() блокирует до ввода.
    """
    deadline = time.time() + timeout
    while not session.enc_decided and time.time() < deadline:
        try:
            data = session.link.recv()
        except (OSError, LinkError):
            return
        if data:
            session.handle_line_reply(data)
        else:
            time.sleep(0.05)


def run_line(parser: Parser, url: str, png: str | None,
             seconds: float, feed: str | None, mode: str = "host",
             koi7: bool = False, **linkkw) -> None:
    import os
    import select
    import termios
    import tty

    from .keyboard import decode_key_bytes

    try:
        link = open_link(url, **linkkw)
    except (LinkError, OSError) as e:
        print(f"[линия] {url} — не удалось подключиться: {e}")
        return
    session = TerminalSession(parser, link=link, mode=mode, koi7=koi7)
    print(f"[линия] {url} — подключено (режим: {mode})")
    print("[клавиши] ввод — в линию/буфер; F8 — РЕЖИМ (набор №1↔№2), "
          "F9 — АВТОНОМНО↔С ЭВМ, F10 — SEND, Ctrl-C — выход")
    # До отправки первого текста читаем баннер SIMH («Encoding is …»),
    # чтобы направление передачи совпало с кодировкой линии.
    if url.startswith(("tcp://", "ssh://")):
        _drain_banner(session, timeout=2.0)
    if feed:
        session.emit(key_to_bytes(feed, koi7=koi7))

    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old = termios.tcgetattr(fd) if fd is not None else None
    if fd is not None:
        tty.setcbreak(fd)
    t0 = time.time()
    try:
        while seconds <= 0 or time.time() - t0 < seconds:
            fds = [link_sock(link)] if link_sock(link) is not None else []
            if fd is not None:
                fds.append(fd)
            if fds:
                ready, _, _ = select.select(fds, [], [], 0.05)
            else:
                ready, _ = [], None
                time.sleep(0.05)
            dirty = False
            if fd is not None and fd in ready:
                chunk = os.read(fd, 256)
                for key in decode_key_bytes(chunk):
                    session.emit(session.feed_key(key))
                    dirty = True       # АВТОНОМНО: эхо в ОЗУ без ответа линии
            data = link.recv()
            if data:
                session.handle_line_reply(data)
                dirty = True
            if dirty:
                sys.stdout.write(text_screen(parser))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    except LinkError as e:            # ЭВМ сбросила линию — не трейсбек, а выход
        sys.stdout.write("\x1b[0m\r\n")
        print(f"[линия] {e} — сеанс завершён")
    finally:
        if old is not None and fd is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        link.close()
    if png:
        _write_png(png, parser)


def link_sock(link):
    """Дескриптор линии для select (если он есть)."""
    sock = getattr(link, "sock", None)
    if sock is not None:
        return sock.fileno()
    chan = getattr(link, "chan", None)
    if chan is not None and hasattr(chan, "recv_ready"):
        return None          # SSH-канал опрашивается неблокирующе
    btn = getattr(link, "_in", None)
    return btn.fileno() if btn is not None else None


def text_screen(parser: Parser) -> str:
    """Псевдо-ЭЛТ: текстовый вывод содержимого 80×25 (коды КОИ7 → буквы).

    Курсор показывается инверсным блоком — как мигающий квадратик
    на настоящем дисплее.
    """
    from .charset import decode_koi7

    sc = parser.screen
    lines = [decode_koi7(bytes(row)) for row in sc.cells]
    if sc.y < len(lines) and sc.x < COLS:
        row = list(lines[sc.y])
        row[sc.x] = f"\x1b[7m{row[sc.x]}\x1b[27m"
        lines[sc.y] = "".join(row)
    lines.append("-" * COLS)
    lines.append("".join(sc.service))
    return "\r\n\x1b[H\x1b[2J" + "\r\n".join(lines) + "\r\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ie15emu",
                                 description="Эмулятор терминала 15ИЭ-00-013")
    ap.add_argument("--conf", default=str(CONF_DEFAULT),
                    help="конфигурационный файл (по умолчанию ie15.conf "
                         "рядом с пакетом)")
    ap.add_argument("--line", default=None,
                    help="tcp://host:port | ssh://user@host:port | stdio:// "
                         "(по умолчанию — из секции [line] конфига)")
    ap.add_argument("--script", choices=["demo"],
                    help="без сети: прогнать встроенный скрипт")
    ap.add_argument("--feed", help="строка, отправить в линию после подключения")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="длительность сеанса (0 — до Ctrl-C)")
    ap.add_argument("--png", help="сохранить итоговый экран в PNG")
    ap.add_argument("--roms", default=str(ROM_DEFAULT))
    ap.add_argument("--password", default=None,
                    help="пароль для ssh://-линии (иначе из [ssh])")
    ap.add_argument("--keyfile", default=None,
                    help="закрытый ключ ssh (иначе keyfile из [ssh])")
    ap.add_argument("--koi7", action="store_true",
                    help="раскладка клавиатуры КОИ7 (ЙЦУКЕН)")
    ap.add_argument("--mode", choices=["local", "host"], default=None,
                    help="автономная работа (local) или работа с ЭВМ (host, "
                         "по умолчанию); F8 — режим наборов №1/№2, "
                         "F9 — смена режима, F10 — SEND")
    ap.add_argument("--sets", choices=["1", "2"], default=None, type=str,
                    help="набор команд терминала при запуске: №1 (только УП) "
                         "или №2 (VT52, по умолчанию — как на линии БЭСМ-6)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    args = apply_conf(args, load_conf(args.conf))
    args.mode = args.mode or "host"
    args.sets = args.sets or "2"

    charset = Charset(Path(args.roms) / "chargen-15ie.bin")
    parser = Parser(mode=int(args.sets))
    parser.charset = charset

    if args.line:
        run_line(parser, args.line, args.png, args.seconds,
                 args.feed, mode=args.mode, koi7=args.koi7,
                 password=args.password, keyfile=args.keyfile)
    else:
        run_script(parser, args.png)


if __name__ == "__main__":
    main()