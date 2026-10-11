"""Пример: агент играет в «ИГРА К ВАШИМ УСЛУГАМ» (ДИСПАК, БЭСМ-6).

Через слой управления из ie15emu.agent: читаем доску из кадра ЭВМ
(«ББ» — белые, «ЧЧ» — чёрная), выбираем ход и набираем его клавишами.
Формат хода машины — «ОТ-ДО» через дефис («1-4» → «НЕВЕРНЫЙ ХОД»).

Запуск: python3 tools/agent_igra.py [--line tcp://127.0.0.1:4203-4223]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.agent import Terminal, fold

# поле-звезда: клетки соединены по рисунку на экране
ADJ = {0: (1, 2, 3), 1: (0, 2, 6), 2: (0, 1, 4, 5), 3: (0, 2, 4),
       4: (2, 3, 5, 9), 5: (2, 4, 6, 8, 9), 6: (1, 5, 7),
       7: (6, 8), 8: (5, 7, 10), 9: (4, 5, 10), 10: (8, 9)}

# горизонтальное положение клетки на рисунке: белые ходят «вперёд» (вправо,
# к чёрным), поэтому ход в клетку правее — продвижение
XPOS = {0: 0, 1: 12, 2: 12, 3: 12, 4: 26, 5: 26, 6: 26,
        7: 40, 8: 40, 9: 40, 10: 54}

# только клетки с фишкой: у пустых клеток в рисунке маркера нет
CELL = re.compile(r"I(\d+)\s+(ББ|ЧЧ)\s*I")


def parse_board(lines) -> tuple[list[int], list[int]]:
    """Белые и чёрные фишки по рядам кадра: «I3 ББ I…» → клетка 3."""
    white, black = [], []
    for ln in lines:
        for num, mark in CELL.findall(ln):
            (black if mark == "ЧЧ" else white).append(int(num))
    return sorted(white), sorted(black)


def moves(white, black) -> list[tuple[int, int]]:
    """Ходы белых, лучшие первыми.

    Чёрные проигрывают, когда все соседние клетки заняты белыми, поэтому
    ход оцениваем тем, сколько у чёрной останется свободных выходов, и
    продвижением белых вправо (их «вперёд»).
    """
    if not black:
        return [(a, b) for a in white for b in ADJ[a]]
    bk = black[0]
    out = []
    for a in white:
        for b in ADJ[a]:
            if b in black:
                continue
            after = (set(white) - {a}) | {b}      # белые после хода
            free = [n for n in ADJ[bk] if n not in after and n != bk]
            score = -len(free) * 4 + (XPOS[b] - XPOS[a])
            out.append((score, a, b))
    out.sort(reverse=True)
    return [(a, b) for _, a, b in out]


def main() -> int:
    ap = argparse.ArgumentParser(description="играть в ИГРА К ВАШИМ УСЛУГАМ")
    ap.add_argument("--line", default="tcp://127.0.0.1:4203-4223")
    ap.add_argument("--moves", type=int, default=40, help="ходов за партию")
    args = ap.parse_args()

    t = Terminal(args.line, mode="host")
    try:
        t.key("KEY_ENTER")                    # приглашение ОС (ДКС: без НУС)
        t.wait_quiet(quiet=2.0, timeout=30)
        t.type("игра 4199"); t.key("KEY_ENTER")
        t.wait_quiet(quiet=2.0, timeout=30)
        if "ОШИБ" in fold(t.screen()):        # первый ввод машина «съедает»
            t.type("игра 4199"); t.key("KEY_ENTER")
            t.wait_quiet(quiet=2.0, timeout=30)
        t.type("нет"); t.key("KEY_ENTER")    # правила не нужны
        t.wait_quiet(quiet=2.0, timeout=30)
        t.type("да"); t.key("KEY_ENTER")      # играем белыми
        t.wait_quiet(quiet=2.0, timeout=30)

        for n in range(args.moves):
            lines = t.lines()
            white, black = parse_board(lines)
            tail = fold(" / ".join([x for x in lines if x.strip()][-3:]))
            if "ВАШ ХОД" not in tail:      # машина вернулась в ОС или конец
                print("партия закончена:", tail, flush=True)
                break
            done = False
            for a, b in moves(white, black):
                t.type(f"{a}-{b}"); t.key("KEY_ENTER")
                t.wait_quiet(quiet=1.5, timeout=30)
                tail = [x for x in t.lines() if x.strip()][-3:]
                reply = fold(" / ".join(tail))
                if "НЕВЕРНЫЙ ХОД" not in reply:
                    print(f"ход {n}: {a}-{b} → {reply}", flush=True)
                    done = True
                    break
            if not done:
                print(f"ход {n}: нет принятого варианта", flush=True)
                break
            if "МОЙ ХОД" in fold(" ".join(t.lines())):
                time.sleep(0.5)
                t.pump()
        print("--- итоговое поле ---")
        for ln in t.lines():
            if ln.strip():
                print(f"   {ln}")
        print("статус:", t.status())
    finally:
        t.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())