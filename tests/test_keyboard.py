"""Запуск: python3 tests/test_keyboard.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ie15emu.charset import encode_koi7
from ie15emu.keyboard import (DEFAULT_LAYOUT, KEY_BLINK, KEY_CMDSET, KEY_CTRLUP,
                              KEY_MODE, KEY_SEND, KEY_WHEELDOWN, KEY_WHEELUP,
                              ctrl_code, decode_key_bytes, decode_keys,
                              group_paste, is_key_combo, key_to_bytes,
                              normalize_combo)


def test_mouse_sgr_wheel():
    # мышь отдана приложению (SGR 1006): колесо — листание «истории»,
    # клики и движение молча съедаются, в линию мусор не идёт
    assert decode_key_bytes(b"\x1b[<64;10;5M") == [KEY_WHEELUP]
    assert decode_key_bytes(b"\x1b[<65;10;5M") == [KEY_WHEELDOWN]
    assert decode_key_bytes(b"\x1b[<64;10;5m") == []        # отпускание
    assert decode_key_bytes(b"\x1b[<68;1;1M") == [KEY_WHEELUP]   # +shift
    assert decode_key_bytes(b"\x1b[<81;1;1M") == [KEY_WHEELDOWN]  # +ctrl
    # левая кнопка — выделение текста: нажатие, протягивание, отпускание
    assert decode_key_bytes(b"\x1b[<0;3;4M") == ["MOUSEDOWN@3,4"]
    assert decode_key_bytes(b"\x1b[<32;9;4M") == ["MOUSEDRAG@9,4"]
    assert decode_key_bytes(b"\x1b[<0;9;4m") == ["MOUSEUP@9,4"]
    assert decode_key_bytes(b"\x1b[<2;5;5M") == []          # правая
    assert decode_key_bytes(b"\x1b[<32;1;1m") == []          # движение без кнопки
    # Ctrl с мышью не стирается: им выбирается выделение колонкой
    # (бит 16 в SGR, бит 4 в urxvt)
    assert decode_key_bytes(b"\x1b[<16;3;4M") == ["ctrl+MOUSEDOWN@3,4"]
    assert decode_key_bytes(b"\x1b[<48;9;4M") == ["ctrl+MOUSEDRAG@9,4"]
    assert decode_key_bytes(b"\x1b[<16;9;4m") == ["ctrl+MOUSEUP@9,4"]
    assert decode_key_bytes(b"\x1b[48;3;4M") == ["ctrl+MOUSEDOWN@3,4"]
    # терминалы без SGR (1006) отвечают urxvt (1015) и старой формой X10 —
    # без их разбора мышь у пользователя не работает вовсе
    assert decode_key_bytes(b"\x1b[32;3;4M") == ["MOUSEDOWN@3,4"]
    assert decode_key_bytes(b"\x1b[64;9;6M") == ["MOUSEDRAG@9,6"]
    assert decode_key_bytes(b"\x1b[M\x20\x23\x24") == ["MOUSEDOWN@3,4"]
    assert decode_key_bytes(b"\x1b[M\x40\x29\x26") == ["MOUSEDRAG@9,6"]
    # X10-код 3 — отпускание левой кнопки (и код 35 — с битом движения);
    # без разбора отпускания выделение не фиксировалось бы на X10
    assert decode_key_bytes(b"\x1b[M\x23\x29\x26") == ["MOUSEUP@9,6"]
    assert decode_key_bytes(b"\x1b[M\x43\x29\x26") == ["MOUSEUP@9,6"]
    assert decode_key_bytes(b"\x1b[M\x60\x21\x21") == [KEY_WHEELUP]
    assert decode_key_bytes(b"\x1b[M\x61\x21\x21") == [KEY_WHEELDOWN]
    assert decode_key_bytes(b"a\x1b[<64;1;1Mb") == ["a", KEY_WHEELUP, "b"]
    # стрелки/функциональные после мышиных событий не пострадали
    assert decode_key_bytes(b"\x1b[<64;1;1M\x1b[A") == [KEY_WHEELUP, "KEY_UP"]


def test_csi_u_modifiers():
    # kitty/foot/wezterm: «ESC [ код;мод u»; xterm modifyOtherKeys:
    # «ESC [ 27;мод;код ~» — нажатие с Ctrl/Alt именуется и не дублирует
    # обычный знак; без модификаторов — тот же знак, что и раньше
    from ie15emu.keyboard import is_key_combo, normalize_combo
    assert decode_key_bytes(b"\x1b[1073;5u") == ["ctrl+б"]
    assert decode_key_bytes(b"\x1b[1073;3u") == ["alt+б"]
    assert decode_key_bytes(b"\x1b[27;5;91~") == ["ctrl+["]
    assert decode_key_bytes(b"\x1b[1073u") == ["б"]
    assert decode_key_bytes(b"a\x1b[1041;5uc") == ["a", "ctrl+Б", "c"]
    # старые пути не тронуты; служебные keysym kitty (>=0xE000) — тоже
    assert decode_key_bytes(b"\x1b[A\x1b[15~\x1b[3~") == ["KEY_UP", "CLEAR",
                                                           "KEY_DELETE"]
    assert not is_key_combo("KEY_UP") and not is_key_combo("1+1")
    assert normalize_combo("Б+Ctrl") == normalize_combo("Ctrl+Б") == "ctrl+Б"


def test_alt_prefix():
    # Alt+знак как ESC-префикс (VTE и другие без CSI-u): именуется
    # «alt+…», привязывается в [keys]; Esc перед «[»/«O» и одиночный
    # Esc остаются как раньше
    assert decode_key_bytes(b"\x1b\xd0\xb1") == ["alt+б"]
    assert decode_key_bytes(b"\x1bA") == ["alt+A"]
    assert decode_key_bytes(b"\x1b[") == ["KEY_ESC", "["]
    assert decode_key_bytes(b"\x1bO") == ["KEY_ESC", "O"]
    assert decode_key_bytes(b"\x1b\x1b") == ["KEY_ESC"]
    assert decode_key_bytes(b"\x1b") == ["KEY_ESC"]
    assert decode_key_bytes(b"\x1bd") == ["alt+d"]
    # экранные команды клавиатурой отдаёт ключами (KEY_HOME и т.п.),
    # поэтому слияние ESC+знак в alt+… их не ломает
    assert decode_key_bytes(b"\x1b[A\x1b[H\x1b[3~") == ["KEY_UP", "KEY_HOME",
                                                         "KEY_DELETE"]


def test_plain_chars():
    assert decode_key_bytes(b"abc") == ["a", "b", "c"]


def test_enter_and_backspace():
    assert decode_key_bytes(b"\r") == ["KEY_ENTER"]
    assert decode_key_bytes(b"\x7f") == ["KEY_BACKSPACE"]


def test_arrow_sequences():
    assert decode_key_bytes(b"\x1b[A\x1b[B\x1b[C\x1b[D") == [
        "KEY_UP", "KEY_DOWN", "KEY_RIGHT", "KEY_LEFT"]


def test_send_mode_keys():
    assert decode_key_bytes(b"\x1b[18~") == [KEY_BLINK]   # F7 — БЛИНК
    assert decode_key_bytes(b"\x1b[19~") == [KEY_CMDSET]  # F8 — НАБОР
    assert decode_key_bytes(b"\x1b[20~") == [KEY_MODE]    # F9 — СЕТЬ
    assert decode_key_bytes(b"\x1b[21~") == [KEY_SEND]    # F10 — ПЕРЕДАЧА


def test_send_then_mode():
    assert decode_key_bytes(b"\x1b[20~\x1b[21~") == [KEY_MODE, KEY_SEND]


def test_f1_f11_ne_razbirayutsya():
    # F1 (\x1bOP) и F11 (\x1b[23~) не служат клавишами терминала: не должны
    # давать ни служебных ключей, ни экранных кодов
    assert KEY_MODE not in decode_key_bytes(b"\x1b[23~")
    assert KEY_SEND not in decode_key_bytes(b"\x1b[23~")
    assert KEY_CMDSET not in decode_key_bytes(b"\x1bOP")


def test_positional_layout_default():
    # Позиционная (по умолчанию): клавиша даёт русскую букву своего места
    assert DEFAULT_LAYOUT == "positional"
    assert key_to_bytes("q", layout="positional") == bytes([0x0A | 0xE0])  # Й
    assert key_to_bytes("w", layout="positional") == bytes([0x03 | 0xE0])  # Ц
    assert key_to_bytes("Q", layout="positional") == b"Q"          # Shift — англ.
    assert key_to_bytes("<", layout="positional") == b"<"
    assert key_to_bytes("1", layout="positional") == b"1"


def test_phonetic_layout():
    # Фонетическая: w→В, q→Я, x→Ч (историческая таблица проекта)
    assert key_to_bytes("w", layout="phonetic") == bytes([0x16 | 0xE0])  # Ж
    assert key_to_bytes("q", layout="phonetic") == bytes([0x11 | 0xE0])  # Я
    assert key_to_bytes("W", layout="phonetic") == b"W"


def test_shift_switches_alphabet_ru_os():
    # RU-раскладка ОС: Shift+ц присылает «Ц» — «регистр» переключает
    # алфавит: буква уходит английской по обратной таблице выбранной раскладки
    assert key_to_bytes("Ц", layout="positional") == b"W"          # pos: w→Ц
    assert key_to_bytes("Ц", layout="phonetic") == b"C"            # phon: c→Ц
    assert key_to_bytes("ц", layout="positional") == encode_koi7("ц")   # без Shift — русская
    assert key_to_bytes("Ж", layout="phonetic") == b"W"            # w/v→Ж: первая w


def test_layout_off_passes_through():
    # Выключенная раскладка: латиница как есть, заглавная кириллица не тронется
    assert key_to_bytes("q", layout=None) == b"q"
    assert key_to_bytes("Ц", layout=None) == encode_koi7("Ц")


def test_control_keys_send_esc():
    assert key_to_bytes("KEY_END") == b"\x1bK"
    # PgUp/PgDn — локальное листание экрана, в линию ничего
    assert key_to_bytes("KEY_PAGEUP") == b""
    assert key_to_bytes("KEY_PAGEDOWN") == b""
    assert key_to_bytes("KEY_INSERT") == b"\x1bb"
    assert key_to_bytes("KEY_DELETE") == b"\x1bc"
    assert key_to_bytes("\t") == b"\x09"          # ТАБ
    assert key_to_bytes("\x07") == b"\x07"        # ЗВН (Ctrl-G)


def test_h2_symbols_same_keys_both_layouts():
    # Н2: RU-раскладка отдаёт знак клавишей того же физического положения,
    # что EN; карта по самому знаку — CapsLock/Shift-состояние не важны
    for rus, lat in (('"', "@"), (":", "^"), ("ё", "|"),
                     ("Ё", "|"), ("ъ", "]"), ("Ъ", "]")):
        assert key_to_bytes(rus, None, shift_to_rus=True) == lat.encode("ascii")
    # «№» (RU клавиша 3) — внутренний номерной знак 0x9F, на линию — 0x23
    assert key_to_bytes("№", None, shift_to_rus=True) == bytes([0x80 | 0x1F])
    # Н2, русская буква+Shift — ТОТ ЖЕ код ЭВМ, что и без Shift: строка
    # ЭВМ одно-регистровая (раньше Shift давал латиницу позиции —
    # «ДЕКАБРЬ» уходил как «LTRF,HM», в файл КРАБа попадал мусор)
    for up_r, low_r, code in (("Х", "х", 0x08), ("Э", "э", 0x1C),
                              ("Ж", "ж", 0x16), ("Щ", "щ", 0x1D)):
        got = key_to_bytes(up_r, None, shift_to_rus=True)
        assert got == bytes([code | 0x80])
        assert got == key_to_bytes(low_r, None, shift_to_rus=True)
    # ASCII-знаки проходят как есть — с обеих раскладок одним и тем же
    for s in "@#^[]'|?%;!<>":
        assert key_to_bytes(s, None, shift_to_rus=True) == s.encode("ascii")
    # вне Н2 карта знаков не работает (Н0/Н1 — историческое поведение)
    assert key_to_bytes('"', None) == b'"'
    assert key_to_bytes('"', "positional") == b'"'


def test_host_mode_key_mapping():
    assert key_to_bytes("KEY_ENTER") == b"\r"
    assert key_to_bytes("KEY_ESC") == b"\x1b"


def test_ctrl_letter_is_control_code_in_any_layout():
    # Ctrl+буква — управляющий символ вне зависимости от раскладки:
    # Ctrl+E = 05, как на 15ВВВ (код = буква & 0x1F)
    assert key_to_bytes("ctrl+e") == b"\x05"
    assert key_to_bytes("ctrl+a") == b"\x01"
    assert key_to_bytes("ctrl+z") == b"\x1a"
    for layout in (None, "positional", "phonetic"):
        assert key_to_bytes("ctrl+e", layout) == b"\x05"
    assert key_to_bytes("ctrl+e", shift_to_rus=True) == b"\x05"
    # Shift на управляющий символ не влияет
    assert key_to_bytes("ctrl+shift+E") == b"\x05"
    # кириллица, цифры и Alt — управляющих символов не дают
    assert key_to_bytes("ctrl+х") == encode_koi7("ctrl+х")
    assert ctrl_code("ctrl+х") is None and ctrl_code("alt+e") is None
    assert ctrl_code("shift+e") is None and ctrl_code("ctrl+5") is None


def test_ctrl_e_arrives_from_every_terminal_form():
    # Ctrl+E приходит по-разному — во всех формах это один УП 05,
    # а не «[» и мусор в наборе (старая форма modifyOtherKeys без CSI-u)
    assert decode_key_bytes(b"\x05") == ["\x05"]                # байт ENQ
    assert decode_key_bytes(b"\x1b[101;5u") == ["ctrl+e"]       # kitty CSI-u
    assert decode_key_bytes(b"\x1b[27;5;101~") == ["ctrl+e"]    # modifyOtherKeys
    assert decode_key_bytes(b"\x1b[1;5E") == ["ctrl+e"]         # старая форма
    assert decode_key_bytes(b"\x1b[1;5e") == ["ctrl+e"]         # строчная буква
    for seq in (b"\x05", b"\x1b[101;5u", b"\x1b[27;5;101~",
                b"\x1b[1;5E", b"\x1b[1;5e"):
        assert decode_key_bytes(seq) == ["ctrl+e"] or \
            decode_key_bytes(seq) == ["\x05"]
    # в старой форме стрелки остались стрелками
    assert decode_key_bytes(b"\x1b[1;5A") == [KEY_CTRLUP]


def test_sequence_split_by_read_is_not_mangled():
    # os.read отдаёт нажатие по частям: недобранный хвост escape-последовательности
    # обязан ждать следующих байтов, иначе ESC погаснет, а «[ 27 ;» уйдёт в
    # набор обычными знаками — тот самый «[» вместо Ctrl+E
    seqs = [b"\x05", b"\x1b[101;5u", b"\x1b[27;5;101~", b"\x1b[1;5E",
            b"\r", b"\x1b[H", b"\x1b[2~", b"\x1b[A", b"\x1b[<0;10;5M",
            b"ABC", b"a\x1b[1;2Ab"]
    for seq in seqs:
        want = decode_key_bytes(seq)
        for cut in range(1, len(seq)):
            keys, tail = decode_keys(seq[:cut], b"")
            keys2, _ = decode_keys(seq[cut:], tail)
            assert keys + keys2 == want, (seq, cut, keys + keys2, want)
        # и совсем по одному байту — так «терминал» может отдать всё сразу
        keys, pending = [], b""
        for b in seq:
            got, pending = decode_keys(bytes([b]), pending)
            keys += got
        assert keys == want, (seq, keys, want)


def test_ordinary_typing_never_buffered():
    # обычные знаки не ждут продолжения: ввод не должен «залипать»
    keys, pending = decode_keys(b"abc", b"")
    assert keys == ["a", "b", "c"] and pending == b""
    # одиночный ESC без продолжения — тоже знак (а не хвост навечно)
    keys, pending = decode_keys(b"\x1b", b"")
    assert keys == [] and pending == b"\x1b"
    keys, pending = decode_keys(b"A", pending)
    assert keys == ["alt+A"] and pending == b""


def test_shift_to_rus_h2():
    # Н2, латинская раскладка без Shift — английские заглавные
    # (одно-регистрный набор: строка 0x60.. линии — кириллица ЭВМ)
    assert key_to_bytes("w", None, shift_to_rus=True) == b"W"
    # Н2, Shift+латинская клавиша — русская буква положения
    assert key_to_bytes("W", None, shift_to_rus=True) == bytes([0x03 | 0x80])
    assert key_to_bytes("Q", None, shift_to_rus=True) == bytes([0x0A | 0x80])
    # Н2, русская раскладка: регистр ввода НЕ важен — и «ц», и «Ц» дают
    # один код ЭВМ (однорегистровость; Shift применяет только клавиатура
    # 15ВВВ — её смена алфавита относится к Н1, см. --layout)
    assert key_to_bytes("ц", None, shift_to_rus=True) == bytes([0x03 | 0x80])
    assert key_to_bytes("Ц", None, shift_to_rus=True) == bytes([0x03 | 0x80])
    assert key_to_bytes("Й", None, shift_to_rus=True) == bytes([0x0A | 0x80])
    assert key_to_bytes("й", None, shift_to_rus=True) == bytes([0x0A | 0x80])
    assert key_to_bytes("Е", None, shift_to_rus=True) == bytes([0x05 | 0x80])
    assert key_to_bytes("е", None, shift_to_rus=True) == bytes([0x05 | 0x80])
    # «Ё» в Н2 — не буква: её клавиша (общая с EN «\|») даёт вертикальную черту
    assert key_to_bytes("Ё", None, shift_to_rus=True) == b"|"
    # без флага (Н0/Н1 вне раскладки) — как раньше
    assert key_to_bytes("W", None) == b"W"
    assert key_to_bytes("w", None) == b"w"

# --- вставка: взрыв знаков одного чтения = буфер обмена -----------------

def test_group_paste_burst():
    # Терминал со СВОЕЙ вставкой шлёт буфер одним чтением: ≥3 разных
    # знака — вставка, одним ключом (иначе Н2 портит заглавные:
    # «ВНИМАНИЕ» → «DYBVFYBT» — мусор)
    text = "ВНИМАНИЕ: ТЕРМ-20"
    keys, until = group_paste(list(text), 100.0, 0.0)
    assert keys == [text]
    assert until > 100.0                      # признак живёт ещё gap сек
    # обычный набор (клавиши по одной) — посимвольно
    keys, until = group_paste(["a", "б"], 100.0, 0.0)
    assert keys == ["a", "б"] and until == 0.0
    # автоповтор одной клавиши — это набор, не вставка
    keys, until = group_paste(["X", "X", "X"], 100.0, 0.0)
    assert keys == ["X", "X", "X"] and until == 0.0
    # ВК и управляющие рвут строку: знаки до и после — свои ключи
    keys, _ = group_paste(["П", "Р", "И", "KEY_ENTER", "В", "Е", "Т"],
                          100.0, 0.0)
    assert keys == ["ПРИ", "KEY_ENTER", "ВЕТ"]
    # события мыши и прочие имена не группируются
    keys, _ = group_paste(["MOUSEDRAG@3,4", "а", "б"], 100.0, 0.0)
    assert keys == ["MOUSEDRAG@3,4", "а", "б"]


def test_group_paste_tail_within_gap():
    # Хвост вставки, пришедший отдельным чтением (буфер > 256 байт):
    # короткий, но в промежутке gap — продолжает вставку; после паузы
    # набор снова посимвольный
    keys, until = group_paste(list("ВНИ"), 100.0, 0.0)        # первая половина
    assert keys == ["ВНИ"] and until == 100.15
    keys, until2 = group_paste(list("МАНИ"), 100.10, until)   # чтение впритык
    assert keys == ["МАНИ"] and until2 >= 100.20
    # одна буква хвостом в окне — тоже вставка (ключ не портится)
    keys, until3 = group_paste(["Е"], 100.12, until2)
    assert keys == ["Е"] and until3 >= 100.25
    # после паузы признак погашен — обычный набор
    keys, until4 = group_paste(["З", "н"], 101.0, until3)
    assert keys == ["З", "н"] and until4 == 0.0


def test_group_paste_keeps_esc_sequences_whole():
    # распознанные последовательности (стрелки и т.п.) уходят как есть —
    # group_paste видит их после decode_keys, но и сырые имена не трогает
    keys, _ = group_paste(["KEY_UP", "a", "b", "c"], 100.0, 0.0)
    assert keys == ["KEY_UP", "abc"]
    keys, _ = group_paste(["ctrl+shift+x", "д", "е", "ж"], 100.0, 0.0)
    assert keys == ["ctrl+shift+x", "деж"]


# --- bracketed paste (режим терминала 2004) -----------------------------

def test_bracketed_paste():
    # Метки вставки: весь текст одним ключом, метки съедены (не дают
    # мусора вида KEY_?200)
    from ie15emu.keyboard import decode_keys
    text = "ВНИМАНИЕ: ТЕРМ-20\nстрочная строка"
    data = f"\x1b[200~{text}\x1b[201~".encode()
    keys, tail = decode_keys(data)
    # перевод строки — ПР (0x0D), как у ВК: разделитель строк и буфера, и линии
    assert keys == [text.replace("\n", "\r")] and tail == b"", (keys, tail)
    # пустая вставка — метки съедены, ключей нет
    assert decode_keys(b"\x1b[200~\x1b[201~")[0] == []
    # один конец без начала (съеденный пробой начало) — гасится молча,
    # в набор «KEY_ESC [201~» не утекает
    assert decode_keys(b"\x1b[201~")[0] == []
    keys = decode_keys("до".encode() + b"\x1b[201~" + "после".encode())[0]
    assert "".join(keys) == "допосле" and "KEY_ESC" not in keys, keys
    # «\r\n» из буферов Windows — один ПР, «\r» тоже
    assert decode_keys(b"\x1b[200~a\r\nb\rc\x1b[201~")[0] == ["a\rb\rc"]
    # управляющие из буфера вычищаются (ESC сломал бы разбор эха)
    assert decode_keys(b"\x1b[200~a\x1b[7mb\x1b[201~")[0] == ["a[7mb"]
    # group_paste вставку-одним-ключом не трогает
    assert group_paste([text], 0.0, 0.0)[0] == [text]


def test_bracketed_paste_split_across_reads():
    # Вставка приходит чтениями (os.read 256 байт): до метки конца
    # split_incomplete держит хвост, итог — один ключ целиком
    from ie15emu.keyboard import decode_keys
    text = "длинная вставка " * 200               # килобайты > 256 байт
    whole = f"\x1b[200~{text}\x1b[201~".encode()
    got: list = []
    pending = b""
    while whole:
        chunk, whole = whole[:256], whole[256:]
        ks, pending = decode_keys(chunk, pending)
        got.extend(ks)
    assert pending == b"" and got == [text], (len(got), got[:1])

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("все проверки пройдены")
