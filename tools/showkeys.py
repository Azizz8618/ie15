#!/usr/bin/env python3
"""СняТЬ сырые байты с клавиатуры — что реально шлёт терминал.

Работает в любом терминале (xterm, консоль Linux, tmux, вкладка
браузера): stdin переводится в cbreak, каждое нажатие печатается
сырыми байтами (hex) и так, как их разбирает эмулятор.

    python3 tools/showkeys.py

Нажмите клавиши (Shift+Home, Shift+End, Shift+Backspace и т. п.),
затем Ctrl-C. Если напротив Shift+Home строка НЕ появится — терминал
эту клавишу не кодирует вовсе.

Разбор — тем же кодом, что и эмулятор (decode_keys), поэтому
видно сразу: распознано как SELHOME/SELEND/SELBSPACE или рассыпалось.
"""
from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ie15emu.keyboard import decode_keys            # noqa: E402


def hexs(b: bytes) -> str:
    if not b:
        return "(пусто)"
    return " ".join(f"{c:02X}" for c in b) + \
        "   " + repr(b.decode("utf-8", "replace"))


def main() -> int:
    if not sys.stdin.isatty():
        print("нужен настоящий терминал (stdin — не tty)")
        return 2
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    print("=== СНЯТИЕ КЛАВИШ === нажимайте клавиши, выход — Ctrl-C")
    print("  сырые байты (hex + repr) -> разбор эмулятора\n")
    pending = b""
    stopped = False
    try:
        while not stopped:
            data = sys.stdin.buffer.raw.read(1)      # по одному байту
            if not data:
                break
            if data == b"\x03":                      # Ctrl-C — выход
                print("\n(Ctrl-C)")
                break
            # байты одного нажатия приходят подряд со скоростью клавиатуры:
            # склеиваем всё, что пришло за 40 мс, — как их видит эмулятор
            while select.select([fd], [], [], 0.04)[0]:
                nxt = os.read(fd, 4096)
                if not nxt:
                    break
                if b"\x03" in nxt:                   # Ctrl-C в середине
                    data += nxt[:nxt.index(b"\x03")]
                    stopped = True
                    break
                data += nxt
            keys, pending = decode_keys(data, pending)
            stamp = time.strftime("%H:%M:%S")
            if keys:
                for k in keys:
                    print(f"[{stamp}] {hexs(data):40} -> {k!r}")
            else:
                print(f"[{stamp}] {hexs(data):40} -> (хвост: "
                      f"{hexs(pending)})")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if pending:
        print(f"\nНЕДОБРАННЫЙ ХВОСТ (остался неразобранным): {hexs(pending)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
