#!/usr/bin/env python3
"""Живая проверка страницы истории: PgUp показывает прошлую страницу,
F5 удаляет её (АВТОНОМНО) — на реальном сеансе с сетевой линией.

Поддельная ЭВМ (маленький TCP-сервер) выдаёт 60 строк, эмулятор 15ИЭ
подключается к ней как к tty-линии БЭСМ-6. После выдачи нажимаем PgUp
(ESC [ 5 ~) — в кадр загружается страница истории; затем F5 (ESC [ 15 ~)
и проверяем, что строки этой страницы с кадра исчезли.

    python3 tools/check_live_history.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_live_keys import Term, SEQ, screen_text   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

MARK = "LINE%02d"         # маркер строки выдачи (ASCII: линия RAW)


class Machine:
    """Поддельная ЭВМ: слушает порт, пишет в линию 60 пронумерованных строк."""

    def __init__(self, n_lines: int = 60):
        self.n_lines = n_lines
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(1)
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(2.0)
            try:                                # баннер tmxr, как у SIMH
                conn.sendall(b"Connected to the BESM-6 SIMULATOR TTY device,\r\n"
                             b"Encoding is RAW\r\n")
            except OSError:
                pass
            time.sleep(0.3)
            for i in range(1, self.n_lines + 1):
                conn.sendall(f"{MARK % i}\r\n".encode())
                time.sleep(0.005)
            time.sleep(1.0)
            self.done.set()
            conn.close()
        except OSError:
            self.done.set()

    def close(self) -> None:
        self.sock.close()


def frame_lines(t: Term) -> list[str]:
    """Строки кадра ЭВМ (без строк состояния и подвала)."""
    return [ln for ln in t.screen()[3:27]]


def main() -> int:
    m = Machine(60)
    t = Term(["--line", f"tcp://127.0.0.1:{m.port}", "--mode", "local",
              "--no-help"])
    fails: list[str] = []

    # ждём выдачи: кадр заполнится строками ЭВМ
    deadline = time.time() + 10
    while time.time() < deadline:
        if any("СТР" in ln for ln in frame_lines(t)):
            break
        t.read(0.4)
    t.read(0.8)
    print("=== выдача ЭВМ (кадр, АВТОНОМНО) ===")
    for ln in frame_lines(t)[:6]:
        print(f"|{ln}|")

    # PgUp: в кадр загружается прошлая страница истории
    before = len(t.buf)
    t.send(SEQ["pgup"], settle=1.0)
    print("\n=== после PgUp (страница истории в кадре) ===")
    for ln in frame_lines(t)[:6]:
        print(f"|{ln}|")
    if not any("LINE" in ln for ln in frame_lines(t)):
        fails.append("PgUp: страница истории не показана")

    # F5: страница истории удаляется — её строк в кадре не остаётся
    t.send(SEQ["F5"], settle=1.0)
    print("\n=== после F5 (страница истории удалена) ===")
    for ln in frame_lines(t)[:6]:
        print(f"|{ln}|")
    if any("LINE" in ln for ln in frame_lines(t)):
        fails.append("F5: строка удалённой страницы осталась в кадре")

    err = t.close()
    m.close()
    if err.strip():
        print("\n--- stderr ---")
        print(err.strip()[:1200])

    print("\n" + "=" * 60)
    if fails:
        print("ПРОВАЛЕНО:")
        for f in fails:
            print("  -", f)
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
