"""Интерпретатор потока ЭВМ → терминал: «набор команд №2» (частично VT52).

15ИЭ-00-013 совместим с командами VT52. Реализован базовый набор:

  УП (0x00..0x1F):  ПР(0D) ПС(0A) ВК(08) ТАБ(09) ЗВН(07) ...
  ESC [ … — CSI (ECMA-48): коды SGR/курсорных последовательностей
              проглатываются (линии SIMH VT340 шлют «ESC [ 2 m» для
              отображаемых УП); после «ESC [» буквы A/B/C/D работают
              как и без «[» — стрелки ANSI
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

from . import COLS
from .charset import ALPHA_BIT
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

# Одно-байтовые экранные команды Видеотона-340 (besm6_tty.c, vt_send):
# в наборе №2 15ИЭ эти коды не занято (русские буквы ЭВМ шлёт строкой
# 0x60..), поэтому терминал отрабатывает их как Видеотон — программы
# ДИСПАК («ИГРА») рисуют ими поле. Ровно то же vt_send выдаёт на
# операторский терминал ANSI-кодами (0x19→ESC[A, 0x1A→ESC[B, 0x18→ESC[C,
# 0x0C→ESC[H, 0x1F→ESC[H ESC[J, а 0x0B передаёт как есть) — курсорные
# команды НИЧЕГО не стирают; этим и объясняется «правильное» поле на
# tty1: новые строки вписываются поверх, хвосты старых строк остаются.
VT_RIGHT   = 0x18      # вправо     — курсор вправо  (ESC [ C)
VT_UP      = 0x19      # вверх      — курсор вверх   (ESC [ A)
VT_DOWN    = 0x1A      # вниз       — курсор вниз    (ESC [ B)
VT_ERASE   = 0x1F      # очистить — стирание экрана + «дом» (ESC [ H ESC [ J)
VT_HOME    = 0x0C      # ДОМ (\f)   — курсор в «дом» (0,0), без стирания (ESC [ H)
VT_VT      = 0x0B      # (\v)       — на Видеотоне-340 действия нет (как ПП
                       # у DEC-терминалов); кадры хода «ИГРА» используют
                       # его как заполнитель — сдвигать курсор нельзя,
                       # иначе сообщение уходит за кадр и поле скроллится


class Parser:
    """Побайтовый автомат разбора потока от ЭВМ.

    Режим 2 (по умолчанию, «набор команд №2», VT52) разбирает ESC-
    последовательности; режим 1 принимает только управляющие коды и
    печать. С БЭСМ-6 терминаль работает в режиме 2.

    Бит алфавита (0x80) у кода 0x00..0x1E — русская заглавная буква
    КОИ7 Н1, кладётся в ОЗУ без изменения значений; без бита те же коды
    — управляющие. Не распознаваемые УП (NUL, ЕОТ …) лежат в ОЗУ как есть
    с признаком ATTR_CTRL и печатаются образным знаком code|0x40 с
    миганием, только когда включён «УПР.СИМВ» (F7, по умолчанию выключен):
    NUL не выводится вовсе, ЕОТ → «C». Настоящие буквы приходят с битом
    алфавита или строкой 0x60..0x7E, поэтому «кириллица» из УП-кодов
    невозможна.
    """

    def __init__(self, mode: int = 2, history: int = 1000) -> None:
        if mode not in (1, 2):
            raise ValueError(f"неизвестный режим набора команд: {mode!r}")
        self.screen = Screen(history=history)
        self.state = "raw"
        self._esc_args: list[int] = []
        self.mode = mode         # 1 — только УП; 2 — набор №2 (VT52), по умолчанию

    # «УПР.СИМВ» (F7) — состояние на экране: рендер (текст/PNG/дамп)
    # смотрит на него при каждой отрисовке, поэтому переключение кнопки
    # действует сразу на весь экран, а не только на будущие байты
    @property
    def show_ctrl(self) -> bool:
        return self.screen.show_ctrl

    @show_ctrl.setter
    def show_ctrl(self, on: bool) -> None:
        self.screen.show_ctrl = bool(on)

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
        if s == "csi":
            # ECMA-48: ESC [ params final(0x40..0x7E). Параметры пропускаются,
            # финальный байт идёт в общий диспетчер (линии VT340 шлют
            # стрелки как ESC [ A, а УП — как «dim» ESC [ 2m …).
            if 0x40 <= b <= 0x7E:
                self.state = "raw"
                return self._esc_byte(b)
            return None
        if s == "esc_y":
            self._esc_args.append(b)
            self.state = "esc_y2"
            return None
        if s == "esc_y2":
            row, col = self._esc_args[0] - 0x20, b - 0x20
            sc = self.screen
            sc.y = max(0, min(row, sc.rows - 1))
            sc.x = max(0, min(col, COLS - 1))
            self.state = "raw"
            self._esc_args = []
            return None

        # raw
        if b >= ALPHA_BIT:
            self.screen.put(b)     # знак алфавита: русская заглавная (Н1+0x80)
            return None
        if b == ESC:
            # режим 1 (набор №1): ESC-последовательностей нет — код гасится
            if self.mode == 2:
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
        elif b == VT_UP:                 # 0x19 — курсор вверх
            self.screen.y = max(0, self.screen.y - 1)
        elif b == VT_RIGHT:              # 0x18 — курсор вправо
            self.screen.x = min(COLS - 1, self.screen.x + 1)
        elif b == VT_DOWN:               # 0x1A — курсор вниз (как ESC [ B)
            self.screen.y = min(self.screen.rows - 1,
                                   self.screen.y + 1)
        elif b == VT_ERASE:              # 0x1F — стирание экрана + «дом»
            self.screen.clear_all()
        elif b == VT_HOME:               # 0x0C — ДОМ: курсор в (0,0), не стирая
            self.screen.x = 0
            self.screen.y = 0
        elif b == VT_VT:                 # 0x0B — на Видеотоне команды нет
            pass
        elif 0x20 <= b <= 0x7F:
            self.screen.put(b)     # печать, 0x7F — сплошная заливка
        elif b == 0x00:
            # NUL — пустой код: не знак терминала, не печатается и
            # каретки не двигает (не путать с командой ДИАЛОГа «НУС» —
            # подключение нового терминала, холодный старт). В кадрах
            # ВТ-340 идёт парами «ПС NUL NUL»; сдвиг на нём ломал
            # раскладку поля (КАЛАХ: очки сыпались в левые колонки)
            pass
        elif b < 0x20:
            # прочие УП (ЕОТ, СО …) кладутся в ВЗУ как есть с признаком
            # ATTR_CTRL: рендер показывает их образом code|0x40 с миганием,
            # только когда «УПР.СИМВ» включён, — и скрывает, когда выключен,
            # сразу по всему экрану
            self.screen.put(b, ctrl=True)
        return None

    def _esc_byte(self, b: int) -> str | None:
        sc = self.screen
        self.state = "raw"
        if b == ord("["):
            self.state = "csi"
            self._esc_args = []
        elif b == ord("A"):
            sc.y = max(0, sc.y - 1)
        elif b == ord("B"):
            sc.y = min(sc.rows - 1, sc.y + 1)
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