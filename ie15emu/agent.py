"""Эмулятор терминала 15ИЭ под управлением программы (агента ИИ).

ПЕРВОЕ ДЕЛО при неисправности или при необходимости выполнить приказ ОС —
прочитать справочники, а не гадать на живой машине:

* `re_dispak/re-dispak/docs/Dispak-operator-guide.md` — «Руководство оператора» ОС
  ДИСПАК: раздел 4.x — директивы (в т.ч. 4.5 управление устройствами
  вывода), раздел 5.x — сообщения ОС и действия оператора;
* `simh/BESM6/doc/ДКС.md` — протокол линии ДКС;
* `re_dispak/re-dispak/ai/` — справочники модулей ОС (СВЯЗЬ7, МОТТ,
  БОНБОТ, ДИСП70, ВЗУ, ТЕРМ);
* `re_dispak/re-dispak/docs/DISPAK_AGENT.md` (проект re_dispak/re-dispak) — как работать с машиной из кода.

Приказы ОС (`DIRECTIVES`) — только с операторского терминала; на этой
машине это tty1, подключённый к консоли SIMH (`dispak.ini: attach tty1
console`), то есть окно besm6 в tmux.

Человеку терминал нужен ради окна: цвета, подвал, мышь. Программе всё
это лишнее — ей нужно состояние машины, а оно и есть «экран», только уже
в виде текста: ЭВМ говорит картинкой 25×80 в КОИ-7, а эмулятор превращает
её в строки Unicode (`Screen.row_text`).

    from ie15emu.agent import Terminal

    with Terminal("tcp://127.0.0.1:4202-4223") as t:
        t.drain()                     # забрать всё, что машина уже выдала
        t.key("KEY_ENTER")            # ДКС: приглашение ОС (без НУС)
        t.wait_quiet()
        print(t.screen())             # видимый кадр — 25 строк текстом
        t.run_task("игра", t.agent_cipher(1))     # «ИГРА 412201» — свой шифр
        t.wait_for("ВАШ ХОД", timeout=120)

Четыре правила, без которых агент обычно и портит:

* читать `row_text()`, а не `screen.cells`: в кадре лежат КОИ-7-коды, а
  не буквы (и набор терминала Н0/Н1/Н2 к ним применит не тот, кто должен);
* ждать `wait_quiet()`/`wait_for()`, а не `sleep()`: БЭСМ-6 считает
  секундами, и без этого читается полузаписанный кадр;
* в режиме «АВТОНОМНО» набранное лежит в буфере и уходит только по
  ПЕРЕДАЧЕ (`type(..., send=True)` или `send()`), в «С ЭВМ» — сразу;
* окно не скроллится: на экране только хвост выдачи, полный лог — в
  `history` (`t.history_text()`), глубина — `[terminal] history`;
* сравнивать выдачу ЭВМ с образцом надо через `fold()`: набор Н2 показывает
  русские буквы машины латинскими гомоглифами («ВАШ ХОД» → «BAШ XОД»);
* свои задачи запускать на шифрах диапазона 4122xx (`agent_cipher`), чтобы
  в списке задач и журнале ОС было видно, где агент, а где оператор.
"""
from __future__ import annotations

import os
import queue
import threading
import time

from .link import LinkError, open_link
from .parser import Parser
from .session import DEFAULT_BUFFER, MODE_HOST, MODE_LOCAL, TerminalSession

# Латинские буквы, которыми набор Н2 показывает русские буквы ЭВМ: коды
# 0x40…0x5F у машины — это русский верхний регистр, а терминал в одно-регист-
# ном наборе выводит их латинскими гомоглифами («ВАШ ХОД» → «BAШ XОД»).
# Свернуть перед сравнением — иначе поиск по экрану ничего не находит.
HOMOGLYPH = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
})


def fold(text: str) -> str:
    """Латинские гомоглифы привести к русским (текст выдачи ЭВМ, Н2)."""
    return text.upper().translate(HOMOGLYPH)


# Шифры задач агента: свой диапазон 4122xx. Тогда в списке задач (приказ
# НОМ), в журнале ОС и на экране сразу видно, где работает агент, а где —
# оператор: «ИГРА 412203» и «КАЛАХ 412204» — наши, «ИГРА 4199» — человеческие.
AGENT_CIPHER_BASE = 412200


def agent_cipher(n: int = 1) -> str:
    """Шифр N-й задачи агента: 412201, 412202, …"""
    if n < 1:
        raise ValueError("номер задачи агента нумеруется с единицы")
    return f"{AGENT_CIPHER_BASE + n:06d}"


# Директивы (приказы) ОС — трёхбуквенные мнемоники из таблицы ДЕШ
# (bonbot.be:1835-1939). Это НЕ запуск задачи: задача запускается именем и
# шифром («ИГРА 412201»), а директива разбирается модулем ПРИКАЗ и проходит
# проверку прав: терминал должен быть в шкале операторских ШКОПТТ, иначе ОС
# отвечает «ВАМ НЕЛЬЗЯ» (bonbot.be:1252-1261). Обычная линия ДКС оператором
# не является — значит директивы с неё посылать бессмысленно.
# Маска прав обычного терминала (МРДИРО, bonbot.be:1975): биты — по
# позициям директив в таблице ДЕШ. Терминал из шкалы операторских (ШКОПТТ)
# может всё; обычная линия ДКС — только то, что в этой маске, и на
# остальное получает «ВАМ НЕЛЬЗЯ» (bonbot.be:1252-1261).
OS_RIGHTS_MASK = 0o4440106500060140

DIRECTIVES = frozenset({
    "вцп", "ввд", "прт", "адр", "сме", "вмг", "вмд", "вкн", "боб", "дай",
    "нет", "отл", "стк", "кпи", "сбв", "свм", "свд", "выд", "пов", "про",
    "выб", "ном", "скв", "зра", "рзд", "реж", "пор", "раз", "рзе", "пак",
    "дат", "сбо", "сбой", "клк", "уст", "нот", "пул", "под", "нус",
    "ква", "мин", "кто", "ост", "прио", "запр", "упр", "ктч", "най",
})


def is_directive(text: str) -> bool:
    """Похоже ли это на приказ ОС (а не на запуск задачи «ИГРА 412201»)."""
    head = fold(text.strip().split(" ", 1)[0].strip()).lower()
    return head in DIRECTIVES


def frame_lines(parser: Parser) -> list[str]:
    """Кадр ЭВМ текстом: строки, которые сейчас на экране машины.

    В кадре строки без ведущих пробелов убраны, чтобы номер строки в
    списке совпадал с номером на экране.
    """
    sc = parser.screen
    used = max(sc.last_used() + 1, sc.y + 1)
    return [(sc.row_text("frame", y) or "").rstrip() for y in range(used)]


def frame_text(parser: Parser) -> str:
    """Кадр ЭВМ одной строкой-разделителем (LF)."""
    return "\n".join(frame_lines(parser))


def history_text(parser: Parser, limit: int | None = None) -> str:
    """Выдача, сошедшая с экрана: строки истории (сверху — последние)."""
    sc = parser.screen
    rows = sc.history if limit is None else sc.history[-limit:]
    return "\n".join((sc.row_text("hist", i) or "").rstrip()
                     for i in range(len(sc.history) - len(rows),
                                    len(sc.history)))


class AcpuWatch:
    """Слежение за выдачей на АЦПУ по файлу принтера SIMH.

    ОС печатает сводки и отчёты на АЦПУ отдельным каналом: даже когда на
    терминале тихо, принтер может ещё печатать — и наоборот, буфер вывода
    переполняется именно там («ОСВОБОДИ БУФЕР ВЫВОДА», руководство
    оператора, 5.40). Поэтому агент смотрит и файл принтера: выдача
    идёт — ждём, файл перестал расти — можно читать терминал.

    Путь берётся из `IE15_ACPU` либо из конфига этой машины
    (`dispak.ini: attach prn0 …/ACPU/output.txt`).
    """

    DEFAULT = os.environ.get(
        "IE15_ACPU", "~/Yandex.Disk/simh/BESM6/ACPU/output.txt")

    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.expanduser(path or self.DEFAULT)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return -1                      # файла нет — принтер не подключён

    def mark(self) -> int:
        """Отметка «здесь»: размер файла на сейчас."""
        return self.size()

    def grew(self, mark: int) -> bool:
        """Принтер печатал после отметки?"""
        now = self.size()
        return now != mark

    def tail(self, size: int = 400) -> str:
        """Хвост выдачи текстом (КОИ-7, как печатает ОС)."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - size))
                return f.read().decode("koi8-r", "replace")
        except OSError:
            return ""

    def wait_output(self, quiet: float = 2.0, timeout: float = 60.0,
                    poll: float = 0.5) -> bool:
        """Ждать, пока принтер закончит печать (рост файла прекратился)."""
        deadline = time.monotonic() + timeout
        last, since = self.size(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(poll)
            now = self.size()
            if now != last:
                last, since = now, time.monotonic()
            elif now >= 0 and time.monotonic() - since >= quiet:
                return True
        return False


class Terminal:
    """Линия ЭВМ под управлением программы: набор — через эмулятор,
    чтение — текстом кадра, ожидание — по счётчику изменений экрана.

    Работает и без окна терминала (никакой отрисовки, панели и мыши):
    link читается вручную методом `pump()` — он же кормит разбор.
    """

    def __init__(self, url: str = "tcp://127.0.0.1:4202",
                 mode: str = MODE_HOST, layout: str | None = None,
                 keymap: dict[str, str] | None = None, machine: str = "",
                 show_help: bool = False, banner_timeout: float = 3.0,
                 read_async: bool = True, buffer_limit: int = DEFAULT_BUFFER,
                 **linkkw) -> None:
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f"неизвестный режим: {mode!r}")
        self.link = open_link(url, **linkkw)
        self.session = TerminalSession(Parser(), link=self.link, mode=mode,
                                       layout=layout, keymap=keymap,
                                       buffer_limit=buffer_limit)
        self.session.parser.screen.set_help(show_help)
        if machine:
            self.session.machine = str(machine)
            self.session._update_status()
        self.parser = self.session.parser
        self.screen_ = self.parser.screen
        self.link_error: Exception | None = None
        self._operator = False   # операторский ли терминал (ШКОПТТ)
        self._events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        # Читать линию надо ВСЕГДА, а не «между командами»: на каждый
        # терминал у машины свой буфер вывода (СЕРП, поле М14), и если
        # терминал его не забирает, буфер переполняется — ОС начинает
        # бесконечно повторять «ПЕРЕП. БУФЕРА ВЫВОДА», и нормальная работа
        # становится невозможной. Поэтому линия читается фоновым потоком,
        # и разбор кадра идёт в нём же (иначе гонка за экраном).
        if read_async:
            self._reader = threading.Thread(target=self._read_loop,
                                            name="ie15-line", daemon=True)
            self._reader.start()
        if url.startswith(("tcp://", "ssh://")):
            self._drain_banner(banner_timeout)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            if self.session.hold:
                # удержание экрана (Ctrl+L): линию не читаем — выдача
                # копится в буфере сокета, машина упирается в него
                time.sleep(0.05)
                continue
            try:
                data = self.link.recv()
            except (OSError, LinkError) as e:
                self.link_error = e
                self._events.put(("error", e))
                return
            if data:
                self.session.handle_line_reply(data)
                self._events.put(("data", len(data)))
            else:
                time.sleep(0.005)      # сокет отдал пусто (таймаут 0.2 с)

    def _check_link(self) -> None:
        """Линия оборвалась — сообщить об этом вызывающему."""
        if self.link_error is not None:
            raise LinkError(str(self.link_error))
        # обрыв «молча» (связь пропала, а данных нет) ловит проверка
        # состояния линии: раз в check секунд спрашиваем у линии (моста)
        if self.session.link_check_tick() and self.session.line_lost:
            raise LinkError(self.session.last_key or "линия потеряна")

    # --- чтение линии -------------------------------------------------
    def _drain_banner(self, timeout: float) -> None:
        """Вступление линии (баннер «Encoding is …») — до первого набора."""
        deadline = time.monotonic() + timeout
        while not self.session.enc_decided and time.monotonic() < deadline:
            try:
                data = self.link.recv()
            except (OSError, LinkError):
                return
            if data:
                self.session.handle_line_reply(data)
            else:
                time.sleep(0.02)

    def pump(self, timeout: float = 0.1) -> bool:
        """Дождаться данных линии (саму линию читает фоновый поток).

        True — пришли байты ответа. Линия оборвалась — LinkError.
        """
        self._check_link()
        if self._reader is None:                 # линия читается вручную
            if self.session.hold:                # удержание: не читаем
                return False
            try:
                data = self.link.recv()
            except (OSError, LinkError):
                return False
            if not data:
                return False
            self.session.handle_line_reply(data)
            return True
        try:
            kind, val = self._events.get(timeout=timeout)
        except queue.Empty:
            return False
        if kind == "error":
            raise LinkError(str(val))
        return True

    def acpu(self, path: str | None = None) -> AcpuWatch:
        """Слежение за выдачей на АЦПУ (файл принтера)."""
        return AcpuWatch(path)

    def health(self, acpu: AcpuWatch | None = None) -> dict:
        """Срез состояния машины для агента и для разбора неисправностей.

        Три источника, которые надо смотреть вместе: терминал агента,
        терминал оператора (приказы ОС уходят туда) и АЦПУ — на принтере
        своя очередь вывода, и переполнение буфера вывода видно там, а не
        на экране.
        """
        w = acpu or AcpuWatch()
        return {
            "строка": self.status(),
            "экран": self.screen(),
            "ревизия": self.screen_.rev,
            "ацпу_файл": w.path,
            "ацпу_байт": w.size(),
            "ацпу_хвост": w.tail(240),
            "линия_жива": self.link_error is None,
        }

    def drain(self, quiet: float = 0.4, timeout: float = 10.0) -> bool:
        """Забрать всё, что машина уже выдала, и дождаться тишины.

        Именно это убирает «ПЕРЕП. БУФЕРА ВЫВОДА»: буфер вывода терминала
        должен быть пуст, прежде чем набирать следующую команду.
        """
        return self.wait_quiet(quiet=quiet, timeout=timeout)

    def wait_quiet(self, quiet: float = 0.5, timeout: float = 60.0,
                    acpu: AcpuWatch | None = None) -> bool:
        """Ждать, пока выдача кончится (счётчик изменений экрана перестал
        расти `quiet` секунд). False — не кончилась за `timeout`.

        С переданным `acpu` ждём и принтер: у АЦПУ своя очередь вывода,
        и «на экране тихо» ещё не значит «машина всё выдала» — именно на
        принтере всплывает переполнение буфера вывода.
        """
        deadline = time.monotonic() + timeout
        last, since = self.screen_.rev, time.monotonic()
        pmark = acpu.mark() if acpu is not None else None
        while time.monotonic() < deadline:
            if self.pump(timeout=0.05):
                if self.screen_.rev != last:
                    last, since = self.screen_.rev, time.monotonic()
                if acpu is not None:
                    pmark = acpu.mark()
                continue
            now = time.monotonic()
            if acpu is not None and acpu.grew(pmark):
                pmark = acpu.mark()
                since = now            # принтер ещё печатает
                continue
            if now - since >= quiet:
                return True
            time.sleep(0.02)
        return False

    def wait_for(self, needle: str, timeout: float = 60.0,
                 quiet: float = 0.5) -> bool:
        """Ждать появления `needle` в кадре или в истории. False — не
        дождались (текст мог уйти с экрана в историю раньше, чем мы
        прочитали, поэтому ищем и там)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.text() or needle in history_text(self.parser):
                return True
            self.pump(timeout=0.05)
        return needle in self.text() or needle in history_text(self.parser)

    # --- чтение экрана ------------------------------------------------
    def mark(self) -> int:
        """Отметка «здесь» для `changed_since()`/`new_lines()`."""
        return self.screen_.rev

    def changed_since(self, rev: int) -> list[int]:
        """Номера рядов кадра, изменившихся после отметки.

        ВНИМАНИЕ: после прокрутки кадра (строка ушла в историю) изменились
        ВСЕ ряды — так и есть, содержимое сдвинулось. Для «что нового
        пришло» пользуйтесь `new_lines()`.
        """
        return self.screen_.changed_since(rev)

    def new_lines(self, rev: int) -> list[str]:
        """Строки, изменившиеся в кадре после отметки, — по номерам рядов
        (без пустых), чтобы не читать весь экран целиком."""
        out = []
        for y in self.screen_.changed_since(rev):
            t = (self.screen_.row_text("frame", y) or "").rstrip()
            if t.strip():
                out.append(t)
        return out

    def lines(self) -> list[str]:
        """Кадр ЭВМ текстом (что сейчас на экране машины)."""
        return frame_lines(self.parser)

    def screen(self) -> str:
        """Кадр ЭВМ одной строкой-разделителем (LF)."""
        return frame_text(self.parser)

    def text(self) -> str:
        """Кадр + история: весь текст, который терминал видел с начала."""
        hist = history_text(self.parser)
        return (hist + "\n" + self.screen()) if hist else self.screen()

    def status(self) -> str:
        """Строка состояния линии текстом (без цвета): тип линии, ЭВМ,
        канал, режим, мышь, размер выделения."""
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", self.screen_.status_line()).strip()

    # --- набор --------------------------------------------------------
    # --- задачи агента -------------------------------------------------
    def agent_cipher(self, n: int = 1) -> str:
        """Шифр N-й задачи агента (свой диапазон 4122xx — отличить от
        человеческих)."""
        return agent_cipher(n)

    def operator(self) -> bool:
        """Операторский ли терминал (шкала ШКОПТТ).

        Обычная линия ДКС оператором не является: приказ проходит проверку
        прав и получает отказ «ВАМ НЕЛЬЗЯ» (bonbot.be:1252-1261). Ставится
        оператором вручную: с операторского терминала — приказ НУС <N> 1
        («ТИП ТЕРМ: 1 → ЭЛЕКТРОНИКА»), либо ПУЛ.
        """
        return self._operator

    def set_operator(self, value: bool = True) -> None:
        """Отметить терминал как операторский (после НУС от оператора)."""
        self._operator = bool(value)

    def probe_operator(self, directive: str = "сбой",
                       timeout: float = 20.0) -> bool:
        """Проверить права терминала пробным приказом.

        Пробуем приказ, которого нет в маске МРДИРО («сбой» — сбой
        устройства): ОС на запрещённый отвечает «ВАМ НЕЛЬЗЯ», на обычные
        приказы (например «вцп») может ответить и без прав — поэтому
        пробовать надо именно тем, что недоступно.
        Возвращает и запоминает результат.
        """
        if is_directive(directive):
            self.type(directive)
            self.key("KEY_ENTER")
            self.wait_quiet(quiet=2.0, timeout=timeout)
            tail = " / ".join([x for x in self.lines() if x.strip()][-2:])
            self._operator = "ВАМ НЕЛЬЗЯ" not in fold(tail)
        return self._operator

    def run_task(self, name: str, cipher: str = "", args: str = "",
                 timeout: float = 90.0, quiet: float = 3.0,
                 tries: int = 2) -> str:
        """Запустить задачу ОС: «<имя> <шифр> [аргументы]».

        Шифр по умолчанию — свой, из диапазона агента (4122xx), чтобы
        задача была видна как агентская. Возвращает хвост экрана с
        ответом ОС («Ф…» — код, «ОШИБ» — приказ не распознан; первый ввод
        после входа машина иногда «съедает», тогда команда повторяется).

        Это запуск задачи, а не приказ ОС: приказы (вцп, ном, сбой…) живут
        отдельно и требуют операторского терминала.
        """
        cmd = f"{name} {cipher or agent_cipher()}".strip()
        if args:
            cmd += " " + args
        if is_directive(cmd):
            raise ValueError(f"«{cmd}» — приказ ОС, а не запуск задачи; "
                             f"приказы доступны только с операторского "
                             f"терминала (см. Terminal.probe_operator)")
        tail = ""
        for _ in range(max(1, tries)):
            before = self.screen_.rev
            self.type(cmd)
            self.key("KEY_ENTER")
            self.wait_quiet(quiet=quiet, timeout=timeout)
            lines = [x for x in self.lines() if x.strip()]
            tail = " / ".join(lines[-3:])
            if self.screen_.rev != before and "ОШИБ" not in fold(tail):
                break
        return tail

    def key(self, name: str) -> None:
        """Нажать клавишу по имени: `KEY_ENTER`, `KEY_PAGEUP`, `KEY_UP`…

        Служебные клавиши терминала понимаются и как `SEND`/`CLEAR`, и как
        `KEY_SEND`/`F10` — как у человека на клавиатуре.
        """
        from .keyboard import SPECIAL_KEYS
        self.session.emit(self.session.feed_key(SPECIAL_KEYS.get(name, name)))

    def type(self, text: str, send: bool = False) -> None:
        """Набрать текст клавишами (раскладка и набор — как у человека).

        send=True — в «АВТОНОМНО» сразу отдать буфер (клавиша ПЕРЕДАЧА);
        в «С ЭВМ» текст уходит в линию сам, лишний SEND не нужен.
        """
        for ch in text:
            self.key(ch)
        if send and self.session.mode == MODE_LOCAL:
            self.send()

    def send(self) -> None:
        """Отдать накопленный буфер (клавиша ПЕРЕДАЧА, F10)."""
        self.key("KEY_SEND")

    def clear(self) -> None:
        """Очистить экран (клавиша ОЧИСТКА, F5): кадр в историю."""
        self.key("CLEAR")

    # --- режим и завершение -------------------------------------------
    @property
    def mode(self) -> str:
        return self.session.mode

    def set_mode(self, mode: str) -> None:
        self.session.set_mode(mode)

    def close(self) -> None:
        """Добрать хвост выдачи и закрыть линию.

        Обрыв с недобранным выводом оставляет у машины полный буфер: ОС
        потом долго повторяет «ПЕРЕП. БУФЕРА ВЫВОДА» на этой линии.
        """
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
            try:                      # добрать хвост, пока линия ещё открыта
                deadline = time.monotonic() + 0.5
                misses = 0
                while time.monotonic() < deadline and misses < 2:
                    try:
                        data = self.link.recv()
                    except (OSError, LinkError):
                        break
                    if not data:
                        misses += 1    # линия молчит — дочитывать нечего
                        time.sleep(0.02)
                        continue
                    misses = 0
                    self.session.handle_line_reply(data)
            except Exception:         # закрытие не должно ронять скрипт
                pass
        try:
            self.link.close()
        except (OSError, LinkError):
            pass

    def __enter__(self) -> "Terminal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()