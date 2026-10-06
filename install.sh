#!/bin/sh
# Установка эмулятора терминала «Электроника 15ИЭ-00-013» (Linux):
#  - делает ./ie15 исполняемым;
#  - создаёт .desktop-запуск в меню приложений;
#  - кладёт ярлык на рабочий стол (двойной щелчок открывает диалог
#    с БЭСМ-6 в окне терминала).
# Конфигурация подключения — ie15.conf (рядом с этим скриптом).
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
chmod +x "$DIR/ie15"

# Выбор эмулятора терминала и способа передать команду
pick_term() {
    for t in x-terminal-emulator xfce4-terminal mate-terminal qterminal \
             gnome-terminal; do
        command -v "$t" >/dev/null 2>&1 && { echo "$t -- $DIR/ie15"; return; }
    done
    for t in konsole st deepin-terminal xterm; do
        command -v "$t" >/dev/null 2>&1 && { echo "$t -e $DIR/ie15"; return; }
    done
    echo ""
}

EXEC=$(pick_term)
if [ -z "$EXEC" ]; then
    echo "Эмулятор терминала не найден — запуск из консоли: $DIR/ie15" >&2
    EXEC="$DIR/ie15"   # без TTY сеанс всё равно покажет баннер/ошибку
fi

APPDIR="$HOME/.local/share/applications"
mkdir -p "$APPDIR"
DESKTOP_FILE="$APPDIR/ie15.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Электроника 15ИЭ-00-013 (терминал)
Comment=Диалог с БЭСМ-6 по SSH или telnet-линии (ie15.conf)
Exec=$EXEC
Path=$DIR
Terminal=false
Categories=System;TerminalEmulator;
EOF

# Ярлык на рабочий стол (если каталог существует)
DESK=$(xdg-user-dir DESKTOP 2>/dev/null || true)
if [ -z "$DESK" ] || [ ! -d "$DESK" ]; then
    for d in "$HOME/Desktop" "$HOME/Рабочий стол"; do
        [ -d "$d" ] && DESK="$d" && break
    done
fi
if [ -n "${DESK:-}" ] && [ -d "$DESK" ]; then
    cp "$DESKTOP_FILE" "$DESK/Электроника 15ИЭ (терминал).desktop"
    chmod +x "$DESK/Электроника 15ИЭ (терминал).desktop"
    # KDE/Trinity могут требовать подтверждения доверия к ярлыку
    echo "Ярлык: $DESK/Электроника 15ИЭ (терминал).desktop"
fi
command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APPDIR" 2>/dev/null || true
echo "Готово: пункт «Электроника 15ИЭ-00-013 (терминал)» в меню приложений."
