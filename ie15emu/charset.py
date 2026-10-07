"""Знакогенератор 15ИЭ-00-013 из ПЗУ КР556РТ5 (rom/chargen-15ie.bin).

Организация (по тех. описанию): 256 глифов × 8 строк × 7 точек.
Адрес глифа (код ВЗУ) — старшие разряды, строка — младшие.
Каждый байт строки: младшие 7 битов = точки слева направо (старший — левая).
"""
from __future__ import annotations

from pathlib import Path

GLYPHS = 256
GLYPH_ROWS = 8
GLYPH_COLS = 7

# КОИ7-алфавит: коды 0x00..0x1E = ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ
RUS7 = "ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ"


def rom_addr(code: int) -> int:
    """Адрес глифа в ПЗУ для кода ВЗУ (КОИ7 Н1, русский алфавит).

    В ПЗУ русские буквы лежат в блоке 0xC0..0xFE: строчные 0xC0..0xDE,
    заглавные 0xE0..0xFE; слоты 0x00..0x1E пусты (латиница — 0x20..0x7E).
    Коды ВЗУ заглавных 0x00..0x1E (и их 8-разрядные формы с битом
    алфавита 0x80..0x9E) и строчных 0x60..0x7E переводятся туда.
    """
    code &= 0xFF
    if 0x80 <= code < 0x80 + len(RUS7):     # заглавные с битом алфавита
        return 0xE0 + (code - 0x80)
    if 0xE0 <= code < 0xE0 + len(RUS7):     # строчные с битом алфавита
        return 0xC0 + (code - 0xE0)
    if code < 0x1F:                      # заглавные ЮАБЦДЕ…Ч (Н1)
        return 0xE0 + code
    if 0x60 <= code < 0x7F:              # строчные юабцде…ч (Н1)
        return 0xC0 + (code - 0x60)
    return code                          # ASCII и блок 0x7F — как есть


class Charset:
    def __init__(self, rom_path: str | Path):
        data = Path(rom_path).read_bytes()
        if len(data) != GLYPHS * GLYPH_ROWS:
            raise ValueError(
                f"{rom_path}: ожидалось {GLYPHS * GLYPH_ROWS} байт, "
                f"получено {len(data)}"
            )
        self.rom = data

    def rows(self, code: int, set_name: str = "n2") -> list[int]:
        """8 байт-строк глифа (7 точек в старших разрядах 7…1) для кода
        ВЗУ в данном наборе знаков; пустое место — строки нулевые."""
        slot = rom_slot(code, set_name)
        if slot is None:
            return [0] * GLYPH_ROWS
        base = slot * GLYPH_ROWS
        return [self.rom[base + r] >> 1 for r in range(GLYPH_ROWS)]

    def bitmap(self, code: int) -> list[list[int]]:
        """Глиф как матрица 8×7 из 0/1."""
        out = []
        for r in self.rows(code):
            out.append([(r >> (6 - c)) & 1 for c in range(GLYPH_COLS)])
        return out

    def label(self, code: int) -> str:
        """ASCII/КИИ7-подпись кода для отладочного вывода (КОИ7 Н1)."""
        if code < 0x1F:
            return RUS7[code]
        if 0x60 <= code < 0x7F:
            return RUS7[code - 0x60].lower()
        if 0x20 <= code < 0x60:
            return chr(code)
        if code == 0x7F:
            return "█"   # сплошная заливка (полный непрозрачный блок)
        return f"<{code:02X}>"


# --- КОИ7 Н1: кодирование/декодирование (ГОСТ 27463-87) -----------------
# Буквы Н1: заглавные 0x00..0x1E, строчные 0x60..0x7E (порядок как в RUS7).
# Ё/ё в КОИ7 Н1 нет — нормализуется в Е/е.
# Внутренний поток терминала (парсер, буфер SEND) добавляет бит алфавита:
# заглавная → 0x80+код, строчная → 0xE0+код — чтобы буквы не сливались
# с УП и латиницей; на линию их переводит to_line().

_LOWER_OFFSET = 0x60
ALPHA_BIT = 0x80     # «бит алфавита»: русская буква во внутреннем потоке
                     # (парсер/буфер) — иначе её код сливается с УП

# Печатные знаки вне Н1, которыми оперирует SIMH (тире в баннерах и
# приглашениях): в алфавите терминала им соответствует ASCII-ближайший.
TYPO_FALLBACK = {"–": 0x2D, "—": 0x2D, "’": 0x27}


def encode_koi7(text: str) -> bytes:
    """Unicode-строка → коды КОИ7 Н1 внутреннего потока терминала.

    Строчные русские — 0xE0..0xFE, заглавные — 0x80..0x9E (код Н1 с
    «битом алфавита», чтобы «М»=0x0D не сливался с возвратом каретки,
    а русская строчная — с латинской). Печатный ASCII проходит как есть;
    управляющие (<0x20) сохраняются; неизвестные символы заменяются
    заполнителем 0x7F.
    """
    out = bytearray()
    for ch in text:
        if ch in TYPO_FALLBACK:
            out.append(TYPO_FALLBACK[ch])   # «–» из баннеров SIMH → «-»
            continue
        if ch == "№":
            # внутренний знак 0x1F с битом алфавита (в Н2 показывается
            # «№»); на линию to_line кладёт его позицию 2/3 — «#»
            out.append(ALPHA_BIT | 0x1F)
            continue
        up = ch.upper().replace("Ё", "Е")
        if up in RUS7:
            code = RUS7.index(up)
            out.append(code | ALPHA_BIT | _LOWER_OFFSET if ch.islower()
                       else code | ALPHA_BIT)
            continue
        b = ord(ch)
        if b < 0x20:
            out.append(b)            # ПР, ПС, ВК, ЗВН, ESC… — как есть
            continue
        b &= 0x7F
        out.append(b if 0x20 <= b < 0x7F else 0x7F)
    return bytes(out)


# --- наборы знаков 15ИЭ (КОИ-7, ГОСТ 27463-87) ---------------------------
# Н0 — международный ISO 646 IRV: как ASCII, но 0x24 = «¤» (дензнак) и
#     0x7E = «¯» (черта сверху) вместо «$» и «~»; 0x5E = «¬».
# Н1 — первый национальный: 0x40…0x5E — русские ЗАГЛАВНЫЕ в порядке
#     ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ, 0x5F — «Ъ», 0x60…0x7E — строчные.
# Внутренние коды кадров (0x01…0x1E заглавные, 0x60… строчные) не меняются:
# набор — режим отображения кодов 0x20…0x7F, как переключатель «набор»
# на щитке терминала.

N0_SPECIAL = {0x24: "¤", 0x5E: "¬"}


def display_char(code: int, name: str) -> str:
    """Знак кода ВЗУ в данном наборе: Н0 — ASCII целиком (кириллических
    кодов нет); Н1 — русские верхний и нижний регистры (+ латинская
    верхняя строка, занятая Н1); Н2 — международная строка без замен
    Н0 («^» и «$» на своих местах, 0x23 — «#»), латиница в обоих
    регистрах, кириллица — только заглавная, кодами ЭВМ 0x00…0x1E
    (как их отдаёт SIMH/ДКС после koi7_raw_upper); 0x1F — номерной знак
    «№» (внутренний 0x9F, на линию — позиция 2/3 = «#»)."""
    code &= 0x7F
    if name == "n2":
        if code == 0x1F:
            return "№"
        if code >= 0x20:
            if code == 0x7F:
                return "█"
            return chr(code)               # «^» «$» — как в ПЗУ, без Н0-замен
    if name in ("n0", "n2") and code >= 0x20:
        if code == 0x7F:
            return "█"
        return N0_SPECIAL.get(code, chr(code))   # 0x60.. — латинская строчная
    if name == "n0":
        return ""                          # в ASCII коды <0x20 — УП, не знаки
    if name == "n1" and 0x40 <= code <= 0x5F:
        code -= 0x40                       # латинская строка → русские верх
    return decode_koi7(bytes([code]))


def rom_slot(code: int, name: str = "n2"):
    """Физический слот ПЗУ для кода ВЗУ в наборе; None — пустое место.
    Н0/Н2: строка 0x60.. — слоты латинской строчной; Н2 кириллицу берёт
    из внутренних кодов ЭВМ 0x00..0x1E (слот 0xE0..)."""
    code &= 0x7F
    if name in ("n0", "n2") and code >= 0x20:
        return code                               # 0x60.. — латинская строчная
    if name == "n0":
        return None if code < 0x20 else code
    if name == "n1" and 0x40 <= code <= 0x5F:
        code -= 0x40
    return rom_addr(code)


def apply_set(code: int, name: str) -> int:
    """Код ВЗУ → код отображения в выбранном наборе знаков ('n0'/'n1')."""
    code &= 0x7F
    if name == "n1" and 0x40 <= code <= 0x5F:
        # латинская/спец-строка 0x40…0x5F в наборе Н1 — русские заглавные
        # (0x40='Ю' … 0x5E='Ч', 0x5F='Ъ'); отображаются тем же слотом
        # знакогенератора, что и внутренние коды 0x00…0x1F
        return code - 0x40
    return code


def decode_koi7(data: bytes) -> str:
    """7-разрядные коды КОИ7 Н1 → Unicode (строчные/заглавные русские)."""
    chars = []
    labels = {i: RUS7[i] for i in range(len(RUS7))}
    labels[0x1F] = "Ъ"                       # Н1: 0x40+0x1F — твёрдый знак
    labels.update({i | _LOWER_OFFSET: RUS7[i].lower()
                   for i in range(len(RUS7))})
    for b in data:
        b &= 0x7F
        if b in labels:
            chars.append(labels[b])
        elif b in N0_SPECIAL:                  # Н0: ¤ вместо $, ¬ вместо ^
            chars.append(N0_SPECIAL[b])
        elif 0x20 <= b < 0x7F:
            chars.append(chr(b))
        elif b == 0x7F:
            chars.append("█")
        else:
            chars.append("")
    return "".join(chars)


def normalize_line_bytes(data: bytes) -> bytes:
    """Нормализовать вход из линии (ЭВМ → терминал) к потоку парсера.

    Линии SIMH БЭСМ-6 (`attach ttyN <порт>`) работают в UTF-8 (баннер
    `Encoding is UTF-8`) либо отдают внутренние 7-разрядные коды (баннеры
    `RAW`/`KOI-7`). Наивная перекодировка UTF-8 в чистый КОИ7 Н1 дала бы
    «М»=0x0D — и парсер принял бы заглавную букву за возврат каретки.
    Поэтому заглавные русские помечаются битом алфавита (0x80).

    Уже готовые 7-разрядные коды КОИ7 (байты < 0x80) проходят без изменений.
    """
    if not data:
        return data
    if not any(b >= 0x80 for b in data):
        return data                      # уже 7-разрядный КОИ7
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return bytes(b & 0x7F for b in data)   # сырой 8-разрядный поток
    return encode_koi7(text)


def normalize_incremental(data: bytes, carry: bytes = b""):
    """normalize_line_bytes для потока, разрезаемого recv() на части.

    Возвращает (поток для парсера, остаток): последние байты, образующие
    незавершённую последовательность UTF-8, переносятся в carry и
    дописываются к следующему чтению (иначе на границе chunks много
    байтовая кириллица ломается на «█»).
    """
    data = carry + data
    if not any(b >= 0x80 for b in data):
        return data, b""
    for cut in range(0, 4):
        end = len(data) - cut
        if end < 0:
            break
        try:
            text = data[:end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return encode_koi7(text), data[end:]
    return bytes(b & 0x7F for b in data), b""   # мусор без UTF-8


def koi7_raw_upper(data: bytes) -> bytes:
    """RAW/KOI-7 линия: алфавит БЭСМ-6 не различает регистр (строка 0x60..),
    машина означает эти коды как ЗАГЛАВНЫЕ русские (так их печатает и
    `koi7_rus_to_unicode` в SIMH). Перекладываем строку 0x60..0x7E в
    форму Н1 с битом алфавита (0x80..0x9E) — экран покажет заглавные.
    """
    return bytes(b + 0x20 if 0x60 <= b < 0x60 + len(RUS7) else b
                 for b in data)


def koi7_display_upper(data: bytes) -> bytes:
    """Локальное эхо АВТОНОМНО: алфавит Н1 одно-регистрный — русские
    буквы печатаются заглавными, как их покажет ЭВМ (`koi7_rus_to_unicode`
    в SIMH отдаёт строку 0x60.. заглавными)."""
    return bytes(b - 0x60 if 0xE0 <= b < 0xE0 + len(RUS7) else b
                 for b in data)


def to_line(data: bytes, encoding: str = "utf8") -> bytes:
    """Внутренний КОИ7 Н1 поток (бит алфавита 0x80) → байты линии ЭВМ.

    encoding="raw"  — линия SIMH `RAW`/`KOI-7`: внутренние коды БЭСМ-6;
    русская буква всегда в строке 0x60..0x7E (регистр машина сворачивает),
    конец строки — ETX (на RAW-линии SIMH не преобразует ПР ПС);
    encoding="dks"  — линия Э-60 через ДКС в raw-кодировке: те же коды,
    но `dks_line_char` оканчивает строку по ПР (ETX для неё — знак);
    encoding="utf8" — линия SIMH `UTF-8`: кириллица уходит как UTF-8,
    дальше `unicode_to_koi7` в SIMH положит её в ту же строку, а ПР
    превратит в ETX сам (`vt_fix`) — поэтому пара ПР ПС сворачивается
    в один ПР, иначе машина прочитала бы два конца строки.
    Латиница, цифры и прочие управляющие проходят без изменений.
    """
    if encoding == "raw":
        # RAW-линия SIMH не применяет vt_fix: конец строки — ETX.
        data = data.replace(b"\r\n", b"\x03")
        data = data.replace(b"\r", b"\x03").replace(b"\n", b"\x03")
    elif encoding in ("utf8", "dks"):
        # И vt_fix, и dks_line_char оканчивают строку по ПР: ПР ПС
        # сворачивается в один ПР, иначе машина прочитала бы два
        # конца строки и «проглотила» пустую.
        data = data.replace(b"\r\n", b"\r").replace(b"\n", b"\r")
    out = bytearray()
    for b in data:
        if 0x80 <= b < 0x80 + len(RUS7):        # заглавная русская Н1+0x80
            ch = RUS7[b - 0x80]
            out += ch.encode("utf-8") if encoding == "utf8" \
                else bytes([0x60 + (b - 0x80)])
        elif 0xE0 <= b < 0xE0 + len(RUS7):      # строчная русская Н1+0xE0
            ch = RUS7[b - 0xE0].lower()
            out += ch.encode("utf-8") if encoding == "utf8" \
                else bytes([0x60 + (b - 0xE0)])
        elif b == ALPHA_BIT | 0x1F:
            # «№»: на линии ВТ у него нет своего кода — позиция 2/3 («#»)
            out.append(0x23)
        else:
            out.append(b)
    return bytes(out)
