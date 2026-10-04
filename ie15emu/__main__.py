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
import sys
import time
from pathlib import Path

from . import COLS, ROWS, __version__
from .charset import Charset
from .keyboard import key_to_bytes
from .link import open_link
from .parser import Parser
from .render import to_png_bytes, to_text
from .session import TerminalSession

ROM_DEFAULT = Path(__file__).resolve().parent.parent / "rom"


def koi7(text: str) -> bytes:
    """Unicode-строка → 7-разрядные коды ВЗУ терминала (КОИ7 Н1).

    Заглавные — 0x00..0x1E, строчные — 0x60..0x7E (как в ГОСТ 27463-87).
    """
    from .charset import RUS7
    out = bytearray()
    for ch in text:
        up = ch.upper()
        if up in RUS7:
            code = RUS7.index(up)
            out.append(code | 0x60 if ch.islower() else code)
        else:
            out.append(ord(ch) & 0x7F)
    return bytes(out)


DEMO_SCRIPT = (
    b"\x1bE",                                   # стирание + курсор в «дом»
    koi7("электроника 15ие-00-013\n\r"),
    koi7("фрязинский дисплей 80x25\n\r"),
    b"\x1bY5\x28" + koi7(">> "),                # курсор в позицию (5;8)
    koi7("готов к работе\n\r"),
)


def run_script(parser: Parser, png: str | None) -> None:
    sc = parser.screen
    sc.set_service("М1=ГОТОВНОСТЬ  М2=ВВОД  М3=КАНАЛ ОК  ПРН")
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


def run_line(parser: Parser, url: str, png: str | None,
             seconds: float, feed: str | None, mode: str = "host",
             koi7: bool = False, **linkkw) -> None:
    import os
    import select
    import termios
    import tty

    from .keyboard import decode_key_bytes

    link = open_link(url, **linkkw)
    session = TerminalSession(parser, link=link, mode=mode, koi7=koi7)
    print(f"[линия] {url} — подключено (режим: {mode})")
    print("[клавиши] ввод — в линию/буфер; F10 — SEND, F11 — АВТОНОМНО↔С ЭВМ, Ctrl-C — выход")
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
            if fd is not None and fd in ready:
                chunk = os.read(fd, 256)
                for key in decode_key_bytes(chunk):
                    session.emit(session.feed_key(key))
            data = link.recv()
            if data:
                session.handle_line_reply(data)
                sys.stdout.write(text_screen(parser))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
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
    """Псевдо-ЭЛТ: текстовый вывод содержимого 80×25."""
    sc = parser.screen
    lines = ["".join(chr(c) if 0x20 <= c < 0x7F else "·" for c in row)
             for row in sc.cells]
    lines.append("-" * COLS)
    lines.append("".join(sc.service))
    return "\r\n\x1b[H\x1b[2J" + "\r\n".join(lines) + "\r\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ie15emu",
                                 description="Эмулятор терминала 15ИЭ-00-013")
    ap.add_argument("--line", help="tcp://host:port | ssh://user@host:port | stdio://")
    ap.add_argument("--script", choices=["demo"],
                    help="без сети: прогнать встроенный скрипт")
    ap.add_argument("--feed", help="строка, отправить в линию после подключения")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="длительность сеанса (0 — до Ctrl-C)")
    ap.add_argument("--png", help="сохранить итоговый экран в PNG")
    ap.add_argument("--roms", default=str(ROM_DEFAULT))
    ap.add_argument("--password", help="пароль для ssh://-линии")
    ap.add_argument("--keyfile", help="ключ для ssh://-линии")
    ap.add_argument("--koi7", action="store_true",
                    help="раскладка клавиатуры КОИ7 (ЙЦУКЕН)")
    ap.add_argument("--mode", choices=["local", "host"], default="host",
                    help="автономная работа (local) или работа с ЭВМ (host, "
                         "по умолчанию); клавиша F10 — SEND, F11 — смена режима")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    charset = Charset(Path(args.roms) / "chargen-15ie.bin")
    parser = Parser(rus_letters=args.koi7 or not args.line)
    parser.charset = charset

    if args.line:
        run_line(parser, args.line, args.png, args.seconds,
                 args.feed, mode=args.mode, koi7=args.koi7,
                 password=args.password, keyfile=args.keyfile)
    else:
        run_script(parser, args.png)


if __name__ == "__main__":
    main()