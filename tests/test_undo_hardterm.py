"""Отмена правок набора (АВТОНОМНО) и работа на «жёстком» терминале.

Запуск: python3 tests/test_undo_hardterm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.hardterm import Output, is_hard
from ie15emu.parser import Parser
from ie15emu.session import MODE_HOST, MODE_LOCAL, TerminalSession
from ie15emu.undo import DEFAULT_UNDO, UndoStack

from test_session import make_session   # noqa: E402  (общий помощник)


# --- стек отмены ----------------------------------------------------

def test_undo_stack_depth_limit():
    # Глубина отмены ограничена: старые снимки вытесняются новыми
    stack = UndoStack(3)
    buf = bytearray()
    for ch in b"abcdefgh":
        stack.step()                 # каждый знак — свой шаг (граница слова)
        stack.snapshot(buf)
        buf += bytes([ch])
    assert len(stack) == 3
    for want in (b"abcdefg", b"abcdef", b"abcde"):
        assert stack.undo(buf) is True
        assert bytes(buf) == want
    assert stack.undo(buf) is False            # дальше отменять нечего
    assert bytes(buf) == b"abcde"


def test_undo_stack_coalesces_within_word():
    # Пока шаг не закрыт, снимок не кладётся: откат идёт к началу слова,
    # а не к прошлому знаку
    stack = UndoStack(20)
    buf = bytearray()
    stack.snapshot(buf)              # шаг открыт (первый снимок) — ложим
    buf += b"he"
    stack.snapshot(buf)              # шаг тот же — не кладём
    buf += b"llo"
    assert len(stack) == 1           # один шаг = одно слово
    stack.undo(buf)
    assert bytes(buf) == b""


def test_undo_stack_ignores_unchanged_buffer():
    # Снимок кладётся, только если буфер действительно изменился: иначе
    # двадцать нажатий «съели» бы всю глубину отмены
    stack = UndoStack(20)
    buf = bytearray(b"AB")
    for _ in range(10):
        stack.snapshot(buf)
    assert len(stack) == 1


def test_undo_stack_zero_depth_keeps_nothing():
    # Глубина 0 — отмена выключена: память не тратится, отменять нечего
    stack = UndoStack(0)
    buf = bytearray()
    stack.snapshot(buf)
    buf += b"A"
    assert len(stack) == 0
    assert stack.undo(buf) is False
    assert bytes(buf) == b"A"


def test_default_undo_depth_is_20():
    assert DEFAULT_UNDO == 20
    session, _ = make_session(MODE_LOCAL)
    assert session.undo.depth == 20


# --- отмена в сеансе ------------------------------------------------

def test_undo_returns_last_typed_word():
    # Ctrl+U (UNDO) убирает последнее набранное СЛОВО целиком — и из
    # буфера, и с экрана: на экране не остаётся «хвоста» отменённого
    session, link = make_session(MODE_LOCAL)
    sc = session.parser.screen
    for ch in "abc":
        session.feed_key(ch)
    assert bytes(session.buffer) == b"ABC"
    assert sc.text().strip() == "ABC"
    assert session.feed_key("UNDO") is None
    assert bytes(session.buffer) == b""
    assert sc.text().strip() == ""
    assert link.sent == []                 # в линию отмена не уходит


def test_undo_step_by_step():
    # Шаг откала — слово: каждый Ctrl+U снимает последнее слово
    # (разделители остаются: шаг закрывается ПОСЛЕ пробела)
    session, _ = make_session(MODE_LOCAL)
    for ch in "ab cd ef":
        session.feed_key(ch)
    for want in (b"AB CD ", b"AB ", b""):
        session.feed_key("UNDO")
        assert bytes(session.buffer) == want


def test_undo_boundary_on_enter_and_navigation():
    # ПР закрывает шаг (это часть текста); движение курсора буфер не
    # правит — шагом не является: набор до и после движения откатывается
    # одним шагом
    session, _ = make_session(MODE_LOCAL)
    for ch in "ab":
        session.feed_key(ch)
    session.feed_key("KEY_ENTER")
    for ch in "cd":
        session.feed_key(ch)
    session.feed_key("KEY_HOME")               # навигация — не шаг
    session.feed_key("x")
    assert bytes(session.buffer) == b"AB\rXCD"
    for want in (b"AB\r",                    # снялись «x» и «cd» вместе
                 b"AB",                      # потом ПР
                 b""):                       # и первое слово
        session.feed_key("UNDO")
        assert bytes(session.buffer) == want


def test_undo_when_nothing_to_undo():
    # Отменять нечего — буфер не трогаем, в подвале видно почему
    session, _ = make_session(MODE_LOCAL)
    assert session.feed_key("UNDO") is None
    assert bytes(session.buffer) == b""
    assert "пусто" in session.last_key


def test_undo_respects_configured_depth():
    # Глубина из конфига: три слова набраны, отмена держит только два шага
    session, _ = make_session(MODE_LOCAL)
    session.undo = UndoStack(2)
    for word in ("ab", "cd", "ef"):
        for ch in word:
            session.feed_key(ch)
        session.feed_key(" ")                   # разделитель — граница шага
    session.feed_key("UNDO")
    assert bytes(session.buffer) == b"AB CD "    # третье слово снято
    session.feed_key("UNDO")
    assert bytes(session.buffer) == b"AB "       # второе
    assert session.undo.available is False       # глубина исчерпана


def test_undo_disabled_by_zero_depth():
    # Глубина 0: Ctrl+U ничего не делает
    session, _ = make_session(MODE_LOCAL)
    session.undo = UndoStack(0)
    for ch in "abc":
        session.feed_key(ch)
    assert session.feed_key("UNDO") is None
    assert bytes(session.buffer) == b"ABC"


def test_undo_also_covers_taken_selection():
    # «ВЗЯТЬ» (F4) кладёт выделенный текст в буфер — это тоже правка,
    # она отменяется тем же Ctrl+U
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen
    sc.view_map = [("frame", 0)]
    sc.cells[0] = [0x41 + (i % 26) for i in range(80)]
    sc.attr[0] = [0] * 80
    session.feed_key("MOUSEDOWN@1,4")
    session.feed_key("MOUSEDRAG@6,4")
    session.feed_key("MOUSEUP@6,4")
    assert session.parser.screen.sel_text() == "ABCDEF"
    session.feed_key("SELTAKE")
    assert session.buffer, "выделенное должно попасть в буфер"
    taken = bytes(session.buffer)
    session.feed_key("UNDO")
    assert bytes(session.buffer) == b"", "взятое выделение отменяется"


def test_send_clears_undo_history():
    # ПЕРЕДАЧА отдаёт буфер линии: отменять уже нечего, следующий набор
    # начинается с чистого листа
    session, link = make_session(MODE_LOCAL)
    for ch in "abc":
        session.feed_key(ch)
    assert len(session.undo) == 1       # одно слово — один шаг отката
    assert session.feed_key("SEND") == b"ABC\r\n"
    assert bytes(session.buffer) == b""
    assert len(session.undo) == 0
    assert session.feed_key("UNDO") is None
    assert bytes(session.buffer) == b""


def test_undo_key_does_nothing_in_host_mode():
    # В режиме С ЭВМ своего буфера нет — отменять нечего, и в линию
    # ничего не уходит
    session, link = make_session(MODE_HOST)
    assert session.feed_key("UNDO") is None
    assert link.sent == []


def test_ctrl_u_arrives_as_undo():
    # Ctrl+U с клавиатуры приходит байтом 0x15 и разбирается как ОТМЕНА
    from ie15emu.keyboard import decode_key_bytes
    assert decode_key_bytes(b"\x15") == ["UNDO"]
    session, _ = make_session(MODE_LOCAL)
    for ch in "abc":
        session.feed_key(ch)
    for key in decode_key_bytes(b"\x15"):
        session.feed_key(key)
    assert bytes(session.buffer) == b""    # слово снимается целиком


def test_undo_counter_shown_in_status_line():
    # В строке состояния видно, что отмена есть и на сколько шагов
    import re
    session, _ = make_session(MODE_LOCAL)
    sc = session.parser.screen

    def status():
        return re.sub(r"\x1b\[[0-9;]*m", "", sc.status_line())

    assert "ОТМЕНА" not in status()        # пустой буфер — не мигаем
    for ch in "abc":
        session.feed_key(ch)
    assert "ОТМЕНА=Ctrl+U (1)" in status() # слово = один шаг
    session.feed_key("UNDO")
    assert "ОТМЕНА" not in status()        # шагов не осталось


def test_undo_with_echo_off_leaves_screen_alone():
    # При выключенном эхом на экране нет набора — отмена не должна
    # трогать вывод ЭВМ
    session, _ = make_session(MODE_LOCAL)
    session.handle_line_reply(b"STROKA\r\n")
    session.feed_key("ECHO")                     # F6 — эхо выключено
    before = session.parser.screen.text()
    for ch in "abc":
        session.feed_key(ch)
    session.feed_key("UNDO")
    assert session.parser.screen.text() == before


# --- «жёсткий» терминал --------------------------------------------

def test_output_detects_hard_terminal():
    assert is_hard("ascii") is True
    assert is_hard("koi8-r") is True
    assert is_hard("cp866") is True
    assert is_hard("utf-8") is False


def test_output_replaces_cyrillic_on_ascii_terminal():
    # На ASCII-терминале кириллица не помещается — показываем латиницей,
    # чтобы сеанс не падал на первом же русском слове
    out = Output(encoding="ascii")
    assert out.ascii is True
    assert out.adapt("ЭБМ") == "EBM"
    assert out.adapt("ПРОВЕРЬ ЛИНИЮ!") == "PROVER' LINIYu!"
    out.adapt("ЭБМ").encode("ascii")          # результат кодируется


def test_output_keeps_cyrillic_when_encoding_allows():
    # KOI8-R — родная «русская» кодировка: кириллицу берёт, и даже
    # псевдографику имеет (это её родные линии). Подменяется только то,
    # чего в ней нет: «№» и стрелки.
    out = Output(encoding="koi8-r")
    assert out.ascii is True               # но не всё влезло — подстраховка
    assert out.adapt("ЭБМ") == "ЭБМ"
    assert out.adapt("─") == "─"            # псевдографика остаётся
    assert out.adapt("↑") == "^"            # стрелки KOI8-R не берёт
    out.adapt("ЭБМ─↑№█").encode("koi8-r")   # результат кодируется


def test_output_replaces_box_drawing_and_arrows():
    # На ASCII-терминале нет ни псевдографики, ни стрелок: рисуем ASCII
    out = Output(encoding="ascii")
    assert out.adapt("─│┌┐") == "-|++"
    assert out.adapt("█") == "#"
    assert out.adapt("№") == "N"
    assert out.adapt("↑") == "^" and out.adapt("↓") == "v"
    assert out.adapt("→") == ">" and out.adapt("←") == "<"
    out.adapt("─│█№↑↓→←").encode("ascii")


def test_output_passes_utf8_terminal_through():
    # Обычный терминал ничего не трогаем
    out = Output(encoding="utf-8")
    assert out.ascii is False
    text = "ЭБМ ─█ ↑↓ СЕТЬ=АВТОНОМНО"
    assert out.adapt(text) == text


def test_output_keeps_ansi_sequences_intact():
    # Управляющие последовательности (позиционирование, цвет) — всегда
    # ASCII: подменять их нельзя, иначе поедет весь кадр
    out = Output(encoding="ascii")
    seq = "\x1b[5;10H\x1b[7m█\x1b[27m"
    got = out.adapt(seq)
    assert got == "\x1b[5;10H\x1b[7m#\x1b[27m"
    assert got.startswith("\x1b[5;10H")


def test_output_caches_replacements():
    # Решение кэшируется по знаку — иначе каждый ка帧 кодировал бы заново
    out = Output(encoding="ascii")
    for _ in range(50):
        assert out.adapt("А") == "A"
    assert len(out._sub) <= 1


def test_whole_frame_encodes_on_ascii_terminal():
    # Главная проверка: весь кадр целиком печатается в жёсткий терминал
    import ie15emu.__main__ as m
    import os as _os
    old = m.os.get_terminal_size
    m.os.get_terminal_size = lambda: _os.terminal_size((100, 30))
    try:
        session, _ = make_session(MODE_LOCAL)
        session.handle_line_reply(bytes([0x80 | 1]) +
                                  "тест ЭВМ".encode() + b"\r\n")
        frame = m.text_screen(session.parser)
        for enc in ("ascii", "koi8-r", "cp866", "latin-1"):
            out = Output(encoding=enc)
            out.adapt(frame).encode(enc)       # не должно бросать
    finally:
        m.os.get_terminal_size = old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")