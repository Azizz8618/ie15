"""ОЗУ кадра терминала 15ИЭ-00-013: 80×25, 7 разрядов на позицию.

Как в терминале: код позиции + атрибут инверсии. Кадр — все 25 строк,
как у Видеотона-340 (кадры ДКС «ИГРА» разложены на 25 строк); служебная
панель выводится под кадром.
"""
from __future__ import annotations

from . import COLS, ROWS, SERVICE_ROW

ATTR_INV = 0x80    # младшие 7 битов — код ВЗУ, старший — инверсия
ATTR_BLINK = 0x40  # мигание (режим blink Видеотона-340)
ATTR_CTRL = 0x20   # знак — управляющий код, сохранённый как есть: виден
                   # только при включённом «УПР.СИМВ» (парсер/экран)


class Screen:
    def __init__(self, history: int = 1000) -> None:
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
        # «история» выдачи: строки, сошедшие с верхнего края кадра при
        # скролле; глубина — из конфига ([terminal] history, по умолчанию 1000)
        self.history: list[tuple[list[int], list[int]]] = []
        self.history_max = history
        self.show_ctrl = False     # клавиша «УПР.СИМВ» (F7): показывать
                                   # управляющие знаки образом с миганием
        self.display_set = "n2"    # набор знаков: 'n0' — ASCII (латиница
                                   # целиком, кирилличных знаков нет),
                                   # 'n1' — русские верх+низ (КОИ-7 Н1),
                                   # 'n2' — международная строка + русские
                                   # коды ЭВМ (VT-52, режим линии БЭСМ-6)
        # представление (PgUp/PgDn): None — авто-следить за курсором;
        # целое — закреплённая верхняя строка окна вывода
        self.view_top: int | None = None
        self._view_h = 24          # высота окна на момент последней отрисовки

    def _push_history(self, row: list[int], attr: list[int]) -> None:
        if self.history_max <= 0:
            return
        if all(c == 0x20 for c in row):
            return                          # пустые строки в историю не пишем
        self.history.append((row[:], attr[:]))
        if len(self.history) > self.history_max:
            del self.history[0]

    def view_rows(self) -> int:
        """Рядов в просмотре: история + кадр (до последней заполненной
        строки) + подвал. Пустоты не листаются: прокрутка доходит до
        первой выданной строки и до последней строки подвала и
        останавливается — без «циклического» показа пустоты."""
        panel = 2 + len(self.service_more)        # черта + ряды подвала
        used = max(self.last_used() + 1, self.y + 1)
        return len(self.history) + used + panel

    def last_used(self) -> int:
        """Последний заполненный ряд кадра (или ряд курсора), -1 — пусто."""
        for y in range(ROWS - 1, -1, -1):
            if any(c != 0x20 for c in self.cells[y]) or y == self.y:
                return y
        return -1

    def page_printed(self) -> bool:
        """Выдано больше одной страницы кадра.

        Пока выведено не больше страницы, листать вверх нельзя: PgUp
        «циклил» бы по пустым рядам кадра вместо истории выдачи. Вверх
        листаем, когда есть что показывать — когда сошло больше одной
        страницы.
        """
        used = max(self.last_used() + 1, self.y + 1)
        return len(self.history) + used > ROWS

    def _follow_top(self, h: int) -> int:
        # следим за кадром: низ окна у курсора (или весь кадр с подвалом,
        # если влезает); в историю автоматически не заглядываем
        base = len(self.history)
        return max(base, min(self.view_rows() - h, base + self.y - h + 1))

    def scroll_view(self, delta: int, h: int | None = None) -> None:
        """PgUp/PgDn: страница просмотра вверх/вниз (в историю и обратно).
        Вверх — только когда выдано больше одной страницы кадра
        (`page_printed`), иначе листать нечего и окно остаётся следящим;
        вниз — всегда. Приём от ЭВМ закреплённого окна не сбрасывает."""
        h = h or self._view_h
        if delta < 0 and not self.page_printed():
            return                 # выдано не больше страницы: вверх нельзя
        max_top = max(0, self.view_rows() - h)
        if max_top == 0:
            return                 # листать нечего: остаёмся следящими
        top = (self._follow_top(h) if self.view_top is None
               else self.view_top)
        self.view_top = max(0, min(max_top, top + delta))

    def sync_view_top(self, h: int) -> int:
        """Верх окна просмотра под текущую высоту окна терминала.

        Окно могло подрасти (масштаб шрифта уменьшили — в терминал влезло
        больше рядов): закреплённое окно сдвигаем вниз, к началу контента,
        чтобы подвал остался внизу экрана. Следящий режим (view_top is None)
        ведёт себя как _follow_top: кадр — с верхнего ряда окна, история в
        поле зрения не выползает — иначе «ОЧИСТКА» (F5) выглядит как
        «текст не стёрт, а уехал вверх».
        """
        if self.view_top is None:
            return self._follow_top(h)
        max_top = max(0, self.view_rows() - h)
        self.view_top = min(self.view_top, max_top)
        return self.view_top

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
        gone = self.cells.pop(0)
        gone_attr = self.attr.pop(0)
        self._push_history(gone, gone_attr)       # в «историю» просмотра
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
    def put(self, code: int, blink: bool = False, ctrl: bool = False,
            advance: bool = True) -> None:
        code &= 0x7F
        self.cells[self.y][self.x] = code
        a = ATTR_INV if self.inverse else 0
        if blink:
            a |= ATTR_BLINK
        if ctrl:
            # УП кладётся в ВЗУ как есть (код < 0x20) и показывается лишь
            # при включённом «УПР.СИМВ» — переключение работает по экрану,
            # а не по будущим байтам
            a |= ATTR_CTRL | ATTR_BLINK
        self.attr[self.y][self.x] = a
        if not advance:
            return
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

    def clear_screen_kept(self) -> None:
        """Клавиша «ОЧИСТКА»: экран (кадр ЭВМ) очищается с возвратом в
        начало первой строки, но сошедший экран целиком уходит в «историю»
        — её очистка не трогает."""
        for row, attr in zip(self.cells, self.attr):
            self._push_history(row, attr)
        self.clear_all()
        self.view_top = None               # смотреть с начала нового экрана

    def set_service(self, text: str) -> None:
        # текст с переводом строк раскладывается на несколько рядов
        # подвала; каждый ряд — строго 80 знаков
        rows = text.split("\n")[:10]
        self.service = [" "] * COLS
        first = (rows[0] if rows else "")[:COLS].ljust(COLS)
        for i, ch in enumerate(first):
            self.service[i] = ch
        self.service_more = [r[:COLS].ljust(COLS) for r in rows[1:]]
        self.service_dirty = True

    # --- выдача -----------------------------------------------------
    def display_code(self, code: int, attr: int) -> int:
        """Код ВЗУ → код отображения для рендеров: образ УП по «УПР.СИМВ»;
        набор знаков применяется далее по display_set (rom_slot)."""
        if attr & ATTR_CTRL:
            return (code | 0x40) if self.show_ctrl else 0x20
        return code

    def char_at(self, y: int, x: int) -> str:
        """Знак позиции как символ — с учётом «УПР.СИМВ» и набора."""
        from .charset import display_char
        a = self.attr[y][x]
        code = self.cells[y][x]
        if a & ATTR_CTRL:
            return display_char(code | 0x40, "n0") if self.show_ctrl else " "
        return display_char(code, self.display_set)

    def glyph(self, y: int, x: int) -> int:
        """Знак в позиции для растровых/текстовых рендеров."""
        return self.display_code(self.cells[y][x], self.attr[y][x])

    def text(self) -> str:
        return "\n".join("".join(self.char_at(y, x) for x in range(COLS))
                         for y in range(ROWS))

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