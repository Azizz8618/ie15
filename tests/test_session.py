"""Запуск: python3 -m pytest tests/  или  python3 tests/test_session.py"""
from __future__ import annotations

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
    assert session.pending() == b"hello"
    assert link.sent == []          # ничего не ушло
    # Н2 (VT-52, по умолчанию) — ASCII: латинская строчная эхом
    # заглавными (строчные коды 0x60.. отданы русским строчным Н1)
    assert "HELLO" in session.parser.screen.text()   # эхо


def test_send_key_transmits_buffer_then_clears():
    session, link = make_session(MODE_LOCAL)
    for ch in "work":
        session.feed_key(ch)
    out = session.feed_key(KEY_SEND)
    # ПЕРЕДАЧА завершает строку (добавлены ПР ПС) и переводит в С ЭВМ
    assert out == b"work\r\n"
    session.emit(out)
    assert link.sent == [b"work\r"]          # utf8: ПР ПС → один ПР
    assert session.pending() == b""
    assert session.mode == MODE_HOST


def test_host_mode_sends_immediately():
    session, link = make_session(MODE_HOST)
    out = session.feed_key("A")
    assert out == b"A"              # при вводе С ЭВМ приходит сразу
    assert session.pending() == b"" # буфер не используется


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
    # АВТОНОМНО: «М» должна печататься буквой, а не двигать каретку;
    # по SEND на линию RAW уходить внутренний КОИ7 (как «wyd» в живом тесте)
    session, link = make_session(MODE_LOCAL)
    for ch in "МАМЫ":
        session.feed_key(ch)
    assert session.parser.screen.text().startswith("МАМЫ")
    session.encoding = "raw"
    session.emit(session.feed_key(KEY_SEND))
    assert link.sent == [b"mamy\x03"]        # без Enter ПЕРЕДАЧА сама закрыла строку
    assert session.pending() == b""


def test_local_echo_uppercase_buffer_keeps_layout():
    # АВТОНОМНО с позиционной раскладкой: эхо заглавная «Й», в буфере —
    # строчный код раскладки (0xEA), на линию уходит КОИ7 (0x6A = Й)
    link = FakeLink()
    session = TerminalSession(Parser(), link=link, mode=MODE_LOCAL,
                              layout="positional")
    session.encoding = session.rx_encoding = "raw"
    session.feed_key("q")
    assert session.parser.screen.text().startswith("Й")
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
    session.emit(session.feed_key("М"))
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
    # ДКС-линия в raw: передача — внутренние КОИ7 (буква «В» → 0x77),
    # но окончание — ПР, не ETX (ETX для dks_line_char — просто знак);
    # приём — raw + заглавные, tmxr-баннер в UTF-8 не ломается
    session, link = make_session(MODE_HOST)
    # реальный порядок баннеров tmxr: сначала «Encoding is RAW», затем
    # регистрация Э-60 «Connected to DKS»
    session.handle_line_reply(b"Encoding is RAW\r\nConnected to DKS\r\n")
    assert session.encoding == "dks" and session.rx_encoding == "raw"
    assert session.enc_decided
    session.emit(session.feed_key("В"))
    assert link.sent == [b"w"]
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
    # PgUp/PgDn — листание окна вывода (кадр до последней заполненной
    # строки + подвал). Выше первой строки — стоп (без циклического
    # показа пустоты); закрепленное окно приём не сбрасывает
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    sc._view_h = 24
    for y in range(3):
        session.handle_line_reply(bytes([0x80 | 1]) * 5 + b"\r\n")
    # контент: 4 ряда с курсором + панель; окно 24 — всего видно
    assert session.feed_key("KEY_PAGEUP") is None
    assert sc.view_top is None                       # листать нечего: следим
    session.feed_key("KEY_PAGEUP")                   # верх — стоп, не цикл
    assert sc.view_top is None
    session.feed_key("KEY_PAGEDOWN")                 # низ — тоже упор в контент
    assert sc.view_top is None
    # узкое окно: листание от первой строки кадра до последней строки
    # подвала; выше первой строки и ниже последней — стоп, без цикла
    sc._view_h = 6
    session.feed_key("KEY_PAGEDOWN")                 # страница вниз
    assert sc.view_top == 5
    session.feed_key("KEY_PAGEDOWN")                 # упор в конец подвала
    assert sc.view_top == sc.view_rows() - 6
    session.feed_key("KEY_PAGEUP")
    session.feed_key("KEY_PAGEUP")                   # до первой строки
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
    session.handle_line_reply(b"HELLO\r\nWORLD\r\n")
    hist0 = len(sc.history)
    assert session.feed_key("CLEAR") is None
    assert (sc.x, sc.y) == (0, 0)
    assert "HELLO" not in sc.text()                    # кадр пуст
    assert len(sc.history) > hist0                     # экран — в историю
    assert any(decode_koi7(bytes(c)).strip() == "HELLO"
               for c, a in sc.history)
    assert link.sent == []
    session.feed_key("KEY_PAGEUP")                     # возврат к нему
    assert sc.view_top == 0
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
    session.handle_line_reply(b"root\r\n")            # эхо-ответ линии
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
        session.emit(session.feed_key(ch))
    session.handle_line_reply(b"abc\r\nOK\r\n")
    assert sc.y == 2 and sc.x == 0
    rows = sc.text().splitlines()
    assert rows[0].startswith("LOGIN:") and "abc" not in rows[0]
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")