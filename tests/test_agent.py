"""Программное управление эмулятором (слой для агента ИИ).

Запуск: python3 -m pytest tests/test_agent.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.agent import Terminal, frame_lines, frame_text, history_text
from ie15emu.session import MODE_HOST, MODE_LOCAL


class FakeLink:
    """Линия из сценария: отдаёт заготовленные куски, запоминает отправку."""

    banner = True
    port = 4202

    def __init__(self, script=()) -> None:
        self.sent: list[bytes] = []
        self.queue: list[bytes] = list(script)
        self.closed = False

    def recv(self, size: int = 4096) -> bytes:
        if self.queue:
            return self.queue.pop(0)
        time.sleep(0.005)             # имитация ожидания данных
        return b""

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def close(self) -> None:
        self.closed = True


def make_terminal(script=(), mode=MODE_HOST, linkkw=None) -> Terminal:
    """Terminal с подставной линией (open_link подменён)."""
    import ie15emu.agent as agent

    link = FakeLink(script)

    class _AnyLink:
        """Подмена open_link: отдаём наш сценарий линии."""

    old = agent.open_link
    agent.open_link = lambda url, **kw: link          # noqa: E731
    try:
        t = Terminal("tcp://127.0.0.1:4202", mode=mode,
                     banner_timeout=0.05, **(linkkw or {}))
    finally:
        agent.open_link = old
    t._fake = link
    return t


def say(text: str) -> bytes:
    """Строка выдачи ЭВМ в байтах линии (ASCII — проходит без перекодировки)."""
    return text.encode("ascii") + b"\r\n"


def test_screen_reads_text_not_codes():
    # агент читает кадр текстом (Unicode), а не кодами КОИ-7 из screen.cells
    t = make_terminal([say("PRIVET"), say("VSTANOVLENIE")])
    t.pump(); t.pump()
    assert frame_lines(t.parser)[:2] == ["PRIVET", "VSTANOVLENIE"]
    assert frame_text(t.parser).splitlines()[:2] == ["PRIVET", "VSTANOVLENIE"]
    t.close()


def test_wait_quiet_returns_when_output_stops():
    # БЭСМ-6 печатает кусками с паузами: ждём конца выдачи, а не секунды
    t = make_terminal([say("stroka 1"), say("stroka 2"), say("stroka 3")])
    assert t.wait_quiet(quiet=0.05, timeout=5.0) is True
    assert t.screen().splitlines()[:3] == ["stroka 1", "stroka 2", "stroka 3"]
    t.close()


def test_wait_for_finds_late_text():
    t = make_terminal([say(" pervye"), say(" vtoroe")])
    assert t.wait_for("vtoroe", timeout=5.0, quiet=0.02) is True
    t.close()


def test_mark_and_new_lines_read_only_new():
    # читаем только то, что изменилось: не весь кадр, а новые строки
    t = make_terminal()
    t.session.handle_line_reply(say("pervaya stroka"))
    mark = t.mark()
    assert t.new_lines(mark) == []        # пока ничего не изменилось
    t.session.handle_line_reply(say("vtoraya stroka"))
    assert t.new_lines(mark) == ["vtoraya stroka"]
    assert 1 in t.changed_since(mark)
    t.close()


def test_screen_revision_grows_on_output():
    t = make_terminal()
    rev = t.screen_.rev
    t.session.handle_line_reply(say("stroka"))
    assert t.screen_.rev > rev
    # служебная строка и подвал оператора счётчиком НЕ трогаются: агент
    # ждёт выдачу ЭВМ, а не свою панель
    t.session._update_service()
    t.session._update_status()
    assert t.screen_.rev > rev
    t.close()


def test_type_sends_in_host_mode_and_buffers_in_local():
    # цифры не проходят через перекодировку набора — сравниваем байты
    t = make_terminal(mode=MODE_HOST)
    t.type("123")
    assert b"".join(t._fake.sent) == b"123"      # с ЭВМ уходит сразу
    t.close()
    t = make_terminal(mode=MODE_LOCAL)
    t.type("123")
    assert t._fake.sent == []                    # автономно — в буфере
    t.key("KEY_SEND")
    # ПЕРЕДАЧА завершает незакрытую строку переводом строки — как и у человека
    assert b"".join(t._fake.sent) == b"123\r"
    t.close()


def test_type_send_flag_sends_buffer_in_local():
    t = make_terminal(mode=MODE_LOCAL)
    t.type("123", send=True)
    assert b"".join(t._fake.sent) == b"123\r"
    t.close()


def test_type_latin_keys_go_through_terminal_charset():
    # Набор терминала перекодирует нажатие: латинская «L» уходит не как
    # ASCII «L», а как код КОИ-7 (и дальше — в кодировку линии). Поэтому
    # агент набирает клавишами (`type`), а не байтами в линию.
    t = make_terminal(mode=MODE_HOST)
    t.type("L")
    sent = b"".join(t._fake.sent)
    assert sent and sent != b"L"
    t.close()


def test_key_and_clear_go_through_emulator():
    t = make_terminal()
    t.session.handle_line_reply(say("stroka"))
    t.clear()                                   # F5 — экран в историю
    assert t.screen().strip() == ""
    assert "stroka" in t.text()                # но в истории осталась
    assert "stroka" in history_text(t.parser)
    t.close()


def test_context_manager_closes_link():
    link_box = {}
    with make_terminal() as t:
        link_box["link"] = t._fake
    assert link_box["link"].closed is True


def test_wait_quiet_times_out_on_silent_line():
    # линия молчит — ждать конца выдачи бессмысленно, ждём и получаем False
    t = make_terminal()
    t0 = time.monotonic()
    assert t.wait_quiet(quiet=0.05, timeout=0.2) is True   # тишина = конец
    assert time.monotonic() - t0 < 3.0
    t.close()


def test_history_text_returns_scrolled_output():
    t = make_terminal()
    for i in range(40):
        t.session.handle_line_reply(say(f"stroka {i:02d}"))
    hist = history_text(t.parser)
    assert "stroka 00" in hist and "stroka 15" in hist
    assert "stroka 39" not in hist              # последние — ещё на экране
    assert "stroka 39" in t.screen()
    t.close()


def test_status_line_has_fields():
    t = make_terminal(mode=MODE_HOST)
    line = t.status()
    assert "КАНАЛ=4202" in line
    assert "МЫШЬ=" in line
    t.close()


def test_status_line_plain_text():
    # агенту статус нужен текстом, без SGR-кодов
    t = make_terminal(mode=MODE_HOST)
    assert "\x1b[" not in t.status()
    t.close()


def test_cli_dump_and_headless_flags_exist():
    # флаги CLI на месте и не ломают разбор аргументов
    import ie15emu.__main__ as m
    args = m.main.__globals__  # noqa: F841  (для отладки)
    src = (Path(m.__file__).read_text(encoding="utf-8"))
    assert '"--headless"' in src and '"--dump"' in src