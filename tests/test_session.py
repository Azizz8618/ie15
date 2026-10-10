"""Запуск: python3 -m pytest tests/  или  python3 tests/test_session.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu import ROWS
from ie15emu.charset import RUS7, decode_koi7
from ie15emu.parser import Parser
from ie15emu.session import MODE_HOST, MODE_LOCAL, TerminalSession
from ie15emu.keyboard import KEY_MODE, KEY_SEND


class FakeLink:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


def make_session(mode=MODE_LOCAL):
    link = FakeLink()
    session = TerminalSession(Parser(), link=link, mode=mode)
    return session, link


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
    assert "УП 1C" in "\n".join(["".join(sc.service)] + sc.service_more)
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
    assert "ctrl+х" in "\n".join(["".join(sc.service)] + sc.service_more)


def test_key_diagnostics_in_footer():
    # Диагностика: в подвале видно каждое нажатие — знак с кодом, имя с
    # модификатором и результат переназначения («ctrl+б→< U+003C»)
    session, _ = make_session(MODE_HOST)

    def posl():
        sc = session.parser.screen
        return "\n".join(["".join(sc.service)] + sc.service_more)

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
    # колонки ровные: разделители « | » стоят на одних и тех же позициях
    # во всех рядах легенды
    k_rows = [l for l in rows() if "Bksp" in l or "Home" in l or "PgUp" in l
              or "F8" in l]
    cols = [l.index("|") for l in k_rows]
    assert all(c == cols[0] for c in cols), cols
    for i, line in enumerate(rows()):
        assert len(line) <= 80, f"ряд {i} шире 80 ({len(line)})"
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
    assert "НАБОР=Н2" in "".join(session.parser.screen.service)


def test_cmdset_key_toggles_rezhim():
    session, _ = make_session(MODE_HOST)
    session.feed_key("CMDSET")                 # F8: Н2 → Н0 (ASCII)
    assert session.nabor == "n0" and session.parser.mode == 1
    assert session.parser.screen.display_set == "n0"
    assert "НАБОР=Н0" in "".join(session.parser.screen.service)
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
    assert "ЭХО=ВЫКЛ" in "\n".join(["".join(session.parser.screen.service)]
                                    + session.parser.screen.service_more)
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
    assert fit_rows(parser) == ROWS + 2               # черта + 1 ряд подвала
    parser.screen.set_service("СЕТЬ=АВТОНОМНО\nЭХО=ВКЛ\nВИДЕО=НОРМ")
    assert fit_rows(parser) == ROWS + 2 + 2
    # подвал сеанса: черта, 3 ряда состояния, черта, 6 рядов клавиш = 11
    session, _ = make_session(MODE_HOST)
    assert fit_rows(session.parser) == ROWS + 11


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
    assert sent[-1] == b"\x1b[8;%d;%dt" % (ROWS + 2, 100)
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
    size[0] = os.terminal_size((100, 27))             # уже 25 + подвал
    fit2 = m.WindowFit()
    fit2.saved = (100, 50)
    assert fit2.restore(parser) is False              # просить нечего
    assert sent == []
    size[0] = os.terminal_size((100, 50))             # масштаб сбили вручную
    fit2.pending = True
    assert fit2.restore(parser) is True               # возврат к масштабу
    assert sent[-1] == b"\x1b[8;%d;%dt" % (ROWS + 2, 100)


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