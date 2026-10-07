"""Конфигурационный файл ie15.conf: разбор и приоритет CLI.
Запуск: python3 tests/test_conf.py"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.__main__ import apply_conf, line_url_from_conf, load_conf

CONF = """
[line]
type = ssh
server = 10.0.0.5
port = 4202
ssh_port = 2222
[ssh]
user = mashina
keyfile = /nonexistent-dir-xyz/id_ed25519
password = secret
[terminal]
mode = local
sets = 1
koi7 = true
layout = phonetic
"""


def ns(**kw):
    base = dict(line=None, script=None, mode=None, sets=None, koi7=False,
                layout=None, password=None, keyfile=None)
    base.update(kw)
    return SimpleNamespace(**base)


def cp_from(text: str):
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False,
                                     encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        return load_conf(path)
    finally:
        Path(path).unlink()


def test_ssh_url():
    # ssh: порт моста остаётся в URL, а ДКС-линия из [line] port едет
    # параметром ?port= (без него мост взял бы свою цель по умолчанию)
    cp = cp_from(CONF)
    assert line_url_from_conf(cp) == "ssh://mashina@10.0.0.5:2222?port=4202"


def test_ssh_url_passes_line_port_spec():
    # диапазон из [line] port доезжает до URL как ?port= (мост по нему
    # сам переберёт ДКС-линии)
    cp = cp_from(CONF.replace("port = 4202", "port = 4202-4223"))
    assert line_url_from_conf(cp) == "ssh://mashina@10.0.0.5:2222?port=4202-4223"


def test_tcp_url():
    cp = cp_from(CONF.replace("type = ssh", "type = tcp"))
    assert line_url_from_conf(cp) == "tcp://10.0.0.5:4202"


def test_no_conf_no_line():
    cp = cp_from("")
    assert line_url_from_conf(cp) is None
    got = apply_conf(ns(), cp)
    assert got.line is None and got.mode == "host" and got.sets == "2"


def test_apply_fills_defaults():
    cp = cp_from(CONF)
    got = apply_conf(ns(), cp)
    assert got.mode == "local" and got.sets == "1" and got.koi7 is True
    assert got.layout == "phonetic"
    assert got.password == "secret"


def test_layout_off_in_conf():
    cp = cp_from(CONF.replace("layout = phonetic", "layout = off"))
    assert apply_conf(ns(), cp).layout == "off"


def test_cli_wins_over_conf():
    cp = cp_from(CONF)
    got = apply_conf(ns(line="tcp://127.0.0.1:4202", mode="host", sets="2",
                        koi7=True, password="иное", keyfile="/no/such"), cp)
    assert got.line == "tcp://127.0.0.1:4202"
    assert got.mode == "host" and got.password == "иное"


def test_port_cli_overrides_line():
    cp = cp_from(CONF)
    got = apply_conf(ns(line="tcp://h:4202", port="4202-4223"), cp)
    assert got.line == "tcp://h:4202-4223"
    # и URL без порта — порт добавляется
    got = apply_conf(ns(line="tcp://h", port="4210"), cp)
    assert got.line == "tcp://h:4210"


def test_conf_port_range_passthrough():
    conf = CONF.replace("type = ssh", "type = tcp").replace("port = 4202",
                                                            "port = 4202-4223")
    cp = cp_from(conf)
    assert line_url_from_conf(cp) == "tcp://10.0.0.5:4202-4223"
    got = apply_conf(ns(), cp)
    assert got.line == "tcp://10.0.0.5:4202-4223"


def test_port_becomes_default_tcp():
    # ни --line, ни сервера в конфиге — --port даёт стоячую tcp-линию
    cp = cp_from("")
    got = apply_conf(ns(port="4202-4204"), cp)
    assert got.line == "tcp://127.0.0.1:4202-4204"


def test_script_mode_ignores_line():
    cp = cp_from(CONF)
    got = apply_conf(ns(script="demo"), cp)
    assert got.line is None


def test_keyfile_only_when_exists():
    cp = cp_from(CONF)
    assert apply_conf(ns(), cp).keyfile is None        # файла нет — не берём
    with tempfile.NamedTemporaryFile(suffix="_key") as kf:
        cp2 = cp_from(CONF.replace("/nonexistent-dir-xyz/id_ed25519", kf.name))
        assert apply_conf(ns(), cp2).keyfile == kf.name


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
