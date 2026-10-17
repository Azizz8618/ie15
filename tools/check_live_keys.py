#!/usr/bin/env python3
"""Проверка клавиш на живом терминале: эмулятор 15ИЭ в псевдо-терминале.

Запускает `python3 -m ie15emu --line stdio:// --mode local` внутри pty и
отправляет настоящие escape-последовательности ПК-клавиатуры (Shift+Home,
Shift+End, Shift+Backspace, F5), после каждой порции читает экран, который
эмулятор нарисовал в терминал, и печатает его текстом (инверсия выделения
помечена символом ▒).

    python3 tools/check_live_keys.py
"""
from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Последовательности ПК-клавиатуры (как их шлёт терминал)
ESC = b"\x1b"
SEQ = {
    "home": ESC + b"[H",
    "end": ESC + b"[F",
    "right": ESC + b"[C",
    "shift+home": ESC + b"[1;2H",
    "shift+end": ESC + b"[1;2F",
    # выделение строки — за Ctrl+Home/End (Shift+Home/End заблокированы:
    # ОС перехватывает их на листание вывода)
    "ctrl+home": ESC + b"[1;5H",
    "ctrl+end": ESC + b"[1;5F",
    # форма консоли Linux/старого xterm: ESC [ код ; модификатор ~
    "shift+home~": ESC + b"[1;2~",
    "shift+end~": ESC + b"[4;2~",
    "ctrl+home~": ESC + b"[1;5~",
    "ctrl+end~": ESC + b"[4;5~",
    "shift+backspace": ESC + b"[127;2u",     # kitty CSI-u
    "shift+backspace-xterm": ESC + b"[27;2;127~",
    "F5": ESC + b"[15~",
    "pgup": ESC + b"[5~",
    "ctrl+u": b"\x15",
    "ctrl+c": b"\x03",
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]")


def strip_ansi(s: str) -> str:
    """Убрать служебные последовательности, оставив SGR-инверсию как есть."""
    s = ANSI.sub("", s)
    return s.replace("\x1b", "")


class Term:
    """Эмулятор в псевдо-терминале: пишем в stdin, читаем stdout."""

    def __init__(self, args: list[str], rows: int = 24, cols: int = 80):
        self.master, slave = pty.openpty()
        import fcntl
        import struct
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(cols),
                   LINES=str(rows))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ie15emu", *args],
            stdin=slave, stdout=slave, stderr=subprocess.PIPE,
            cwd=str(ROOT), env=env, close_fds=True)
        os.close(slave)
        self.buf = ""
        # ждём, пока эмулятор дойдёт до чтения клавиш: до перевода tty в
        # cbreak терминал ещё эхоит ввод, и наши байты попали бы в кадр
        # дважды — как «мусор» до первой отрисовки
        time.sleep(1.0)
        self.read(0.5)

    def send(self, data: bytes, settle: float = 0.45) -> str:
        """Отправить байты и вернуть НАКОПЛЕННЫЙ вывод.

        Эмулятор рисует только изменившиеся знаки, поэтому полный кадр
        виден лишь в накопленном потоке — как на настоящем терминале.
        """
        os.write(self.master, data)
        self.read(settle)
        return self.buf

    def screen(self) -> list[str]:
        return screen_text(self.buf)

    def row(self, n: int = 3) -> str:
        """Ряд кадра ЭВМ (0 — верхний ряд выдачи)."""
        return self.screen()[3 + n]

    def read(self, settle: float = 0.45) -> str:
        """Собрать перерисовку: ждём, пока эмулятор перестанет писать."""
        end = time.time() + settle
        while time.time() < end:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.buf += chunk.decode("utf-8", "replace")
                end = time.time() + 0.25      # данные ещё идут — ждём
        return self.buf

    def close(self) -> str:
        try:
            os.write(self.master, SEQ["ctrl+c"])
            time.sleep(0.2)
        except OSError:
            pass
        self.proc.terminate()
        try:
            err = self.proc.communicate(timeout=5)[1]
        except subprocess.TimeoutExpired:
            self.proc.kill()
            err = b""
        os.close(self.master)
        return err.decode("utf-8", "replace")


def screen_text(raw: str) -> list[str]:
    """Разобрать вывод терминала в строки экрана (инверсия → ▒)."""
    lines = [""] * 30
    y = x = 0
    i = 0
    cur_inv = False
    while i < len(raw):
        ch = raw[i]
        if ch == "\x1b":
            m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", raw[i:])
            if not m:
                m2 = re.match(r"\x1b[()][A-Z0-9]", raw[i:])
                i += len(m2.group(0)) if m2 else 1
                continue
            params, fin = m.group(1), m.group(2)
            i += len(m.group(0))
            if fin == "H":
                nums = [int(p) if p else 1 for p in params.split(";")] or [1, 1]
                y = (nums[0] - 1) if nums else 0
                x = (nums[1] - 1) if len(nums) > 1 else 0
            elif fin == "J":
                lines = [""] * 30
                y = x = 0
            elif fin == "m":
                # SGR: 7 — инверсия, 27 — её снятие, 0 — сброс
                ps = [p for p in params.split(";")]
                if not params or "0" in ps:
                    cur_inv = False
                for p in ps:
                    if p == "7":
                        cur_inv = True
                    elif p == "27":
                        cur_inv = False
            continue
        if ch == "\n":
            y, x = y + 1, 0
            i += 1
            continue
        if ch == "\r":
            x = 0
            i += 1
            continue
        if 0 <= y < len(lines):
            row = lines[y].ljust(x)
            # под инверсией знак читается как метка (сама буква не важна)
            lines[y] = row[:x] + ("▒" if cur_inv else ch) + row[x + 1:]
        x += 1
        i += 1
    return [ln.rstrip() for ln in lines]


def show(title: str, t: "Term", rows: int = 10) -> None:
    print(f"\n=== {title} ===")
    # строки 1-3: строка состояния, черта, пустая; кадр ЭВМ — с четвёртой
    for ln in t.screen()[3:3 + rows]:
        print(f"|{ln}|")


def marks(row: str) -> int:
    """Знаков ряда подсвечено инверсией (в том числе курсор)."""
    return row.count("▒")


def sel_chars(row: str) -> int:
    """Выделенных знаков ряда: инверсия минус курсор 15ИЭ (он всегда один)."""
    return max(0, row.count("▒") - 1)


def without_cursor(row: str) -> str:
    """Ряд без знака под курсором (проверка содержимого)."""
    return row.replace("▒", "")


def cursor_row(t: "Term") -> str:
    """Ряд кадра, на котором стоит курсор 15ИЭ (единственный инверсный)."""
    for ln in t.screen()[3:3 + 24]:
        if "▒" in ln:
            return ln
    return ""


def sel_span(row: str) -> tuple[int, int] | None:
    """Границы подсвеченного участка ряда (включая курсор внутри него).

    Считать «инверсия минус курсор» ненадёжно: курсор, ставший внутри
    выделения, не добавляет своей клетки. Границы участка точнее.
    """
    idx = [i for i, ch in enumerate(row) if ch == "▒"]
    return (idx[0], idx[-1]) if idx else None


def main() -> int:
    args = ["--line", "stdio://", "--mode", "local", "--no-help",
            "--koi7", "--nabor", "n1"]
    t = Term(args)
    fails: list[str] = []

    # 1. Набор текста и эхо (в Н1 латиница показывается кириллицей)
    t.send(b"ABCDEFGH")
    show("после набора ABCDEFGH (АВТОНОМНО, Н1)", t)
    row0 = t.row(0)
    if not row0.startswith("АБЦДЕФГХ"):
        fails.append(f"эхо набора не показано: {row0!r}")

    # 2. Home, затем Ctrl+End — выделение от курсора до конца строки
    # (Shift+Home/End заблокированы — см. проверку ниже)
    t.send(SEQ["home"])
    t.send(SEQ["ctrl+end"])
    show("Home, Ctrl+End — выделение до конца строки", t)
    if marks(t.row(0)) != 8:
        fails.append(f"Ctrl+End: подсвечено {marks(t.row(0))} знаков, ждали 8")

    # 3. Shift+Backspace (kitty CSI-u) — фрагмент удалён целиком
    t.send(SEQ["shift+backspace"])
    show("Shift+Backspace — удаление выделенного", t)
    if sel_chars(t.row(0)):
        fails.append("Shift+Backspace: подсветка не снята")
    if without_cursor(t.row(0)).strip():
        fails.append(f"Shift+Backspace: строка не очищена: {t.row(0)!r}")

    # 4. Home, →, Ctrl+Home — выделение от курсора до начала строки (один знак)
    t.send(b"ABCDE")
    t.send(SEQ["home"])
    t.send(SEQ["right"])
    t.send(SEQ["ctrl+home"])
    show("Home, →, Ctrl+Home — выделение до начала строки", t)
    if sel_chars(t.row(0)) != 1 or not t.row(0).startswith("▒"):
        fails.append(f"Ctrl+Home: подсвечено {sel_chars(t.row(0))}, ждали 1")

    # 5. Shift+Backspace в форме xterm modifyOtherKeys
    t.send(SEQ["shift+backspace-xterm"])
    show("Shift+Backspace (xterm modifyOtherKeys)", t)
    if sel_chars(t.row(0)):
        fails.append("Shift+Backspace (xterm): фрагмент не удалён")
    # курсор встал на первый знак — уводим его в конец строки (End) и
    # проверяем, что текст строки стал короче ровно на один знак
    t.send(SEQ["end"])
    show("Shift+Backspace (xterm), курсор в конец строки", t)
    if not t.row(0).startswith("БЦДЕ"):
        fails.append(f"Shift+Backspace (xterm): строка {t.row(0)!r}")

    # 6. Вторая строка набора: Ctrl+Home выделяет остаток второй строки
    t.send(SEQ["home"])
    t.send(SEQ["right"])
    t.send(b"\r")                          # Enter — новая строка набора
    t.send(b"XY")
    t.send(SEQ["ctrl+home"])               # выделение до начала строки
    show("вторая строка набора: Ctrl+Home", t)
    if sel_chars(t.row(1)) != 2:
        fails.append(f"вторая строка: подсвечено {sel_chars(t.row(1))}, ждали 2")

    # 7. Та же пара в форме «ESC [ код ; модификатор ~» (консоль Linux).
    # Буфер к этому моменту уже правился (в нём две строки) — работаем
    # по строке под курсором, а не по первой строке кадра
    t.send(SEQ["end"])
    t.read(0.4)
    text = without_cursor(cursor_row(t))
    show("строка под курсором", t)
    if len(text) < 2:
        fails.append(f"в строке меньше двух знаков: {text!r}")
    t.send(SEQ["home"])
    t.send(SEQ["right"])
    t.send(SEQ["ctrl+end~"])
    row = cursor_row(t)
    show("Ctrl+End форма ESC [ 4 ; 5 ~", t)
    span = sel_span(row)
    if span != (1, len(text) - 1):
        fails.append(f"Ctrl+End (~): подсвечен {span}, ждали "
                     f"{(1, len(text) - 1)}")
    t.send(SEQ["shift+backspace"])
    t.send(SEQ["end"])
    t.read(0.4)
    show("после удаления фрагмента", t)
    if without_cursor(cursor_row(t)) != text[0]:
        fails.append(f"Ctrl+End (~): остались "
                     f"{without_cursor(cursor_row(t))!r}, ждали {text[0]!r}")
    t.send(SEQ["home"])
    t.send(SEQ["right"])
    t.send(SEQ["ctrl+home~"])
    row = cursor_row(t)
    show("Ctrl+Home форма ESC [ 1 ; 5 ~", t)
    if sel_chars(row) != 1:
        fails.append(f"Ctrl+Home (~): подсвечено {sel_chars(row)}, ждали 1")
    t.send(SEQ["shift+backspace"])
    t.send(SEQ["end"])
    t.read(0.4)
    if without_cursor(cursor_row(t)):
        fails.append(f"Ctrl+Home (~) не удалил фрагмент: {cursor_row(t)!r}")

    # 8. Shift+Home/End БЛОКИРОВАНЫ: нажатие приходит, но ничего не делает
    # (ОС перехватывает их на листание вывода — везде гасим одинаково)
    t.send(SEQ["home"])
    t.send(SEQ["right"])
    before = cursor_row(t)
    t.send(SEQ["shift+home"])
    t.send(SEQ["shift+end"])
    t.send(SEQ["shift+home~"])
    t.send(SEQ["shift+end~"])
    t.read(0.4)
    row = cursor_row(t)
    show("Shift+Home/End — блокированы (ничего не изменилось)", t)
    if sel_chars(row):
        fails.append(f"Shift+Home/End: выделение появилось — не блок: {row!r}")
    if row != before:
        fails.append(f"Shift+Home/End изменили экран: {before!r} → {row!r}")

    # 8. F5 — страница удаляется: подсветка и текст исчезают
    t.send(SEQ["F5"])
    show("F5 — удаление страницы (АВТОНОМНО)", t)
    if without_cursor(t.row(0)).strip() or without_cursor(t.row(1)).strip() \
            or sel_chars(t.row(0)):
        fails.append("F5: кадр не очищен")

    err = t.close()
    if err.strip():
        print("\n--- stderr ---")
        print(err.strip()[:2000])

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
