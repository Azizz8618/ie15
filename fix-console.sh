#!/bin/sh
# Починка виртуальных консолей Linux (tty1…tty6): шрифт с кириллицей
# и мышь.
#
# Зачем шрифт: консоль по умолчанию рисует встроенным шрифтом ядра VGA
# 8x16, где кириллических глифов нет. Программа выводит русские буквы
# правильно, а консоль показать их не может — буквы не видны (либо, если
# шрифт кодировочный, все буквы схлопываются в один знак).
#
# Зачем мышь: консоль сама события мыши терминалу не отдаёт — их
# перехватывает gpm (escape-последовательности X10, их понимает
# эмулятор). Без gpm выделение текста мышью на консоли не работает.
#
# Что делаем: кладём в /etc/vconsole.conf юникодный шрифт с кириллицей,
# перезапускаем systemd-vconsole-setup и включаем gpm. Локаль и раскладку
# НЕ трогаем — они должны остаться UTF-8, как уже настроены для X11 и
# оконных терминалов; смешивать UTF-8 с однобайтовой кодовой страницей
# нельзя.
#
# Запуск:   sudo ./fix-console.sh
# Или без sudo, скрипт перезапустит себя сам.
set -eu

VCONSOLE=/etc/vconsole.conf
DROPIN_DIR=/etc/systemd/system/getty@.service.d
# Каталоги со шрифтами консоли: где именно они лежат — зависит от дистрибутива
FONT_DIRS="/usr/lib/kbd/consolefonts /usr/share/consolefonts /usr/share/kbd/consolefonts"

say() { echo "консоль: $*"; }
warn() { echo "консоль: $*" >&2; }

# --- права root ------------------------------------------------------
if [ "$(id -u)" != 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        say "нужны права root — перезапускаю через sudo"
        exec sudo "$0" "$@"
    fi
    warn "нужны права root. Запустите так: sudo $0"
    exit 1
fi

# --- шрифт с кириллицей ---------------------------------------------
# Универсального имени нет: без пакета kbd на машине лежит только встроенный
# VGA-шрифт, а состав kbd отличается между дистрибутивами. Перебираем список
# и берём ПЕРВЫЙ, который реально найден на диске.
#
# Только .psfu: такой шрифт несёт встроенную карту юникода, и консоль в
# режиме UTF-8 знает, какой глиф на каждую букву. Обычный .psf (например
# популярный UniCyr_8x16) карты не несёт — на UTF-8-консоли русские буквы
# в нём опять схлопываются в один глиф (виден сплошной «Ч»).
FONT_CANDIDATES="cyr-sun16 Cyr_a8x16 Cyr_a8x14 Cyr_a8x8 koi8r.8x8 \
                  LatArCyrHeb-16 LatArCyrHeb-08 \
                  LatGrkCyr-8x16 pancyrillic.f16"

if ! command -v setfont >/dev/null 2>&1; then
    warn "нет setfont (пакет kbd). Установите его и запустите снова:"
    warn "  sudo apt install kbd            # Debian/Ubuntu/Fedora/Arch"
    warn "  sudo apt install console-setup   # либо этот (Debian/Ubuntu)"
    exit 1
fi

FONT_ALL=$(ls $FONT_DIRS 2>/dev/null || true)
FONT=""
for f in $FONT_CANDIDATES; do
    case "$FONT_ALL" in
        *"$f.psfu"*|*"$f.psf"*) FONT="$f"; break ;;
    esac
done

if [ -z "$FONT" ]; then
    warn "не нашлось шрифта с кириллицей — без него русских букв не будет."
    case "$FONT_ALL" in
        *UniCyr_8x16.psf*)
            warn "Есть UniCyr_8x16.psf, но без карты юникода: на UTF-8-консоли"
            warn "из-за него буквы схлопнутся. Вручную сработает:"
            warn "  setfont UniCyr_8x16 -u koi8-r" ;;
        *)
            warn "Установите пакет со шрифтами и запустите снова:"
            warn "  sudo apt install kbd" ;;
    esac
    exit 1
fi
say "шрифт: $FONT"

# --- ключи раскладки -------------------------------------------------
# Оставляем UTF-8. Если предыдущая неудачная настройка оставила однобайтовую
# раскладку (-KOI8-R), вернём -UTF-8: с кодировочным шрифтом UTF-8 нельзя.
KEYMAP=""
CUR_KEYMAP=$(sed -n 's/^KEYMAP=//p' "$VCONSOLE" 2>/dev/null | head -1 || true)
if [ -n "${CUR_KEYMAP:-}" ]; then
    case "$CUR_KEYMAP" in
        *-UTF-8)  KEYMAP="$CUR_KEYMAP" ;;
        *-KOI8-R) KEYMAP="${CUR_KEYMAP%-KOI8-R}-UTF-8" ;;
        *)        KEYMAP="$CUR_KEYMAP" ;;    # своя раскладка — не трогаем
    esac
fi

# --- записать /etc/vconsole.conf -------------------------------------
# Меняем только FONT= и KEYMAP=, остальные ключи файла сохраняем.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
if [ -f "$VCONSOLE" ]; then
    cp -n "$VCONSOLE" "$VCONSOLE.bak" 2>/dev/null || true
    grep -v -e '^FONT=' -e '^KEYMAP=' "$VCONSOLE" > "$TMP" || true
fi
printf 'FONT=%s\n' "$FONT" >> "$TMP"
if [ -n "$KEYMAP" ]; then
    printf 'KEYMAP=%s\n' "$KEYMAP" >> "$TMP"
fi
cat "$TMP" > "$VCONSOLE"     # cat, а не mv: сохраняем права и владельца

if ! grep -qx "FONT=$FONT" "$VCONSOLE" 2>/dev/null; then
    warn "не удалось записать $VCONSOLE — прав нет?"
    exit 1
fi
say "записано в $VCONSOLE:"
sed 's/^/    /' "$VCONSOLE"

# --- убрать следы прежней однобайтовой настройки ----------------------
# Локаль ru_RU.koi8r в консолях с юникодным шрифтом несовместима.
STALE="$DROPIN_DIR/ie15-koi8r.conf"
if [ -f "$STALE" ]; then
    rm -f "$STALE"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    say "убрана прежняя локаль ru_RU.koi8r из консолей"
fi

# --- применить --------------------------------------------------------
# Одной записи FONT= мало: systemd читает vconsole.conf только при загрузке.
# Перезапуск службы перезагружает шрифт на ВСЕ консоли разом.
if command -v systemctl >/dev/null 2>&1; then
    if systemctl restart systemd-vconsole-setup.service 2>/dev/null; then
        say "шрифт применён ко всем консолям"
    else
        warn "systemd-vconsole-setup не перезапустился — применяю вручную"
    fi
else
    warn "нет systemctl — применяю setfont (текущая консоль)"
fi
# текущую консоль грузим сразу, не дожидаясь перезапуска
setfont "$FONT" 2>/dev/null || true

# --- Shift+Ctrl+стрелки: выделение колонкой с клавиатуры ---------------
# Консольная раскладка не знает «модификатор+стрелка»: стрелка шлёт
# ESC[A…D всегда одинаково, Shift и Ctrl на неё не влияют. Эмулятор
# понимает xterm-формы ESC[1;6A…D (Shift+Ctrl) и ESC[1;5A…D (Ctrl) —
# дописываем их в действующую раскладку свободными F-кодами: Shift+Ctrl+
# стрелка станет выделением колонкой, Ctrl+стрелка — прыжком по словам.
if [ -n "$KEYMAP" ] && command -v loadkeys >/dev/null 2>&1; then
    KMAP_DIRS="/usr/lib/kbd/keymaps /lib/kbd/keymaps /usr/share/kbd/keymaps"
    KF=$(find $KMAP_DIRS -name "${KEYMAP}.map*" 2>/dev/null | head -1)
    if [ -n "$KF" ]; then
        BUILT=$(mktemp /tmp/ie15-kmap.XXXXXX.map)
        {
            zcat -f "$KF"
            cat <<'IE15EOF'

# 15ИЭ: Shift+Ctrl+стрелки — выделение колонкой (коды xterm для эмулятора)
shift control keycode 103 = F221
shift control keycode 104 = F222
shift control keycode 105 = F223
shift control keycode 106 = F224
string F221 = "\033[1;6A"
string F222 = "\033[1;6B"
string F223 = "\033[1;6D"
string F224 = "\033[1;6C"
IE15EOF
        } > "$BUILT"
        if loadkeys -u -m "$BUILT" >/dev/null 2>&1 \
           && loadkeys -u "$BUILT" >/dev/null 2>&1; then
            say "Shift+Ctrl+стрелки (выделение колонкой) добавлены в раскладку $KEYMAP"
            # карта на будущее: после перезагрузки действует KEYMAP из
            # vconsole.conf без наших добавок — восстановить можно одной
            # командой, пересобирать не нужно
            SAVED="$(dirname "$0")/ie15-keymap.map"
            if cp "$BUILT" "$SAVED" 2>/dev/null; then
                say "собранная раскладка сохранена: $SAVED"
                say "  (после перезагрузки верните её: sudo loadkeys \"$SAVED\")"
                # скрипт идёт от root — файл останется root'у, и ни git,
                # ни оператор читать его не смогут (закоммитить, сравнить).
                # Отдаём тому, кто запускал через sudo; если запускали
                # прямо от root — владельцу каталога репозитория
                OWNER="${SUDO_USER:-$(stat -c '%U' "$(dirname "$0")")}"
                chown "$OWNER" "$SAVED" 2>/dev/null || true
                chmod 644 "$SAVED" 2>/dev/null || true
            fi
        else
            warn "раскладку с Shift+Ctrl+стрелками собрать не удалось —"
            warn "  колонкой выделяйте мышью с Ctrl"
        fi
        rm -f "$BUILT"
    fi
fi

# --- мышь на виртуальных консолях ---------------------------------------
# Выделение текста мышью на консолях: сама консоль ядра события мыши
# терминалу НЕ отдаёт — их перехватывает gpm и превращает в escape-
# последовательности (X10, «ESC [ M» + три байта — их эмулятор разбирает
# и по ним выделяет текст; колесо листает «историю» выдачи). Поэтому
# мышь на консолях — это gpm; без него эмулятор мышь не увидит.
#
# gpm ставится один раз, дальше работает как служба. Если его нет —
# предупреждаем и подсказываем команду (шаг необязательный: без мыши
# остаётся всё, кроме выделения).
MOUSE_DEV=""
for d in /dev/input/mice /dev/input/by-id/*; do
    [ -e "$d" ] || continue
    case "$d" in
        /dev/input/by-id/*-event-kbd) continue ;;   # клавиатура, не мышь
    esac
    MOUSE_DEV="$d"
    break
done

if [ -z "$MOUSE_DEV" ]; then
    warn "мышь не найдена (/dev/input/mice отсутствует) — шаг пропущен"
elif command -v gpm >/dev/null 2>&1; then
    # устройство мыши запоминаем: после перезагрузки gpm сам его не найдёт
    if [ -f /etc/gpm.conf ]; then
        cp -n /etc/gpm.conf /etc/gpm.conf.bak 2>/dev/null || true
        grep -v '^device *=' /etc/gpm.conf > /tmp/ie15-gpm.$$ 2>/dev/null || true
        cat /tmp/ie15-gpm.$$ > /etc/gpm.conf 2>/dev/null || true
        rm -f /tmp/ie15-gpm.$$
        printf 'device=%s\n' "$MOUSE_DEV" >> /etc/gpm.conf
    else
        printf 'device=%s\n' "$MOUSE_DEV" > /etc/gpm.conf
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable gpm >/dev/null 2>&1 || \
            warn "gpm включить при загрузке не удалось — запускаю вручную"
        if systemctl restart gpm >/dev/null 2>&1; then
            say "мышь на консолях включена (gpm, $MOUSE_DEV)"
        else
            gpm -m "$MOUSE_DEV" >/dev/null 2>&1 &
            say "gpm запущен вручную ($MOUSE_DEV)"
        fi
    else
        gpm -m "$MOUSE_DEV" >/dev/null 2>&1 &
        say "gpm запущен ($MOUSE_DEV); при загрузке не поднимется — нет systemd"
    fi
else
    warn "gpm не установлен — выделение мышью на консоли не заработает:"
    warn "  sudo apt install gpm    # Debian/Ubuntu/Fedora/Arch"
    warn "  затем запустите скрипт снова, либо сразу: sudo gpm -m $MOUSE_DEV"
    warn "Второй способ (нужна перезагрузка) — параметр ядра: mouse=1"
fi

say "готово. Перезайдите на консоль, чтобы терминал перерисовался."
echo
echo "Проверка (N — номер консоли, например 3):"
echo "    sudo cat /dev/vcsN > /tmp/vcsN.bin"
echo "    stty size < /dev/ttyN > /tmp/vcsN.size.txt"
echo "    python3 tools/show_console.py /tmp/vcsN.bin /tmp/vcsN.size.txt utf-8"
echo
echo "Мышь на консоли проверяется запуском с --mouse (он включён по"
echo "умолчанию): протяните по тексту — он подсветится, F4 заберёт."
echo "F3 снимает выделение."