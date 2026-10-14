"""Показать, что реально нарисовано на виртуальной консоли.

Читает дамп буфера экрана, снятый командой:
    sudo cat /dev/vcs3 > /tmp/tty3.bin
    stty size < /dev/tty3 > /tmp/tty3_size.txt

Байты лежат в кодировке, которую задаёт текущий шрифт консоли.
"""
import io
import sys

VCS = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tty3.bin"
SIZE = sys.argv[2] if len(sys.argv) > 2 else "/tmp/tty3_size.txt"
ENC = sys.argv[3] if len(sys.argv) > 3 else "koi8-r"

raw = open(VCS, "rb").read()
try:
    rows, cols = (int(v) for v in open(SIZE).read().split())
except Exception:
    rows, cols = 25, 80
print("дамп: %d байт | размер экрана: %dx%d | кодировка шрифта: %s"
      % (len(raw), cols, rows, ENC))
print("=" * min(cols, 100))

text = raw.decode(ENC, errors="replace")
lines = []
for y in range(min(rows, len(text) // cols)):
    lines.append(text[y * cols:(y + 1) * cols].rstrip())
    # консоль забивает конец строки пробелами и мусором атрибутов
for y, ln in enumerate(lines):
    ln = "".join(c for c in ln if c.isprintable() or c == " ").rstrip()
    print("%2d|%s" % (y + 1, ln[:cols]))
print("=" * min(cols, 100))

# что видно: сводка по непустым строкам
nonempty = [(i + 1, ln.strip()) for i, ln in enumerate(lines) if ln.strip()]
print("непустых строк: %d из %d" % (len(nonempty), rows))
cyr = sum(1 for _, ln in nonempty for c in ln if "Ѐ" <= c <= "ӿ")
print("кириллических знаков на экране: %d" % cyr)