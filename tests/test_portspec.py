"""Спецификация портов и перебор «занято → следующий».
Запуск: python3 tests/test_portspec.py"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.link import (LinkError, SSHLink, StdioLink, TCPLink,
                           open_link, parse_port_spec, spec_hint)


def test_sshlink_parses_ports_like_tcp():
    # регрессия: разбор портов в SSHLink потерялся — конструктор падал с
    # NameError до первой попытки подключения. Проверяем, что перебор
    # портов идёт и неудача честно объявляется LinkError.
    pytest = __import__("pytest")
    if pytest.importorskip("paramiko", reason="нужен paramiko") is None:
        return
    try:
        SSHLink("127.0.0.1", "1,2-3", user="test", timeout=0.3)
        raise AssertionError("ожидалась ошибка подключения")
    except LinkError as e:
        assert "1-3" in str(e)


def test_link_banner_flags():
    # баннер ждут у сетевых линий (нужен для «ПРОВЕРЬ ЛИНИЮ!»), у stdio —
    # нет: там предупреждение не показывается
    assert TCPLink.banner is True and SSHLink.banner is True
    assert StdioLink.banner is False
    assert open_link("stdio://").banner is False


def test_parse():
    assert parse_port_spec("4202") == [4202]
    assert parse_port_spec(4202) == [4202]
    assert parse_port_spec("4202-4205") == [4202, 4203, 4204, 4205]
    assert parse_port_spec("4204,4202-4203") == [4204, 4202, 4203]
    assert parse_port_spec([4203, 4202, 4203]) == [4203, 4202]
    for bad in ("4205-4202", "0", "70000", ""):
        try:
            parse_port_spec(bad)
            assert False, bad
        except (LinkError, ValueError):
            pass


def test_hint():
    assert spec_hint([4202, 4203, 4204, 4210]) == "4202-4204,4210"
    assert spec_hint([4202]) == "4202"
    assert spec_hint([]) == "нет"


class _Server:
    """Локальная «линия»: busy — отвечает занятостью и закрывает,
    free — шлёт баннер и держит соединение."""

    def __init__(self, mode: str):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(4)
        self.mode = mode
        self.keep: list[socket.socket] = []
        self.thr = threading.Thread(target=self._run, daemon=True)
        self.thr.start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            if self.mode == "busy":
                c.sendall(b"Line connection busy\r\n")
                c.close()
            else:
                c.sendall(b"Connected to the line\r\nEncoding is RAW\r\n")
                self.keep.append(c)

    def close(self):
        for c in self.keep:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


def test_scan_skips_busy_ports():
    busy = _Server("busy")
    free = _Server("free")
    try:
        link = TCPLink("127.0.0.1", [busy.port, free.port],
                       timeout=2.0, probe=1.0)
        assert link.port == free.port
        # баннер разведки не потерян — первый recv отдаёт его
        got = b""
        t0 = time.time()
        while b"Encoding" not in got and time.time() - t0 < 2:
            got += link.recv()
        assert b"Connected to the line" in got
        link.close()
    finally:
        busy.close()
        free.close()


def test_all_busy_message():
    a = _Server("busy")
    b = _Server("busy")
    try:
        try:
            TCPLink("127.0.0.1", [a.port, b.port], timeout=2.0, probe=1.0)
            assert False, "ожидался LinkError"
        except LinkError as e:
            assert "заняты" in str(e)
    finally:
        a.close()
        b.close()


def test_unreachable_only():
    s = _Server("busy")
    dead = s.port + 1                     # заведомо никого не слушаем
    s.close()                              # освободили свой порт — оба мертвы
    try:
        TCPLink("127.0.0.1", [dead, s.port], timeout=0.5, probe=0.5)
        assert False
    except LinkError as e:
        assert "недоступны" in str(e) or "нет свободной" in str(e)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
