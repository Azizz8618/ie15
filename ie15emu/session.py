"""Режимы работы терминала 15ИЭ-00-013 (аналог клавиши SEND).

  АВТОНОМНО (LOCAL) — набор идёт в локальный буфер и сразу на экран
                      терминала, обмена с линией нет;
  С ЭВМ (HOST)     — каждый знак немедленно уходит в линию
                      (токовая петля / SSH), эхо приходит от ЭВМ;
  SEND             — «передать накопленное»: байты буфера уходят в линию,
                      буфер очищается. Так же работает клавиша SEND
                      на реальной клавиатуре 15ВВВ-97-006.

Сеанс (`TerminalSession`) не знает о транспорте: он возвращает байты для
линии, а отправку делает вызывающая сторона через `emit()` / сам session
через `link` (если подключён).
"""
from __future__ import annotations

from .keyboard import key_to_bytes

MODE_LOCAL = "local"      # «АВТОНОМНО»
MODE_HOST = "host"        # «С ЭВМ»

SERVICE_LOCAL = "М1=ГОТОВНОСТЬ  М2=ВВОД  М3=КАНАЛ ИНТ  АВТОНОМНО"
SERVICE_HOST = "М1=ГОТОВНОСТЬ  М2=ВВОД  М3=КАНАЛ ОК  С ЭВМ"


class TerminalSession:
    """Состояние терминала: режим работы + накопленный буфер автономного набора."""

    def __init__(self, parser, link=None, mode: str = MODE_LOCAL) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        self.parser = parser
        self.link = link
        self.mode = mode
        self.buffer = bytearray()
        self._update_service()

    # --- режим ------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        self.mode = mode
        self._update_service()

    def toggle_mode(self) -> str:
        self.mode = MODE_HOST if self.mode == MODE_LOCAL else MODE_LOCAL
        self._update_service()
        return self.mode

    def _update_service(self) -> None:
        self.parser.screen.set_service(
            SERVICE_HOST if self.mode == MODE_HOST else SERVICE_LOCAL)

    # --- клавиатура -------------------------------------------------
    def feed_key(self, key: str) -> bytes | None:
        """Нажатие клавиши оператора.

        Возвращает байты, которые нужно немедленно передать в линию
        (в режиме С ЭВМ — сам знак, по SEND — весь накопленный буфер),
        либо None, если передавать нечего.
        """
        if key == "SEND":
            return self.send()
        if key == "MODE":
            self.toggle_mode()
            return None
        data = key_to_bytes(key)
        if not data:
            return None
        if self.mode == MODE_HOST:
            return data
        # АВТОНОМНО: накапливаем в буфере и печатаем на экране,
        # держим линию свободной (как на незаполненном PLAN-символ «Р»).
        self.buffer += data
        self.parser.feed(data)
        return None

    # --- передача буфера --------------------------------------------
    def send(self) -> bytes:
        """Клавиша SEND: выдать накопленное в линию и очистить буфер."""
        data = bytes(self.buffer)
        self.buffer.clear()
        return data

    def pending(self) -> bytes:
        """Ожидает ли буфер передачи (что на реальном терминале гасит РТ)."""
        return bytes(self.buffer)

    def emit(self, data: bytes | None) -> None:
        """Фактическая передача в линию (если она есть)."""
        if data and self.link is not None:
            self.link.send(data)

    def handle_line_reply(self, data: bytes) -> bytes | None:
        """"Приём из линии (ответ ЭВМ), возможный ответ терминала — ESC/ и т.п."""
        if not data:
            return None
        reply = self.parser.feed(data)
        return reply.encode("latin-1") if reply else None