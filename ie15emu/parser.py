"""Интерпретатор потока ЭВМ → терминал: «набор команд №2» (частично VT52).

15ИЭ-00-013 совместим с командами VT52. Реализован базовый набор:

  УП (0x00..0x1F):  ПР(0D) ПС(0A) ВК(08) ТАБ(09) ЗВН(07) ...
  ESC A/B/C/D  — курсор вверх/вниз/вправо/влево
  ESC H        — курсор в «дом» (0,0)
  ESC I        — курсор вверх со скроллом вниз (RI)
  ESC J        — стирать до конца экрана
  ESC K        — стирать до конца строки
  ESC Y r c    — курсор в позицию (r+20h, c+20h) [VT52]
  ESC E        — стирать экран + курсор в «дом»
  ESC b/c      — инверсный/нормальный видео
  ESC Z        — идентификация (ответ ESC / K)
"""
from __future__ import annotations

from . import COLS, ROWS
from .screen import Screen

# Управляющие коды
BEL = 0x07
BS = 0x08
HT = 0x09
LF = 0x0A
VT = 0x0B
FF = 0x0C
CR = 0x0D
SO = 0x0E
SI = 0x0F
XON = 0x11
XOFF = 0x13
CAN = 0x18
SUB = 0x1A
ESC = 0x1B


class Parser:
    """Побайтовый автомат разбора потока от ЭВМ.

    rus_letters=True — КОИ7 Н1: коды 0x01..0x1A (кроме 08/09/0A/0D)
    выводятся как русские буквы; иначе трактуются как УП
    (гашение, ВЛК, ЗП, КАН …). Выбор соответствует биту алфавита
    терминала (ГОСТ 27463-87, «З[8]3»).
    """

    CTRL = {BS, HT, LF, CR, BEL, VT, FF}

    def __init__(self, rus_letters: bool = False) -> None:
        self.screen = Screen()
        self.state = "raw"
        self._esc_args: list[int] = []
        self.rus_letters = rus_letters

    def feed(self, data: bytes) -> str | None:
        """Разобрать буфер; вернуть текст запроса от терминала (ESC Z и т.п.)."""
        replies: list[str] = []
        for b in data:
            r = self._byte(b)
            if r:
                replies.append(r)
        return "".join(replies) or None

    def _byte(self, b: int) -> str | None:
        s = self.state
        if s == "esc":
            return self._esc_byte(b)
        if s == "esc_y":
            self._esc_args.append(b)
            self.state = "esc_y2"
            return None
        if s == "esc_y2":
            row, col = self._esc_args[0] - 0x20, b - 0x20
            sc = self.screen
            sc.y = max(0, min(row, ROWS - 1))
            sc.x = max(0, min(col, COLS - 1))
            self.state = "raw"
            self._esc_args = []
            return None

        # raw
        if b == ESC:
            self.state = "esc"
            return None
        if b == CR:
            self.screen.cr()
        elif b == LF:
            self.screen.lf()
        elif b == BS:
            self.screen.bs()
        elif b == HT:
            self.screen.ht()
        elif b == BEL:
            self.screen.bell = True
        elif b in (VT, FF):
            self.screen.erase_eod()
        elif 0x20 <= b < 0x7F:
            self.screen.put(b)
        elif b < 0x20 and self.rus_letters and b not in self.CTRL:
            self.screen.put(b)     # КОИ7-буква
        else:
            self.screen.put(b)     # 7-разрядный код: старший бит игнорируется
        return None

    def _esc_byte(self, b: int) -> str | None:
        sc = self.screen
        self.state = "raw"
        if b == ord("A"):
            sc.y = max(0, sc.y - 1)
        elif b == ord("B"):
            sc.y = min(ROWS - 1, sc.y + 1)
        elif b == ord("C"):
            sc.x = min(COLS - 1, sc.x + 1)
        elif b == ord("D"):
            sc.x = max(0, sc.x - 1)
        elif b == ord("H"):
            sc.x = sc.y = 0
        elif b == ord("I"):
            if sc.y == 0:
                sc.scroll_down()
            else:
                sc.y -= 1
        elif b == ord("J"):
            sc.erase_eod()
        elif b == ord("K"):
            sc.erase_eol()
        elif b == ord("E"):
            sc.clear_all()
        elif b == ord("Y"):
            self.state = "esc_y"
            self._esc_args = []
        elif b == ord("b"):
            sc.inverse = True
        elif b == ord("c"):
            sc.inverse = False
        elif b == ord("Z"):
            return "\x1b/"      # ответ «терминал есть» (VT52)
        elif b == ESC:
            pass                # ESC ESC — сброс
        return None