"""Запуск: python3 -m pytest tests/  или  python3 tests/test_session.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    # КОИ7 Н1: 0x60..0x7E — русские строчные, «hello» показывается ими
    assert decode_koi7(b"hello") in session.parser.screen.text()   # эхо


def test_send_key_transmits_buffer_then_clears():
    session, link = make_session(MODE_LOCAL)
    for ch in "work":
        session.feed_key(ch)
    out = session.feed_key(KEY_SEND)
    assert out == b"work"
    session.emit(out)
    assert link.sent == [b"work"]
    assert session.pending() == b""


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
    assert link.sent == [b"mamy"]
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
    assert link.sent == [bytes([0x60 | 0x0A])]        # «j» = код Й в Н1


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
    session.handle_line_reply(b"Connected to DKS\r\n")
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
    # Служебная строка 25 — панель состояний управляющих клавиш с
    # расшифровкой + последняя нажатая клавиша с её кодом
    session, link = make_session(MODE_HOST)

    def svc() -> str:
        return "".join(session.parser.screen.service)

    for lab in ("С ЭВМ", "РЕЖИМ 2=VT52", "ЛИНИЯ=UTF-8", "АЛФ=Н1",
                "ВИДЕО=НОРМ", "ПЕРЕДАЧА=ПУСТО"):
        assert lab in svc(), lab
    assert len(svc().rstrip()) <= 80, "панель не влезает в 80 знаков"
    session.feed_key("KEY_END")                        # не в LOCAL — хост
    assert "END=ESCK" in svc()
    session.handle_line_reply(b"\x1bb")                # ESC b — инверсное
    assert "ВИДЕО=ИНВ" in svc()
    session.handle_line_reply(b"Connected to DKS\r\n")
    assert "ЛИНИЯ=ДКС" in svc()
    session.feed_key("MODE")                           # клавиша РЕЖИМ (F9)
    assert "АВТОНОМНО" in svc() and "РЕЖИМ" in svc()   # последняя клавиша
    for ch in "ЗАД":
        session.feed_key(ch)
    assert "ПЕРЕДАЧА=3" in svc()                       # буфер ждёт SEND
    session.emit(session.feed_key("SEND"))
    assert "ПЕРЕДАЧА=ПУСТО" in svc() and link.sent


def test_default_rezhim2_vt52():
    session, _ = make_session(MODE_HOST)
    assert session.parser.mode == 2
    assert "РЕЖИМ 2" in "".join(session.parser.screen.service)


def test_cmdset_key_toggles_rezhim():
    session, _ = make_session(MODE_HOST)
    session.feed_key("CMDSET")                 # F8 — клавиша «РЕЖИМ»
    assert session.parser.mode == 1
    assert "РЕЖИМ 1" in "".join(session.parser.screen.service)
    session.feed_key("CMDSET")
    assert session.parser.mode == 2


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")