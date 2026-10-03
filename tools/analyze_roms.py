#!/usr/bin/env python3
"""Анализ ПЗУ терминала 15ИЭ-00-013 (rom/).

Определяет назначение каждого дампа, печатает карту знакогенератора
и структуру таблицы клавиатуры. Запуск:

    python3 tools/analyze_roms.py [--roms rom/] [--glyphs]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

CHARGEN = "chargen-15ie.bin"
FIRMWARE = ("dump1.bin", "dump5.bin")
KBD = "15bbb.rt5"

# Таблица кодов кириллицы терминала (КОИ7, см. besm6_tty.c koi7_rus_to_unicode)
RUS = "ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ"


def digest(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def entropy(block: bytes) -> float:
    import math

    if not block:
        return 0.0
    return -sum(
        (block.count(x) / len(block)) * math.log2(block.count(x) / len(block))
        for x in set(block)
    )


def glyph_rows(chargen: bytes, code: int) -> list[int]:
    """8 строк глифа: точки в разрядах 7…1 (старший бит — левая точка)."""
    return [chargen[code * 8 + r] >> 1 for r in range(8)]


def render_glyph(rows: list[int]) -> str:
    return "\n".join(
        "".join("#" if row & (1 << (6 - i)) else "." for i in range(7))
        for row in rows
    )


def analyze_chargen(data: bytes) -> None:
    print(f"\n== {CHARGEN}: {len(data)} байт, md5={digest(data)}")
    print("   назначение: ПЗУ знакогенератора (КР556РТ5): 256 глифов 7×8")
    for code in (0x40, 0x26, 0x30, 0x41, 0x5F):
        print(f"\n   глиф 0x{code:02X} ({chr(code)!r}):")
        for line in render_glyph(glyph_rows(data, code)).splitlines():
            print("     " + line)
    if "--glyphs" in sys.argv:
        print("\n   ПОЛНАЯ КАРТА ЗНАКОГЕНЕРАТОРА:")
        for code in range(256):
            rows = glyph_rows(data, code)
            ch = chr(code) if 0x20 <= code < 0x7F else "."
            print(f"\n   [{code:02X}] '{ch}'")
            for line in render_glyph(rows).splitlines():
                print("     " + line)


def analyze_firmware(name: str, data: bytes) -> None:
    print(f"\n== {name}: {len(data)} байт, md5={digest(data)}")
    print("   назначение: дамп ПЗУ блока логики (прошивка МПУ/микропрограммы)")
    for off in range(0, len(data), 256):
        b = data[off : off + 256]
        print(f"   {off:04X}-{off + 255:04X}  энтропия={entropy(b):.2f}")
    # зоны заполнения 0xFF (программируемые маской)
    ff_runs = []
    run = 0
    for i, x in enumerate(data):
        if x == 0xFF:
            run += 1
        elif run:
            ff_runs.append((i - run, run))
            run = 0
    if run:
        ff_runs.append((len(data) - run, run))
    top = sorted(ff_runs, key=lambda t: -t[1])[:5]
    print("   крупные зоны 0xFF (empty/маска): " +
          ", ".join(f"{off:04X}+{ln}" for off, ln in top))


def diff_firmware(a: bytes, b: bytes) -> None:
    diffs = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    if not diffs:
        print("\n   dump1.bin и dump5.bin идентичны")
        return
    print(f"\n   dump1.bin vs dump5.bin: различаются {len(diffs)} байт")
    lo, hi = min(diffs), max(diffs)
    print(f"   диапазон различий: 0x{lo:04X}..0x{hi:04X}")


def analyze_kbd(data: bytes) -> None:
    print(f"\n== {KBD}: {len(data)} байт, md5={digest(data)}")
    print("   назначение: ПЗУ клавиатуры (КР556РТ5, контроллер 15ВВВ-97-006)")
    print(f"   заполненность 0xFF: {data.count(0xFF)}/{len(data)}, "
          f"уникальных байт {len(set(data))}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roms", default=str(Path(__file__).resolve().parent.parent / "rom"))
    ap.add_argument("--glyphs", action="store_true", help="печать всех 256 глифов")
    args = ap.parse_args()
    roms = Path(args.roms)

    print("Анализ ПЗУ терминала «Электроника 15ИЭ-00-013» (Фрязинский дисплей)")
    print("=" * 60)

    analyze_chargen((roms / CHARGEN).read_bytes())
    d1 = (roms / FIRMWARE[0]).read_bytes()
    d5 = (roms / FIRMWARE[1]).read_bytes()
    analyze_firmware(FIRMWARE[0], d1)
    analyze_firmware(FIRMWARE[1], d5)
    diff_firmware(d1, d5)
    analyze_kbd((roms / KBD).read_bytes())
    print("\nКОИ7-алфавит терминала:", " ".join(f"{i:02X}:{c}" for i, c in enumerate(RUS)))


if __name__ == "__main__":
    main()