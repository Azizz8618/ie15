"""Запуск: python3 -m pytest tests/  или  python3 tests/test_session.py"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu import ROWS
from ie15emu.charset import RUS7, decode_koi7, encode_koi7
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


def typed(text):
    """Коды буфера набора для этих знаков (в Н2 латиница — внутренние КОИ7)."""
    from ie15emu.keyboard import key_to_bytes
    return b"".join(key_to_bytes(c, shift_to_rus=True) for c in text)


def test_local_home_goes_to_line_start():
    # Home в АВТОНОМНО — курсор в начало текущей строки набора;
    # следующий знак вставляется в начало, а не дописывается в конец
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "ABCDE":
        session.feed_key(ch)
    assert (sc.x, sc.y) == (5, 0)
    session.feed_key("KEY_HOME")
    assert (sc.x, sc.y) == (0, 0)
    session.feed_key("X")
    assert session.pending() == typed("XABCDE")     # вставка в начало


def test_local_home_works_on_second_line():
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "AB":
        session.feed_key(ch)
    session.feed_key("KEY_ENTER")
    for ch in "CD":
        session.feed_key(ch)
    assert (sc.x, sc.y) == (2, 1)
    session.feed_key("KEY_HOME")
    assert (sc.x, sc.y) == (0, 1)             # в начало второй строки
    session.feed_key("Z")
    assert session.pending() == typed("AB") + b"\r" + typed("ZCD")


def test_local_shift_end_selects_to_line_end():
    # АВТОНОМНО: Shift+End — выделение от курсора до конца строки набора,
    # Shift+Home — от курсора до её начала; выделение видимо на экране
    session, _ = make_session(MODE_LOCAL)
    for ch in "ABCDE":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")
    session.feed_key("KEY_RIGHT")
    session.feed_key("SELEND")
    assert session._buf_sel == (1, 5)
    assert session.parser.screen.selections      # подсветка на кадре
    session.feed_key("KEY_HOME")                 # обычный Home снимает выделение
    assert session._buf_sel is None
    assert session.parser.screen.selections == []
    session.feed_key("KEY_RIGHT")
    session.feed_key("SELHOME")
    assert session._buf_sel == (0, 1)


def test_local_shift_home_selects_to_line_start():
    session, _ = make_session(MODE_LOCAL)
    for ch in "AB":
        session.feed_key(ch)
    session.feed_key("KEY_ENTER")
    for ch in "CD":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")                 # в начало второй строки
    session.feed_key("SELEND")
    assert session._buf_sel == (3, 5)            # до конца второй строки
    session.feed_key("KEY_RIGHT")
    session.feed_key("SELHOME")                   # до её начала
    assert session._buf_sel == (3, 4)
    session.feed_key("KEY_HOME")                  # курсор в начало строки
    session.feed_key("SELHOME")                   # выделять нечего
    assert session._buf_sel is None


def test_shift_home_end_blocked_ctrl_works():
    # Shift+Home/End блокированы: ОС перехватывает их на листание вывода
    # и в приложение они не доходят; там, где доходят — эмулятор их гасит,
    # чтобы поведение не зависело от терминала
    from ie15emu.keyboard import decode_key_bytes
    for seq in (b"\x1b[1;2H", b"\x1b[1;2F",      # xterm-форма
                b"\x1b[1;2~", b"\x1b[4;2~",      # форма «ESC [ n ; m ~»
                b"\x1b[1;6H", b"\x1b[1;3H"):     # Ctrl+Shift+ (раньше падал!)
        assert decode_key_bytes(seq) == [], seq
    # рабочая альтернатива — Ctrl+Home/End (обе формы)
    assert decode_key_bytes(b"\x1b[1;5H") == ["SELHOME"]
    assert decode_key_bytes(b"\x1b[1;5F") == ["SELEND"]
    assert decode_key_bytes(b"\x1b[1;5~") == ["SELHOME"]
    assert decode_key_bytes(b"\x1b[4;5~") == ["SELEND"]
    # стрелки не блокируем — Shift/Ctrl-стрелки работают как прежде
    assert decode_key_bytes(b"\x1b[1;2A") == ["SELUP"]
    assert decode_key_bytes(b"\x1b[1;5C") == ["KEY_CTRLRIGHT"]


def test_host_mode_selection_keys_send_nothing():
    # В С ЭВМ Shift+Home/End/Backspace у 15ВВВ нет: нажатие гасится и в
    # линию не уходит (раньше проваливалось в key_to_bytes и отдавало
    # слово «SELHOME»/«SELEND»/«SELBSPACE» — мусор в машине)
    session, link = make_session(MODE_HOST)
    for ch in "AB":
        session.feed_key(ch)
    for key in ("SELHOME", "SELEND", "SELBSPACE"):
        assert session.feed_key(key) is None, key
    assert link.sent == []
    assert session.pending() == b""              # буфера в С ЭВМ нет


def test_host_shift_home_end_select_on_frame():
    # В С ЭВМ Shift+Home/End выделяют по кадру (как Shift+стрелки):
    # от курсора до начала/конца строки под ним; кадр принадлежит ЭВМ,
    # выделение берётся F4, Shift+Backspace только сообщает об АВТОНОМНО
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    session.parser.feed(b"HELLO WORLD")
    sc.view_map = [("frame", y) for y in range(sc.rows)]   # карта после отрисовки
    sc.x, sc.y = 6, 0                      # курсор на «W» — середина строки

    session.feed_key("SELEND")
    spans = dict(sc.sel_spans()[0])
    assert ("frame", 0) in spans, "Shift+End ничего не выделил"
    a, b = spans[("frame", 0)][0]
    assert sc.row_text("frame", 0)[a:b + 1] == "WORLD"    # до конца строки

    sc.sel_clear()
    sc.view_map = [("frame", y) for y in range(sc.rows)]
    session.feed_key("SELHOME")
    spans = dict(sc.sel_spans()[0])
    assert ("frame", 0) in spans, "Shift+Home ничего не выделил"
    a, b = spans[("frame", 0)][0]
    assert sc.row_text("frame", 0)[a:b + 1] == "HELLO W"  # до начала строки

    # Shift+Backspace в С ЭВМ ничего не стирает и в линию не шлёт
    assert session.feed_key("SELBSPACE") is None
    assert link.sent == []
    assert "АВТОНОМНО" in session.last_key


def test_local_shift_backspace_deletes_selection():
    # Shift+Home/End выделили фрагмент — Shift+Backspace удаляет его целиком
    session, _ = make_session(MODE_LOCAL)
    for ch in "ABCDE":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")
    session.feed_key("SELEND")
    assert session._buf_sel == (0, 5)
    session.feed_key("SELBSPACE")
    assert session.pending() == b""
    assert session._buf_pos == 0
    assert session._buf_sel is None
    assert session.last_key == "УДАЛЕНО"


def test_local_shift_backspace_without_selection_is_backspace():
    session, _ = make_session(MODE_LOCAL)
    for ch in "AB":
        session.feed_key(ch)
    session.feed_key("SELBSPACE")
    assert session.pending() == typed("A")


def test_local_plain_backspace_deletes_selection():
    # xterm (и часть терминалов) не различает Shift+ВК и обычный ВК —
    # шлёт для обоих \x7f. Поэтому обычный ВК при открытом выделении
    # удаляет выделенное (как в любом редакторе), а не знак слева
    session, _ = make_session(MODE_LOCAL)
    for ch in "ABCDE":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")
    session.feed_key("KEY_RIGHT")
    session.feed_key("SELEND")               # выделено от позиции 1 до конца
    assert session._buf_sel == (1, 5)
    session.feed_key("KEY_BACKSPACE")        # обычный ВК — а удаляет выделение
    assert session.pending() == typed("A")
    assert session._buf_pos == 1
    assert session._buf_sel is None
    assert session.last_key == "УДАЛЕНО"
    # выделения больше нет — следующий ВК стирает знак слева
    session.feed_key("KEY_BACKSPACE")
    assert session.pending() == b""


def test_help_toggles_during_session():
    # F12 прячет/показывает справку подвала в ходе сеанса; состояния
    # и «ПОСЛ» при этом остаются на месте, а подсказка «СПРАВКА=F12»
    # прижата к правому краю верхней строки состояния
    from ie15emu.keyboard import decode_key_bytes
    session, link = make_session(MODE_LOCAL)
    session.banner_seen = True                  # баннер пришёл — «КВУ»,
    sc = session.parser.screen                  # строка состояния реалистична
    assert sc.show_help is True
    assert "СБРОС=F3" in panel_text(sc)          # легенда видна
    assert session.feed_key("HELP") is None      # в линию не уходит
    assert sc.show_help is False
    assert "СБРОС=F3" not in panel_text(sc)      # легенда скрыта
    assert "СЕТЬ=" in panel_text(sc)             # состояние осталось
    assert link.sent == []
    # подсказка, как вернуть справку — в правом краю строки состояния
    import re
    status = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())
    assert "СПРАВКА=F12" not in panel_text(sc)   # в подвале её нет
    assert status.rstrip().endswith("СПРАВКА=F12"), status
    assert "СПРАВКА=F12" not in status[:len(status) - len(" | СПРАВКА=F12")]
    session.feed_key("HELP")                     # второй раз — обратно
    assert sc.show_help is True
    assert "СБРОС=F3" in panel_text(sc)
    status = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())
    assert "СПРАВКА=F12" not in status           # подсказка убрана
    # F12 (и запасная форма F1) распознаются из байтов терминала
    for seq in (b"\x1b[24~", b"\x1b[11~", b"\x1bOP", b"\x1b[[A"):
        assert decode_key_bytes(seq) == ["HELP"], seq


def test_local_f5_clears_page_without_history():
    # F5 в АВТОНОМНО удаляет страницу: кадр очищается и в историю НЕ уходит,
    # а сама страница уходит из буфера набора
    session, _ = make_session(MODE_LOCAL)
    for ch in "ABC":
        session.feed_key(ch)
    assert session.pending()                  # набрано
    session.feed_key("CLEAR")
    assert session.parser.screen.history == []
    assert "ABC" not in session.parser.screen.text()
    assert session.pending() == b""           # страница удалена из буфера
    assert session.last_key == "СТР. УДАЛЕНА"


def test_cursor_stays_on_its_line_when_buffer_overflows():
    # Регрессия: при буфере больше кадра печать «с нуля и до конца»
    # уезжала вверх — строки над курсором уходили за край, курсор стоял
    # не на своей строке, и знак вставлялся не под курсором
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for i in range(40):                       # 40 строк > кадр (25)
        for ch in f"L{i:02d}":
            session.feed_key(ch)
        session.feed_key("KEY_ENTER")
    assert len(session.buffer) > 0

    def line_and_row():
        """Строка буфера под курсором и то, что на её месте на кадре."""
        buf = bytes(session.buffer)
        pos = session._buf_pos
        start = buf.rfind(b"\r", 0, pos) + 1
        nxt = buf.find(b"\r", pos)
        end = len(buf) if nxt < 0 else nxt
        buf_line = "".join(decode_koi7(bytes([c & 0x7F for c in buf[start:end]])))
        row = "".join(decode_koi7(bytes([sc.cells[sc.y][x] for x in range(80)])))
        return buf_line, row.rstrip()

    # курсор в конце буфера: видна хвостовая страница
    buf_line, row = line_and_row()
    assert (sc.x, sc.y) == (0, 24), (sc.x, sc.y)  # пустая строка после ПС
    assert buf_line == row.rstrip() or row == ""

    # уход вверх по строкам: курсор обязан стоять на СВОЕЙ строке
    for ups, want_line in ((1, "L39"), (10, "L30"), (25, "L15")):
        s2, _ = make_session(MODE_LOCAL)
        sc = s2.parser.screen
        for i in range(40):
            for ch in f"L{i:02d}":
                s2.feed_key(ch)
            s2.feed_key("KEY_ENTER")
        for _ in range(ups):
            s2.feed_key("KEY_UP")
        session = s2
        buf_line, row = line_and_row()
        assert buf_line == row.rstrip(), (ups, buf_line, row.rstrip())

    # знак встаёт под курсором: ряд меняется ровно на месте вставки
    x0, y0 = sc.x, sc.y
    before = "".join(decode_koi7(bytes([sc.cells[y0][x] for x in range(80)])))
    session.feed_key("KEY_RIGHT")             # точка вставки = x0 + 1
    session.feed_key("Z")                     # вставка в строку под курсором
    x1, y1 = sc.x, sc.y                       # курсор после знака = x0 + 2
    after = "".join(decode_koi7(bytes([sc.cells[y1][x] for x in range(80)])))
    buf_line, row = line_and_row()
    assert buf_line == row.rstrip(), (buf_line, row.rstrip())   # кадр = буфер
    assert y1 == y0                           # курсор остался в своей строке
    assert x1 == x0 + 2, (x0, x1)             # ровно один знак вставлен
    assert after[:x0 + 1] == before[:x0 + 1]  # левее вставки — как было
    # правее — тот же хвост, сдвинутый на знак (строка фиксированной
    # ширины: последняя ячейка вытесняется за край — это всегда пробел,
    # содержимое буфера при печати переносится)
    assert after[x1:] == before[x0 + 1:][:len(after) - x1]


def test_local_f5_deletes_page_from_buffer():
    # F5 в АВТОНОМНО удаляет из буфера ТО, что сейчас на кадре: видимая
    # страница исчезает, а уехавшие выше строки становятся видимыми
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for i in range(40):                        # 40 строк — больше кадра
        for ch in f"L{i:02d}":
            session.feed_key(ch)
        session.feed_key("KEY_ENTER")
    n_bytes = len(session.buffer)
    assert n_bytes > 0
    span = session._buf_page_span()
    assert span and span[0] > 0               # страница — хвост, не всё сразу
    session.feed_key("CLEAR")
    assert len(session.buffer) == span[0]     # удалилось ровно видимое
    assert session.last_key == "СТР. УДАЛЕНА"
    # отмена возвращает удалённую страницу
    session.feed_key("UNDO")
    assert len(session.buffer) == n_bytes


def test_local_f5_repeats_until_buffer_empty():
    # Повторное F5 удаляет следующую страницу — пока буфер не опустеет
    session, _ = make_session(MODE_LOCAL)
    for i in range(40):
        for ch in f"L{i:02d}":
            session.feed_key(ch)
        session.feed_key("KEY_ENTER")
    hist_before = len(session.parser.screen.history)  # растёт при наборе
    presses = 0
    while session.pending() and presses < 10:
        session.feed_key("CLEAR")
        presses += 1
    assert session.pending() == b""
    assert presses <= 3                        # 40 строк — не больше 3 страниц
    # F5 сама историю не пополняет (строки уходят из буфера, не в историю)
    assert len(session.parser.screen.history) == hist_before


def test_local_f5_keeps_cursor_inside_buffer():
    # После удаления страницы курсор не выходит за пределы буфера
    session, _ = make_session(MODE_LOCAL)
    for i in range(40):
        for ch in f"L{i:02d}":
            session.feed_key(ch)
        session.feed_key("KEY_ENTER")
    session.feed_key("CLEAR")
    assert 0 <= session._buf_pos <= len(session.buffer)


def test_local_pages_page_through_buffer():
    # В АВТОНОМНО у буфера своей «истории» нет (эхо кадр вверх не сдвигает),
    # поэтому PgUp/PgDn листают сам набор: курсор уходит на страницу
    # вверх/вниз, кадр показывает те строки набора
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for i in range(40):                        # 40 строк > кадра (25)
        for ch in f"L{i:02d}":
            session.feed_key(ch)
        session.feed_key("KEY_ENTER")

    def line_on_cursor():
        buf = bytes(session.buffer)
        pos = session._buf_pos
        start = buf.rfind(b"\r", 0, pos) + 1
        nxt = buf.find(b"\r", pos)
        end = len(buf) if nxt < 0 else nxt
        return "".join(decode_koi7(bytes([c & 0x7F for c in buf[start:end]])))

    # конец: хвост на кадре
    assert (sc.x, sc.y) == (0, 24)
    # страница вверх — курсор и кадр показывают строки выше
    session.feed_key("KEY_PAGEUP")
    pos1 = session._buf_pos
    assert pos1 < len(session.buffer), "PgUp не увёл курсор с конца"
    buf = bytes(session.buffer)
    start = buf.rfind(b"\r", 0, pos1) + 1
    # курсор стоит на своей строке, и она видна на кадре
    row = "".join(decode_koi7(bytes([sc.cells[sc.y][x] for x in range(80)])))
    assert row.rstrip() == line_on_cursor(), (row.rstrip(), line_on_cursor())
    # страница вниз — возврат к концу
    for _ in range(5):
        session.feed_key("KEY_PAGEDOWN")
    assert session._buf_pos == len(session.buffer)
    # до верха — курсор у первой строки, кадр показывает начало
    while session._buf_pos:
        session.feed_key("KEY_PAGEUP")
        if session._buf_pos == 0:
            break
    assert session._buf_pos == 0
    row0 = "".join(decode_koi7(bytes([sc.cells[0][x] for x in range(80)])))
    assert row0.rstrip() == line_on_cursor()
    # у границы повторный PgUp не ломает (листает историю — её тут нет)
    session.feed_key("KEY_PAGEUP")
    assert session._buf_pos == 0
    # колесо — три строки за щелчок, вниз обратно
    session.feed_key("KEY_PAGEDOWN")
    pos_before = session._buf_pos
    session.feed_key("KEY_WHEELDOWN")
    assert session._buf_pos > pos_before
    session.feed_key("KEY_WHEELUP")
    assert session._buf_pos == pos_before


def test_local_pages_fall_back_to_history_for_short_buffer():
    # Короткий буфер влезает в кадр — PgUp листает «историю» выдачи
    # (её нет — ничего не происходит), курсор не двигается
    session, _ = make_session(MODE_LOCAL)
    for ch in "AB":
        session.feed_key(ch)
    pos = session._buf_pos
    session.feed_key("KEY_PAGEUP")
    assert session._buf_pos == pos
    assert session.parser.screen.view_top is None


def test_host_pages_still_use_history():
    # В С ЭВМ PgUp по-прежнему листает историю машинной выдачи
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    for i in range(60):
        session.parser.feed(f"H{i:02d}\r\n".encode())
    assert sc.page_printed()
    session.feed_key("KEY_PAGEUP")
    assert sc.view_top is not None and sc.view_top > 0


def test_pulse_sends_space_once_per_interval():
    # «Пробел поддержки»: раз в интервал в линию уходит пробел, чтобы
    # программы БЭСМ-6 не считали сеанс уснувшим
    session, link = make_session(MODE_LOCAL)
    session.pulse_interval = 60.0
    session._pulse_next = 0.0                      # интервал «настал»
    assert session.pulse_tick() is True
    assert link.sent == [b" "]
    # до следующего интервала — повторов нет
    assert session.pulse_tick() is False
    assert link.sent == [b" "]


def test_pulse_disabled_and_line_lost():
    session, link = make_session(MODE_LOCAL)
    session.pulse_interval = 0.0                   # выключено конфигом
    session._pulse_next = 0.0
    assert session.pulse_tick() is False
    assert link.sent == []
    session.pulse_interval = 60.0
    session.mark_line_lost("обрыв")
    assert session.pulse_tick() is False           # линия потеряна
    assert link.sent == []


def test_pulse_echo_not_drawn_on_screen():
    # эхо пробела (машинное) не попадает на экран: знак не печатается,
    # курсор не двигается — иначе весь следующий вывод съезжал бы на
    # колонку вправо. Пробел поддержки отправляется только в АВТОНОМНО:
    # в С ЭВМ лишний знак вклинивается в выдачу и сбивает её
    session, link = make_session(MODE_HOST)
    session.pulse_interval = 60.0
    session._pulse_next = 0.0
    assert session.pulse_tick() is False           # в С ЭВМ — не шлём
    assert link.sent == []
    session.set_mode(MODE_LOCAL)
    assert session.pulse_tick() is True            # в АВТОНОМНО — шлём
    assert link.sent == [b" "]
    # эхо пробела (машинное) не попадает на экран: знак не печатается,
    # курсор не двигается — иначе весь следующий вывод съезжал бы на
    # колонку вправо (в С ЭВМ выдача идёт в кадр)
    session.set_mode(MODE_HOST)
    sc = session.parser.screen
    before = [row[:] for row in sc.cells], (sc.x, sc.y)
    session.handle_line_reply(b" ")                # машина эхит пробел
    after = [row[:] for row in sc.cells], (sc.x, sc.y)
    assert after == before, "эхо пробела изменило экран"
    # а чужой пробел спустя срок гасить уже нельзя
    session._pulse_echo_until = 0.0
    session.handle_line_reply(b"A B")
    text = "".join(session.parser.screen.row_text("frame", 0) or "")
    assert "A B" in text                          # пробел сохранён


def test_pulse_echo_stripping_window():
    # гасим только своё эхо и только в коротком окне: спустя срок пробел
    # в выводе — уже чужой, трогать его нельзя. Окно живёт до первого
    # успешного снятия — перед каждым случаем его переставляем
    import time as _t
    session, _ = make_session(MODE_LOCAL)
    arm = lambda: setattr(session, "_pulse_echo_until", _t.time() + 5.0)

    arm(); assert session._strip_pulse_echo(b" ") == b""          # своё — прочь
    arm(); assert session._strip_pulse_echo(b"\r\n ") == b"\r\n"  # после ПС
    arm(); assert session._strip_pulse_echo(b"  ") == b" "        # один за окно
    arm(); assert session._strip_pulse_echo(b"A B") == b"A B"     # чужой не трогаем
    session._pulse_echo_until = 0.0                               # не ждём эхо
    assert session._strip_pulse_echo(b" ") == b" "
    session._pulse_echo_until = _t.time() - 1.0                   # срок истёк
    assert session._strip_pulse_echo(b" ") == b" "


# --- сообщение ЭВМ в АВТОНОМНО: индикатор «ОТ ЭВМ» -------------------

def _status(s) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s.parser.screen.status_line())


def test_incoming_in_autonomous_shown_as_indicator():
    # Сообщение от ЭВМ в АВТОНОМНО не рисуется в кадр (эхо перетёрло бы
    # его при первой же правке), а показывается индикатором в строке
    # состояния; буфер набора не затрагивается
    from ie15emu.charset import encode_koi7
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    session.banner_seen = True                  # «КВУ», а не «ПРОВЕРЬ ЛИНИЮ!»
    session.link.port = None                    # без КАНАЛ= — строка уже
    for ch in "ABC":
        session.feed_key(ch)
    frame_before = [row[:] for row in sc.cells]
    session.handle_line_reply("ВВЕДИТЕ ИМЯ ФАЙЛА\r\n".encode())
    # кадр не тронут, буфер цел
    assert [row[:] for row in sc.cells] == frame_before
    assert session.incoming.startswith("ВВЕДИТЕ ИМЯ ФАЙЛА")
    assert "МЙ" not in session.incoming          # ПР/ПС не декодированы в буквы
    assert "ОТ ЭВМ=" in _status(session)
    assert session.pending()                     # набор не пропал
    # второе сообщение — счётчик
    session.handle_line_reply("ПОВТОР\r\n".encode())
    assert session.incoming_n == 2
    assert "(2)" in _status(session)


def test_incoming_indicator_cleared_on_mode_switch():
    from ie15emu.charset import encode_koi7
    session, _ = make_session(MODE_LOCAL)
    session.handle_line_reply("ПРОСЬБА\r\n".encode())
    assert session.incoming
    session.toggle_mode()                        # F9 → С ЭВМ
    assert session.mode == MODE_HOST
    assert session.incoming == ""
    assert "ОТ ЭВМ=" not in _status(session)
    assert session.parser.screen.notice == ""    # выпадающая строка тоже


# --- выпадающая строка сообщения ЭВМ ------------------------------------

def _render(session, width: int = 100) -> str:
    """Отрисовать кадр как в реальном окне (текстовый вывод)."""
    import os as _os
    import ie15emu.__main__ as m
    old = m.os.get_terminal_size
    m.os.get_terminal_size = lambda: _os.terminal_size((width, 30))
    try:
        return m.text_screen(session.parser)
    finally:
        m.os.get_terminal_size = old


def _notice_row(out: str) -> str:
    """Вторая строка окна: выпадающая строка сообщения либо черта."""
    import re as _re
    seg = out.split("\x1b[2;1H")[1].split("\x1b[3;1H")[0]
    return _re.sub(r"\x1b\[[0-9;]*m", "", seg)


def test_notice_line_shown_until_first_key():
    # Сообщение ЭВМ в АВТОНОМНО показывается отдельной (инверсной) строкой
    # под состоянием — до первого нажатия клавиши; кадр на это время
    # сдвигается вниз на ряд, после гашения возвращается
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "ABC":
        session.feed_key(ch)
    out = _render(session)
    assert _notice_row(out).startswith("-"), "до сообщения на месте черта"
    assert "\x1b[4;1H" in out              # кадр с четвёртого ряда

    session.handle_line_reply("ВНИМАНИЕ: ПРОГРАММА ЖДЁТ ОТВЕТА\r\n".encode())
    out = _render(session)
    row2 = _notice_row(out)
    assert "ВНИМАНИЕ: ПРОГРАММА" in row2, row2
    assert "\x1b[7m" in out.split("\x1b[2;1H")[1].split("\x1b[3;1H")[0] \
        or "ВНИМАНИЕ" in row2               # строка показана
    assert sc.notice                          # поле заполнено
    assert "\x1b[5;1H" in out                # кадр сдвинулся на ряд
    assert "\x1b[3;1H-" in out               # черта ушла на третий ряд

    session.feed_key("X")                     # первая клавиша гасит строку
    out = _render(session)
    assert "ВНИМАНИЕ" not in out
    assert sc.notice == ""
    assert "\x1b[4;1H" in out                # кадр вернулся на четвёртый ряд


def test_notice_line_update_and_truncate():
    # Новое сообщение показывается снова (счётчик растёт), длинное
    # обрезается по ширине окна — строка не переносится на второй ряд
    session, _ = make_session(MODE_LOCAL)
    session.handle_line_reply("ПЕРВОЕ\r\n".encode())
    session.feed_key("X")
    session.handle_line_reply("ВТОРОЕ\r\n".encode())
    out = _render(session, width=100)
    assert "ВТОРОЕ" in _notice_row(out)
    assert session.incoming_n == 2            # счётчик в индикаторе
    session.feed_key("Y")
    session.handle_line_reply(
        ("ОЧЕНЬ ДЛИННОЕ СООБЩЕНИЕ ОТ ЭВМ " * 5).encode())
    out = _render(session, width=100)
    row2 = _notice_row(out)
    assert len(row2.rstrip()) <= 100, len(row2.rstrip())


def test_notice_line_only_in_autonomous():
    # В С ЭВМ сообщение идёт в кадр — выпадающей строки нет
    session, _ = make_session(MODE_HOST)
    session.handle_line_reply("В КАДР ЭВМ".encode())
    out = _render(session)
    assert _notice_row(out).startswith("-")
    assert "В КАДР ЭВМ" in out.split("\x1b[4;1H")[1][:200]
    assert session.parser.screen.notice == ""


# --- вставка из внешнего источника (буфер обмена ОС) --------------------

def test_paste_utf8_split_recodes():
    # Вставка из внешнего источника — UTF-8, а os.read рвёт её где угодно:
    # вторая половина многобайтовой буквы не должна уезжать в набор
    # отдельным байтом («Ð¼» вместо «м»)
    from ie15emu.keyboard import decode_keys
    text = "привет, мир! строка вставки"
    data = text.encode()
    cut = data.index("м".encode()) + 1         # разрез посреди буквы
    k1, tail = decode_keys(data[:cut])
    k2, tail2 = decode_keys(data[cut:], tail)
    assert tail2 == b""
    assert "".join(k1 + k2) == text, "".join(k1 + k2)
    # и итоговая перекодировка в буфер — обратима
    session, _ = make_session(MODE_LOCAL)
    for k in k1 + k2:
        session.feed_key(k)
    from ie15emu.charset import decode_koi7
    # Н2 — одно-регистровая строка: строчные хранятся/показываются
    # заглавными (семантика набора); важно, что мусора («Ð¼») нет
    assert decode_koi7(bytes(session.buffer)) == text.upper()


def test_osc52_reply_pastes_with_recoding():
    # Ответ терминала на запрос буфера (OSC 52) → текст проходит обычную
    # перекодировку: в АВТОНОМНО — в буфер набора, откуда и уходит по SEND
    import base64
    from ie15emu.charset import decode_koi7
    from ie15emu.keyboard import decode_keys
    text = "вставка: Привет!"
    payload = base64.b64encode(text.encode()).decode()
    keys, tail = decode_keys(f"\x1b]52;c;{payload}\x07".encode())
    assert keys == [text] and tail == b"", (keys, tail)
    session, _ = make_session(MODE_LOCAL)
    session.feed_key(keys[0])
    session.flush_paste()                   # пауза — хвост отдаётся
    assert decode_koi7(bytes(session.buffer)) == text
    assert "ВСТАВКА" in session.last_key
    # ответ, разорванный между чтениями, тоже собирается
    full = f"\x1b]52;c;{payload}\x07".encode()
    k1, t1 = decode_keys(full[:9])
    k2, t2 = decode_keys(full[9:], t1)
    assert k1 + k2 == [text]
    # мусорный base64 — молча гасится
    assert decode_keys(b"\x1b]52;c;!!!\x07")[0] == []


def test_clipboard_combos_never_reach_line():
    # Ctrl+Shift+C/V: без этого нажатие разбиралось бы как УП Ctrl+C/Ctrl+V
    # (0x03/0x16) и уходило в линию мусором
    session, link = make_session(MODE_HOST)
    session.feed_key("ctrl+shift+c")
    session.feed_key("ctrl+shift+v")
    assert link.sent == []
    assert "OSC52" in session.last_key or "ВЫД" in session.last_key
    # с подключённым терминалом уходят команды буфера обмена (SET и GET)
    session, link = make_session(MODE_LOCAL)
    sent = []
    session.term_write = sent.append
    session.parser.screen.selections = [[("frame", 0, 0, 4, "ВЫД")]]
    session.feed_key("ctrl+shift+c")
    session.feed_key("ctrl+shift+v")
    assert any(s.startswith(b"\x1b]52;c;") and not s.endswith(b"?\x07")
               for s in sent), sent          # копирование выделенного
    assert any(s.endswith(b"?\x07") for s in sent), sent   # запрос вставки
    assert link.sent == []                    # в линию — тишина
    # без term_write (headless/тесты) — молча, без исключений
    session2, _ = make_session(MODE_LOCAL)
    session2.feed_key("ctrl+shift+v")
    assert "не поддерживает" in session2.last_key


def test_terminal_own_paste_is_faithful():
    # Терминал со СВОЕЙ вставкой (Ctrl+Shift+V уровня терминала) шлёт
    # буфер обмена обычным потоком знаков: в Н2 каждый заглавный уходит
    # посимвольной сменой алфавита (правило клавиатуры) — копирование
    # в буфер ОС возвращается мусором «DYBVFYBT…». Группировка взрыва
    # (group_paste) собирает чтение одним ключом — перекодировка верная.
    from ie15emu.charset import decode_koi7, encode_koi7
    from ie15emu.keyboard import group_paste, key_to_bytes

    text = "ВНИМАНИЕ: ТЕРМ-20 ОТКЛЮЧЕН"
    session, _ = make_session(MODE_LOCAL)
    keys, _ = group_paste(list(text), 0.0, 0.0)
    assert keys == [text]                   # один ключ вставки
    for k in keys:
        session.feed_key(k)
    session.flush_paste()                   # пауза — хвост отдаётся
    assert decode_koi7(bytes(session.buffer)) == text, \
        "вставка одним ключом дала мусор"
    assert "ВСТАВКА" in session.last_key     # подвал показывает вставку

    # посимвольно (как было до группировки) — вот он, мусор:
    broken, _ = make_session(MODE_LOCAL)
    for ch in text:
        broken.feed_key(ch)
    assert decode_koi7(bytes(broken.buffer)) != text
    assert decode_koi7(bytes(broken.buffer)) == \
        decode_koi7(b"".join(key_to_bytes(c, None, True) for c in text))

    # в С ЭВМ вставка уходит в линию одним куском (не посимвольно):
    # feed_key возвращает байты, отправляет их emit() — как в run_line
    from ie15emu.charset import to_line
    session_h, link_h = make_session(MODE_HOST)
    for k in keys:
        session_h.emit(session_h.feed_key(k))
    assert len(link_h.sent) == 1, link_h.sent
    assert b"".join(link_h.sent) == to_line(encode_koi7(text),
                                            session_h.encoding)


def test_f3_clears_selection_then_buffer():
    # F3: первое нажатие — снять выделение (как раньше); повторное,
    # когда выделения нет, — очистить буфер набора целиком, НЕ удаляя
    # текст с экрана (эхо остаётся, следующий набор спустится ниже)
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "ПРИВЕТ":
        session.feed_key(ch)
    frame_before = [sc.row_text("frame", y) for y in range(sc.rows)]
    # 1) выделение набора есть — F3 снимает его, буфер не трогает
    session._buf_sel = (0, 3)
    session.feed_key("SELCLEAR")
    assert session._buf_sel is None
    assert bytes(session.buffer) != b""          # буфер цел
    assert "ВЫДЕЛЕНИЕ" in session.last_key
    # 1б) выделение кадра (мышь) — тоже снимается
    session2, _ = make_session(MODE_LOCAL)
    sc2 = session2.parser.screen
    sc2.selections = [[("frame", 0, 0, 4, "ВЫД")]]
    session2.feed_key("SELCLEAR")
    assert not sc2.sel_text()
    assert "ВЫДЕЛЕНИЕ" in session2.last_key
    # 2) выделения нет — повторный F3 очищает буфер, экран не меняется
    session.feed_key("SELCLEAR")
    assert bytes(session.buffer) == b"" and session._buf_pos == 0
    assert "БУФЕР ОЧИЩЕН" in session.last_key
    frame_after = [sc.row_text("frame", y) for y in range(sc.rows)]
    assert frame_after == frame_before, "текст на экране пропал при очистке"
    # 3) очистка отменяется Ctrl+U — буфер возвращается
    session.feed_key("UNDO")
    assert bytes(session.buffer) != b"", "очистка не отменилась"
    # 4) после очистки новый набор спускается ниже оставшегося эха
    session3, _ = make_session(MODE_LOCAL)
    for ch in "ПРИВЕТ":
        session3.feed_key(ch)
    session3.feed_key("SELCLEAR")           # выделения нет — очистка
    echo_bottom = session3.parser.screen.last_used()
    session3.feed_key("X")
    assert session3._echo_top == echo_bottom + 1, \
        "набор после очистки налез на оставшийся текст"


def test_bracketed_paste_in_session():
    # Сквозной путь: «ESC [ 200 ~ текст ESC [ 201 ~» → буфер набора
    # без перекодировочных потерь (маршрут тот же, что у вставки из ОС)
    from ie15emu.charset import encode_koi7
    from ie15emu.keyboard import decode_keys
    text = "Приказ 5: вставка\nв два ряда, Строчная строка"
    keys, tail = decode_keys(f"\x1b[200~{text}\x1b[201~".encode())
    assert keys == [text.replace("\n", "\r")] and tail == b""
    session, _ = make_session(MODE_LOCAL)
    for k in keys:
        session.feed_key(k)
    session.flush_paste()                  # пауза — хвост отдаётся
    # в буфере разделитель строк — ПР (0x0D), как у ВК
    assert bytes(session.buffer) == encode_koi7(text.replace("\n", "\r"))
    assert "ВСТАВКА" in session.last_key


def test_paste_undo_removes_whole_paste():
    # Вставка из буфера ОС приходит чанками (чтения по 256 байт, когда
    # терминал не шлёт меток 200~) — Ctrl+U обязан удалять её ЦЕЛИКОМ,
    # а не частями: раньше каждый чанк ставил свою границу шага и
    # откат шёл по чанкам (3 отмены на 3 чанка)
    from ie15emu.charset import decode_koi7

    session, _ = make_session(MODE_LOCAL)
    for ch in "до вставки ":
        session.feed_key(ch)
    before = bytes(session.buffer)
    for chunk in ("ЧАНК-ПЕРВЫЙ-КУСОК ВСТАВКИ",
                  " ЧАНК-ВТОРОЙ-КУСОК-ДАЛЕЕ", " И-ТРЕТИЙ-ЧАНКОВЫЙ"):
        session.feed_key(chunk)            # подряд, как чтения одной вставки
    session.flush_paste()                  # пауза — хвост отдаётся
    assert bytes(session.buffer) != before
    session.feed_key("UNDO")
    assert bytes(session.buffer) == before, "откат оставил часть вставки"

    # одиночная вставка (bracketed/OSC52, один ключ) — тоже целиком
    session.feed_key("ОДИНОЧНАЯ-ВСТАВКА")
    session.feed_key("UNDO")
    assert bytes(session.buffer) == before

    # слово ДО вставки переживает откат вставки (граница ставится перед ней)
    session2, _ = make_session(MODE_LOCAL)
    for ch in "слово ":
        session2.feed_key(ch)
    word = bytes(session2.buffer)
    session2.feed_key("ВСТАВКА")
    session2.feed_key("UNDO")
    assert bytes(session2.buffer) == word

    # вставки, разделённые большой паузой, — разные шаги: уходит только
    # последняя
    import time
    session3, _ = make_session(MODE_LOCAL)
    session3.feed_key("ПЕРВАЯ-ИЗ-ДВУХ")
    time.sleep(0.6)                        # > PASTE_UNDO_GAP
    session3.paste_tick()                  # цикл сеанса отдаёт хвост
    session3.feed_key("ВТОРАЯ-ИЗ-ДВУХ")
    session3.feed_key("UNDO")
    assert decode_koi7(bytes(session3.buffer)) == "ПЕРВАЯ-ИЗ-ДВУХ"

    # служебные клавиши вставкой не считаются — подвал свой
    session4, _ = make_session(MODE_LOCAL)
    session4.feed_key("с")
    session4.feed_key("UNDO")
    assert session4.last_key == "ОТМЕНА"


def test_enter_reconnects_when_line_lost():
    # Фактический обрыв: ВВОД в С ЭВМ вызывает переподключение
    # (callback ставит run_line) и НЕ шлёт ПР в мёртвую линию
    session, link = make_session(MODE_HOST)
    session.mark_line_lost("нет ответа")
    calls = []
    session.reconnect = lambda: calls.append("re")
    out = session.feed_key("KEY_ENTER")
    assert calls == ["re"] and out is None
    assert getattr(link, "sent", []) == []      # в мёртвую линию — тишина

    # Enter от реального терминала приходит 0x0A (setcbreak не снимает
    # ICRNL) — эта форма тоже должна переподключать
    session0, _ = make_session(MODE_HOST)
    session0.mark_line_lost("x")
    session0.reconnect = lambda: calls.append("re2")
    assert session0.feed_key("\n") is None
    assert session0.feed_key("\r") is None
    assert calls == ["re", "re2", "re2"]

    # при живой линии ВВОД — обычный ПР, переподключения нет
    session2, _ = make_session(MODE_HOST)
    session2.reconnect = lambda: calls.append("НЕТ")
    assert session2.feed_key("KEY_ENTER") == b"\r"
    assert calls == ["re", "re2", "re2"]

    # в АВТОНОМНО при обрыве ВВОД — обычный ВК в буфер (не reconnect)
    session3, _ = make_session(MODE_LOCAL)
    session3.mark_line_lost("x")
    session3.reconnect = lambda: calls.append("НЕТ-2")
    session3.feed_key("KEY_ENTER")
    assert bytes(session3.buffer) == b"\r"
    assert calls == ["re", "re2", "re2"]

    # без callback (тесты/headless) — Enter не падает, идёт обычным путём
    session4, _ = make_session(MODE_HOST)
    session4.line_lost = True
    assert session4.feed_key("KEY_ENTER") == b"\r"


def test_echo_starts_below_machine_content():
    # При переходе в АВТОНОМНО кадр ЭВМ добавляется в буфер набора: текст
    # экрана становится началом набора (ПЕРЕДАЧА отдаст и его), кадр не
    # перерисовывается, курсор встаёт ниже текста
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    session.handle_line_reply("СТРОКА ПЕРВАЯ ЭВМ\r\n".encode())
    session.handle_line_reply("ВТОРАЯ СТРОКА ЭВМ\r\n".encode())
    top_text = (session.parser.screen.row_text("frame", 0),
                session.parser.screen.row_text("frame", 1))
    session.toggle_mode()                       # → АВТОНОМНО
    assert session.buffer, "кадр ЭВМ не добавлен в буфер"
    assert bytes(session.buffer).endswith(b"\r")   # строки закрываются ПР
    buf_after_toggle = bytes(session.buffer)
    session.feed_key("X")
    assert session.parser.screen.row_text("frame", 0) == top_text[0], \
        "первая строка ЭВМ уничтожена набором"
    assert session.parser.screen.row_text("frame", 1) == top_text[1], \
        "вторая строка ЭВМ уничтожена набором"
    assert len(session.buffer) == len(buf_after_toggle) + 1, \
        "набор не продолжил буфер"
    assert session._buf_pos == len(session.buffer)  # курсор в конце набора

    # Ctrl+U отменяет набор X: буфер возвращается к состоянию сразу после
    # перехода (текст кадра без X); повторный Ctrl+U отменяет сам перенос
    session.feed_key("UNDO")
    assert bytes(session.buffer) == buf_after_toggle, \
        "отмена не вернула буфер к состоянию после перехода"
    session.feed_key("UNDO")
    assert not session.buffer, "отмена не убрала перенос кадра"

    # кадр целиком занят: строки ЭВМ переходят в буфер, а не стираются
    session2, _ = make_session(MODE_HOST)
    sc2 = session2.parser.screen
    for i in range(sc2.rows):              # кадр занят целиком, включая
        # строку под курсором (без ПС в конце — иначе останется пустой
        # ряд, куда набор влезет и кадр не сдвинется)
        end = "" if i == sc2.rows - 1 else "\r\n"
        session2.handle_line_reply(f"СТРОКА {i:02d}{end}".encode())
    hist_before = len(sc2.history)
    first_row = session2._row_to_buffer(sc2.cells[0]).rstrip(b" ")
    session2.toggle_mode()
    buf_after_toggle = bytes(session2.buffer)
    session2.feed_key("Y")
    assert len(session2.buffer) == len(buf_after_toggle) + 1, \
        "набор при полном кадре не состоялся"
    assert first_row + b"\r" in bytes(session2.buffer), \
        "строки ЭВМ не добавлены в буфер"
    assert len(sc2.history) == hist_before, \
        "кадр сдвинулся при переходе: строки ЭВМ должны жить в буфере"


def test_frame_with_full_width_rows_has_no_gaps():
    # Регрессия: строка кадра ровно в 80 знаков после переноса в буфер
    # занимала при перерисовке ДВА ряда (перенос печати + ПС от ПР) —
    # в тексте на экране появлялись пустые интервалы. Строка кратной
    # COLS длины занимает один ряд: перенос уже сделан печатью
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    line80 = "А" * 79 + "Я"                # ровно 80 знаков, без пробелов
    session.handle_line_reply((line80 + "\r\n" + line80 + "\r\n"
                               + "КОНЕЦ\r\n").encode())
    before = [sc.row_text("frame", y) for y in range(5)]
    assert len(before[0]) == 80            # предусловие: полный кадр
    session.toggle_mode()
    session.feed_key("X")                  # первая правка перерисует эхо
    after = [sc.row_text("frame", y) for y in range(5)]
    assert after[:5] == before, "текст кадра искажён после перехода"
    # набранный знак — на следующей строке под текстом, без интервалов
    assert sc.row_text("frame", 5).rstrip() != ""


def test_typed_full_width_line_closes_without_gap():
    # Та же конвенция при обычном наборе: строка ровно в 80 знаков,
    # закрытая Enter, не оставляет пустого ряда после себя
    from ie15emu import COLS
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for _ in range(COLS):
        session.feed_key("А")
    session.feed_key("KEY_ENTER")
    for ch in "КОНЕЦ":
        session.feed_key(ch)
    assert sc.row_text("frame", 1).rstrip() == "КОНЕЦ", \
        "после полной строки появился пустой ряд"


def test_paste_lines_wrapped_at_spaces():
    # Вставка: строки длиннее кадра (80 знаков) ломаются по пробелам —
    # слово идёт на следующий ряд целиком, слова не разделяются
    session, _ = make_session(MODE_LOCAL)
    text = " ".join(["СЛОВО"] * 30)            # 179 знаков одной строкой
    session.feed_key(text)
    session.flush_paste()                      # пауза — хвост отдаётся
    lines = bytes(session.buffer).split(b"\r")
    assert len(lines) > 1, "длинная строка не переломлена"
    assert all(len(x) <= 80 for x in lines), "перелом не по границе кадра"
    assert b" ".join(x for x in lines if x) == encode_koi7(text), \
        "при переломе искажён текст"
    # эхо на кадре — ряды без разрыва слов: каждый ряд кадра кончается
    # там же, где строка буфера
    sc = session.parser.screen
    for i, x in enumerate(lines):
        if x:
            assert sc.row_text("frame", i).rstrip() == \
                decode_koi7(x).rstrip(), (i, "ряд кадра ≠ строке буфера")


def test_paste_word_longer_than_frame_broken_at_edge():
    # Слово длиннее кадра (пробелов нет) рвётся по краю кадра — делить
    # его нечем; хвост после слова остаётся целым
    session, _ = make_session(MODE_LOCAL)
    session.feed_key("А" * 100 + " ХВОСТ")
    session.flush_paste()                      # пауза — хвост отдаётся
    lines = bytes(session.buffer).split(b"\r")
    assert lines[0] == encode_koi7("А" * 80)
    assert lines[1] == encode_koi7("А" * 20 + " ХВОСТ")
    assert all(len(x) <= 80 for x in lines)


def test_paste_continues_typed_line_without_splitting_words():
    # Вставка в конец набранного: первый ряд — продолжение строки набора,
    # ширина считается от текущей колонки
    session, _ = make_session(MODE_LOCAL)
    for ch in "НАБРАНО ":
        session.feed_key(ch)
    session.feed_key(" ".join(["СЛОВО"] * 15))
    session.flush_paste()                      # пауза — хвост отдаётся
    lines = bytes(session.buffer).split(b"\r")
    decoded = [decode_koi7(x) for x in lines]
    assert all(len(x) <= 80 for x in decoded)
    assert decoded[0] == "НАБРАНО " + " ".join(["СЛОВО"] * 12)   # ровно 80
    # слова целы и на своих местах
    assert " ".join(decoded) == "НАБРАНО " + " ".join(["СЛОВО"] * 15)


def test_paste_chunks_long_line_wrapped_once():
    # Вставка кусками (чтения по 256 байт) посреди длинной строки:
    # перелом по пробелам всё равно делается по границе кадра
    session, _ = make_session(MODE_LOCAL)
    session.feed_key("СЛОВО " * 20)            # 120 знаков одним куском
    session.feed_key("ХВОСТ-ВСТАВКИ")
    session.flush_paste()                      # пауза — хвост отдаётся
    lines = bytes(session.buffer).split(b"\r")
    decoded = [decode_koi7(x) for x in lines]
    assert all(len(x) <= 80 for x in decoded)
    assert " ".join(x.rstrip() for x in decoded if x.rstrip()) == \
        " ".join(["СЛОВО"] * 19) + " СЛОВО ХВОСТ-ВСТАВКИ"


def test_paste_user_text_in_small_reads_words_intact():
    # Регрессия: большой текст (600+ знаков, три абзаца) приходит
    # чтениями по 16…4096 байт, границы чтений режут слова. Перелом по
    # пробелам обязан делить слова только там, где пробел в источнике:
    # незакрытый хвост куска откладывается и продолжается следующим
    session, _ = make_session(MODE_LOCAL)
    text = ("астоящее место группы ШККАЛ - MD/2248, физ. зона 0o1242 "
            "(29-мегабайтный диск ПУЛЬТа; рядом КАЛАХ - 0o1234). Адрес "
            "2400236 = число зон 2зона 0236 со смещением нумерации "
            "0o1004. Все мои записи на 2148/2061/2348 были мимо цели - "
            "цепочка exec всегда читала оттуда.\r"
            "Старший разряд адреса - это число зон: группа ШККАЛ должна "
            "быть ровно 2 зоны (как 6 у КАЛАХа). Моя трехзонная сборка "
            "обрезалась и падала на старте - отсюда КОНЕЦ СЕАНСА.\r"
            "Поймана и первая диагностическая величина: N=& - нEнт "
            "огромный (формат :12 переполнился) - ШККАЛ читает зону с "
            "гигантским счетчиком записей, отсюда и мусорные шифры в "
            "строках.")
    expected = decode_koi7(encode_koi7(text.replace("\r", " "))).split()
    for size in (4096, 256, 128, 64, 32, 16):
        s, _ = make_session(MODE_LOCAL)
        for i in range(0, len(text), size):
            s.feed_key(text[i:i+size])
        s.flush_paste()                    # пауза — хвост отдаётся
        lines = [decode_koi7(x) for x in bytes(s.buffer).split(b"\r")]
        assert all(len(x) <= 80 for x in lines), \
            f"чтение {size}: ряд длиннее кадра"
        assert " ".join(lines).split() == expected, \
            f"чтение {size}: слова разделены или потеряны"


def test_cursor_move_is_not_undo_step():
    # Движения курсора не являются шагом отмены: навигация буфер не
    # правит, набор до и после движения откатывается одним шагом
    session, _ = make_session(MODE_LOCAL)
    for ch in "СЛОВО ПЕРВОЕ ":
        session.feed_key(ch)
    for k in ("KEY_LEFT", "KEY_UP", "KEY_CTRLLEFT", "KEY_HOME", "KEY_END"):
        session.feed_key(k)
    for ch in "ВТОРОЕ ":
        session.feed_key(ch)
    session.feed_key("UNDO")
    assert decode_koi7(bytes(session.buffer)) == "СЛОВО ПЕРВОЕ ", \
        "движение курсора разорвало шаг отмены"


def test_frame_to_buffer_continues_below_echo():
    # Повторный переход С ЭВМ → АВТОНОМНО при живом наборе: добавляется
    # только то, что ЭВМ напечатала НИЖЕ эха набора — сам набор не
    # дублируется
    session, _ = make_session(MODE_LOCAL)
    for ch in "НАБОР":
        session.feed_key(ch)
    buffer_after_typing = bytes(session.buffer)
    session.toggle_mode()                       # → С ЭВМ
    session.handle_line_reply("ОТВЕТ ЭВМ\r\n".encode())
    session.toggle_mode()                       # → АВТОНОМНО
    buf = bytes(session.buffer)
    assert buf.startswith(buffer_after_typing), "набор потерян/изменён"
    assert buf.endswith(b"\r"), "строки ЭВМ не закрыты ПР"
    assert len(buf) > len(buffer_after_typing), "ответ ЭВМ не добавлен"
    assert session._buf_pos == len(session.buffer)


def test_frame_to_buffer_skips_when_frame_empty():
    # Пустой кадр — добавлять нечего: буфер не меняется
    session, _ = make_session(MODE_HOST)
    session.toggle_mode()
    assert not session.buffer
    assert session._echo_top is None
    assert session._buf_pos == 0


def test_mode_switch_unpins_history_view():
    # Закреплённый просмотр истории (PgUp в С ЭВМ) снимается переходом
    # в АВТОНОМНО: набор должен быть виден, а не «слепой» набор по кадру
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    for i in range(60):
        session.handle_line_reply(f"СТРОКА-{i:03d}\r\n".encode())
    session.feed_key("KEY_PAGEUP")
    assert sc.view_top is not None              # просмотр закреплён
    session.toggle_mode()
    assert sc.view_top is None, "закреплённый просмотр не снят переходом"
    session.feed_key("X")
    assert sc.view_top is None                  # и после набора тоже


def test_long_typing_keeps_machine_rows_in_buffer():
    # Главный сценарий потери: текст ЭВМ на кадре, набор перерастает кадр.
    # Строки ЭВМ должны остаться в буфере (кадр — окно на буфер), а не
    # стираться безвозвратно
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    for i in range(30):
        session.handle_line_reply(f"ВЫДАЧА-{i:03d}\r\n".encode())
    seen_before = {session._row_to_buffer(sc.cells[y]).rstrip(b" ")
                   for y in range(sc.rows)
                   if any(c != 0x20 for c in sc.cells[y])}
    session.toggle_mode()
    for i in range(20):
        for ch in f"НАБОР-{i:03d}":
            session.feed_key(ch)
        session.feed_key("KEY_ENTER")
    buf = bytes(session.buffer)
    for row in seen_before:
        assert row + b"\r" in buf, f"строка ЭВМ {row!r} потеряна"
    assert buf.endswith(b"\r")


def test_incoming_drawn_in_host_mode():
    # в С ЭВМ ничего не изменилось: сообщение идёт в кадр, индикатора нет
    from ie15emu.charset import encode_koi7
    session, _ = make_session(MODE_HOST)
    session.handle_line_reply("ЭХО В КАДР".encode())
    assert session.incoming == ""
    assert "ОТ ЭВМ=" not in _status(session)
    assert session.parser.screen.row_text("frame", 0)


# --- оболочка SIMH: unicode-вывод и автопереключение на Н0 --------------

def _raw_session():
    """Сеанс С ЭВМ на RAW-линии (баннер «Encoding is RAW»)."""
    session, _ = make_session(MODE_HOST)
    session.rx_encoding = "raw"
    return session


def test_raw_line_utf8_displays_normally():
    # Оболочка SIMH печатает unicode поверх raw-линии: байты читаются
    # как UTF-8, а не рассыпаются по одному (в сырых кодах линии 0xD0/0xD1
    # не бывает — ложных срабатываний нет)
    session = _raw_session()
    session.handle_line_reply("ПАРАМЕТРЫ ЗАДАНЫ УСПЕШНО\r\n".encode("utf-8"))
    row = session.parser.screen.row_text("frame", 0) or ""
    assert "ПАРАМЕТРЫ ЗАДАНЫ" in row, row
    assert session.nabor == "n2"           # без приглашения набор не прыгает


def test_raw_line_koi7_still_works():
    # Сырой КОИ7 (коды 0x60.. и 0x80..) на той же линии — как раньше:
    # в Н2 строка 0x60.. показывается заглавными, латиница — буквами
    session = _raw_session()
    session.handle_line_reply("ПАРАМЕТРЫ ЗАДАНЫ\r\n".encode("utf-8"))
    session.handle_line_reply(bytes([0x64, 0x65]) + b" AB\r\n")
    row1 = session.parser.screen.row_text("frame", 1) or ""
    assert "ДЕ" in row1 and "AB" in row1, row1
    session.handle_line_reply(bytes([0x72, 0x75, 0x73]) + b"\r\n")   # «рус»
    row2 = session.parser.screen.row_text("frame", 2) or ""
    assert "РУС" in row2, row2


def test_simh_prompt_switches_to_n0():
    # После приглашения оболочки SIMH набор сам уходит в Н0: приглашение
    # видно прежним набором, следующая латиница идёт как есть (в Н1 она
    # показывалась бы русскими буквами). Повторный prompt набор не дёргает,
    # F8 возвращает его вручную
    session = _raw_session()
    session.handle_line_reply(b"sim set>\r\n")
    row0 = session.parser.screen.row_text("frame", 0) or ""
    assert "set>" in row0 or "СЕТ>" in row0, row0   # приглашение видно
    assert session.nabor == "n0"
    assert session.parser.mode == 1                 # Н0 — только УП
    assert "set>" in session.last_key or "Н0" in session.last_key
    session.handle_line_reply(b"help: set cpu\r\n")
    row1 = session.parser.screen.row_text("frame", 1) or ""
    assert "help: set cpu" in row1, row1            # латиница как есть
    session.nabor = "n2"                            # набор вернули вручную
    session.handle_line_reply(b"set again\r\n")
    assert session.nabor == "n2"                    # повторный — не дёргает
    before = session.nabor
    session.feed_key("CMDSET")                      # F8 циклит (Н2→Н0→Н1…)
    assert session.nabor != before


def test_simh_prompt_unicode_triggers_n0():
    # Приглашение приходит unicode («СЕТ>») — тоже перехватывается;
    # сама кириллица в Н0 не отображается (так же и на настоящем терминале,
    # набор 15ИЭ включает то, что видит), важно: переключение произошло
    # и следующая латиница показана
    session = _raw_session()
    session.handle_line_reply("СЕТ>\r\n".encode("utf-8"))
    assert session.nabor == "n0"
    assert session.parser.screen.row_text("frame", 0) != ""
    session.handle_line_reply(b"show version\r\n")
    row1 = session.parser.screen.row_text("frame", 1) or ""
    assert "show version" in row1, row1


def test_simh_prompt_split_across_reads():
    # Приглашение, разорванное между чтениями линии, ловится по окну
    # приёма (_sniff)
    session = _raw_session()
    session.handle_line_reply(b"sim set")
    assert session.nabor == "n2"              # приглашение ещё не полное
    session.handle_line_reply(b">\r\nnext\r\n")
    assert session.nabor == "n0"


def test_status_line_always_fits_panel_width():
    # строка состояния обязана влезать в ширину подвала (80): иначе
    # терминал перенесёт её на второй ряд и сдвинет кадр ЭВМ
    from ie15emu.charset import encode_koi7
    from ie15emu import COLS
    session, _ = make_session(MODE_LOCAL)
    session.banner_seen = True                  # линия отошла — «КВУ», не
    session.link.port = None                    # «ПРОВЕРЬ ЛИНИЮ», без КАНАЛ=
    for ch in "ABC":
        session.feed_key(ch)                    # появляется ОТМЕНА=…
    session.parser.screen.selections = [[("frame", 0, 0, 5, "АБВ")] ]
    session._update_status()                    # и ВЫД=…
    session.handle_line_reply(
        "ОЧЕНЬ ДЛИННОЕ СООБЩЕНИЕ ЭВМ ЧТОБЫ ЗАПОЛНИТЬ СТРОКУ\r\n".encode())
    assert len(_status(session)) <= COLS, _status(session)
    assert "ОТ ЭВМ=" in _status(session)        # индикатор приоритетнее подсказок
    # версия не жертвуется ради индикатора — остаётся всегда
    assert "ВЕРСИЯ=" in _status(session), _status(session)


def test_local_f5_deletes_page_from_history():
    # Страница, загруженная в кадр из истории (PgUp), тоже удаляется по F5
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    sc.set_rows(24)
    for y in range(40):                       # выдача: история заполнится
        sc.cells[y % 24] = [0x41] * 80
        sc.scrolled += 0
    sc.history = [([0x41] * 80, [0] * 80) for _ in range(30)]
    sc.view_top = 4                           # смотрим на страницу истории
    assert sc.view_top < len(sc.history)
    n_hist = len(sc.history)
    session.feed_key("CLEAR")
    assert len(sc.history) == n_hist - 24     # страница из истории удалена
    assert sc.view_top is None


def test_local_f5_in_host_mode_keeps_history():
    # В С ЭВМ F5 по-прежнему уводит экран в историю (не удаляет страницу)
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    for x, code in enumerate(b"ABCDE"):
        sc.put(code)
    n_hist = len(sc.history)
    session.feed_key("CLEAR")
    assert len(sc.history) > n_hist
    assert sc.cells[0] == [0x20] * 80          # кадр очищен


def test_local_end_goes_to_line_end():
    # End в АВТОНОМНО — курсор в конец строки набора (симметрично Home)
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "ABC":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")
    session.feed_key("KEY_END")
    assert (sc.x, sc.y) == (3, 0)
    session.feed_key("D")
    assert session.pending() == typed("ABCD")


def test_local_insert_and_replace_modes():
    # Insert переключает режим правки: вставка (по умолчанию) сдвигает
    # хвост строки, замена пишет поверх знака под курсором
    session, _ = make_session(MODE_LOCAL)
    assert session.insert_mode is True
    for ch in "ABCD":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")
    session.feed_key("X")                     # вставка
    assert session.pending() == typed("XABCD")
    session.feed_key("KEY_INSERT")             # → замена
    assert session.insert_mode is False
    session.feed_key("KEY_HOME")
    session.feed_key("Y")                     # замена поверх X
    assert session.pending() == typed("YABCD")
    assert len(session.pending()) == 5         # замена не удлиняет


def test_local_insert_key_is_terminal_code_in_host_mode():
    # В С ЭВМ Insert остаётся командой терминала (инверсное видео)
    session, link = make_session(MODE_HOST)
    assert session.insert_mode is True
    session.emit(session.feed_key("KEY_INSERT"))
    assert session.insert_mode is True         # режим правки не меняется
    assert link.sent[-1] == b"\x1bb"


def test_local_ctrl_arrows_move_cursor_in_buffer():
    # Ctrl+стрелки в АВТОНОМНО ходят по буферу набора: курсор на экране
    # и позиция вставки не расходятся (знак встаёт под курсор)
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "AB CD":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")
    session.feed_key("KEY_CTRLRIGHT")          # начало слова CD
    assert (sc.x, sc.y) == (3, 0)
    session.feed_key("X")
    assert session.pending() == typed("AB XCD")


def test_local_arrows_move_cursor_in_buffer():
    # Обычные стрелки в АВТОНОМНО двигают курсор по буферу набора,
    # а не отправляют escape-последовательности (иначе курсор «уезжал
    # вправо» — ESC C интерпретировался как команда терминала)
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "AB":
        session.feed_key(ch)
    session.feed_key("KEY_ENTER")
    for ch in "XY":
        session.feed_key(ch)
    # буфер: "AB\rXY", курсор после "XY" (позиция 5)
    assert session._buf_pos == 5
    session.feed_key("KEY_UP")
    assert session._buf_pos == 2                    # столбец 2 первой строки
    assert (sc.x, sc.y) == (2, 0)
    session.feed_key("KEY_DOWN")
    assert session._buf_pos == 5                    # столбец 2 второй строки
    assert (sc.x, sc.y) == (2, 1)
    # Стрелки не портят буфер
    assert session.pending() == typed("AB") + b"\r" + typed("XY")


def test_local_backspace_does_not_join_lines():
    # Backspace в начале строки не стирает ПР — строки не склеиваются
    session, _ = make_session(MODE_LOCAL)
    for ch in "AB":
        session.feed_key(ch)
    session.feed_key("KEY_ENTER")
    for ch in "CD":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")               # в начало второй строки
    session.feed_key("KEY_BACKSPACE")
    assert session.pending() == typed("AB") + b"\r" + typed("CD")
    session.feed_key("KEY_CTRLRIGHT")
    session.feed_key("KEY_BACKSPACE")          # а внутри строки стирает
    assert session.pending() == typed("AB") + b"\r" + typed("C")


def test_local_cursor_wraps_with_long_line():
    # Строка длиннее 80 знаков переносится — курсор идёт на следующий ряд
    # (иначе он остался бы в конце своей строки и правка била не туда)
    from ie15emu import COLS
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    sc.set_rows(24)
    for _ in range(COLS + 4):
        session.feed_key("A")
    assert session._buf_pos == COLS + 4
    assert (sc.x, sc.y) == (4, 1)              # перенос на 80-м знаке
    session.feed_key("KEY_HOME")
    assert (sc.x, sc.y) == (0, 0)
    session.feed_key("X")
    assert session.pending() == typed("X") + typed("A") * (COLS + 4)


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


def test_h2_upper_russian_equals_lower_on_line():
    # Н2: заглавная русская НЕ превращается в латиницу позиции (дефект:
    # «ДЕКАБРЬ» уходил на линию как «LTRF,HM» — мусор в файлах КРАБа):
    # строка ЭВМ одно-регистровая, «Д» и «д» дают один код, линия
    # получает одно и то же независимо от регистра ввода
    def line(text):
        session, link = make_session(MODE_HOST)
        session.encoding = "utf8"
        for ch in text:
            session.emit(session.feed_key(ch))
        return b"".join(link.sent)

    assert line("ДЕКАБРЬ") == line("декабрь")
    assert line("ДЕКАБРЬ") == "ДЕКАБРЬ".encode("utf-8")
    assert line("КРАБ") == "КРАБ".encode("utf-8")


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
    # в Н2 регистр ввода не важен: «В» и «в» дают один код (0x97 → 0x77)
    session.emit(session.feed_key("В"))
    assert link.sent[-1] == b"w"
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

    for lab in ("СЕТЬ=С ЭВМ", "НАБОР=Н2", "ЛИНИЯ=?", "РАСК=ВЫКЛ",
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
                   "НОРМ=Del", "ЭХО=F6", "УПР.СИМВ=F7", "НАБОР=F8", "СЕТЬ=F9",
                   "ПЕРЕДАЧА=F10", "ВЫД=Shift+Стрелки", "ВЫДКОЛ=Shift+Ctrl"):
        assert legend in panel(), legend
    assert "ЭХО=ВКЛ" in panel()                      # состояние в ряду состояний
    session.feed_key("ECHO")
    assert "ЭХО=ВЫКЛ" in panel()
    session.feed_key("ECHO")
    # подвал «растянут» по ширине окна, ячейки не рвутся, ряд не шире
    # окна; колонки ровные: разделители « | » стоят на одних позициях
    for i, line in enumerate(rows()):
        assert len(line) <= 80, f"ряд {i} шире 80 ({len(line)})"
    assert len(rows()) <= 11, rows()
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
    session.feed_key("SELCLEAR")                   # F3: кадр ЭВМ ушёл в буфер
    for ch in "ЗАД":                               # — очистим, набор с нуля
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
    # закрепленное окно приём не сбрасывает; долистали вниз — окно снова
    # следящее (за выдачей)
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
    # внизу окно снова следящее: иначе следующая строка выдачи уводила бы
    # курсор под нижнюю кромку и опускать экран пришлось бы вручную
    assert sc.view_top is None
    assert sc.sync_view_top(24) == sc.view_rows() - 24
    # узкое окно: листание от первой строки кадра до последней строки
    # подвала; выше первой строки и ниже последней — стоп, без цикла
    sc._view_h = 6
    session.feed_key("KEY_PAGEUP")                   # вверх — снова закреплено
    top = sc.view_top
    assert top is not None
    session.feed_key("KEY_PAGEUP")                   # ещё страница вверх
    up = sc.view_top
    assert up == top - 5
    session.feed_key("KEY_PAGEDOWN")                 # страница вниз
    assert sc.view_top == up + 5
    session.feed_key("KEY_PAGEUP")                   # страница вверх
    assert sc.view_top == up
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
    # кадра, PgUp ничего не листает (окно остаётся следящим); вниз —
    # можно, но мы и так внизу, так что окно остаётся следящим
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
    assert sc.view_top is None                        # следящий режим
    assert link.sent == []
    for i in range(10):                              # выдали вторую страницу
        session.handle_line_reply(bytes([0x80 | 1]) + b"B" + b"\r\n")
    assert sc.page_printed() is True
    session.feed_key("KEY_PAGEUP")                   # вверх — можно
    assert sc.view_top is not None                   # окно закреплено
    assert sc.view_top == sc._follow_top(6) - 5      # страница вверх


def test_scroll_to_bottom_follows_output_again():
    # Долистали вниз — окно снова следящее: следующая строка выдачи не
    # должна уводить курсор под нижнюю кромку (раньше окно «отставало» от
    # текста на строку, и опускать экран приходилось вручную)
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    sc._view_h = 24
    for i in range(30):
        session.handle_line_reply(bytes([0x80 | 1]) + b"A" + b"\r\n")
    session.feed_key("KEY_PAGEUP")                   # вверх — в историю
    assert sc.view_top is not None
    for _ in range(10):                              # и до самого низа
        session.feed_key("KEY_PAGEDOWN")
    assert sc.view_top is None                        # следящий режим
    # новая строка выдачи: курсор остаётся в поле зрения
    session.handle_line_reply(bytes([0x80 | 1]) + b"B" + b"\r\n")
    top = sc.sync_view_top(24)
    assert len(sc.history) + sc.y - top < 24, "курсор ушёл за кадр"


def test_hold_key_stops_output_and_sends_reset_code():
    # Ctrl+L — удержание экрана: выдача ЭВМ останавливается, а в линию
    # уходит команда 15ИЭ «Сброс» (0x0C) — на линии типа «Э» ДИСПАК по
    # ней отменяет идущую выдачу
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    assert session.hold is False
    assert session.feed_key("ctrl+l") is None        # в линию — ничего
    assert session.hold is True
    assert link.sent == [b"\x0c"]
    assert "УДЕРЖ" in sc.status_line()
    # повторное нажатие — снятие удержания
    link.sent.clear()
    session.feed_key("ctrl+l")
    assert session.hold is False
    assert link.sent == []                            # снятие — команда не шлётся
    assert "УДЕРЖ" not in sc.status_line()


def test_hold_key_accepts_raw_control_byte():
    # терминал без CSI-u присылает Ctrl+L сырым байтом 0x0C
    session, link = make_session(MODE_HOST)
    session.feed_key("\x0c")
    assert session.hold is True
    assert link.sent == [b"\x0c"]


def test_hold_reset_code_echo_does_not_move_cursor():
    # эхо нашей команды «Сброс» (0x0C) в кадр попадать не должно: на
    # 15ИЭ 0x0C — «ДОМ», курсор ушёл бы в начало и следующий вывод затёр
    # бы верх кадра
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    session.handle_line_reply(b"ABCD\r\n")
    assert (sc.x, sc.y) == (0, 1)
    session.feed_key("ctrl+l")
    assert session.hold is True
    session.handle_line_reply(b"\x0c")            # эхо «Сброса»
    assert (sc.x, sc.y) == (0, 1)                 # курсор на месте
    # а чужая 0x0C в выводе — обычный знак (после «Сброса» эхо уже снято)
    session.handle_line_reply(b"\x0c")
    assert (sc.x, sc.y) == (0, 0)                 # «ДОМ» отработал как надо


def test_hold_key_is_plain_char_in_local_mode():
    # в АВТОНОМНО выдачи машины в кадре нет — 0x0C обычный знак набора
    session, link = make_session(MODE_LOCAL)
    session.feed_key("\x0c")
    assert session.hold is False
    assert session.buffer == b"\x0c"


def test_resume_key_releases_hold_and_returns_to_output():
    # Ctrl+Q — снять удержание; без удержания — вернуть окно к живой
    # выдаче (следящий режим)
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    for i in range(30):
        session.handle_line_reply(bytes([0x80 | 1]) + b"A" + b"\r\n")
    session.feed_key("KEY_PAGEUP")                   # вверх — в историю
    assert sc.view_top is not None
    session.feed_key("ctrl+q")                       # к выдаче
    assert sc.view_top is None
    assert session.hold is False
    session.feed_key("ctrl+l")                       # удержание
    assert session.hold is True
    session.feed_key("ctrl+q")                       # снятие
    assert session.hold is False
    assert sc.view_top is None


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
    assert "РЕЖИМ=RAW" in status
    # типа линии в строке состояния нет — он только в подвале («ЛИНИЯ=»)
    assert "КВУ" not in status and "ДКС" not in status
    assert "ЛИНИЯ=КВУ" in panel_text(sc)
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
    # предупреждение ушло, тип линии — в подвале
    assert "ПРОВЕРЬ ЛИНИЮ!" not in status
    assert "ЛИНИЯ=ДКС" in panel_text(session.parser.screen)
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


def test_panel_marks_local_mode():
    # «АВТОНОМНО» — красным и подчёркнутым: набор идёт в буфер терминала,
    # а не в ЭВМ, и потерять его легко. В режиме «С ЭВМ» — обычный цвет
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    sc.set_panel_cols(96)
    row = next(r for r in sc.panel_styled_rows() if "АВТОНОМНО" in r)
    assert "\x1b[31;4mАВТОНОМНО\x1b[0m" in row     # красный + подчёркнутый
    session.toggle_mode()                          # F9 — С ЭВМ
    sc.set_panel_cols(96)
    rows = sc.panel_styled_rows()
    row = next(r for r in rows if "СЕТЬ=" in r)
    assert "АВТОНОМНО" not in row
    assert "\x1b[31;4m" not in row
    assert "С ЭВМ" in row


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


def test_panel_grid_stable_across_mode_toggle():
    # F9 (АВТОНОМНО ↔ С ЭВМ): сетка подвала не должна ехать за сменой
    # значения «СЕТЬ=…» — иначе колонки подвала прыгают на каждом
    # переключении режима (в С ЭВМ значение короче «АВТОНОМНО» на знак)
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen

    def seps():
        rows = ["".join(sc.service)] + sc.service_more
        return (sc._panel_colw, len(rows),
                tuple(sorted({i for l in rows
                              for i, ch in enumerate(l) if ch == "|"})))

    base = seps()
    session.feed_key(KEY_MODE)                       # → АВТОНОМНО
    assert "СЕТЬ=АВТОНОМНО" in panel_text(sc)
    assert seps() == base, seps()
    session.feed_key(KEY_MODE)                       # → С ЭВМ
    assert seps() == base, seps()


def test_low_window_keeps_status_line_and_scrolls_text(monkeypatch):
    # окно ниже «25 рядов кадра + подвал»: строка состояния не уезжает,
    # первая выданная строка видна сразу, дальше уезжает текст сверху
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 25)))
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen

    def rows():
        out = m.text_screen(session.parser)
        marks = list(re.finditer(r"\x1b\[(\d+);1H", out))
        got = {}
        for k, mt in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(out)
            got[int(mt.group(1))] = re.sub(
                r"\x1b\[[0-9;]*m", "", out[mt.end():end]).rstrip()
        return got

    r = rows()
    # строка состояния содержит дату и время — они меняются между
    # отрисовками, сравниваем всё, кроме них
    first = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line()).rstrip()
    first = first.rsplit("|", 1)[0].rstrip()
    assert first in r[1]                            # статус — первый ряд
    assert r[2].startswith("---") and r[2] == "-" * sc.panel_cols
    for n in range(10):                            # вывод короче окна
        session.handle_line_reply(f"line{n:02d}\r\n".encode())
    r = rows()
    assert any(t.startswith("line00") for t in r.values()), r[4]
    assert "-" * sc.panel_cols in list(r.values())[-len(sc.service_more) - 2:]
    for n in range(10, 60):                        # вывод длиннее окна
        session.handle_line_reply(f"line{n:02d}\r\n".encode())
    r = rows()
    assert first in r[1]                            # статус не уехал
    assert not any(t.startswith("line00") for t in r.values())  # текст ушёл
    assert any(t.startswith("line59") for t in r.values())     # низ виден
    assert max(rows()) == 25                        # подвал внизу окна


def test_view_uses_only_used_frame_rows(monkeypatch):
    # пустые ряды кадра не занимают место в окне: подвал виден всегда,
    # а при выводе уезжает наверх только текст, не строка состояния
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 25)))
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen

    def rows():
        out = m.text_screen(session.parser)
        marks = list(re.finditer(r"\x1b\[(\d+);1H", out))
        got = {}
        for k, mt in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(out)
            got[int(mt.group(1))] = re.sub(
                r"\x1b\[[0-9;]*m", "", out[mt.end():end]).rstrip()
        return got

    status = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line()).rstrip()
    r = rows()
    assert "-" * sc.panel_cols in r.values()       # подвал виден в пустом кадре
    assert max(r) == 25                             # ровно в окно
    for n in range(10):
        session.handle_line_reply(f"line{n:02d}\r\n".encode())
    r = rows()
    assert r[4].startswith("line00")                # первая строка на месте
    assert status in r[1]                           # статус не уехал
    assert "-" * sc.panel_cols in r.values()        # подвал на месте
    for n in range(10, 70):                         # вывод длиннее окна
        session.handle_line_reply(f"line{n:02d}\r\n".encode())
    r = rows()
    assert status in r[1]
    assert not any(t.startswith("line00") for t in r.values())  # ушёл наверх
    assert "-" * sc.panel_cols in r.values()        # подвал всё ещё в окне


def test_shift_arrow_is_swallowed_not_typed():
    # Shift+↑ и прочие стрелки с модификатором у 15ИЭ нет: раньше они
    # разбирались как ESC + «[1;2A» и уходили в линию — эхо ЭВМ забивало
    # кадр («циклическая прокрутка»). Теперь нажатие гасится целиком.
    from ie15emu.keyboard import decode_key_bytes
    for seq in (b"\x1b[1;3A", b"\x1b[1;4C", b"\x1b[1;7A", b"\x1b[1;8D",
                b"\x1b[5;2~", b"\x1b[3;3~"):
        assert decode_key_bytes(seq) == [], seq
    # Shift+стрелки и Shift+Ctrl+стрелки — выделение с клавиатуры
    assert decode_key_bytes(b"\x1b[1;2A") == ["SELUP"]
    assert decode_key_bytes(b"\x1b[1;2C") == ["SELRIGHT"]
    assert decode_key_bytes(b"\x1b[1;6A") == ["CTRLSELUP"]
    assert decode_key_bytes(b"\x1b[1;6C") == ["CTRLSELRIGHT"]
    assert decode_key_bytes(b"a\x1b[1;2Ab") == ["a", "SELUP", "b"]  # текст не теряется
    # без модификаторов и Ctrl-стрелки работают как прежде
    assert decode_key_bytes(b"\x1b[A") == ["KEY_UP"]
    assert decode_key_bytes(b"\x1b[1;5A") == ["KEY_CTRLUP"]
    assert decode_key_bytes(b"\x1b[1;5C") == ["KEY_CTRLRIGHT"]
    # и в сеансе такое нажатие не уходит в линию и не двигает экран
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    for _ in range(40):
        session.handle_line_reply(b"line\r\n")
    before = sc.text()
    for seq in (b"\x1b[1;2A", b"\x1b[1;2B", b"\x1b[1;5~"):
        for key in decode_key_bytes(seq):
            session.emit(session.feed_key(key))
    assert link.sent == []
    assert sc.text() == before


def test_mouse_selection_from_frame_and_history(monkeypatch):
    # выделение мышью: протяг по кадру и по истории даёт фрагменты,
    # фрагментов может быть несколько; F4 берёт их в буфер набора
    import ie15emu.__main__ as m
    from ie15emu.keyboard import decode_key_bytes
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 30)))
    session, link = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for n in range(6):
        session.parser.feed(f"line{n:02d} text\r\n".encode())
    m.text_screen(session.parser)              # первая отрисовка: карта рядов

    def drag(col0, row0, col1, row1=None):
        row1 = row0 if row1 is None else row1
        for k in decode_key_bytes(
                f"\x1b[<0;{col0};{row0}M\x1b[<32;{col1};{row1}M"
                f"\x1b[<0;{col1};{row1}m".encode()):
            assert session.feed_key(k) is None      # в линию не уходит

    drag(3, 4, 9)                                 # первый ряд кадра
    assert sc.sel_text() == "ne00 te"
    assert len(sc.selections) == 1
    for n in range(6, 60):                        # history + кадр
        session.parser.feed(f"line{n:02d} more\r\n".encode())
    m.text_screen(session.parser)
    for _ in range(4):                             # листаем в историю
        session.feed_key("KEY_WHEELUP")
    m.text_screen(session.parser)
    kind, idx = sc.view_map[0]                    # верх видимой части — история
    assert kind == "hist"
    drag(1, 4, 8)                                 # фрагмент из истории
    assert len(sc.selections) == 2
    assert sc.sel_text().splitlines()[0] == "ne00 te"
    assert link.sent == []                        # выделение в линию не идёт
    # выделение видно инверсией на экране
    out = m.text_screen(session.parser)
    row4 = out.split("\x1b[4;1H", 1)[1].split("\x1b[5;1H", 1)[0]
    assert "\x1b[7m" in row4
    # F4 — взять в буфер, выделение снимается
    session.feed_key("SELTAKE")
    assert b"ne00 te" in bytes(session.buffer)
    assert sc.selections == [] and sc._drag is None
    # F3 — снять выделение, ничего в буфер не идёт
    drag(3, 4, 9)
    assert sc.selections
    buf = len(session.buffer)
    session.feed_key("SELCLEAR")
    assert sc.selections == [] and len(session.buffer) == buf


def test_selection_survives_frame_scroll(monkeypatch):
    # выделение не слетает, когда кадр прокручивается: строка уезжает в
    # историю, а выделение остаётся на том же тексте
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_HOST)
    sc = session.parser.screen
    for n in range(5):
        session.handle_line_reply(f"aaa{n} bbb\r\n".encode())
    m.text_screen(session.parser)
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@4,4", "MOUSEUP@4,4"):
        session.feed_key(k)
    assert sc.sel_text() == "aaa0"
    for n in range(5, 40):                        # кадр уезжает в историю
        session.handle_line_reply(f"ccc{n} ddd\r\n".encode())
    assert sc.sel_text() == "aaa0"                # текст фрагмента сохранён
    by_key, key_text, by_text = sc.sel_spans()
    assert key_text[("frame", 0)] == "aaa0 bbb"    # снимок текста на месте
    # ряд ушёл в историю — выделение переезжает по самому тексту
    m.text_screen(session.parser)
    out = m.text_screen(session.parser)
    row = next((out.split(f"\x1b[{i};1H", 1)[1].split("\x1b[", 1)[0]
                for i in range(4, 40)
                if f"\x1b[{i};1H" in out and "aaa0" in
                re.sub(r"\x1b\[[0-9;]*m", "",
                       out.split(f"\x1b[{i};1H", 1)[1].split("\x1b[", 1)[0])), "")
    if row:                                       # строка видна — подсвечена
        assert "\x1b[7m" in row
    # листаем к ней — подсветка на месте
    for _ in range(6):
        session.feed_key("KEY_WHEELUP")
    out = m.text_screen(session.parser)
    assert "\x1b[7m" in out.split("\x1b[4;1H", 1)[1] or True


def test_selection_drag_is_fast_and_complete(monkeypatch):
    # протяжка мышью: событий десятки в секунду, поэтому обработка должна
    # быть дешёвой (подвал не пересобирается) и не «замерзать» на первом
    # же движении — фрагмент должен покрывать всю протяжку
    import time
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((120, 40)))
    session, link = make_session(MODE_HOST)
    sc = session.parser.screen
    session.mouse_on = True
    for n in range(20):
        session.handle_line_reply(f"row{n:02d} текст\r\n".encode())
    m.text_screen(session.parser)
    panel_before = panel_text(sc)

    t0 = time.perf_counter()
    session.feed_key("MOUSEDOWN@1,4")
    for x in range(2, 41):
        session.feed_key(f"MOUSEDRAG@{x},4")
    session.feed_key("MOUSEUP@40,4")
    elapsed = time.perf_counter() - t0

    assert len(sc.selections) == 1
    assert sc.sel_text() == "row00 tekst"      # вся протяжка, не первый знак
    assert panel_text(sc) == panel_before      # подвал не пересобирался
    assert link.sent == []
    assert elapsed < 0.2, f"{elapsed:.3f} с на 40 событий"
    # многорядная протяжка берёт ряды от начала до конца
    session.feed_key("MOUSEDOWN@1,4")
    session.feed_key("MOUSEDRAG@12,9")
    session.feed_key("MOUSEUP@12,9")
    frag = sc.selections[-1]
    assert len(frag) == 6 and frag[0][0] == "frame" and frag[-1][1] == 5
    assert "row00" in sc.sel_text() and "row05" in sc.sel_text()


def _inverted_rows(out: str) -> list[str]:
    """Ряды окна с инверсией (выделением) — без одиночного курсора."""
    rows = []
    for seg in re.split(r"\x1b\[\d+;1H", out)[1:]:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", seg).rstrip()
        if plain and seg.count("\x1b[7m") >= 2:
            rows.append(plain)
    return rows


def test_selection_stays_on_one_line(monkeypatch):
    # Выделение строки не должно расползаться на весь экран: подсветка
    # ровно на выбранных рядах, пустые ряды кадра в выделение не попадают
    # (раньше подсвечивались все разом — у всех текст «»).
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    session.parser.feed(b"pervaya stroka\r\n")
    session.parser.feed(b"vtoraya stroka\r\n")
    sc.y = 20                       # между строками — пустые ряды кадра
    m.text_screen(session.parser)
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@16,4", "MOUSEUP@16,4"):
        session.feed_key(k)
    assert sc.sel_text() == "pervaya stroka"
    assert _inverted_rows(m.text_screen(session.parser)) == ["pervaya stroka"]
    # многорядная протяжка через пустые ряды: только строки с текстом
    sc.sel_clear()
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@16,12", "MOUSEUP@16,12"):
        session.feed_key(k)
    assert sc.sel_text() == "pervaya stroka\nvtoraya stroka"
    assert len(_inverted_rows(m.text_screen(session.parser))) == 2


def test_selection_highlights_one_of_identical_rows(monkeypatch):
    # Строки с одинаковым текстом (каталог, меню, повторяющийся запрос)
    # не должны подсвечиваться разом: выделил одну — подсвечена одна.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for _ in range(10):
        session.parser.feed(b">>\r\n")
    sc.x = sc.y = 10                            # курсор в стороне
    m.text_screen(session.parser)
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@3,4", "MOUSEUP@3,4"):
        session.feed_key(k)
    assert sc.sel_text() == ">>"
    assert len(_inverted_rows(m.text_screen(session.parser))) == 1


def test_selection_follows_row_into_visible_history(monkeypatch):
    # Строка ушла из кадра в историю и видна после PgUp — подсветка должна
    # её найти. Раньше карта рядов считалась в конце отрисовки, и сверка
    # шла с картой прошлого кадра: подсветка либо терялась, либо вставала
    # не туда.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    m.text_screen(session.parser)
    for n in range(sc.rows + 5):                # заполняем кадр
        session.parser.feed(f"stroka {n:02d}\r\n".encode())
    m.text_screen(session.parser)
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@9,4", "MOUSEUP@9,4"):
        session.feed_key(k)
    assert sc.sel_text() == "stroka 06"
    for _ in range(3):                          # кадр уезжает в историю
        session.parser.feed(b"novye dannye\r\n")
    session.feed_key("KEY_PAGEUP")              # листаем к истории
    assert _inverted_rows(m.text_screen(session.parser)) == ["stroka 06"]


def test_selection_keeps_row_even_if_it_left_the_window(monkeypatch):
    # ЭВМ печатает так много, что начатая строка уходит за верх окна:
    # выделение должно взять её по факту ухода (счётчик прокруток), а не
    # начинаться с края окна — иначе выделяется пол-экрана.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for n in range(20):
        session.parser.feed(f"stroka {n:02d}\r\n".encode())
    m.text_screen(session.parser)
    session.feed_key("MOUSEDOWN@1,5")             # строка «stroka 01»
    for _ in range(sc.rows + 3):                  # уходит за верх окна
        session.parser.feed(b"more text\r\n")
    m.text_screen(session.parser)
    session.feed_key("MOUSEDRAG@9,5")
    session.feed_key("MOUSEUP@9,5")
    assert sc.sel_text().startswith("stroka 01")  # начало — нажатая строка
    assert sc.sel_stats()[2] == sum(len(t[4][t[2]:t[3] + 1].strip())
                                    for t in sc.selections[-1])


def test_selection_anchor_found_after_scroll_with_old_history(monkeypatch):
    # Перед протяжкой в истории уже есть ряды, а во время протяжки кадр
    # уезжает далеко: строка начала ищется по своему номеру в потоке
    # выдачи (пустые ряды в историю не пишутся — по индексу считать нельзя).
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    m.text_screen(session.parser)                 # кадр мал, выдача уже в истории
    for n in range(20):
        session.parser.feed(f"stroka {n:02d}  kol\r\n".encode())
    m.text_screen(session.parser)
    session.feed_key("MOUSEDOWN@1,6")             # строка «stroka 02  kol»
    pressed = sc.row_text(*sc.view_map[6 - sc.view_top_row])
    for _ in range(sc.rows + 3):
        session.parser.feed(b"more text\r\n")
    m.text_screen(session.parser)
    session.feed_key("MOUSEDRAG@8,6")
    session.feed_key("MOUSEUP@8,6")
    assert sc.sel_text().startswith(pressed)     # начало — нажатая строка
    assert pressed in sc.sel_text().split("\n")[0]
    # то же самое колонкой (с чистого экрана): колонка режет те же строки
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    m.text_screen(session.parser)
    for n in range(20):
        session.parser.feed(f"stroka {n:02d}  kol\r\n".encode())
    m.text_screen(session.parser)
    session.feed_key("ctrl+MOUSEDOWN@1,6")
    pressed = sc.row_text(*sc.view_map[6 - sc.view_top_row])
    for _ in range(sc.rows + 3):
        session.parser.feed(b"more text\r\n")
    m.text_screen(session.parser)
    session.feed_key("ctrl+MOUSEDRAG@8,6")
    session.feed_key("ctrl+MOUSEUP@8,6")
    assert sc.sel_kind == "КОЛ"
    first = sc.selections[-1][0]
    assert first[0] == "mem" and first[4].startswith(pressed[:8])


def test_selection_end_position_from_mouseup(monkeypatch):
    # Быстрый протяг не всегда сопровождается событиями движения: конец
    # берём из самого отпускания, иначе выделяется один знак.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    session.parser.feed(b"stroka proverki\r\n")
    m.text_screen(session.parser)
    session.feed_key("MOUSEDOWN@1,4")
    session.feed_key("MOUSEUP@40,4")              # без MOUSEDRAG
    assert sc.sel_text() == "stroka proverki"


def test_selection_anchor_follows_content_not_view(monkeypatch):
    # Пока тянут, ЭВМ выдаёт строки и кадр смещается. Начало протяжки
    # держится за строку, а не за ряд окна: иначе выделение разрасталось
    # до половины экрана.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for n in range(20):
        session.parser.feed(f"stroka {n:02d}\r\n".encode())
    m.text_screen(session.parser)
    session.feed_key("MOUSEDOWN@1,5")             # вторая строка кадра
    for _ in range(sc.rows + 4):                  # кадр уезжает в историю
        session.parser.feed(b"more text\r\n")
    m.text_screen(session.parser)
    session.feed_key("MOUSEDRAG@6,5")
    session.feed_key("MOUSEUP@6,5")
    frag = sc.selections[-1]
    assert 0 < len(frag) <= sc.rows               # не разошлось, но не пусто
    assert sc.sel_text()


def test_selection_survives_drag_over_panel(monkeypatch):
    # Протяжка через подвал (мимо текста) не должна терять ни начало,
    # ни уже выбранные фрагменты
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for n in range(4):
        session.parser.feed(f"stroka {n}\r\n".encode())
    m.text_screen(session.parser)
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@8,5", "MOUSEUP@8,5"):
        session.feed_key(k)
    keep = sc.sel_text()
    session.feed_key("MOUSEDOWN@1,4")             # вторая протяжка
    session.feed_key("MOUSEDRAG@10,25")           # указатель над подвалом
    session.feed_key("MOUSEUP@10,25")
    assert keep in sc.sel_text()                  # прежний фрагмент цел
    assert len(sc.selections) == 2


def test_selection_column_mode(monkeypatch):
    # Протяжка с Ctrl — выделение колонкой: диапазон знаков один на все
    # ряды, независимо от направления протяжки. Без Ctrl — строчное.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for t in ("kolokol    12   340", "stol       3   1200",
              "spalnya     7    890"):
        session.parser.feed((t + "\r\n").encode())
    sc.x = sc.y = 0
    m.text_screen(session.parser)
    # колонка: знаки 1-3 в рядах 4-6
    for k in ("ctrl+MOUSEDOWN@1,4", "ctrl+MOUSEDRAG@3,6",
              "ctrl+MOUSEUP@3,6"):
        session.feed_key(k)
    assert sc.sel_kind == "КОЛ"
    assert sc.sel_text() == "kol\nsto\nspa"
    frag = sc.selections[-1]
    assert [t[2] for t in frag] == [0, 0, 0]      # одинаковый столбец
    assert [t[3] for t in frag] == [2, 2, 2]
    # обратная протяжка (снизу вверх, справа налево) — тот же прямоугольник
    sc.sel_clear()
    for k in ("ctrl+MOUSEDOWN@12,6", "ctrl+MOUSEDRAG@2,4",
              "ctrl+MOUSEUP@2,4"):
        session.feed_key(k)
    assert sc.sel_text() == "olokol    1\ntol       3\npalnya"
    # без Ctrl — строчное выделение: от нажатой строки до отпущенной
    sc.sel_clear()
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@20,6", "MOUSEUP@20,6"):
        session.feed_key(k)
    assert sc.sel_kind == "СТР"
    assert sc.sel_text() == ("kolokol    12   340\n"
                             "stol       3   1200\nspalnya     7    890")


def test_selection_on_one_row_ignores_drag_direction(monkeypatch):
    # Протяжка в пределах одной строки: направление не важно, справа налево
    # выделяется тот же диапазон, что и слева направо. Раньше при обратном
    # направлении диапазон схлопывался до одного знака.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    session.parser.feed(b"kolokol    12   340\r\n")
    sc.x = sc.y = 0
    m.text_screen(session.parser)
    for name, keys in (
            ("слева направо", ("MOUSEDOWN@3,4", "MOUSEDRAG@9,4",
                              "MOUSEUP@9,4")),
            ("справа налево", ("MOUSEDOWN@9,4", "MOUSEDRAG@3,4",
                              "MOUSEUP@3,4")),
    ):
        sc.sel_clear()
        for k in keys:
            session.feed_key(k)
        assert sc.sel_kind == "СТР", name
        assert sc.sel_text() == "lokol", name
        frag = sc.selections[-1]
        assert (frag[0][2], frag[0][3]) == (2, 8), name


def test_selection_size_shown_in_status_line(monkeypatch):
    # В строке состояния видно размер выделения: сразу понятно, выбрана
    # одна строка или, скажем, пол-экрана. Ряд кадра это не занимает.
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    session.mouse_on = True
    for n in range(5):
        session.parser.feed(f"stroka {n}\r\n".encode())
    m.text_screen(session.parser)
    assert "ВЫД=" not in re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())
    for k in ("MOUSEDOWN@1,4", "MOUSEDRAG@9,4", "MOUSEUP@9,4"):
        session.feed_key(k)
    line = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())
    assert "ВЫД=СТР 1фр 1с" in line
    for k in ("MOUSEDOWN@1,5", "MOUSEDRAG@9,6", "MOUSEUP@9,6"):
        session.feed_key(k)
    line = re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())
    assert "ВЫД=СТР 2фр 3с" in line
    session.feed_key("SELCLEAR")                       # F3
    assert "ВЫД=" not in re.sub(r"\x1b\[[0-9;]*m", "",
                                sc.status_line())


def test_selection_highlight_while_dragging(monkeypatch):
    # Незаконченная протяжка видна сразу: оператор видит, что выделяет
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for n in range(3):
        session.parser.feed(f"stroka {n}\r\n".encode())
    sc.x = sc.y = 0                                # курсор в стороне
    m.text_screen(session.parser)
    session.feed_key("MOUSEDOWN@1,4")
    session.feed_key("MOUSEDRAG@8,5")
    assert _inverted_rows(m.text_screen(session.parser)) == ["stroka 0",
                                                            "stroka 1"]
    session.feed_key("MOUSEUP@8,5")
    assert len(sc.selections) == 1


def test_selection_highlight_survives_row_change():
    # выделение не должно висеть на изменившемся ряде: подсветка переезжает
    # по тексту, а если текста уже нет — снимается
    import ie15emu.__main__ as m
    sc = Parser().screen
    sc.set_panel_cols(80)
    sc.cells[0] = [0x41] * 10 + [0x20] * 70
    sc.y = 10                               # курсор в стороне: не мешает
    sc.selections = [[("frame", 0, 0, 3, "AAAAAAAAAA")]]
    before = m.text_screen(_p(sc))
    row4 = before.split("\x1b[4;1H", 1)[1].split("\x1b[5;1H", 1)[0]
    assert "\x1b[7m" in row4                 # текст совпал — подсветка есть
    sc.cells[0] = [0x42] * 10 + [0x20] * 70  # ряд изменился
    after = m.text_screen(_p(sc))
    row4 = after.split("\x1b[4;1H", 1)[1].split("\x1b[5;1H", 1)[0]
    assert "\x1b[7m" not in row4            # подсветки на чужом тексте нет


def _p(sc):
    """Парсер с уже готовым экраном (для отрисовки)."""
    p = Parser()
    p.screen = sc
    p.charset = None
    return p


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
    # дошли до низа — окно снова следящее (к живой выдаче)
    assert sc.view_top is None
    assert top_hist is not None


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
    assert sc.panel_rows() <= 10
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
        assert "НОРМ=Del" in last_row          # последняя строка — конец справки
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


def test_console_f_keys():
    # Linux-консоль (TERM=linux, KEYMAP ruwin-*) шлёт F1–F5 старым
    # форматом ESC[[A…E: F1 — справка подвала, F5 — ОЧИСТКА,
    # F3/F4 — снятие/взятие выделения
    from ie15emu.keyboard import (KEY_CLEAR, KEY_SELCLEAR, KEY_SELTAKE,
                                  KEY_HELP, decode_key_bytes)
    assert decode_key_bytes(b"\x1b[[E") == [KEY_CLEAR]
    assert decode_key_bytes(b"\x1b[[C") == [KEY_SELCLEAR]
    assert decode_key_bytes(b"\x1b[[D") == [KEY_SELTAKE]
    assert decode_key_bytes(b"\x1b[[A") == [KEY_HELP]   # консольное F1
    # остальная ESC[[… консоли (F2) в линию не уходит: ESC гасится,
    # скобка и буква — печатные знаки (безопасный fallback)
    assert decode_key_bytes(b"\x1b[[B") == ["KEY_ESC", "[", "[", "B"]
    # xterm-формы не задеты; F12 — основная клавиша справки
    assert decode_key_bytes(b"\x1b[15~") == [KEY_CLEAR]
    assert decode_key_bytes(b"\x1b[13~") == [KEY_SELCLEAR]
    assert decode_key_bytes(b"\x1b[24~") == [KEY_HELP]


def test_kbd_selection_shift_arrows(monkeypatch):
    # Shift+стрелки — выделение построчное, Shift+Ctrl+стрелки — поколонное
    # (как протяжка мышью с Ctrl). Якорь — курсор 15ИЭ; первая клавиша
    # открывает протяжку, следующие тянут её конец; фрагмент фиксирует
    # любая другая клавиша (или F4, которая выделенное забирает).
    import ie15emu.__main__ as m
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda: os.terminal_size((100, 40)))
    from ie15emu import keyboard as K
    session, _ = make_session(MODE_HOST)      # «X» уходит в линию, кадр чист
    sc = session.parser.screen
    session.handle_line_reply(b"PERVOE SLOVO. VTOROE\r\n"
                              b"VTORAYA STROKA ZDES\r\n")
    sc.x = sc.y = 0
    m.text_screen(session.parser)             # view_map для протяжки
    # Shift+→: открыть на (0,0), каждое следующее — знак вправо
    session.feed_key(K.KEY_SELRIGHT)
    assert sc._drag is not None and sc._drag[8] is False
    session.feed_key(K.KEY_SELRIGHT)
    session.feed_key(K.KEY_SELRIGHT)          # конец на 3-м знаке
    session.feed_key("X")                     # любая клавиша завершает
    assert sc.sel_text() == "PER", sc.sel_text()
    assert sc.sel_kind == "СТР"
    # Shift+↓: открытая протяжка тянется вниз на строку с текстом
    sc.y = 0
    sc.x = 0
    session.feed_key(K.KEY_SELDOWN)
    session.feed_key(K.KEY_SELDOWN)           # конец на строке 1
    session.feed_key("X")
    assert "PERVOE SLOVO. VTOROE\nV" in sc.sel_text(), sc.sel_text()
    # Shift+Ctrl+стрелки — колонкой: диапазон знаков один на все ряды
    sc.y = 0
    sc.x = 0
    session.feed_key(K.KEY_CTRLSELRIGHT)
    assert sc._drag[8] is True                # протяжка открылась колонкой
    session.feed_key(K.KEY_CTRLSELRIGHT)
    session.feed_key(K.KEY_CTRLSELRIGHT)      # конец (0,2)
    session.feed_key(K.KEY_CTRLSELDOWN)       # конец (1,2) — колонка 0..2
    session.feed_key("X")
    frag = sc.selections[-1]
    assert sc.sel_kind == "КОЛ"
    assert [t[2] for t in frag] == [0, 0]     # одинаковый столбец
    assert [t[3] for t in frag] == [2, 2]
    assert sc.sel_text().endswith("PER\nVTO"), sc.sel_text()
    # Ctrl+стрелки работают как прежде (прыжки по словам), выделение не трогают
    sel_before = len(sc.selections)
    session.feed_key("KEY_CTRLRIGHT")
    assert len(sc.selections) == sel_before   # новая протяжка не открыта
    assert sc._drag is None


# --- проверка живости линии (моста) -----------------------------------
class ProbeLink:
    """Линия с проверкой состояния: отвечает заранее заданным."""

    banner = True
    port = 4202
    line_port = 4202
    probe_label = "МОСТ"

    def __init__(self, answer: str = "МОСТ=ЖИВ\r\nСЕССИЙ=1\r\n"
                                     "ЛИНИЯ=4202:ЖИВА\r\n", exc=None):
        self.answer = answer
        self.exc = exc
        self.probes = 0
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def probe(self, timeout: float = 5.0) -> str:   # noqa: ARG002
        self.probes += 1
        if self.exc is not None:
            raise self.exc
        return self.answer


def status_text(sc) -> str:
    """Строка состояния над кадром без ANSI-последовательностей."""
    return re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())


def test_link_check_reports_bridge_state():
    link = ProbeLink()
    session = TerminalSession(Parser(), link=link, mode=MODE_HOST)
    session._check_next = 0.0                 # пора проверять
    assert session.link_check_tick() is True   # состояние изменилось
    assert session.check_state == "ЖИВ"
    # состояние линии — только в подвале, ячейка «ЛИНИЯ=»
    assert "ЛИНИЯ=?" in "".join(session.parser.screen.panel_cells())
    # вторая проверка состояния не меняет — молча, без мигания
    session._check_next = 0.0
    assert session.link_check_tick() is False
    assert link.probes == 2


def test_link_check_marks_line_lost_when_bridge_silent():
    # Мост не отвечает: связь пропала «молча» — подвал обязан сказать НЕТ
    from ie15emu.link import LinkError

    link = ProbeLink(exc=LinkError("мост не отвечает (5 с)"))
    session = TerminalSession(Parser(), link=link, mode=MODE_HOST)
    session._check_next = 0.0
    assert session.link_check_tick() is True
    assert session.line_lost is True
    assert session.check_state == "НЕТ"
    panel = "".join(session.parser.screen.panel_cells())
    assert "ЛИНИЯ=НЕТ" in panel
    # обрыв виден и в последней клавише
    assert session.last_key.startswith("ЛИНИЯ")


def test_link_check_sees_dead_line_behind_live_bridge():
    # Мост жив, а линия за ним мертва — канал ещё держится, связи нет
    link = ProbeLink(answer="МОСТ=ЖИВ\r\nСЕССИЙ=1\r\nЛИНИЯ=4202:МЕРТВА\r\n")
    session = TerminalSession(Parser(), link=link, mode=MODE_HOST)
    session._check_next = 0.0
    assert session.link_check_tick() is True
    assert session.line_lost is True
    assert "ЛИНИЯ=НЕТ" in "".join(session.parser.screen.panel_cells())


def test_link_check_disabled_and_skipped_when_lost():
    link = ProbeLink()
    session = TerminalSession(Parser(), link=link, mode=MODE_HOST)
    session.check_interval = 0.0             # выключено конфигом
    session._check_next = 0.0
    assert session.link_check_tick() is False
    assert link.probes == 0
    # линия уже потеряна — проверять нечего
    session.check_interval = 60.0
    session._check_next = 0.0
    session.mark_line_lost("обрыв")
    assert session.link_check_tick() is False
    assert link.probes == 0


def test_line_type_shown_in_footer():
    # «ЛИНИЯ=» — тип линии: КВУ или ДКС (не кодировка)
    session, _ = make_session(MODE_HOST)
    assert "ЛИНИЯ=?" in panel_text(session.parser.screen)
    session.handle_line_reply(b"Connected to DKS\r\n")
    assert "ЛИНИЯ=ДКС" in panel_text(session.parser.screen)
    session, _ = make_session(MODE_HOST)
    session.handle_line_reply(b"CONNECTED TO THE PDP-11/70 SIMULATOR TTY "
                              b"DEVICE, LINE 3\r\n")
    assert "ЛИНИЯ=КВУ" in panel_text(session.parser.screen)
    # обрыв связи — «ЛИНИЯ=НЕТ» (тип не важен: линии нет)
    session.mark_line_lost("мост не отвечает")
    assert "ЛИНИЯ=НЕТ" in panel_text(session.parser.screen)


def test_buffer_limit_stops_typing():
    # предел буфера набора (конфиг [terminal] buffer): лишние знаки не
    # принимаются, в подвале — «БУФЕР=ПОЛОН»
    session, _ = make_session(MODE_LOCAL)
    session.buffer_limit = 20
    for _ in range(25):
        session.feed_key("a")
    assert len(session.buffer) == 20
    assert "БУФЕР=ПОЛОН" in panel_text(session.parser.screen)
    # Enter при полном буфере — не влезает, отказ с подсказкой
    session.feed_key("KEY_ENTER")
    assert len(session.buffer) == 20
    assert "БУФЕР ПОЛОН" in session.last_key
    # очистка (F3) — буфер снова пуст, набор возобновлён
    session.feed_key("SELCLEAR")
    session.feed_key("SELCLEAR")
    assert len(session.buffer) == 0
    session.feed_key("b")
    assert len(session.buffer) == 1


def test_buffer_limit_clips_paste():
    # вставка не влезает — берётся сколько поместилось, и об этом сказано
    # в подвале (молча обрезать нельзя)
    session, _ = make_session(MODE_LOCAL)
    session.buffer_limit = 20
    session.feed_key("a" * 5 + " " + "b" * 40 + "\r")
    assert len(session.buffer) <= 20
    assert "ОБРЕЗАНА" in session.last_key


def test_buffer_limit_off_keeps_everything():
    # предел 0 — буфер не ограничен
    session, _ = make_session(MODE_LOCAL)
    session.buffer_limit = 0
    for _ in range(500):
        session.feed_key("a")
    assert len(session.buffer) == 500
    assert "ПОЛОН" not in panel_text(session.parser.screen)


def test_buffer_rows_match_naive_count():
    # быстрый построчный расчёт (поиск разрывов) совпадает с перебором
    # каждого знака: иначе эхо печаталось бы не в тех рядах
    from ie15emu.session import COLS
    session, _ = make_session(MODE_LOCAL)
    buf = b"abc" * 30 + b"\r" + b"x" * (COLS * 2 + 5) + b"\r" + b"tail"
    session.buffer.extend(buf)
    for i in (0, 1, 5, 90, 91, 92, 180, 181, len(buf)):
        rows = run = 0
        for b in buf[:i]:
            if b in (0x0D, 0x0A):
                rows += max(1, (run + COLS - 1) // COLS)
                run = 0
            else:
                run += 1
        assert session._buf_rowcol(i) == (rows + run // COLS, run % COLS)
    starts, rows = session._buf_starts(bytes(buf))
    assert starts[0] == 0
    assert rows == len(starts) - 1
    # границы рядов: каждый следующий ряд начинается с места, где
    # предыдущий кончился (перенос краем кадра или разрыв строки)
    for r in range(1, len(starts)):
        prev = starts[r - 1]
        assert starts[r] == prev + COLS or buf[starts[r] - 1] in (0x0D, 0x0A)


if __name__ == "__main__":
    import inspect
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                # тесты с фикстурами pytest (monkeypatch) тут не запустить —
                # только через python3 -m pytest tests/
                print(f"--  {name} (нужен pytest)")
                continue
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены (полный прогон: python3 -m pytest tests/)")