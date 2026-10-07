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
from .keyboard import DEFAULT_LAYOUT, key_to_bytes
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


def apply_port(url: str | None, spec: str) -> str:
    """Спецификация портов (--port) становится портовой частью URL линии;
    без URL —/tcp://127.0.0.1:<spec> (дефолт стоячего запуска)."""
    if url and url.startswith("stdio"):
        return url                       # локальная линия — порта нет
    if not url or "://" not in url:
        return f"tcp://127.0.0.1:{spec}"
    head, _, rest = url.partition("://")
    pre, colon, last = rest.rpartition(":")
    if colon and (not last or last[0].isdigit() or last[0] == "-"):
        return f"{head}://{pre}:{spec}"        # порт есть — заменяем
    return f"{head}://{rest}:{spec}"           # порта нет — добавляем


def apply_conf(ns, cp: configparser.ConfigParser):
    """Значения из конфига туда, где CLI-аргумент не задан (None/false)."""
    if ns.line is None and not getattr(ns, "script", None):
        ns.line = line_url_from_conf(cp)
    # --port (один/список/диапазон) важнее порта в URL --line/конфига;
    # без URL вовсе — стоячая локальная линия с этим диапазоном
    if getattr(ns, "port", None) and not getattr(ns, "script", None):
        ns.line = apply_port(ns.line, ns.port)
    if ns.mode is None:
        ns.mode = cp.get("terminal", "mode", fallback="host") or "host"
    if ns.sets is None:
        ns.sets = cp.get("terminal", "sets", fallback="2") or "2"
    if not ns.koi7:
        ns.koi7 = cp.getboolean("terminal", "koi7", fallback=False)
    if ns.layout is None:
        lay = cp.get("terminal", "layout", fallback="").strip().lower()
        ns.layout = lay or None    # "" — не задано; "off" снимет разбор в main()
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
    sc.set_service("СЕТЬ=АВТОНОМНО|НАБОР=2(VT52)|ЛИНИЯ=ДЕМО|РАСК=ВЫКЛ\n"
                   "ВИДЕО=НОРМ|БУФЕР=ПУСТО")
    for chunk in DEMO_SCRIPT:
        parser.feed(chunk)
    parser.feed(b"\x1bY8\x20")  # курсор к последнему ряду кадра (поз. 25)
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
             layout: str | None = None, **linkkw) -> None:
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
    session = TerminalSession(parser, link=link, mode=mode, layout=layout)
    print(f"[линия] {url} — подключено (режим: {mode})")
    print("[клавиши] ввод — в линию/буфер; F8 — РЕЖИМ (набор №1↔№2), "
          "F9 — АВТОНОМНО↔С ЭВМ, F10 — SEND, Ctrl-C — выход")
    # До отправки первого текста читаем баннер SIMH («Encoding is …»),
    # чтобы направление передачи совпало с кодировкой линии.
    if url.startswith(("tcp://", "ssh://")):
        _drain_banner(session, timeout=2.0)
    if feed:
        session.emit(key_to_bytes(feed, layout=layout))

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

    Курсор — инверсный блок; образные знаки УП (блинк) мигают, как dim
    на Видеотоне-340.
    """
    from .charset import decode_koi7
    from .screen import ATTR_BLINK

    sc = parser.screen
    lines = []
    for y, row in enumerate(sc.cells):
        toks = []
        for x, code in enumerate(row):
            ch = decode_koi7(bytes([code]))
            if sc.attr[y][x] & ATTR_BLINK:
                # 5 мигающий, 7 инверсия: там, где мигание отключено
                # (большинство современных терминалов), знак остаётся
                # различим инверсным блоком
                ch = f"\x1b[5;7m{ch}\x1b[0m"
            if y == sc.y and x == sc.x:
                ch = f"\x1b[7m{ch}\x1b[27m"
            toks.append(ch)
        lines.append("".join(toks))
    lines.append("-" * COLS)
    lines.append("".join(sc.service).rstrip())
    lines.extend(r.rstrip() for r in sc.service_more)
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
    ap.add_argument("--port", default=None,
                    help="порт линии: один («4202»), список («4202,4210») или "
                         "диапазон («4202-4223»); подставляется в --line/конфиг, "
                         "при занятой линии берётся следующий свободный порт")
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
                    help="включить русскую раскладку (по умолчанию — позиционная)")
    ap.add_argument("--layout", choices=["positional", "phonetic", "off"],
                    default=None, help="русская раскладка: positional — по "
                    "положению ЙЦУКЕН (по умолчанию), phonetic — фонетическая, "
                    "off — выключена")
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
    # Итоговая раскладка: --layout/off явнее всего; иначе --koi7 (или
    # koi7 в конфиге) включает позиционную по умолчанию.
    if args.layout == "off":
        layout = None
    elif args.layout:
        layout = args.layout
    elif args.koi7:
        layout = DEFAULT_LAYOUT
    else:
        layout = None

    charset = Charset(Path(args.roms) / "chargen-15ie.bin")
    parser = Parser(mode=int(args.sets))
    parser.charset = charset

    if args.line:
        run_line(parser, args.line, args.png, args.seconds,
                 args.feed, mode=args.mode, layout=layout,
                 password=args.password, keyfile=args.keyfile)
    else:
        run_script(parser, args.png)


if __name__ == "__main__":
    main()