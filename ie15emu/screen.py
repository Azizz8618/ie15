"""ОЗУ кадра терминала 15ИЭ-00-013: 80×25, 7 разрядов на позицию.

Как в терминале: код позиции + атрибут инверсии. Кадр — все 25 строк,
как у Видеотона-340 (кадры ДКС «ИГРА» разложены на 25 строк); служебная
панель выводится под кадром.
"""
from __future__ import annotations

from . import COLS, ROWS, SERVICE_ROW

ATTR_INV = 0x80    # младшие 7 битов — код ВЗУ, старший — инверсия
ATTR_BLINK = 0x40  # «блинк», как на Видеотоне-340: образные знаки УП


class Screen:
    def __init__(self) -> None:
        self.cells = [[0x20] * COLS for _ in range(ROWS)]
        self.attr = [[0] * COLS for _ in range(ROWS)]
        self.service = [" "] * COLS   # служебная строка (ряд 25)
        self.service_more: list[str] = []   # дополнительные ряды подвала
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

    # --- навигация по словам (Ctrl+стрелки клавиатуры 15ВВВ) ----------
    # разделители слова — пробел и точка (знак конца предложений в Н1);
    # прочие коды (в т.ч. русские буквы без бита алфавита) — знаки слова.
    WORD_DELIMS = (0x20, ord("."))

    def _grid_at(self, i: int) -> int:
        return self.cells[i // COLS][i % COLS]

    def word_right(self) -> None:
        """Ctrl+→: курсор на начало следующего слова (из середины слова —
        на начало идущего за ним)."""
        i = self.y * COLS + self.x
        n = ROWS * COLS
        i += 1
        while i < n and self._grid_at(i) not in self.WORD_DELIMS:
            i += 1                       # через хвост текущего слова
        while i < n and self._grid_at(i) in self.WORD_DELIMS:
            i += 1                       # через разделители
        if i < n:
            self.y, self.x = divmod(i, COLS)

    def word_left(self) -> None:
        """Ctrl+←: курсор на начало текущего слова; если уже там —
        на начало предыдущего."""
        cur = self.y * COLS + self.x
        if cur == 0:
            return
        i = cur
        while i > 0 and self._grid_at(i - 1) in self.WORD_DELIMS:
            i -= 1                       # назад через разделители
        j = i
        while j > 0 and self._grid_at(j - 1) not in self.WORD_DELIMS:
            j -= 1                       # к началу слова слева
        if j != cur:
            self.y, self.x = divmod(j, COLS)
            return
        i = j                            # уже в начале — предыдущее слово
        while i > 0 and self._grid_at(i - 1) in self.WORD_DELIMS:
            i -= 1
        j = i
        while j > 0 and self._grid_at(j - 1) not in self.WORD_DELIMS:
            j -= 1
        self.y, self.x = divmod(j if i > 0 else 0, COLS)

    def line_start(self) -> None:
        """Ctrl+↑: начало текущей строки."""
        self.x = 0

    def line_below(self) -> None:
        """Ctrl+↓: начало строки ниже."""
        self.x = 0
        self.y = min(ROWS - 1, self.y + 1)

    # --- изображение ------------------------------------------------
    def put(self, code: int, blink: bool = False) -> None:
        code &= 0x7F
        self.cells[self.y][self.x] = code
        a = ATTR_INV if self.inverse else 0
        if blink:
            a |= ATTR_BLINK
        self.attr[self.y][self.x] = a
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
        # текст с переводом строк раскладывается на несколько рядов
        # подвала; каждый ряд — строго 80 знаков
        rows = text.split("\n")[:8]
        first = (rows[0] if rows else "")[:COLS].ljust(COLS)
        for i, ch in enumerate(first):
            self.service[i] = ch
        self.service_more = [r[:COLS].ljust(COLS) for r in rows[1:]]
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
        out.append("|" + "".join(self.service) + "|  (служебная панель, поз."
                   f"{SERVICE_ROW} — под кадром ЭВМ)")
        for extra in self.service_more:
            out.append("|" + extra + "|")
        out.append(f"курсор: x={self.x} y={self.y} inverse={self.inverse} bell={self.bell}")
        return "\n".join(out)