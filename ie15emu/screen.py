"""ОЗУ кадра терминала 15ИЭ-00-013: 80 знаков в ряду, 7 разрядов на позицию.

Как в терминале: код позиции + атрибут инверсии. Кадр у настоящего
15ИЭ — 25 строк (как у Видеотона-340: кадры ДКС «ИГРА» разложены на
25 строк); служебная панель выводится под кадром.

Рядов кадра может быть больше 25: подвал (панель состояний и легенда
клавиш) занимает минимум рядов, а остальное место окна терминала отдано
кадру — `set_rows()`. Ширина кадра всегда 80 знаков, как на «железе».
"""
from __future__ import annotations

from contextlib import contextmanager

from . import COLS, ROWS, SERVICE_ROW

MAX_ROWS = 100       # предохранитель: кадр выше не растём

ATTR_INV = 0x80    # младшие 7 битов — код ВЗУ, старший — инверсия
ATTR_BLINK = 0x40  # мигание (режим blink Видеотона-340)
ATTR_CTRL = 0x20   # знак — управляющий код, сохранённый как есть: виден
                   # только при включённом «УПР.СИМВ» (парсер/экран)

SEP = " | "         # разделитель ячеек подвала

# Ячейка подвала — сегменты (текст, стиль): метка обычная, значение или
# целая ячейка легенды может быть выделена цветом.
Cell = tuple[tuple[str, str], ...]

# Стили подвала (цвет значения состояния и групп легенды)
STYLE_ANSI = {
    "on": "\x1b[32m",        # включено — зелёный
    "off": "\x1b[31m",       # выключено — красный
    "state": "\x1b[96m",     # прочее значение состояния — светло-голубой
    "last": "\x1b[97m",      # последнее нажатие (ПОСЛ) — белый
    "fkey": "\x1b[93m",      # клавиши F5…F10 — жёлтый
    "ctrl": "\x1b[95m",      # сочетания с Ctrl (УПР) — маджента
    "edit": "\x1b[94m",      # правка: ДОМ/КОНЕЦ/ВСТАВКА/УДАЛИТЬ — синий
}


def mk(*segs) -> Cell:
    """Ячейка подвала из сегментов: mk("СЕТЬ=", ("ВКЛ", "on"))."""
    return tuple((s, "") if isinstance(s, str) else s for s in segs)


class _Rule:
    """Отбивка групп подвала сплошной чертой «-» (см. `RULE`)."""

    def __repr__(self) -> str:                       # pragma: no cover
        return "<черта>"


class _Help:
    """Маркер начала справки (рядов-легенд подвала) — см. `HELP`."""

    def __repr__(self) -> str:                       # pragma: no cover
        return "<справка>"


RULE = _Rule()   # ячейка-разделитель: ряд состояний от справки и т. п.
HELP = _Help()   # с этой ячейки начинается справка — её можно убрать


def cell_text(cell: Cell) -> str:
    """Ячейка подвала сплошным текстом (для дампов и PNG)."""
    return "".join(t for t, _ in cell)


class Screen:
    def __init__(self, history: int = 1000) -> None:
        self.rows = ROWS       # рядов кадра: 25 у «железа», больше — на всю
                               # высоту окна минус подвал
        self.cells = [[0x20] * COLS for _ in range(self.rows)]
        self.attr = [[0] * COLS for _ in range(self.rows)]
        self.service = [" "] * COLS   # служебная строка (первый ряд подвала)
        self.service_more: list[str] = []   # дополнительные ряды подвала
        self.service_dirty = True
        self.panel_cols = COLS     # ширина подвала: тянется на окно терминала
        self._panel: tuple[list[Cell], list[Cell]] | None = None
        self._panel_rows: list[list[Cell]] = []   # ряды подвала (с цветами)
        self._panel_colw = COLS    # ширина колонки сетки подвала
        self.status_cells: list[Cell] = []   # строка состояния линии
        # выделение текста мышью: список фрагментов, каждый — список
        # (вид, номер ряда, от, до, текст ряда); незаконченное протягивание
        self.selections: list[list[tuple[str, int, int, int, str]]] = []
        self._drag: list | None = None   # [ak, ai, ax, ek, ei, ex, текст, база, колонка]
        self.sel_kind = "СТР"     # вид последнего выделения: СТР/КОЛ
        # счётчики для протяжки мышью: сколько раз кадр смещался вверх,
        # сколько непустых рядов ушло в историю и сколько из них отброшено
        # (пустые ряды в историю не пишутся — поэтому счётчика два)
        self.scrolled = 0
        self.pushed = 0
        self.dropped = 0
        # карта видимой части окна: номер ряда окна → (вид, номер ряда);
        # её заполняет отрисовка (text_screen), по ней клик попадает в текст
        self.view_map: list[tuple[str, int] | None] = []
        self.view_top_row = 4          # номер ряда окна для view_map[0]
        self._rowtext: dict[tuple[str, int], tuple[int, str]] = {}  # кэш текста
        self.show_help = True   # ряды-легенды подвала (справка по клавишам)
        self._service_rows: list[str] = []   # готовые ряды (set_service)
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

    def set_rows(self, rows: int) -> None:
        """Рядов кадра — под текущую высоту окна терминала.

        Подвал занимает минимум рядов, всё остальное место окна отдано
        кадру ЭВМ (у настоящего 15ИЭ — 25, как у Видеотона-340). Расти
        выше MAX_ROWS не даём; при уменьшении окна лишние ряды кадра
        уходят снизу (текст сверху не уплывает).
        """
        rows = max(ROWS, min(int(rows), MAX_ROWS))
        if rows == self.rows:
            return
        if rows > self.rows:
            for _ in range(rows - self.rows):
                self.cells.append([0x20] * COLS)
                self.attr.append([0] * COLS)
        else:
            del self.cells[rows:]
            del self.attr[rows:]
        self.rows = rows
        self.y = min(self.y, rows - 1)

    def _push_history(self, row: list[int], attr: list[int]) -> None:
        if self.history_max <= 0:
            return
        if all(c == 0x20 for c in row):
            return                          # пустые строки в историю не пишем
        self.history.append((row[:], attr[:]))
        self.pushed += 1              # порядковый номер ряда в потоке выдачи
        if len(self.history) > self.history_max:
            del self.history[0]
            self.dropped += 1

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
        for y in range(self.rows - 1, -1, -1):
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
        return len(self.history) + used > self.rows

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
        if self.y >= self.rows:
            self.scroll_up()
            self.y = self.rows - 1

    def cr(self) -> None:
        self.x = 0

    def lf(self) -> None:
        # ПС на алфавитно-цифровых устройствах того времени — «новая
        # строка» с возвратом каретки: драйвер ДИСПАК печатает строки
        # Электроники шаблоном «ПС текст ПР» и не дублирует ПР в начале
        self.x = 0
        self.y += 1
        if self.y >= self.rows:
            self.scroll_up()
            self.y = self.rows - 1

    def bs(self) -> None:
        if self.x > 0:
            self.x -= 1

    def ht(self) -> None:
        self.x = min((self.x // 8 + 1) * 8, COLS - 1)

    def scroll_up(self) -> None:
        self.scrolled += 1             # ряд ушёл из кадра (для протяжки)
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
        n = self.rows * COLS
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
        self.y = min(self.rows - 1, self.y + 1)

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
        for y in range(self.y + 1, self.rows):
            self.cells[y] = [0x20] * COLS
            self.attr[y] = [0] * COLS

    def clear_all(self) -> None:
        self.cells = [[0x20] * COLS for _ in range(self.rows)]
        self.attr = [[0] * COLS for _ in range(self.rows)]
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
        # готовые ряды подвала (каждый — строка, ряды добиты пробелами);
        # ширину рядов и их число правит relayout()
        self._service_rows = [r for r in text.split("\n")[:10] if r]
        self._panel = None
        self.relayout()

    # --- подвал ------------------------------------------------------
    def set_help(self, on: bool) -> None:
        """Показывать ли ряды-легенды (справку по клавишам) в подвале.

        Справку можно убрать (`[terminal] help = off`, `--no-help`): в
        подвале остаются ряды состояний (нижняя строка состояния), они
        неотключаемы — по ним видно режим сеанса и последнее нажатие.
        """
        on = bool(on)
        if on != self.show_help:
            self.show_help = on
            self.relayout()

    def set_panel(self, cells: list[Cell], wide: list[Cell] = ()) -> None:
        """Подвал списком ячеек: «метка=значение», разделены « | ».

        Ячейки (кроме wide) ложатся в РОВНЫЕ колонки: ширина колонки
        одинакова во всех рядах, колонок столько, сколько влезает в
        ширину окна, — разделители « | » стоят на одних позициях. Ячейки
        из wide (длинные, например «ПОСЛ: …») занимают ряд целиком и на
        ширину колонок не влияют. Пустая ячейка — конец группы: группа
        начинается с нового ряда и отбивается чертой «-».

        Цветов не видно в дампах и PNG — там панель выводится текстом
        (см. `service`/`service_more`), цвета только в терминале
        (`panel_styled_rows`).
        """
        self._service_rows = []
        self._panel = ([mk(c) if isinstance(c, str) else c for c in cells],
                       [mk(c) if isinstance(c, str) else c for c in wide])
        self.relayout()

    # --- выделение текста мышью ------------------------------------
    # Выделение живёт в координатах содержимого: ряд кадра («frame», y)
    # или ряд истории («hist», i) плюс диапазон знаков. Каждый завершённый
    # «протяг» — отдельный фрагмент, фрагментов может быть сколько угодно;
    # текст фрагмента снимается снимком в момент отпускания кнопки.
    #
    # Протяжка держится в координатах содержимого, а НЕ окна: во время
    # протягивания ЭВМ успевает выдать строки, кадр смещается, и привязка
    # к ряду окна уводила начало на другую строку — выделение разрасталось
    # до половины экрана. Снимок текста анкера позволяет найти строку и
    # после того, как она ушла в историю.
    def sel_begin(self, kind: str, idx: int, x: int,
                    block: bool = False) -> None:
        """Начало протягивания (kind, idx — ряд кадра или истории).

        block=True — выделение колонкой (протяжка мышью с Ctrl):
        берётся прямоугольник знаков, одинаковый для всех рядов.
        Иначе — строчное выделение: от нажатой строки до отпущенной.
        """
        atext = self.row_text(kind, idx) or ""
        # где строка окажется, когда кадр сместится: её номер в потоке
        # выдачи (непустые ряды считаются отдельно — пустые в историю не
        # пишутся) и число смещений кадра на момент нажатия
        above = sum(1 for y in range(idx)
                    if (self.row_text("frame", y) or "").strip()) \
            if kind == "frame" else 0
        base = (self.scrolled, self.pushed + above if kind == "frame" else -1)
        self._drag = [kind, idx, x, kind, idx, x, atext, base, block]

    def sel_drag(self, kind: str, idx: int, x: int) -> None:
        """Текущая точка протягивания (конец тянется за указателем)."""
        if self._drag is not None:
            self._drag[3] = kind
            self._drag[4] = idx
            self._drag[5] = x

    def _view_of(self, kind: str, idx: int, text: str | None = None
                 ) -> int | None:
        """Ряд окна с данным содержимым.

        Если задан текст, ряд подходит только когда текст совпал: при выводе
        ЭВМ кадр смещается, и ряд с прежним номером уже другой строкой.
        """
        try:
            v = self.view_map.index((kind, idx))
        except ValueError:
            return None
        if text is not None and self.row_text(kind, idx) != text:
            return None
        return v

    def _view_by_text(self, text: str, near: int | None) -> int | None:
        """Ряд окна с таким же текстом (строка уехала в историю)."""
        if not text:
            return None
        best = None
        for v, ent in enumerate(self.view_map):
            if ent is None:
                continue
            if self.row_text(*ent) == text and (best is None or
                                                (near is not None and
                                                 abs(v - near) <
                                                 abs(best - near))):
                best = v
        return best

    def _anchor_now(self, ak: str, ai: int, atext: str, base):
        """Где оказалась строка начала протяжки, пока шла выдача ЭВМ.

        Строка помнит свой порядковый номер в потоке выдачи (непустые
        ряды) и число смещений кадра на момент нажатия — по ним она
        находится и в кадре, и в истории, даже если кадр уехал на десятки
        рядов. Проверяем текстом: на всякий случай строка могла быть
        перезаписана.
        """
        scrolled0, k = base
        if ak != "frame" or not atext or k < 0:
            return (ak, ai)
        frame_idx = ai - (self.scrolled - scrolled0)      # где она в кадре
        if 0 <= frame_idx < self.rows:
            cand = ("frame", frame_idx)
            if self.row_text(*cand) == atext:
                return cand
        hist_idx = k - self.dropped                        # где она в истории
        if 0 <= hist_idx < len(self.history):
            cand = ("hist", hist_idx)
            if self.row_text(*cand) == atext:
                return cand
        return (ak, ai)

    def _off_text(self, anchor, first) -> list[str]:
        """Текст строк между якорем и первой видимой — они вне окна.

        Идём по содержимому, а не по номерам: пустые ряды в историю не
        пишутся, и арифметика по индексам тут соврала бы.
        """
        ak, ai = anchor
        fk, fi = first
        out = []

        def add(kind, i):
            t = self.row_text(kind, i)
            if t and t.strip():
                out.append(t)

        if ak == "hist" and fk == "hist":
            for i in range(ai, fi, 1 if fi >= ai else -1):
                add("hist", i)
        elif ak == "hist":                        # якорь в истории, окно ниже
            for i in range(ai, len(self.history)):
                add("hist", i)
            for y in range(fi):
                add("frame", y)
        elif fk == "hist":                        # якорь в кадре, окно выше
            for y in range(ai, fi, 1 if fi >= ai else -1):
                add("frame", y)
        return out

    def _clamp_view(self, ve: int) -> int | None:
        """Ряд окна для начала протяжки, если строка ушла из вида.

        Выводит ЭВМ, начало уехало за верх окна — прижимаем его к краю
        видимой части (ближнему к концу протяжки), иначе выделение было бы
        пустым.
        """
        n = len(self.view_map)
        if n == 0:
            return None
        va = 0 if ve >= n // 2 else n - 1
        step = 1 if va == 0 else -1
        while 0 <= va < n:
            ent = self.view_map[va]
            if ent is not None and (self.row_text(*ent) or "").strip():
                return va
            va += step
        return None

    def _span(self, va: int, xa: int, vb: int, xb: int, block: bool = False):
        """Фрагмент от точки (va, xa) до точки (vb, xb) по рядам окна.

        block — колонка: диапазон знаков один на все ряды (порядок
        столбцов не важен, берём от меньшего к большему). Иначе —
        строчное выделение: у первой строки от xa до конца, у последней
        от начала до xb, промежуточные — целиком.
        """
        if vb < va:                              # ряд всегда сверху вниз
            va, vb = vb, va
            xa, xb = xb, xa
        if block:
            xa, xb = min(xa, xb), max(xa, xb)
        out: list[tuple[str, int, int, int, str]] = []
        for v in range(va, vb + 1):
            ent = self.view_map[v] if 0 <= v < len(self.view_map) else None
            if ent is None:
                continue
            kind, idx = ent
            row_text = self.row_text(kind, idx)
            if row_text is None:
                continue
            if block:
                a, b = xa, xb
            else:
                a = xa if v == va else 0
                b = xb if v == vb else len(row_text) - 1
            a = max(0, min(a, len(row_text) - 1))
            b = max(a, min(b, len(row_text) - 1))
            piece = row_text[a:b + 1].strip()
            # пустой ряд в выделение не попадает: подсветка по тексту иначе
            # нашла бы его вместе со всеми пустыми рядами кадра
            if not piece:
                continue
            out.append((kind, idx, a, b, row_text))
        return out

    def _fragment(self) -> list[tuple[str, int, int, int, str]]:
        """Фрагмент текущей протяжки — и для подсветки, и для отдачи.

        Ряд кадра или истории — их видно, их подсвешиваем. Если строка
        начала ушла за окно, её текст (и текст строк между ней и окном)
        попадает в фрагмент служебными рядами «mem»: выделение не должно
        расползаться на пол-экрана, но и текст выбранной строки теряться
        не должен.
        """
        drag = self._drag
        if not drag:
            return []
        ak, ai, ax, ek, ei, ex, atext, base, block = drag
        ve = self._view_of(ek, ei)                   # конец протяжки в окне
        anchor = self._anchor_now(ak, ai, atext, base)
        va = self._view_of(*anchor, atext)           # начало в окне
        if va is None:
            va = (self._view_by_text(atext, ve) if ve is not None else None)
        if va is None:
            return self._off_fragment(anchor, ve, ex, ax, block)
        if ve is None:
            # отпустили мимо текста (над подвалом, за краем) — конец
            # прижимаем к последней строке с текстом
            ve = self._clamp_view(va) or va
        return self._span(va, ax, ve, ex, block)

    def _off_fragment(self, anchor, ve: int | None, ex: int, ax: int,
                      block: bool):
        """Начало протяжки за пределами окна: ушедшие строки + видимая часть."""
        first = next((v for v, e in enumerate(self.view_map) if e), None)
        if first is None:                           # в окне нет текста
            return []
        out = []
        if anchor != self.view_map[first]:
            # колонка режет ушедшие строки по тому же диапазону знаков,
            # строчное выделение — от знака нажатия (дальше до конца строки)
            lo, hi = (min(ax, ex), max(ax, ex)) if block else (ax, 10 ** 6)
            for i, t in enumerate(self._off_text(anchor, self.view_map[first])):
                piece = t[lo:hi + 1].strip() if i or not block else t[lo:].strip()
                if piece:
                    out.append(("mem", 0, 0, len(piece) - 1, piece))
        if ve is None:
            ve = self._clamp_view(first) or first
        if ve >= first:                             # конец ниже начала окна
            out += self._span(first, 0, ve, ex, block)
        else:                                       # конец выше: окно снизу
            out += self._span(ve, ex, first, 0, block)
        return out

    def sel_end(self) -> str:
        """Отпускание кнопки: фрагмент зафиксирован, вернём его текст."""
        block = bool(self._drag and self._drag[8])
        frag = self._fragment()
        self._drag = None
        if not frag:
            return ""
        self.selections.append(frag)
        self.sel_kind = "КОЛ" if block else "СТР"
        return "\n".join(t[4][t[2]:t[3] + 1].strip() for t in frag)

    def sel_cancel(self) -> None:
        self._drag = None

    def sel_stats(self) -> tuple[int, int, int]:
        """Размер выделения: (фрагментов, строк, знаков)."""
        lines = chars = 0
        for frag in self.selections:
            for _, _, a, b, row_text in frag:
                piece = row_text[a:b + 1].strip()
                if piece:
                    lines += 1
                    chars += len(piece)
        return len(self.selections), lines, chars

    def sel_clear(self) -> None:
        """Снять всё выделение (в том числе незаконченное протягивание)."""
        self._drag = None
        self.selections.clear()

    def sel_text(self) -> str:
        """Весь выделенный текст: фрагменты в порядке выделения."""
        out = []
        for frag in self.selections:
            for _, _, a, b, row_text in frag:
                s = row_text[a:b + 1].strip()
                if s:
                    out.append(s)
        return "\n".join(out)

    def sel_spans(self):
        """Данные для отрисовки выделения инверсией.

        Возвращает (по ряду, текст ряда, по тексту): диапазоны знаков по
        ключу (вид, номер ряда) — им пользуемся, только если текст ряда
        совпал со снимком (иначе ряд уже другой — кадр прокрутился), и
        запасной ключ по самому тексту — тогда выделение остаётся на
        «уехавшей» строке, когда она ушла в историю.
        """
        by_key: dict[tuple[str, int], list[tuple[int, int]]] = {}
        key_text: dict[tuple[str, int], str] = {}
        by_text: dict[str, list[tuple[int, int]]] = {}
        if not self.selections and self._drag is None:
            return by_key, key_text, by_text
        # сколько раз каждый текст встречается в видимой части: запасной
        # ключ по тексту нужен лишь для строк, у которых нет копий, иначе
        # выделил одну строку — подсветились бы все одноимённые (а пустые
        # строки кадра и подавно разом)
        seen: dict[str, int] = {}
        for ent in self.view_map:
            if ent is not None:
                t = self.row_text(*ent) or ""
                seen[t] = seen.get(t, 0) + 1
        for frag in list(self.selections) + [self._fragment()]:
            for kind, idx, a, b, row_text in frag:
                if kind == "mem":
                    continue          # строка вне окна: подсвечивать нечего
                by_key.setdefault((kind, idx), []).append((a, b))
                key_text[(kind, idx)] = row_text
                # запасной ключ по тексту: им воспользуется отрисовка, когда
                # строка ушла из кадра в историю (у неё уже другой ключ).
                # Только для уникальных текстов — иначе выделение одной
                # строки подсветило бы все одноимённые.
                if row_text and seen.get(row_text) == 1:
                    by_text.setdefault(row_text, []).append((a, b))
        return by_key, key_text, by_text

    def row_text(self, kind: str, idx: int) -> str | None:
        """Текст ряда кадра или истории как он виден на экране.

        Ряд не меняется между кадрами развёртки (а история тем более),
        поэтому текст держим в кэше по подписи содержимого ряда: протяжка
        мышью даёт десятки событий в секунду, а пересборка текста всех рядов
        на каждом — самая дорогая часть отрисовки.
        """
        from .charset import display_char
        if kind == "hist":
            if not 0 <= idx < len(self.history):
                return None
            cells, attrs = self.history[idx]
        else:
            if not 0 <= idx < self.rows:
                return None
            cells, attrs = self.cells[idx], self.attr[idx]
        key = (kind, idx)
        sig = hash(bytes(cells) + bytes(attrs))
        hit = self._rowtext.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        out = []
        for x, code in enumerate(cells[:COLS]):
            a = attrs[x]
            if a & ATTR_CTRL:
                out.append(display_char(code | 0x40, "n0") if self.show_ctrl
                           else " ")
            else:
                out.append(display_char(code, self.display_set))
        text = "".join(out).rstrip()
        if len(self._rowtext) > 4000:        # история растёт — чистим кэш
            self._rowtext.clear()
        self._rowtext[key] = (sig, text)
        return text

    def panel_cells(self) -> list[str]:
        """Ячейки подвала текстом, как их сложил set_panel (для проверок)."""
        if self._panel is not None:
            return [cell_text(c) for c in self._panel[0] + self._panel[1]
                    if c is not RULE and c is not HELP]
        return [c for r in self._service_rows for c in r.split(SEP)]

    def _grid(self, cells: list[Cell], wide: list[Cell],
              w: int) -> tuple[list[list[Cell]], int]:
        """Ряды подвала с ровными колонками, растянутые на ширину окна.

        Ячейки идут группами (пустая ячейка — конец группы, RULE — конец
        группы с отбивкой чертой, HELP — начало справки, её можно
        убрать). Группа, которая целиком влезает в остаток текущего
        ряда, дописывается в него (цвет отделяет её от соседей) — лишних
        рядов подвал не занимает; не влезает — начинает новый ряд. RULE
        всегда разрывает ряд и рисует черту.
        """
        cells = list(cells)
        if not self.show_help:
            for i, c in enumerate(cells):
                if c is HELP:                 # справка убрана — режем с неё
                    end = i - (i and cells[i - 1] is RULE)
                    cells = cells[:end]
                    break
        plain = ["" if c is RULE or c is HELP else cell_text(c)
                 for c in cells]
        fields = [p for p in plain if p]
        if not fields:
            return [], w
        # ширину колонки задают обычные ячейки: длинная «ПОСЛ: …» не должна
        # сжимать сетку, но в колонку не влезает — занимает ряд целиком
        longest = max((len(p) for p, c in zip(plain, cells)
                       if p and c is not RULE and c not in wide), default=0)
        cols = max(1, (w + len(SEP)) // (longest + len(SEP)))
        colw = (w - len(SEP) * (cols - 1)) // cols
        rows: list[list[Cell]] = []
        row: list[Cell] = []

        def flush() -> None:
            if row:
                rows.append(row[:])
                row.clear()

        groups: list[tuple[bool, list[Cell]]] = []   # (черта после?, ячейки)
        cur: list[Cell] = []
        for p, c in zip(plain, cells):
            if c is RULE or not p:
                groups.append((c is RULE, cur))
                cur = []
            else:
                cur.append(c)
        groups.append((False, cur))

        for rule, group in groups:
            if group:
                # группа с широкой ячейкой («ПОСЛ») в остаток ряда не
                # вписывается: она всегда идёт своим рядом, иначе высота
                # подвала (а с ней и высота кадра) скачет от нажатия
                fits = row and all(c not in wide for c in group) \
                    and all(len(cell_text(c)) <= colw for c in group) \
                    and len(row) + len(group) <= cols
                if fits:                     # дописываем в остаток ряда
                    row.extend(group)
                    if len(row) == cols:
                        flush()
                else:
                    flush()
                    for c in group:
                        if c in wide or len(cell_text(c)) > colw:  # ряд сам
                            flush()
                            rows.append([c])
                            continue
                        row.append(c)
                        if len(row) == cols:
                            flush()
            if rule:                         # отбивка после группы — черта
                flush()
                rows.append([mk("-" * w)])
        flush()
        return rows, colw

    def relayout(self) -> None:
        """Пересобрать ряды подвала под текущую ширину окна терминала.

        Ряды добиваются пробелами до panel_cols, чтобы не оставалось
        хвоста от прошлой отрисовки; ни одна ячейка не рвётся.
        """
        w = self.panel_cols
        if self._panel is not None:
            self._panel_rows, self._panel_colw = self._grid(
                self._panel[0], self._panel[1], w)
            rows = [self._row_text(r) for r in self._panel_rows]
        else:
            self._panel_rows = []
            self._panel_colw = w
            rows = list(self._service_rows)
        if not rows:
            rows = [""]
        rows = [r[:w].ljust(w) for r in rows]
        self.service = list(rows[0])
        self.service_more = rows[1:]
        self.service_dirty = False

    # --- строка состояния линии (вне кадра ЭВМ) --------------------
    def set_status(self, cells: list[Cell]) -> None:
        """Строка состояния над кадром: тип линии, номера, режим, время.

        Это «обвязка» сеанса на стороне оператора, в кадр ЭВМ она не
        входит: её рисует терминал над кадром, в историю выдачи и в
        листание (PgUp/колесо) не попадает.
        """
        self.status_cells = [mk(c) if isinstance(c, str) else c for c in cells]

    def status_line(self) -> str:
        """Строка состояния с цветом, дописанная до ширины окна."""
        if not self.status_cells:
            return ""
        parts = []
        for cell in self.status_cells:
            text = ""
            for t, st in cell:
                color = STYLE_ANSI.get(st, "")
                text += f"{color}{t}\x1b[0m" if color else t
            parts.append(text)
        line = SEP.join(parts).rstrip()
        # видимую длину считаем без управляющих кодов
        visible = sum(len(cell_text(c)) for c in self.status_cells) + \
            len(SEP) * (len(self.status_cells) - 1)
        return line + " " * max(self.panel_cols - visible, 0)

    def _row_text(self, row: list[Cell]) -> str:
        """Ряд подвала текстом: ячейки в колонках, разделители « | »."""
        return SEP.join(cell_text(c).ljust(self._panel_colw)
                        for c in row).rstrip()

    def panel_styled_rows(self) -> list[str]:
        """Ряды подвала с цветом — для вывода в терминал.

        Каждая ячейка печатается в своей колонке (ширина колонки общая
        для всех рядов), разделители « | » стоят на одних позициях, ряд
        добивается пробелами до ширины окна.
        """
        if not self._panel_rows:
            return [r.rstrip() for r in
                    ["".join(self.service)] + self.service_more]
        out = []
        for row in self._panel_rows:
            parts = []
            for cell in row:
                text = ""
                for t, st in cell:
                    color = STYLE_ANSI.get(st, "")
                    text += f"{color}{t}\x1b[0m" if color else t
                # добиваем по ВИДИМОЙ длине: управляющие коды в строке
                # есть, в колонке — нет, иначе цветные ячейки «поедут»
                pad = self._panel_colw - len(cell_text(cell))
                parts.append(text + " " * max(pad, 0))
            # ширину режет сетка (cols*colw + разделители ≤ panel_cols),
            # обрезать по знакам нельзя — в строке есть управляющие коды
            out.append(SEP.join(parts).rstrip())
        return out

    def set_panel_cols(self, cols: int) -> None:
        """Ширина подвала = ширина окна терминала (кадр всегда 80)."""
        cols = max(COLS, min(int(cols), 400))
        if cols != self.panel_cols:
            self.panel_cols = cols
            self.relayout()

    def panel_rows(self) -> int:
        """Рядов подвала (без черты над ним) — столько места он занимает."""
        return 1 + len(self.service_more)

    @contextmanager
    def panel_width(self, cols: int):
        """Временно сузить подвал до ширины кадра (PNG/дамп 80 знаков)."""
        old = self.panel_cols
        self.set_panel_cols(cols)
        try:
            yield self
        finally:
            self.set_panel_cols(old)

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
                         for y in range(self.rows))

    def dump(self) -> str:
        out = ["+" + "-" * COLS + "+"]
        for i, line in enumerate(self.text().splitlines()):
            marker = "*" if i == self.y else " "
            out.append(f"|{line}|{marker}")
        out.append("+" + "-" * COLS + "+")
        with self.panel_width(COLS):
            out.append("|" + "".join(self.service) + "|  (служебная панель, поз."
                       f"{SERVICE_ROW} — под кадром ЭВМ)")
            for extra in self.service_more:
                out.append("|" + extra + "|")
        out.append(f"курсор: x={self.x} y={self.y} inverse={self.inverse} bell={self.bell}")
        return "\n".join(out)