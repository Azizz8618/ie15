#!/usr/bin/env python3
"""Снифер клавиатуры для виртуальной консоли: что РЕАЛЬНО приходит на Ctrl+E.

Запуск на консоли (tty3 и любой другой):
    python3 tools/keysniff.py

Показывает для каждого нажатия: сырые байты (hex) → как их разбирает
эмулятор → что уходит в линию. Пишет тот же разбор в лог.
"""
import os
import sys
import termios
import time
import tty

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from ie15emu.keyboard import decode_keys, key_to_bytes

LOG = "/tmp/keysniff.log"
def hexs(b):
    return " ".join(f"{c:02X}" for c in b) if b else "(пусто)"


def show(fd, line):
    os.write(fd, (line + "\r\n").encode("utf-8", "replace"))


def main():
    tty_path = sys.argv[1] if len(sys.argv) > 1 else "/dev/tty3"
    fd = os.open(tty_path, os.O_RDWR | os.O_NOCTTY)
    old = termios.tcgetattr(fd)
    log = open(LOG, "w", encoding="utf-8")

    def emit(*parts):
        text = " | ".join(parts)
        show(fd, text)
        log.write(text + "\n")
        log.flush()

    try:
        tty.setraw(fd)
        show(fd, "")
        show(fd, "=== СНИФЕР КЛАВИАТУРЫ === нажмите Ctrl+E (и другие), Ctrl+C — выход")
        show(fd, " сырые байты -> разбор эмулятора -> в линию")
        show(fd, "")
        pending = b""
        last = time.time()
        while True:
            try:
                data = os.read(fd, 256)
            except OSError:
                break
            if not data:
                break
            if b"\x03" in data and len(data) == 1:      # Ctrl+C
                break
            keys, pending = decode_keys(data, pending)
            for k in keys:
                try:
                    out = key_to_bytes(k, shift_to_rus=True)
                except Exception as e:                    # noqa: BLE001
                    out = f"<ошибка: {e}>"
                emit(f"байты {hexs(data):28}", f"разбор {k!r}",
                     f"в линию {out!r}")
            if not keys:
                log.write(f"  (хвост ждёт продолжения: {hexs(pending)})\n")
                log.flush()
            last = time.time()
        # показать недобранный хвост, если он остался
        if pending:
            emit(f"НЕДОБРАННЫЙ ХВОСТ: {hexs(pending)}", "— разбор не выполнен")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)
        log.close()


if __name__ == "__main__":
    main()