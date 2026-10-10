"""Запуск: python3 -m pytest tests/  или  python3 tests/test_session.py"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu import ROWS
from ie15emu.charset import RUS7, decode_koi7
from ie15emu.parser import Parser
from ie15emu.screen import RULE
from ie15emu.session import MODE_HOST, MODE_LOCAL, TerminalSession
from ie15emu.keyboard import KEY_MODE, KEY_SEND


class FakeLink:
    banner = True      # как у реальной линии: ждём баннер tmxr
    port = 4202

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


def make_session(mode=MODE_LOCAL):
    link = FakeLink()
    session = TerminalSession(Parser(), link=link, mode=mode)
    return session, link


def panel_text(sc) -> str:
    """Весь подвал текстом: ряды состояний и легенды."""
    return "\n".join(["".join(sc.service)] + sc.service_more)


def test_local_mode_buffers_and_echoes():
    session, link = make_session(MODE_LOCAL)
    for ch in "hello":
        out = session.feed_key(ch)
        assert out is None          # линия молчит
    assert session.pending() == b"HELLO"
    assert link.sent == []          # ничего не ушло
    # Н2 (VT-52, по умолчанию) — набор одно-регистрный: латинская
    # клавиша без Shift даёт верх международной строки (строчные
    # коды 0x60.. линии отданы кириллице ЭВМ), он же и на экране
    assert "HELLO" in session.parser.screen.text()   # эхо


def test_send_key_transmits_buffer_then_clears():
    session, link = make_session(MODE_LOCAL)
    for ch in "work":
        session.feed_key(ch)
    out = session.feed_key(KEY_SEND)
    # ПЕРЕДАЧА завершает строку (добавлены ПР ПС) и переводит в С ЭВМ;
    # Н2 одно-регистрный — латиница в буфере и на линии верхним
    assert out == b"WORK\r\n"
    session.emit(out)
    assert link.sent == [b"WORK\r"]          # utf8: ПР ПС → один ПР
    assert session.pending() == b""
    assert session.mode == MODE_HOST


def test_keymap_remaps_before_nabor_rules():
    # [keys]: привязка применяется до правил набора; пустое значение
    # гасит клавишу; непогашенный УП виден кодом в подвале (для привязки)
    session, link = make_session(MODE_HOST)
    out = session.feed_key("\x1c")          # пока без карты — уходит УП
    assert out == b"\x1c"
    sc = session.parser.screen
    assert "УП 1C" in panel_text(sc)
    session.keymap = {"\x1c": "[", "Б": "<", "\x02": ""}
    assert session.feed_key("\x1c") == b"["       # Н2: ASCII-знак как есть
    assert session.feed_key("Б") == b"<"
    assert session.feed_key("\x02") is None       # погашена
    # и служебные клавиши переназначаемы: SEND как знак
    session.keymap = {"SEND": "\x13"}
    assert session.feed_key("SEND") == b"\x13"
    # CSI-u имена: привязка «ctrl+б = <» работает без дублей обычных
    # знаков; незакрытое сочетание показывает своё имя в подвале —
    # именно его пишут ключом в [keys]
    session.keymap = {"ctrl+б": "<"}
    assert session.feed_key("ctrl+б") == b"<"
    assert session.feed_key("б") == bytes([0x82])   # та же клавиша без
                                                    # Ctrl — русская Б
    assert session.feed_key("ctrl+х") is None
    sc = session.parser.screen
    assert "ctrl+х" in panel_text(sc)


def test_key_diagnostics_in_footer():
    # Диагностика: в подвале видно каждое нажатие — знак с кодом, имя с
    # модификатором и результат переназначения («ctrl+б→< U+003C»)
    session, _ = make_session(MODE_HOST)

    def posl():
        sc = session.parser.screen
        return panel_text(sc)

    session.feed_key("б")
    assert "б U+0431" in posl()
    session.feed_key("^")
    assert "^ U+005E" in posl()
    session.keymap = {"ctrl+б": "<"}
    session.feed_key("ctrl+б")
    assert "ctrl+б→< U+003C" in posl()
    session.keymap = {"alt+х": ""}
    session.feed_key("alt+х")
    assert "alt+х→гашение" in posl()


def test_host_mode_sends_immediately():
    session, link = make_session(MODE_HOST)
    out = session.feed_key("A")     # Н2 + латинская раскладка: Shift+A — «Ф»
    assert out == bytes([0x86])     # «Ф»=0x06 + бит алфавита
    assert session.pending() == b""  # буфер не используется
    out = session.feed_key("a")
    assert out == b"A"               # Н2 одно-регистрный: латинская — верх


def test_mode_toggle_switches_and_service_row():
    session, _ = make_session(MODE_LOCAL)
    assert "АВТОНОМНО" in "".join(session.parser.screen.service)
    session.feed_key(KEY_MODE)
    assert session.mode == MODE_HOST
    assert "С ЭВМ" in "".join(session.parser.screen.service)
    session.feed_key(KEY_MODE)
    assert session.mode == MODE_LOCAL


def test_rule_reply_escaped():
    session, _ = make_session(MODE_HOST)
    reply = session.handle_line_reply(b"\x1bZ")
    assert reply == b"\x1b/"        # «терминал есть»


def test_reply_goes_to_line():
    # ответ ESC Z обязан уходить в линию (раньше терялся в run_line)
    session, link = make_session(MODE_HOST)
    session.handle_line_reply(b"\x1bZ")
    assert link.sent == [b"\x1b/"]


def test_local_uppercase_echo_and_send_raw():
    # АВТОНОМНО: «м» должна печататься буквой, а не двигать каретку
    # (в Н2 русская клавиша без Shift — внутренний верх 0x80+код);
    # по SEND на линию RAW уходить КОИ7-строка (как «wyd» в живом тесте)
    session, link = make_session(MODE_LOCAL)
    for ch in "мамы":
        session.feed_key(ch)
    assert session.parser.screen.text().startswith("МАМЫ")
    session.encoding = "raw"
    session.emit(session.feed_key(KEY_SEND))
    assert link.sent == [b"mamy\x03"]        # без Enter ПЕРЕДАЧА сама закрыла строку
    assert session.pending() == b""


def test_local_echo_uppercase_buffer_keeps_layout():
    # Н1 с позиционной раскладкой: эхо — как даёт набор (строчная «й»),
    # в буфере — строчный код раскладки (0xEA), на линию КОИ7 (0x6A)
    link = FakeLink()
    parser = Parser()
    parser.screen.display_set = "n1"   # раскладка живёт только в Н1
    session = TerminalSession(parser, link=link, mode=MODE_LOCAL,
                              layout="positional")
    session.encoding = session.rx_encoding = "raw"
    session.feed_key("q")
    assert session.parser.screen.text().startswith("й")
    assert session.layout == "positional"
    # Н0/Н2 раскладку снимают (инвариант клавиатуры Н1):
    s2 = TerminalSession(Parser(), mode=MODE_LOCAL, layout="positional")
    assert s2.layout is None and s2.koi7 is False
    assert session.pending() == bytes([0x8A | 0x60])
    session.emit(session.feed_key("SEND"))
    assert link.sent == [bytes([0x60 | 0x0A, 0x03])]  # «j»=Й в Н1 + ETX-окончание


def test_send_dks_replaces_local_echo():
    # ДКС: Э-60 всегда эхит отданный блок. ПЕРЕДАЧА стирает локальную
    # копию — эхо машины вписывает текст на её место, без «PFLPFL»
    link = FakeLink()
    session = TerminalSession(Parser(), link=link, mode=MODE_LOCAL)
    session.encoding = "dks"
    for ch in "abc":
        session.feed_key(ch)
    cells = session.parser.screen.cells
    # локальное эхо Н2/без раскладки — заглавная латиница (0x60.. — русские)
    assert cells[0][:3] == [ord("A"), ord("B"), ord("C")]
    session.emit(session.feed_key("SEND"))
    # после SEND: строка чиста, курсор в её начало, режим — С ЭВМ
    assert cells[0][:3] == [0x20, 0x20, 0x20]
    assert (session.parser.screen.x, session.parser.screen.y) == (0, 0)
    assert session.mode == MODE_HOST
    # эхо ЭВМ ложится единственной копией (rx=utf8 — как есть)
    session.handle_line_reply(b"abc\r\n")
    assert cells[0][:3] == [ord("a"), ord("b"), ord("c")]
    assert cells[0][3] == 0x20


def test_host_uppercase_utf8_line():
    session, link = make_session(MODE_HOST)
    session.encoding = "utf8"
    session.emit(session.feed_key("м"))   # Н2: ru без Shift — внутренний верх 0x8D
    assert link.sent == ["М".encode("utf-8")]


def test_banner_sets_encoding():
    # баннер SIMH задаёт направление передачи; для UTF-8 решение
    # остаётся открытым — «Connected to DKS» способно опоздать
    session, _ = make_session(MODE_HOST)
    session.encoding = "raw"
    session.handle_line_reply(b"\r\nEncoding is UTF-8\r\n")
    assert session.encoding == "utf8" and session.rx_encoding == "utf8"
    assert not session.enc_decided

    session, _ = make_session(MODE_HOST)
    session.handle_line_reply(b"Encoding is RAW\r\n")
    assert session.encoding == "raw" and session.enc_decided


def test_dks_line_raw_with_cr_eol():
    # ДКС-линия в raw: передача — внутренние КОИ7 (русская «в» без Shift
    # в Н2 — внутренний верх 0x97 → код линии 0x77), но окончание — ПР,
    # не ETX (ETX для dks_line_char — просто знак); приём — raw + заглавные
    # (только Н2), tmxr-баннер в UTF-8 не ломается
    session, link = make_session(MODE_HOST)
    # реальный порядок баннеров tmxr: сначала «Encoding is RAW», затем
    # регистрация Э-60 «Connected to DKS»
    session.handle_line_reply(b"Encoding is RAW\r\nConnected to DKS\r\n")
    assert session.encoding == "dks" and session.rx_encoding == "raw"
    assert session.enc_decided
    session.emit(session.feed_key("в"))
    assert link.sent == [b"w"]
    # в Н2 Shift на русской раскладке меняет алфавит: «В» — клавиша d,
    # значит её верхний регистр печатает латинскую D
    session.emit(session.feed_key("В"))
    assert link.sent[-1] == b"D"
    session.emit(session.feed_key("KEY_ENTER"))      # ПР ПС → один ПР
    assert link.sent[-1] == b"\r"
    raw = bytes(0x60 + RUS7.index(c) for c in "ВЫД")  # «wyd» от машины
    session.handle_line_reply(raw)
    assert "ВЫД" in session.parser.screen.text()   # баннер занял строку 0


def test_service_panel_all_bits():
    # Подвал: ряд состояний + ряды-легенды «функция=клавиша:код» через «|»,
    # последняя нажатая клавиша — справа
    session, link = make_session(MODE_HOST)

    def rows():
        sc = session.parser.screen
        return [l.rstrip() for l in
                ["".join(sc.service)] + sc.service_more]

    def panel() -> str:
        return "\n".join(rows())

    for lab in ("СЕТЬ=С ЭВМ", "НАБОР=Н2", "ЛИНИЯ=UTF-8", "РАСК=ВЫКЛ",
                "ВИДЕО=НОРМ", "БУФЕР=ПУСТО", "УПР.СИМВ=ВЫКЛ"):
        assert lab in panel(), lab
    assert "ЗВУК" not in panel()
    session.feed_key("BLINK")                            # F7 — показать УП
    assert "УПР.СИМВ=ВКЛ" in panel() and session.parser.show_ctrl
    session.feed_key("BLINK")
    assert "УПР.СИМВ=ВЫКЛ" in panel() and not session.parser.show_ctrl
    for legend in ("ВК=Bksp", "ТАБ=Tab", "ЗВН=Ctrl-G", "ПРПС=Enter",
                   "ЭКРАН↑=PgUp", "ЭКРАН↓=PgDn",
                   "ESC=Esc", "КУРСОР=Стрелки", "ДОМ=Home", "СТЕРСТР=End",
                   "СЛОВО=Ctrl+→", "СЛОВО=Ctrl+←", "НАЧСТР=Ctrl+↑",
                   "НИЖСТР=Ctrl+↓", "ИНВЕРС=Ins",
                   "НОРМ=Del", "ЭХО=F6", "УПР.СИМВ=F7", "УПР.СИМВ=F7", "НАБОР=F8", "СЕТЬ=F9",
                   "ПЕРЕДАЧА=F10"):
        assert legend in panel(), legend
    assert "ЭХО=ВКЛ" in panel()                      # состояние в ряду состояний
    session.feed_key("ECHO")
    assert "ЭХО=ВЫКЛ" in panel()
    session.feed_key("ECHO")
    # подвал «растянут» по ширине окна, ячейки не рвутся, ряд не шире
    # окна; колонки ровные: разделители « | » стоят на одних позициях
    for i, line in enumerate(rows()):
        assert len(line) <= 80, f"ряд {i} шире 80 ({len(line)})"
    assert len(rows()) <= 10, rows()
    cells = session.parser.screen.panel_cells()
    for cell in cells:
        if cell and cell not in panel():          # ячейка не разорвана
            raise AssertionError(cell)
    seps = [l.index("|") for l in rows() if "|" in l]
    assert seps and all(p == seps[0] for p in seps), seps
    session.feed_key("KEY_END")                        # подсветка нажатия
    assert "ПОСЛ: END=ESCK" in panel()
    session.handle_line_reply(b"\x1bb")                # ESC b — инверсное
    assert "ВИДЕО=ИНВ" in panel()
    x0 = session.parser.screen.x
    session.handle_line_reply(b"\x00")             # НУС: ни знака, ни сдвига
    assert session.parser.screen.x == x0
    session.handle_line_reply(b"\x03")             # ЕОТ → знак при УПР.СИМВ
    from ie15emu.screen import ATTR_BLINK, ATTR_CTRL
    sc = session.parser.screen
    ux, uy = sc.x - 1, sc.y                        # позиция УП-клетки
    assert sc.cells[uy][ux] == 0x03                # код УП в ВЗУ как есть
    assert sc.attr[uy][ux] & (ATTR_BLINK | ATTR_CTRL)
    assert "C" not in sc.text().splitlines()[uy]   # выключено — не виден
    session.feed_key("BLINK")                          # включить показ УП
    assert "C" in sc.text().splitlines()[uy]           # виден образом «C»
    session.feed_key("BLINK")                          # выключить — исчезает
    assert "C" not in sc.text().splitlines()[uy]
    session.handle_line_reply(b"Connected to DKS\r\n")
    assert "ЛИНИЯ=ДКС" in panel() and "РАСК=ВЫКЛ" in panel()
    session.feed_key("MODE")                           # клавиша СЕАНС (F9)
    assert "СЕТЬ=АВТОНОМНО" in panel()
    for ch in "ЗАД":
        session.feed_key(ch)
    assert "БУФЕР=3" in panel()                        # буфер ждёт SEND
    session.emit(session.feed_key("SEND"))
    assert "БУФЕР=ПУСТО" in panel() and link.sent
    assert "СЕТЬ=С ЭВМ" in panel()                    # SEND вернул в сеть


def test_default_rezhim2_vt52():
    session, _ = make_session(MODE_HOST)
    assert session.parser.mode == 2
    assert session.nabor == "n2"
    assert "НАБОР=Н2" in panel_text(session.parser.screen)


def test_cmdset_key_toggles_rezhim():
    session, _ = make_session(MODE_HOST)
    session.feed_key("CMDSET")                 # F8: Н2 → Н0 (ASCII)
    assert session.nabor == "n0" and session.parser.mode == 1
    assert session.parser.screen.display_set == "n0"
    assert "НАБОР=Н0" in panel_text(session.parser.screen)
    session.feed_key("CMDSET")                 # Н0 → Н1 (русские верх+низ)
    assert session.nabor == "n1" and session.parser.mode == 1
    assert session.parser.screen.display_set == "n1"
    session.feed_key("CMDSET")                 # Н1 → Н2 — полный цикл
    assert session.nabor == "n2" and session.parser.mode == 2


def test_raw_line_upper_letters():
    # RAW-линия: буква машины из строки 0x60.. — заглавная (алфавит БЭСМ-6
    # одно-регистрный): «wyd» (0x77 0x79 0x64) → на экране «ВЫД»
    session, link = make_session(MODE_HOST)
    session.encoding = session.rx_encoding = "raw"
    raw = bytes(0x60 + RUS7.index(c) for c in "ВЫД")
    session.handle_line_reply(raw)
    assert session.parser.screen.text().startswith("ВЫД")


def test_utf8_split_across_chunks():
    # разрыв многобайтовой кириллицы между recv() не создаёт «█»-шум
    session, link = make_session(MODE_HOST)
    src = "ГОТОВ".encode("utf-8")
    for i in range(len(src)):
        session.handle_line_reply(src[i:i + 1])
    assert session.parser.screen.text().startswith("ГОТОВ")
    assert "█" not in session.parser.screen.text()


def test_ctrl_arrows_word_navigation():
    # Ctrl+→/← — по словам (разделители — пробел и точка), Ctrl+↑/↓ — по
    # началам строк; в линию эти клавиши ничего не шлют
    session, link = make_session(MODE_LOCAL)
    session.parser.feed(b"PERVOE SLOVO. VTOROE\nDRUGAYA STROKA")
    p = session.parser.screen
    p.x, p.y = 0, 0
    session.feed_key("KEY_CTRLRIGHT")          # -> «SLOVO» (после пробела)
    assert (p.x, p.y) == (7, 0)
    session.feed_key("KEY_CTRLRIGHT")          # -> «VTOROE» (после точки)
    assert (p.x, p.y) == (14, 0)
    session.feed_key("KEY_CTRLLEFT")           # уже в начале слова -> назад
    assert (p.x, p.y) == (7, 0)
    session.feed_key("KEY_CTRLLEFT")
    assert (p.x, p.y) == (0, 0)
    session.feed_key("KEY_CTRLUP")             # x и так 0
    assert (p.x, p.y) == (0, 0)
    session.feed_key("KEY_CTRLDOWN")           # начало нижней строки
    assert (p.x, p.y) == (0, 1)
    session.feed_key("KEY_CTRLRIGHT")          # «STROKA» в строке 2
    assert (p.x, p.y) == (8, 1)
    assert link.sent == []


def test_ctrl_arrows_inside_word_jump_to_start():
    session, link = make_session(MODE_LOCAL)
    session.parser.feed(b"ABC DEF")
    p = session.parser.screen
    p.x, p.y = 5, 0                            # внутри «DEF»
    session.feed_key("KEY_CTRLLEFT")
    assert (p.x, p.y) == (4, 0)                # в начало слова
    session.feed_key("KEY_CTRLLEFT")
    assert (p.x, p.y) == (0, 0)                # в начало предыдущего
    session.feed_key("KEY_CTRLRIGHT")
    assert (p.x, p.y) == (4, 0)


def test_ctrl_arrows_on_host_mode_send_nothing():
    session, link = make_session(MODE_HOST)
    session.parser.feed(b"AA BB")
    session.parser.screen.x = 3
    assert session.feed_key("KEY_CTRLLEFT") is None
    assert link.sent == []                     # в линию — ничего
    assert session.parser.screen.x == 0         # локальный курсор сдвинулся


def test_pgup_pagedown_scroll_view():
    # PgUp/PgDn — листание окна вывода (история + кадр до последней
    # заполненной строки + подвал). Вверх листаем, только когда выдано
    # больше одной страницы; выше первой строки — стоп (без цикла);
    # закрепленное окно приём не сбрасывает
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    for i in range(30):                              # 30 рядов > страницы 25
        session.handle_line_reply(bytes([0x80 | 1]) +
                                  str(i % 10).encode() + b"\r\n")
    assert len(sc.history) == 6                       # 30 - 24 строки кадра
    assert sc.page_printed() is True                  # выдано больше страницы
    sc._view_h = 24
    assert session.feed_key("KEY_PAGEUP") is None
    assert sc.view_top == 0                           # к первой строке выдачи
    session.feed_key("KEY_PAGEUP")
    assert sc.view_top == 0                           # выше — стоп, не цикл
    session.feed_key("KEY_PAGEDOWN")                 # страница вниз
    assert sc.view_top == sc.view_rows() - 24
    # узкое окно: листание от первой строки кадра до последней строки
    # подвала; выше первой строки и ниже последней — стоп, без цикла
    sc._view_h = 6
    top = sc.view_top
    session.feed_key("KEY_PAGEDOWN")                 # страница вниз
    assert sc.view_top == top + 5
    session.feed_key("KEY_PAGEUP")                   # страница вверх
    assert sc.view_top == top
    while sc.view_top > 0:                           # вверх до первой строки
        session.feed_key("KEY_PAGEUP")
    assert sc.view_top == 0
    session.feed_key("KEY_PAGEUP")                   # выше — стоп, не цикл
    assert sc.view_top == 0
    session.handle_line_reply(b"y\r\n")              # приём: окно не прыгает
    assert sc.view_top == 0
    link.sent.clear()
    session.feed_key("KEY_PAGEUP")
    session.feed_key("KEY_PAGEDOWN")
    assert link.sent == []                           # клавиши в линию не идут


def test_pageup_blocked_until_more_than_one_screen():
    # циклической прокрутки вверх нет: пока выдано не больше страницы
    # кадра, PgUp ничего не листает (окно остаётся следящим), вниз — можно
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    sc._view_h = 6
    for i in range(20):                              # одна страница, без истории
        session.handle_line_reply(bytes([0x80 | 1]) + b"A" + b"\r\n")
    assert len(sc.history) == 0
    assert sc.page_printed() is False
    for _ in range(3):
        assert session.feed_key("KEY_PAGEUP") is None
        assert sc.view_top is None                    # следящий режим
    session.feed_key("KEY_PAGEDOWN")                 # вниз — можно
    top = sc.view_top
    assert top is not None
    session.feed_key("KEY_PAGEUP")                   # вверх — по-прежнему нет
    assert sc.view_top == top                        # окно не прыгает
    assert link.sent == []
    for i in range(10):                              # выдали вторую страницу
        session.handle_line_reply(bytes([0x80 | 1]) + b"B" + b"\r\n")
    assert sc.page_printed() is True
    session.feed_key("KEY_PAGEUP")                   # вверх — можно
    assert sc.view_top == top - 5                   # страница вверх


def test_mouse_wheel_scrolls_history_without_cycle():
    # колесо мыши листает «историю» выдачи (по 3 ряда) и упирается в упор:
    # циклической прокрутки нет — вверх только при выданном >1 страницы
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    sc._view_h = 12
    for i in range(40):
        session.handle_line_reply(bytes([0x80 | 1]) + b"A" + b"\r\n")
    assert sc.page_printed() is True
    follow = sc._follow_top(sc._view_h)
    for _ in range(4):
        assert session.feed_key("KEY_WHEELUP") is None
    top = sc.view_top
    assert top is not None and top < follow          # колесо подняло окно
    for _ in range(50):                              # упор, без цикла
        session.feed_key("KEY_WHEELUP")
    assert sc.view_top == 0
    assert sc.view_top <= len(sc.history)            # первая выданная строка
    session.feed_key("KEY_WHEELDOWN")
    assert sc.view_top == 3                          # вниз — те же 3 ряда
    assert link.sent == []                           # в линию не уходит


def test_panel_stretches_and_stays_compact():
    # подвал тянется на всю ширину окна: шире — ниже панель, и окно
    # терминала можно сделать ниже (больше места оператору)
    sc = Parser().screen
    cells = [f"ПОЛЕ{i}=значение" for i in range(12)]
    sc.set_panel(cells)
    assert sc.panel_cols == 80
    narrow = sc.panel_rows()
    sc.set_panel_cols(200)
    assert sc.panel_cols == 200
    assert sc.panel_rows() <= narrow
    assert len(sc.service) == 200
    text = "\n".join(["".join(sc.service)] + sc.service_more)
    for c in cells:                       # ячейки не рвутся
        assert c in text
    assert all(len(r) == 200 for r in sc.service_more)
    with sc.panel_width(80):              # PNG/дамп всегда шириной кадра
        assert sc.panel_cols == 80
    assert sc.panel_cols == 200
    # группа, влезающая в остаток ряда, дописывается в него
    sc.set_panel(["СЕТЬ=С ЭВМ", "", "ВК=Bksp", "ТАБ=Tab"])
    assert sc.panel_rows() == 1
    assert "ВК=Bksp" in "".join(sc.service)
    # не влезает в остаток — группа начинает новый ряд (сетка 80 знаков)
    sc.set_panel_cols(80)
    many = [f"ПОЛЕ{i}=значение" for i in range(13)]
    sc.set_panel(many + [""] + ["Х=1", "Х=2", "Х=3"])
    assert sc.panel_rows() == 4, sc.panel_rows()
    # RULE — отбивка чертой: ряд состояний всегда отдельно от справки
    sc.set_panel(["СЕТЬ=С ЭВМ", RULE, "ВК=Bksp", "ТАБ=Tab"])
    assert sc.panel_rows() == 3
    assert sc.service_more[0] == "-" * sc.panel_cols


def test_panel_colors_and_groups():
    # значения состояния — цветом (ВКЛ зелёный, ВЫКЛ красный), группы
    # справки — своими цветами и в своём порядке: F-клавиши (F5…F10) в
    # верхнем ряду, сочетания с Ctrl и правка — отдельными группами
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    styled = sc.panel_styled_rows()
    plain = [l.rstrip() for l in ["".join(sc.service)] + sc.service_more]

    # значения: включено — зелёное, выключено — красное
    all_styled = "\n".join(styled)
    assert "\x1b[32mВКЛ\x1b[0m" in all_styled              # ЭХО=ВКЛ
    session.feed_key("ECHO")
    styled = sc.panel_styled_rows()
    plain = [l.rstrip() for l in ["".join(sc.service)] + sc.service_more]
    all_styled = "\n".join(styled)
    assert "\x1b[31mВЫКЛ\x1b[0m" in all_styled             # ЭХО=ВЫКЛ
    assert all_styled.count("\x1b[31mВЫКЛ\x1b[0m") >= 2    # ЭХО и УПР.СИМВ
    assert "\x1b[96m" in all_styled                       # прочие значения

    # группы справки: F-клавиши в верхнем ряду по порядку F5…F10
    fkeys = [l for l in plain if "=F" in l]
    assert "ОЧИСТКА=F5" in fkeys[0] and "ЭХО=F6" in fkeys[0]
    order = []                          # F-клавиши в порядке чтения панели
    off = 0
    for l in plain:
        for n in range(5, 11):
            p = l.find(f"F{n}")
            if p >= 0:
                order.append((off + p, n))
        off += len(l) + 1
    assert [n for _, n in sorted(order)] == [5, 6, 7, 8, 9, 10], order
    assert all("\x1b[93m" in l for l in
               styled[plain.index(fkeys[0])].split("|")[:5])  # группа F
    # группа Ctrl и группа правки — отдельными рядами и своими цветами
    ctrl = [i for i, l in enumerate(plain) if "Ctrl+" in l]
    edit = [i for i, l in enumerate(plain) if "Home" in l or "СТЕРСТР" in l]
    assert ctrl and edit and max(ctrl) < min(edit)
    assert all("\x1b[95m" in styled[i] for i in ctrl)     # Ctrl — маджента
    assert all("\x1b[94m" in styled[i] for i in edit)     # правка — синий
    # в текстовых дампах и PNG цветов нет
    assert all("\x1b" not in l for l in plain)
    assert all("\x1b[" not in l for l in
               ["".join(sc.service)] + sc.service_more)
    # …и цветные ряды дают ту же сетку: видимый текст совпадает с дампом,
    # разделители « | » стоят на одних позициях (регрессия: при выводе с
    # цветами ячейка не добивалась пробелами — колонки «плыли»)
    import re
    visible = [re.sub(r"\x1b\[[0-9;]*m", "", l).rstrip()
               for l in sc.panel_styled_rows()]
    assert visible == plain, [v for v, p in zip(visible, plain) if v != p]
    seps = {i for l in visible for i, ch in enumerate(l) if ch == "|"}
    assert len(seps) == 3, seps
    assert all(l.index("|") in seps for l in visible if "|" in l)


def test_banner_goes_to_status_line_not_frame():
    # баннер tmxr/telnet уходит в строку состояния над кадром, в кадре его
    # нет; строка состояния вне кадра ЭВМ (в листание не попадает)
    session, link = make_session(MODE_HOST)
    link.banner = True
    link.port = 4203
    session.machine = "6"
    session.handle_line_reply(
        b"Connected to the PDP-11/70 simulator TTY device, line 3\r\n"
        b"\r\nEncoding is RAW\r\n"
        b"WRU \xd0\x92\xd0\x90\r\n"
        b"Sun Oct 11 02:03:04 2026 From 127.0.0.1\r\n"
        b"Cmd> ready\r\n")
    frame = session.parser.screen.text().strip()
    assert "READY" in frame or "ЕАДЫ" in frame       # выдача ЭВМ на месте
    for junk in ("SIMULATOR", "WRU", "From", "10203"):
        assert junk not in frame, junk
    sc = session.parser.screen
    status = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line()).rstrip()
    assert session.line_type == "КВУ" and session.line_no == 3
    assert session.line_mode == "RAW" and session.banner_seen is True
    assert "ЭВМ=6" in status and "ТЕРМ=3" in status and "КАНАЛ=4203" in status
    assert "РЕЖИМ=RAW" in status and status[:4] == "КВУ "
    # дата-время в конце строки состояния: ДД.ММ.ГГГГ ЧЧ:ММ:СС
    assert re.search(r"\d\d\.\d\d\.\d{4} \d\d:\d\d:\d\d$", status)
    # баннер может прийти двумя кусками — фильтр не должен потерять выдачу
    session2, link2 = make_session(MODE_HOST)
    link2.banner, link2.port = True, 4202
    for part in (b"Encoding is UTF-8\r\n", b"Cmd> go\r\n"):
        session2.handle_line_reply(part)
    assert "go" in session2.parser.screen.text().lower()
    assert session2.line_mode == "UNICODE"


def test_status_line_warns_when_no_banner():
    # линия не «отошла» — сообщения о подключении нет: ПРОВЕРЬ ЛИНИЮ!
    session, link = make_session(MODE_HOST)
    link.banner = True
    link.port = 4202
    status = re.sub(r"\x1b\[[0-9;]*m", "",
                    session.parser.screen.status_line()).rstrip()
    assert status.startswith("ПРОВЕРЬ ЛИНИЮ!")
    assert "КАНАЛ=4202" in status                      # канал известен сразу
    session.handle_line_reply(b"Connected to DKS\r\n")
    status = re.sub(r"\x1b\[[0-9;]*m", "",
                    session.parser.screen.status_line()).rstrip()
    assert status.startswith("ДКС") and "ПРОВЕРЬ ЛИНИЮ!" not in status
    # у линии без баннера (stdio — отладка) предупреждения нет
    session3, link3 = make_session(MODE_HOST)
    link3.banner = False
    session3._update_status()
    st3 = session3.parser.screen.status_line()
    assert "ПРОВЕРЬ ЛИНИЮ" not in re.sub(r"\x1b\[[0-9;]*m", "", st3)


def test_help_rows_can_be_hidden():
    # справку (ряды-легенды) можно убрать — нижняя строка состояния с
    # полями сеанса остаётся в любом случае
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    assert sc.show_help is True
    assert "ОЧИСТКА=F5" in panel_text(sc) and "ДОМ=Home" in panel_text(sc)
    sc.set_help(False)
    panel = panel_text(sc)
    for legend in ("ОЧИСТКА=F5", "ДОМ=Home", "СЛОВО=Ctrl+→", "ВК=Bksp"):
        assert legend not in panel, legend
    for state in ("СЕТЬ=", "НАБОР=", "ЭХО=", "УПР.СИМВ=", "ПОСЛ:"):
        assert state in panel, state              # нижняя строка состояния
    assert "-" * sc.panel_cols not in panel       # черта справки убрана
    sc.set_help(True)                             # и возвращается обратно
    assert "ОЧИСТКА=F5" in panel_text(sc)


def test_panel_grid_stable_while_values_change():
    # сетка подвала (ширина колонки, число рядов, позиции « | ») не должна
    # скакать из-за меняющихся значений — в первую очередь «ПОСЛ: …»
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen

    def sig():
        rows = ["".join(sc.service)] + sc.service_more
        seps = {i for l in rows for i, ch in enumerate(l) if ch == "|"}
        return sc._panel_colw, len(rows), tuple(sorted(seps))

    base = sig()
    for key in ("a", "KEY_CTRLLEFT", "KEY_PAGEDOWN", "Б", "KEY_END",
                "KEY_WHEELUP", "F6", "F7", "F8"):
        session.feed_key(key)
        assert sig() == base, (key, sig(), base)
    for _ in range(40):                      # буфер набора растёт
        session.feed_key("x")
    assert sig() == base, sig()
    # длинное имя нажатия (переназначение клавиши видно в подвале) — тоже
    session.last_key = "ctrl+б→< U+003C"
    session._update_service()
    assert sig() == base, sig()
    assert "ПОСЛ: ctrl+б→< U+003C" in panel_text(sc)
    assert panel_text(sc).splitlines()[2].strip() == "ПОСЛ: ctrl+б→< U+003C"
    # смена ширины окна пересобирает сетку — это ожидаемо
    sc.set_panel_cols(120)
    assert sig() != base and sig()[1] < base[1]     # панель ниже


def test_history_keeps_scrolled_rows():
    # сошедшие с верхнего края кадра строки сохраняются в «историю»
    # (глубина настраивается), пустые — нет; PgUp доходит до первой
    # выданной строки
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    sc._view_h = 8
    for i in range(30):                              # 30 строк > кадра 25
        session.handle_line_reply(bytes([0x80 | 1]) +
                                  str(i % 10).encode() + b"\r\n")
    assert len(sc.history) == 6                       # 30 - 24 строки кадра
    from ie15emu.charset import decode_koi7
    assert decode_koi7(bytes(sc.history[0][0])).strip() == "А0"
    for _ in range(6):
        session.feed_key("KEY_PAGEUP")
    assert sc.view_top == 0                           # дошли до первой строки
    session.feed_key("KEY_PAGEUP")
    assert sc.view_top == 0                           # выше — стоп
    session.feed_key("KEY_PAGEDOWN")                  # назад к курсору
    assert sc.view_top > 0
    # пустые строки в историю не пишутся: чистый кадр при скролле — без роста
    session.handle_line_reply(b"\x1f")              # очистка + «дом»
    sc.y = ROWS - 1
    n0 = len(sc.history)
    for _ in range(5):
        session.handle_line_reply(b"\r\n")
    assert len(sc.history) == n0


def test_clear_key_moves_screen_to_history():
    # F5 «ОЧИСТКА»: кадр ЭВМ уходит в «историю», экран — чист, курсор в
    # начало первой строки; история не стирается, к ней возвращается PgUp
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    sc._view_h = 8
    for i in range(60):                              # выдано больше страницы
        session.handle_line_reply(bytes([0x80 | 1]) * 2 + b"\r\n")
    session.handle_line_reply(b"HELLO\r\nWORLD\r\n")  # последние строки кадра
    hist0 = len(sc.history)
    assert session.feed_key("CLEAR") is None
    assert (sc.x, sc.y) == (0, 0)
    assert "HELLO" not in sc.text()                    # кадр пуст
    assert len(sc.history) > hist0                     # экран — в историю
    assert any(decode_koi7(bytes(c)).strip() == "HELLO"
               for c, a in sc.history)
    assert any(decode_koi7(bytes(c)).strip() == "WORLD"
               for c, a in sc.history)
    assert link.sent == []
    assert sc.page_printed() is True                   # истории — больше страницы
    session.feed_key("KEY_PAGEUP")                     # возврат к нему
    assert sc.view_top is not None
    assert sc.view_top <= len(sc.history)
    top_hist = sc.view_top
    session.feed_key("KEY_PAGEDOWN")
    assert sc.view_top > 0 or top_hist is None


def test_history_limit_from_conf():
    from ie15emu.parser import Parser as P2
    from ie15emu import ROWS
    sc = P2(history=3).screen
    for i in range(30):
        sc.cells[sc.y] = [0x41 + (i % 26)] + [0x20] * (80 - 1)
        sc.lf()
    # ушли 6 строк (A..F), глубина 3 — остались последние (D, E, F)
    assert len(sc.history) == 3
    assert sc.history[0][0][0] == 0x44
    assert sc.history[-1][0][0] == 0x46
    sc0 = P2(history=0).screen
    sc0.cells[ROWS - 1] = [0x41] + [0x20] * 79
    sc0.y = ROWS - 1
    sc0.lf()
    assert sc0.history == []                          # 0 — не сохранять


def test_pgup_sends_nothing_to_line():
    session, link = make_session(MODE_HOST)
    session.feed_key("KEY_PAGEUP")
    session.feed_key("KEY_PAGEDOWN")
    assert link.sent == []


def test_echo_off_hides_password():
    # F6 «ЭХО»: набранное уходит в линию, но не печатается; эхо-ответ
    # линии срезается, настоящий вывод ЭВМ виден как обычно
    session, link = make_session(MODE_HOST)
    session.handle_line_reply("ПАРОЛЬ? ".encode("utf-8"))
    assert "ПАРОЛЬ" in session.parser.screen.text()
    session.feed_key("ECHO")
    assert "ЭХО=ВЫКЛ" in panel_text(session.parser.screen)
    for ch in "root":
        session.emit(session.feed_key(ch))
    assert link.sent                                  # в линию ушло
    # Н2 одно-регистрный: клавиши уходят верхом, эхо-ответ линии — верхом
    session.handle_line_reply(b"ROOT\r\n")            # эхо-ответ линии
    screen = session.parser.screen.text()
    assert "root" not in screen and "ROOT" not in screen
    session.handle_line_reply("ВХОД РАЗРЕШЁН\r\n".encode("utf-8"))
    assert "ВХОД РАЗРЕШ" in session.parser.screen.text()
    session.feed_key("ECHO")                          # эхо включили
    session.handle_line_reply("ВИДНО".encode("utf-8"))
    assert "ВИДНО" in session.parser.screen.text()


def test_echo_off_line_feeds_still_work():
    # при выключенном эхе переводы строки отрабатываются: ответ машины
    # после пароля начинается с новой строки, а не липнет к приглашению
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    session.handle_line_reply(b"LOGIN: ")
    session.feed_key("ECHO")
    for ch in "abc":
        session.emit(session.feed_key(ch))           # Н2: ушло «ABC»
    session.handle_line_reply(b"ABC\r\nOK\r\n")      # одно-регистное эхо линии
    assert sc.y == 2 and sc.x == 0
    rows = sc.text().splitlines()
    assert rows[0].startswith("LOGIN:") and "ABC" not in rows[0]
    assert rows[1].strip() == "OK"              # без эха, с новой строки


def test_echo_off_autonomous_no_echo():
    session, link = make_session(MODE_LOCAL)
    session.feed_key("ECHO")
    for ch in "пароль":
        assert session.feed_key(ch) is None
    assert session.pending()                          # буфер набирается
    assert "ПАРОЛЬ" not in session.parser.screen.text()
    session.feed_key("ECHO")
    session.feed_key("п")                    # локальное эхо — заглавной Н1
    assert "П" in session.parser.screen.text()


# --- «правильный масштаб»: окно терминала = кадр 25 рядов + подвал ------

def test_fit_rows_is_frame_plus_panel():
    # столько рядов должно вмещать окно: 25 кадра + черта + ряды подвала
    from ie15emu.__main__ import fit_rows
    parser = Parser()
    parser.screen.set_service("СЕТЬ=АВТОНОМНО")
    assert fit_rows(parser) == ROWS + 5       # статус+черта+пусто, черта, подвал
    parser.screen.set_service("СЕТЬ=АВТОНОМНО\nЭХО=ВКЛ\nВИДЕО=НОРМ")
    assert fit_rows(parser) == ROWS + 5 + 2
    # подвал сеанса: ячейки в ровных колонках, рядов — минимум
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    assert fit_rows(session.parser) == ROWS + 5 + (sc.panel_rows() - 1)
    assert sc.panel_rows() <= 12                     # 80 знаков
    sc.set_panel_cols(120)                           # шире окно — ниже панель
    assert sc.panel_rows() <= 9
    assert fit_rows(session.parser) == ROWS + 5 + (sc.panel_rows() - 1)


def test_frame_grows_to_window_and_panel_sits_at_bottom(monkeypatch):
    # кадр ЭВМ дотягивается до низа окна: подвал занимает минимум рядов,
    # остальное место окна — кадру; пустоты под подвалом не остаётся
    import re
    import ie15emu.__main__ as m
    for width, height in ((80, 40), (80, 60), (120, 40)):
        monkeypatch.setattr(m.os, "get_terminal_size",
                            lambda w=width, h=height: os.terminal_size((w, h)))
        session, _ = make_session(MODE_HOST)
        sc = session.parser.screen
        session.handle_line_reply(b"DKS 6: READY\r\n")
        out = m.text_screen(session.parser)
        # сверху — строка состояния линии (вне кадра), внизу — подвал
        assert sc.rows == max(ROWS, height - sc.panel_rows() - 4), \
            (width, height, sc.rows)
        assert sc.rows + 3 + 1 + sc.panel_rows() == height  # окно занято целиком
        # последняя отрисованная строка — последний ряд подвала
        last = max(int(n) for n in re.findall(r"\x1b\[(\d+);1H", out))
        assert last == height
        last_row = out.split(f"\x1b[{height};1H", 1)[1]
        assert "ДОМ=Home" in last_row          # последняя строка — конец справки
        # строка состояния — первый ряд окна, в кадр ЭВМ не входит
        first_row = out.split("\x1b[1;1H", 1)[1].split("\x1b[2;1H", 1)[0]
        assert sc.status_line().strip() in first_row


def test_window_fit_sends_resize_and_returns_after_data(monkeypatch):
    # окно шире «25 + подвал» (мелкий масштаб шрифта) — эмулятор просит
    # у терминала нужный размер (CSI 8;h;w t) и проверяет результат
    import ie15emu.__main__ as m
    size = [os.terminal_size((100, 50))]              # (столбцы, ряды)
    monkeypatch.setattr(m.os, "get_terminal_size", lambda: size[0])
    sent: list[bytes] = []
    monkeypatch.setattr(m, "_esc", lambda data: sent.append(data))

    fit = m.WindowFit()
    parser = Parser()
    assert fit.restore(parser) is True                # 1-я попытка
    assert sent[-1] == b"\x1b[8;%d;%dt" % (ROWS + 5, 100)
    # терминал размер не держит — после трёх попыток больше не просим
    for _ in range(2):
        fit.last = 0.0
        assert fit.restore(parser) is True
    fit.last = 0.0
    assert fit.restore(parser) is False               # сдались
    assert fit.unsupported is True
    assert len(sent) == 3

    # терминал держит размер: сброс масштаба (Ctrl-/+ или граница окна)
    # и приход символа от ЭВМ возвращают окно к правильному размеру
    sent.clear()
    size[0] = os.terminal_size((100, ROWS + 5))      # уже 25 + шапка + подвал
    fit2 = m.WindowFit()
    fit2.saved = (100, 50)
    assert fit2.restore(parser) is False              # просить нечего
    assert sent == []
    size[0] = os.terminal_size((100, 50))             # масштаб сбили вручную
    fit2.pending = True
    assert fit2.restore(parser) is True               # возврат к масштабу
    assert sent[-1] == b"\x1b[8;%d;%dt" % (ROWS + 5, 100)


def test_restore_saved_window_on_exit(monkeypatch):
    import ie15emu.__main__ as m
    cur = os.terminal_size((100, 50))
    monkeypatch.setattr(m.os, "get_terminal_size", lambda: cur)
    sent: list[bytes] = []
    monkeypatch.setattr(m, "_esc", lambda data: sent.append(data))
    fit = m.WindowFit()
    fit.saved = (100, 50)
    fit.restore_saved()                              # окно не трогали
    assert sent == []
    fit.saved = (120, 60)                             # эмулятор окно двигал
    fit.restore_saved()
    assert sent == [b"\x1b[8;60;120t"]


def test_clear_key_clears_visible_page_in_wide_window(monkeypatch):
    # F5 «ОЧИСТКА» в широком окне (>25 рядов + подвал): видны пустой кадр
    # и подвал, а ушедший в историю текст — не выползает над кадром
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 50)))
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    for i in range(40):                              # выдано больше страницы
        session.handle_line_reply(bytes([0x80 | 1]) + b"A" + b"\r\n")
    session.handle_line_reply(b"HELLO\r\nWORLD\r\n")
    out = m.text_screen(session.parser)
    assert "HELLO" in out
    session.feed_key("CLEAR")
    out = m.text_screen(session.parser)
    assert "HELLO" not in out and "WORLD" not in out    # страница очищена
    assert "\x1b[1;1H" in out                          # курсор в видимой области
    assert sc.x == 0 and sc.y == 0
    # история на месте: сошедший экран в ней лежит целиком
    assert any(decode_koi7(bytes(c)).strip() == "HELLO"
               for c, a in sc.history)
    assert any(decode_koi7(bytes(c)).strip() == "WORLD"
               for c, a in sc.history)
    # в узком окне страница листается и сошедший экран виден (PgUp)
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 10)))
    m.text_screen(session.parser)                 # окно просмотра — 10 рядов
    session.feed_key("KEY_PAGEUP")
    assert sc.view_top is not None and sc.view_top > 0
    assert "HELLO" in m.text_screen(session.parser)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")