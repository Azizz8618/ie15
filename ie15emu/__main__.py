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
import os
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
    """Читает ie15.conf (рядом с пакетом, если путь не указан).

    optionxform=str — опции [keys] чувствительны к знаку (это нажатия,
    а не имена настроек); interpolation=None — «%» и «$» в значениях
    читаются как есть."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    p = Path(path).expanduser() if path else CONF_DEFAULT
    if p.is_file():
        cp.read(p, encoding="utf-8")
    return cp


def keymap_from_conf(cp: configparser.ConfigParser) -> dict[str, str]:
    """Переназначение клавиш из [keys]: «что прислал терминал = что
    набирать». Ключ/значение — знак, строка, KEY-имя или код «\\xNN»
    (УП от Ctrl+клавиша); пустое значение гасит клавишу. Если терминал
    говорит по CSI-u (kitty/foot/wezterm/xterm modifyOtherKeys), ключом
    бывает и нажатие с модификатором: «ctrl+Б = <» — тогда сочетания
    не дублируют обычные знаки. Знак «=» ключом не выразить."""
    if not cp.has_section("keys"):
        return {}
    from .keyboard import normalize_combo, unescape_key
    return {normalize_combo(unescape_key(k)): unescape_key(v)
            for k, v in cp.items("keys")}


def line_url_from_conf(cp: configparser.ConfigParser) -> str | None:
    """URL линии из секции [line]: ssh://user@server:ssh_port[?port=...] |
    tcp://server:port.

    Для ssh порт ДКС-линии из [line] port (один/список/диапазон) едет
    параметром ?port= — мост по нему сам перебирает линии Э-60.
    """
    server = cp.get("line", "server", fallback="") if cp.has_section("line") else ""
    if not server:
        return None
    port = cp.get("line", "port", fallback="4202-4223")
    if cp.get("line", "type", fallback="tcp").lower() == "ssh":
        user = cp.get("ssh", "user", fallback="ie15")
        sport = cp.get("line", "ssh_port", fallback="2222")
        url = f"ssh://{user}@{server}:{sport}"
        if str(port).strip() not in (sport, ""):
            url += f"?port={str(port).strip()}"
        return url
    return f"tcp://{server}:{port}"


def say(msg: str) -> None:
    """Сообщение пользователю: перенос по 80 знаков, слова не разрывать."""
    import textwrap
    for ln in textwrap.wrap(msg, width=COLS) or [""]:
        print(ln)


def apply_port(url: str | None, spec: str) -> str:
    """Спецификация портов (--port) становится портовой частью URL линии;
    без URL — tcp://127.0.0.1:<spec> (дефолт стоячего запуска).

    У ssh:// спецификация линий Э-60 живёт в параметре ?port= (порт в URL
    — это порт самого моста), --port заменяет именно его.
    """
    if url and url.startswith("stdio"):
        return url                       # локальная линия — порта нет
    if not url or "://" not in url:
        return f"tcp://127.0.0.1:{spec}"
    head, _, rest = url.partition("://")
    if "?" in rest:                       # «:port?param…» — порт в параметре
        stem, _, tail = rest.partition("?")
        kv = [p for p in tail.split("&") if p and not p.startswith("port=")]
        kv.append("port=" + str(spec))
        out = f"{head}://{stem}"
        if kv:
            out += "?" + "&".join(kv)
        return out
    if head == "ssh":
        # у ssh:порт в URL — это мост; линии Э-60 кладём в ?port=
        return f"{head}://{rest}?port={spec}"
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
    if getattr(ns, "history", None) is None:
        ns.history = cp.get("terminal", "history", fallback="1000") or "1000"
    if getattr(ns, "nabor", None) is None:
        term = "terminal" if cp.has_section("terminal") else None
        nb = cp.get(term, "nabor", fallback="") if term else ""
        if not nb:                       # устаревшие ключи: charset > sets
            ch = getattr(ns, "charset", None) or (
                cp.get(term, "charset", fallback="") if term else "")
            if ch == "n1":
                nb = "n1"
            elif ch == "n0":
                nb = "n0" if ns.sets == "1" else "n2"
            elif ns.sets == "1":
                nb = "n0"
            else:
                nb = "n2"
        ns.nabor = nb
    ns.keymap = keymap_from_conf(cp)
    if not getattr(ns, "mouse", None):
        # мышь терминала отдана эмулятору: колесо листает «историю» выдачи
        # (циклической прокрутки нет); mouse = off — листает сам терминал
        ns.mouse = cp.getboolean("terminal", "mouse", fallback=True)
    if not getattr(ns, "no_help", None):
        # справка подвала (легенда клавиш) по умолчанию видна
        ns.no_help = cp.getboolean("terminal", "help", fallback=True)
    if not getattr(ns, "machine", None):
        # номер ЭВМ в строке состояния над кадром (пусто — не показываем)
        ns.machine = (cp.get("line", "machine", fallback="") or "").strip()
    if not getattr(ns, "fit", None):
        # fit = off (по умолчанию): окно не трогаем — кадр ЭВМ дотягивается
        # до низа окна, подвал занимает минимум рядов; fit = on — эмулятор
        # подгоняет окно ровно на «25 рядов кадра + подвал»
        ns.fit = cp.getboolean("terminal", "fit", fallback=False)
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
    sc.set_service("СЕТЬ=АВТОНОМНО|НАБОР=Н2|ЛИНИЯ=ДЕМО|РАСК=ВЫКЛ\n"
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


def _esc(data: bytes) -> None:
    """Управляющая последовательность мимо текстового буфера stdout."""
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def fit_rows(parser: Parser) -> int:
    """Рядов, на которые подгоняем окно при включённом «fit»: строка
    состояния, черта и пустая строка над кадром, сам кадр 25 рядов, черта
    и ряды подвала.

    По умолчанию окно не трогаем: кадр ЭВМ дотягивается до низа окна сам
    (`Screen.set_rows`), а подвал занимает минимум рядов. «fit» нужен
    только тем, кто хочет окно ровно под «железные» 25 рядов кадра.
    """
    return ROWS + 5 + len(parser.screen.service_more)


class WindowFit:
    """«Правильный масштаб» окна терминала: кадр 25 рядов + подвал.

    Эмулятор просит у терминала размер текстового поля
    (``CSI 8 ; высота ; ширина t`` — размер окна в знаках) и сверяет
    результат по ioctl: терминал без этой последовательности запрос
    проигнорирует — тогда перестаём просить (попытки считаются) и просто
    рисуем кадр с подвалом от верхнего ряда окна.

    К нужному размеру возвращаемся сами: как только от ЭВМ (или по
    нажатию клавиши) пришёл символ, окно подгоняется заново — сбитый
    масштаб шрифта («Ctrl-»/«Ctrl+», перетаскивание границы окна) сам
    собой возвращается к правильному.
    """

    TRIES = 3         # столько попыток подгонки на один «сбой» размера
    RETRY = 0.5       # с, между попытками (окно переезжает не мгновенно)

    def __init__(self) -> None:
        self.pending = True       # размер окна ещё не подогнан
        self.unsupported = False  # терминал размер не держит — не мучаем
        self.tries = 0
        self.last = 0.0
        self.saved: tuple[int, int] | None = None   # размер до подгонки

    def restore(self, parser: Parser) -> bool:
        """Подогнать окно под кадр с подвалом. True — отправили запрос."""
        if self.unsupported:
            return False
        need = fit_rows(parser)
        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            return False
        if rows == need and cols >= COLS:
            self.pending = False
            self.tries = 0
            return False
        if not self.pending:       # окно сбили (масштаб/граница) — начинаем сначала
            self.pending = True
            self.tries = 0
            self.last = 0.0
        if self.tries >= self.TRIES or time.time() - self.last < self.RETRY:
            if self.tries >= self.TRIES:
                self.unsupported = True      # терминал запрос не выполняет
            return False
        self.tries += 1
        self.last = time.time()
        if self.saved is None:
            self.saved = (cols, rows)         # вернём окно по выходе
        _esc(f"\x1b[8;{need};{max(cols, COLS)}t".encode())
        return True

    def restore_saved(self) -> None:
        """Вернуть окно терминала к размеру, какой был до подгонки."""
        if not self.saved:
            return
        cols, rows = self.saved
        self.saved = None
        try:
            cur = os.get_terminal_size()
            if (cur.columns, cur.lines) != (cols, rows):
                _esc(f"\x1b[8;{rows};{cols}t".encode())
        except OSError:
            pass


def _install_winch(fit: WindowFit) -> None:
    """SIGWINCH: окно терминала переехало — масштаб помечаем «сбитым»."""
    try:
        import signal
        signal.signal(signal.SIGWINCH,
                      lambda *_a: setattr(fit, "pending", True))
    except (AttributeError, ImportError, OSError, ValueError):
        pass                              # не POSIX/нет сигнала — не страшно


def run_line(parser: Parser, url: str, png: str | None,
             seconds: float, feed: str | None, mode: str = "host",
             layout: str | None = None, keymap: dict[str, str] | None = None,
             take_mouse: bool = True, take_fit: bool = False,
             machine: str = "", show_help: bool = True,
             **linkkw) -> None:
    import os
    import select
    import termios
    import tty

    from .keyboard import decode_key_bytes

    try:
        link = open_link(url, **linkkw)
    except (LinkError, OSError) as e:
        say(f"[линия] {url} — не удалось подключиться: {e}")
        return
    session = TerminalSession(parser, link=link, mode=mode, layout=layout,
                              keymap=keymap)
    if machine:
        session.machine = str(machine)   # номер ЭВМ — в строке состояния
        session._update_status()
    # справка (ряды-легенды подвала) — по желанию; нижняя строка
    # состояния с полями сеанса остаётся в любом случае
    parser.screen.set_help(show_help)
    say(f"[линия] {url} — подключено (режим: {mode})")
    say("[клавиши] ввод — в линию/буфер; F6 — ЭХО (пароль без вывода), "
        "F8 — РЕЖИМ (набор №1↔№2), F9 — АВТОНОМНО↔С ЭВМ, F10 — SEND, "
        "Ctrl-C — выход")
    # До отправки первого текста читаем баннер SIMH («Encoding is …»),
    # чтобы направление передачи совпало с кодировкой линии.
    if url.startswith(("tcp://", "ssh://")):
        _drain_banner(session, timeout=2.0)
    if feed:
        session.emit(key_to_bytes(feed, layout=layout))

    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old = termios.tcgetattr(fd) if fd is not None else None
    csi_u = False
    mouse = False
    if fd is not None:
        tty.setcbreak(fd)
        # экран собирается абсолютной позицией — настоящая каретка не нужен
        sys.stdout.write("\x1b[?25l")
        # Мышь — приложению: колесо листает «историю» выдачи, а не экран
        # самого терминала (циклической прокрутки нет). Клики приложению
        # не нужны — снимаем отчётность о них, SGR-форма (1006).
        if take_mouse:
            _esc(b"\x1b[?1000h\x1b[?1006h")
            mouse = True
        # Опрос протокола CSI-u: kitty/foot/wezterm/alacritty отвечают
        # «ESC [ ? флаги u» — тогда просим кодировать нажатия с
        # модификаторами отдельно (ctrl+Б перестаёт дублировать знак)
        try:
            sys.stdout.buffer.write(b"\x1b[?u")
            sys.stdout.buffer.flush()
            if select.select([fd], [], [], 0.25)[0]:
                probe = os.read(fd, 32)
                if probe.startswith(b"\x1b[?") and probe.endswith(b"u"):
                    sys.stdout.buffer.write(b"\x1b[>1u")
                    sys.stdout.buffer.flush()
                    csi_u = True
        except OSError:
            pass
        say("[клавиши] " + (
            "CSI-u включён — Ctrl/Alt+клавиша приходит своим именем, "
            "привязки вида «ctrl+Б = <» работают без дублей"
            if csi_u else
            "терминал не ответил на запрос CSI-u — Ctrl+кириллица может "
            "приходить обычным знаком (неотличимо), Alt читается как "
            "ESC+знак и именуется «alt+…»; что прислала клавиша — видно "
            "в подвале (ПОСЛ), этим и привязывают в [keys]"))
    t0 = time.time()
    last_stamp = 0.0
    fit = WindowFit()
    if take_fit:                      # по умолчанию окно не трогаем:
        _install_winch(fit)         # кадр сам дотягивается до низа окна
    dirty = True                         # первый кадр рисуем сразу
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
            now = time.time()
            if now - last_stamp >= 60:  # часы в строке состояния — раз в минуту
                session._update_status()
                last_stamp = now
                dirty = True
            if dirty:
                # «правильный масштаб»: окно ровно на кадр 25 + подвал;
                # проверяем по символу от ЭВМ (и по нажатию) — сбитый
                # масштаб шрифта возвращается сам
                dirty = fit.restore(parser) if take_fit else False
                sys.stdout.write(text_screen(parser))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    except LinkError as e:            # ЭВМ сбросила линию — не трейсбек, а выход
        sys.stdout.write("\x1b[0m\r\n")
        say(f"[линия] {e} — сеанс завершён")
    finally:
        if take_fit:
            fit.restore_saved()
        if fd is not None:
            if mouse:
                _esc(b"\x1b[?1006l\x1b[?1000l")
            if csi_u:
                sys.stdout.write("\x1b[<1u")
            sys.stdout.write("\x1b[?25h")
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
    """Псевдо-ЭЛТ: текстовый вывод 80×25 (коды КОИ7 → буквы) с историей.

    Над кадром — «история» сошедших наверх строк (глубина — [terminal]
    history в конфиге, по умолчанию 1000); PgUp/PgDn листают окно вывода
    до первой выданной строки и обратно. Курсор — инверсный блок; образные
    знаки УП (блинк) мигают, как dim на Видеотоне-340.
    """
    from .charset import decode_koi7
    from .screen import ATTR_BLINK, ATTR_CTRL

    sc = parser.screen
    try:
        term_w, term_h = os.get_terminal_size()
    except OSError:
        term_w, term_h = COLS, 24
    # подвал тянется на всю ширину окна: ячеек в ряду больше — панель ниже
    sc.set_panel_cols(term_w)
    # верх окна: строка состояния линии, под ней черта и пустая строка —
    # обе вне кадра ЭВМ; дальше кадр на всю оставшуюся высоту, внизу
    # подвал с чертой: пустот не остаётся ни при каком размере окна
    view_h = max(1, term_h - 3)
    sc.set_rows(view_h - sc.panel_rows() - 1)

    def row_line(cells, attr, y=None):
        from .charset import display_char
        toks = []
        for x, code in enumerate(cells):
            a = attr[x]
            if a & ATTR_CTRL:
                ch = (display_char(code | 0x40, "n0") if sc.show_ctrl
                      else " ")
                if not sc.show_ctrl:
                    a = 0                        # скрытый УП не мигает
            else:
                ch = display_char(code, sc.display_set)
            if a & ATTR_BLINK:
                # 5 мигающий, 7 инверсия: там, где мигание отключено
                # (большинство современных терминалов), знак остаётся
                # различим инверсным блоком
                ch = f"\x1b[5;7m{ch}\x1b[0m"
            if y is not None and y == sc.y and x == sc.x:
                ch = f"\x1b[7m{ch}\x1b[27m"
            toks.append(ch)
        return "".join(toks)

    lines = [row_line(c, a) for c, a in sc.history]
    lines += [row_line(row, sc.attr[y], y) for y, row in enumerate(sc.cells)]
    panel_n = sc.panel_rows() + 1       # ряды подвала + черта над ним
    lines.append("-" * sc.panel_cols)     # черта подвала — на всю ширину окна
    lines.extend(sc.panel_styled_rows())   # подвал с цветом значений и групп
    # окно просмотра (PgUp/PgDn): закреплённый сдвиг или авто-следование
    # за курсором; рисуем абсолютной позицией — в коротком окне ничего
    # не скроллится, верх поля всегда виден
    sc._view_h = view_h
    # верх окна — по sync_view_top: в следящем режиме кадр всегда с первого
    # ряда (история над ним не выползает), в закреплённом (PgUp/PgDn) — по
    # контенту; остаток окна добираем пустыми рядами перед подвалом,
    # чтобы подвал всегда стоял по нижней кромке экрана
    top = sc.sync_view_top(view_h)
    # окно меньше содержимого (кадр не может быть ниже 25 рядов) — в
    # следящем режиме режем верх кадра, но не подвал: строку состояния и
    # легенду резать нельзя; в закреплённом (PgUp/колесо) — как листали
    if sc.view_top is None:
        top = max(top, len(lines) - view_h)
    view = lines[top:top + view_h]
    if len(view) < view_h:
        pad = view_h - len(view)
        view = (view[:len(view) - panel_n] + [""] * pad +
                view[len(view) - panel_n:])
    out = "\x1b[H\x1b[2J"
    # строка состояния линии, черта и пустая строка — вне кадра ЭВМ, в
    # листание (PgUp/колесо) они не попадают
    out += f"\x1b[1;1H{sc.status_line()}"
    out += f"\x1b[2;1H{'-' * sc.panel_cols}\x1b[3;1H"
    for i, ln in enumerate(view):
        out += f"\x1b[{i + 4};1H{ln}"
    return out


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
    ap.add_argument("--history", default=None, type=str,
                    help="строк «истории» выдачи над экраном для PgUp "
                         "(по умолчанию 1000; 0 — не сохранять)")
    ap.add_argument("--nabor", choices=["n0", "n1", "n2"], default=None,
                    help="НАБОР терминала: Н0 — знаки ASCII, Н1 — русские "
                         "верхний и нижний регистры (КОИ-7), Н2 — набор "
                         "команд №2 (VT-52, по умолчанию)")
    ap.add_argument("--charset", choices=["n0", "n1"], default=None,
                    help="только набор знаков (без смены командного режима); "
                         "устаревший — см. --nabor")
    ap.add_argument("--mouse", action="store_true", default=None,
                    help="отдать мышь терминала эмулятору: колесо листает "
                         "«историю» выдачи (циклической прокрутки нет); без "
                         "флага мышь остаётся терминалу (mouse = off в "
                         "конфиге)")
    ap.add_argument("--machine", default=None,
                    help="номер ЭВМ для строки состояния над кадром "
                         "(в конфиге: [line] machine)")
    ap.add_argument("--no-help", action="store_true", default=None,
                    help="убрать из подвала ряды-справку (легенду клавиш); "
                         "нижняя строка состояния остаётся")
    ap.add_argument("--fit", action="store_true", default=None,
                    help="подогнать окно терминала ровно на «25 рядов кадра + "
                         "подвал» (по умолчанию окно не трогаем: кадр ЭВМ "
                         "дотягивается до низа окна)")
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
    parser = Parser(mode=2 if args.nabor == "n2" else 1,
                    history=int(args.history))
    parser.screen.display_set = args.charset or args.nabor
    parser.charset = charset

    if args.line:
        run_line(parser, args.line, args.png, args.seconds,
                 args.feed, mode=args.mode, layout=layout,
                 keymap=args.keymap, take_mouse=bool(args.mouse),
                 take_fit=bool(args.fit), machine=args.machine or "",
                 show_help=not bool(args.no_help),
                 password=args.password, keyfile=args.keyfile)
    else:
        run_script(parser, args.png)


if __name__ == "__main__":
    main()