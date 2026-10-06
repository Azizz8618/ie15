"""ОЗУ кадра терминала 15ИЭ-00-013: 80×25, 7 разрядов на позицию.

Как в терминале: код позиции + атрибут инверсии, служебная строка 25
выводится по таймеру (прерывание БЛНС) или по флагу режима.
"""
from __future__ import annotations

from . import COLS, ROWS, SERVICE_ROW

ATTR_INV = 0x80  # младшие 7 битов — код ВЗУ, старший — инверсия


class Screen:
    def __init__(self) -> None:
        self.cells = [[0x20] * COLS for _ in range(ROWS)]
        self.attr = [[0] * COLS for _ in range(ROWS)]
        self.service = [" "] * COLS   # служебная строка
        self.service_dirty = False
        self.x = 0
        self.y = 0
        self.inverse = False
        self.blink = False
        self.bell = False

    # --- курсор -----------------------------------------------------
    def wrap_cursor(self) -> None:
        if self.x >= COLS:
            self.x = 0
            self.y += 1
        if self.y >= ROWS:
            self.scroll_up()
            self.y = ROWS - 1

    def cr(self) -> None:
        self.x = 0

    def lf(self) -> None:
        # ПС на алфавитно-цифровых устройствах того времени — «новая
        # строка» с возвратом каретки: драйвер ДИСПАК печатает строки
        # Электроники шаблоном «ПС текст ПР» и не дублирует ПР в начале
        self.x = 0
        self.y += 1
        if self.y >= ROWS:
            self.scroll_up()
            self.y = ROWS - 1

    def bs(self) -> None:
        if self.x > 0:
            self.x -= 1

    def ht(self) -> None:
        self.x = min((self.x // 8 + 1) * 8, COLS - 1)

    def scroll_up(self) -> None:
        self.cells.pop(0)
        self.attr.pop(0)
        self.cells.append([0x20] * COLS)
        self.attr.append([0] * COLS)

    def scroll_down(self) -> None:
        self.cells.pop()
        self.attr.pop()
        self.cells.insert(0, [0x20] * COLS)
        self.attr.insert(0, [0] * COLS)

    # --- изображение ------------------------------------------------
    def put(self, code: int) -> None:
        code &= 0x7F
        self.cells[self.y][self.x] = code
        self.attr[self.y][self.x] = ATTR_INV if self.inverse else 0
        self.x += 1
        self.wrap_cursor()

    def erase_eol(self) -> None:
        for x in range(self.x, COLS):
            self.cells[self.y][x] = 0x20
            self.attr[self.y][x] = 0

    def erase_eod(self) -> None:
        self.erase_eol()
        for y in range(self.y + 1, ROWS):
            self.cells[y] = [0x20] * COLS
            self.attr[y] = [0] * COLS

    def clear_all(self) -> None:
        self.cells = [[0x20] * COLS for _ in range(ROWS)]
        self.attr = [[0] * COLS for _ in range(ROWS)]
        self.x = self.y = 0

    def set_service(self, text: str) -> None:
        text = text[:COLS].ljust(COLS)
        for i, ch in enumerate(text):
            self.service[i] = ch
        self.service_dirty = True

    # --- выдача -----------------------------------------------------
    def text(self) -> str:
        from .charset import decode_koi7
        return "\n".join(decode_koi7(bytes(row)) for row in self.cells)

    def dump(self) -> str:
        out = ["+" + "-" * COLS + "+"]
        for i, line in enumerate(self.text().splitlines()):
            marker = "*" if i == self.y else " "
            out.append(f"|{line}|{marker}")
        out.append("+" + "-" * COLS + "+")
        out.append("|" + "".join(self.service) + f"|  (служебная, стр.{SERVICE_ROW})")
        out.append(f"курсор: x={self.x} y={self.y} inverse={self.inverse} bell={self.bell}")
        return "\n".join(out)