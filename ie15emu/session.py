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

from . import COLS
from .charset import (koi7_display_upper, koi7_raw_upper,
                      normalize_incremental, to_line)
from .keyboard import DEFAULT_LAYOUT, key_to_bytes

MODE_LOCAL = "local"      # «АВТОНОМНО»
MODE_HOST = "host"        # «С ЭВМ»

# Подписи установленных управляющих разрядов (служебная строка 25);
# единый пробел — вся строка должна умещаться в 80 знаков
LINE_LABELS = {"utf8": "ЛИНИЯ=UTF-8", "raw": "ЛИНИЯ=RAW", "dks": "ЛИНИЯ=ДКС"}

# Индикация последней нажатой управляющей клавиши: клавиша=отданный код
CONTROL_HINTS = {
    "KEY_ENTER": "ENTER=0D0A", "KEY_BACKSPACE": "BKSP=08", "\x7f": "BKSP=08",
    "\t": "TAB=09", "\x07": "BEL=07", "KEY_ESC": "ESC=1B", "\x1b": "ESC=1B",
    "KEY_UP": "UP=ESCA", "KEY_DOWN": "DOWN=ESCB", "KEY_RIGHT": "RGHT=ESCC",
    "KEY_LEFT": "LEFT=ESCD", "KEY_HOME": "HOME=ESCH", "KEY_END": "END=ESCK",
    "KEY_PAGEUP": "PGUP=ESCJ", "KEY_PAGEDOWN": "PGDN=ESCE",
    "KEY_INSERT": "INS=ESCb", "KEY_DELETE": "DEL=ESCc",
    "SEND": "SEND", "MODE": "РЕЖИМ", "CMDSET": "НАБОР",
}


LAYOUT_LABELS = {"phonetic": "ФОН", "positional": "ПОЗ"}


class TerminalSession:
    """Состояние терминала: режим работы + накопленный буфер автономного набора."""

    def __init__(self, parser, link=None, mode: str = MODE_LOCAL,
                 koi7: bool = False, encoding: str = "utf8",
                 layout: str | None = None) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        self.parser = parser
        self.link = link
        self.mode = mode
        # layout — выбранная русская раскладка; koi7 (старый флаг) включает
        # раскладку по умолчанию (позиционную).
        if layout is None and koi7:
            layout = DEFAULT_LAYOUT
        self.layout = layout
        self.koi7 = layout is not None
        self.buffer = bytearray()
        self.encoding = encoding     # «utf8» | «raw» | «dks» — передача
        self.rx_encoding = encoding  # приём (у ДКС-линии — raw без НУСов)
        self.enc_decided = False     # баннер «Encoding is …» обработан
        self.last_key = ""           # последняя управляющая клавиша (панель)
        self._carry = b""            # незавершённый хвост UTF-8 из линии
        self._sniff = b""            # окно поиска баннера
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
        # Служебная строка 25 — панель управляющих клавиш/сигналов:
        # состояние каждого с расшифровкой + справа последняя нажатая
        # клавиша и отданный ею код.
        sc = self.parser.screen
        parts = ["С ЭВМ" if self.mode == MODE_HOST else "АВТОНОМНО",
                 f"РЕЖИМ {self.parser.mode}=" + ("VT52" if self.parser.mode == 2 else "УП"),
                 LINE_LABELS.get(self.encoding, f"ЛИНИЯ={self.encoding}"),
                 "АЛФ=Н1"]
        if self.layout:
            parts.append("РАСК=" + LAYOUT_LABELS.get(self.layout, self.layout))
        parts += ["ВИДЕО=" + ("ИНВ" if sc.inverse else "НОРМ"),
                  "ПЕРЕДАЧА=" + (str(len(self.buffer)) if self.buffer else "ПУСТО")]
        left = " ".join(parts)
        right = self.last_key or ""
        gap = COLS - len(left) - len(right)
        text = left + " " * max(gap, 1) + right if gap > 1 and right else left
        sc.set_service(text)

    # --- клавиатура -------------------------------------------------
    def feed_key(self, key: str) -> bytes | None:
        """Нажатие клавиши оператора.

        Возвращает байты, которые нужно немедленно передать в линию
        (в режиме С ЭВМ — сам знак, по SEND — весь накопленный буфер),
        либо None, если передавать нечего.
        """
        hint = CONTROL_HINTS.get(key)
        if hint:
            self.last_key = hint
            self._update_service()
        if key == "SEND":
            return self.send()
        if key == "MODE":
            self.toggle_mode()
            return None
        if key == "CMDSET":
            self.parser.mode = 1 if self.parser.mode == 2 else 2
            self._update_service()
            return None
        data = key_to_bytes(key, layout=self.layout)
        if not data:
            return None
        if self.mode == MODE_HOST:
            return data
        # АВТОНОМНО: накапливаем в буфере и печатаем на экране,
        # держим линию свободной (как на незаполненном PLAN-символ «Р»).
        # Эхо — заглавными (Н1 не различает регистр, так же отвечает ЭВМ);
        # в буфере код остаётся как дана раскладка.
        self.buffer += data
        self.parser.feed(koi7_display_upper(data))
        self._update_service()
        return None

    # --- передача буфера --------------------------------------------
    def send(self) -> bytes:
        """Клавиша SEND: выдать накопленное в линию и очистить буфер."""
        data = bytes(self.buffer)
        self.buffer.clear()
        self._update_service()         # «ПЕРЕДАЧА=ПУСТО»
        return data

    def pending(self) -> bytes:
        """Ожидает ли буфер передачи (что на реальном терминале гасит РТ)."""
        return bytes(self.buffer)

    def emit(self, data: bytes | None) -> None:
        """Фактическая передача в линию: внутренний КОИ7-поток → байты линии."""
        if data and self.link is not None:
            self.link.send(to_line(bytes(data), self.encoding))

    def _detect_encoding(self, data: bytes) -> None:
        """Кодировки приёма/передачи по баннеру линии SIMH.

        «Encoding is …» задаёт оба направления; для ДКС-линии (Э-60)
        приём остаётся raw-КОИ7, но окончание строки — ПР, а не ETX.
        Смена типа линии обязывает обновить служебную строку.
        """
        self._sniff = (self._sniff + data)[-200:]
        was = self.encoding
        if b"Connected to DKS" in self._sniff:
            # Э-60 регистрируется ОС не сразу — строка может прийти позже
            # любых serial-баннеров и имеет приоритет. Линия ДКС в raw-
            # кодировке: обе стороны — внутренние КОИ7 (`set ttyN raw`
            # в dispak.ini), окончание строки — ПР, а не ETX.
            self.encoding, self.rx_encoding = "dks", "raw"
            self.enc_decided = True
        elif self.enc_decided:
            return
        elif b"Encoding is UTF-8" in self._sniff:
            self.encoding = self.rx_encoding = "utf8"
            # decided не ставим: «Connected to DKS» способно опоздать
        elif (b"Encoding is RAW" in self._sniff
              or b"Encoding is KOI-7" in self._sniff):
            self.encoding = self.rx_encoding = "raw"
            self.enc_decided = True
        if self.encoding != was:
            self._update_service()   # табло разрядов следует за конфигурацией

    def handle_line_reply(self, data: bytes) -> bytes | None:
        """Приём из линии (ответ ЭВМ); возможный ответ терминала — ESC/ и т.п.

        Ответ сразу уходит в линию (например, идентификация по ESC Z), как
        это делал настоящий терминал, и возвращается для вызывающей стороны.
        """
        if not data:
            return None
        self._detect_encoding(data)
        # raw-приём читается инкрементально: tmxr-баннер («Connected to
        # the БЭСМ-6…») приходит в UTF-8 даже на raw/dks-линии
        stream, self._carry = normalize_incremental(data, self._carry)
        if self.rx_encoding == "raw":
            stream = koi7_raw_upper(stream)
        reply = self.parser.feed(stream)
        self._update_service()         # ИНВ/ЗВН могли измениться разбором
        if not reply:
            return None
        wire = to_line(reply.encode("latin-1"), self.encoding)
        if self.link is not None:
            self.link.send(wire)
        return wire