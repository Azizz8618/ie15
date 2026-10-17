#!/usr/bin/env python3
"""Живой сеанс с --keylog: клавиши + наблюдение за линией.

Запускает эмулятор в pty (настоящий терминал), ждёт подключения,
шлёт Shift+Home/End/Backspace, затем сидит в простое и собирает всё,
что эмулятор пишет: экран, сообщения о линии, stderr (там логи
paramiko) и журнал --keylog.

    python3 tools/live_session.py [--idle 90] [--keys]
"""
from __future__ import annotations

import argparse
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESC = b"\x1b"

KEYS = {
    "shift+home": ESC + b"[1;2H",
    "shift+end": ESC + b"[1;2F",
    "shift+backspace": ESC + b"[127;2u",
    "home": ESC + b"[H",
    "right": ESC + b"[C",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", type=float, default=90.0,
                    help="сколько секунд сидеть в простое после клавиш")
    ap.add_argument("--keys", action="store_true",
                    help="послать Shift+Home/End/Backspace")
    ap.add_argument("--log", default="/tmp/keys.log")
    args = ap.parse_args()

    Path(args.log).unlink(missing_ok=True)
    master, slave = pty.openpty()
    import fcntl, struct, termios
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    env = dict(os.environ, TERM="xterm-256color")
    cmd = [sys.executable, "-m", "ie15emu", "--keylog", args.log]
    err_path = "/tmp/.private/azizz/opencode/ie15_stderr.log"
    os.makedirs(os.path.dirname(err_path), exist_ok=True)
    err = open(err_path, "wb")
    p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=err,
                         cwd=str(ROOT), env=env)
    os.close(slave)

    out = []

    def drain(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                out.append(data.decode("utf-8", "replace"))

    t0 = time.time()
    drain(4.0)
    print(f"[запуск] процесс жив: {p.poll() is None}")

    if args.keys:
        drain(1.0)
        for name in ("home", "right", "shift+home", "shift+end",
                     "shift+backspace"):
            os.write(master, KEYS[name])
            drain(0.6)
            print(f"[нажатие] {name}")
        drain(1.0)

    print(f"[простой] {args.idle:.0f} с — наблюдаю за линией…")
    # каждые 10 с отмечаем, жив ли процесс
    left = args.idle
    while left > 0:
        step = min(10.0, left)
        drain(step)
        left -= step
        print(f"  …процесс жив: {p.poll() is None}, "
              f"прошло {time.time() - t0:.0f} с")

    os.write(master, b"\x03")          # Ctrl-C — выход
    drain(1.5)
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    err.close()
    try:
        os.close(master)
    except OSError:
        pass

    text = "".join(out)
    print("\n=== сообщения о линии/сеансе (вывод эмулятора) ===")
    for ln in text.splitlines():
        if any(w in ln for w in ("линия", "ЛИНИЯ", "сеанс", "paramiko",
                                 "SSH", "Exception", "Traceback", "порт")):
            print("  " + ln.strip()[:200])

    print("\n=== stderr (логи paramiko) ===")
    try:
        s = Path(err_path).read_text("utf-8", "replace")
    except OSError:
        s = ""
    print(s[-3000:] if s.strip() else "  (пусто)")

    print("\n=== журнал --keylog ===")
    try:
        print(Path(args.log).read_text("utf-8", "replace"))
    except OSError:
        print("  (нет файла)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
