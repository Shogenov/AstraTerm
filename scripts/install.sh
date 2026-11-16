#!/bin/sh
# Install AstraTerm without changing user settings or Python alternatives.
set -eu

system_install=0
install_dependencies=0
for argument in "$@"; do
    case "$argument" in
        --system) system_install=1 ;;
        --deps) install_dependencies=1 ;;
        --help|-h)
            printf '%s\n' 'Использование: sh scripts/install.sh [--system] [--deps]' \
                'По умолчанию: установка для текущего пользователя в ~/.local.' \
                '--system: установка в /usr/share и /usr/bin (запустите через sudo).' \
                '--deps: установить GTK/VTE/SSH/SFTP и доступные дополнительные клиенты через apt.'
            exit 0 ;;
        *) printf 'Неизвестный параметр: %s\n' "$argument" >&2; exit 2 ;;
    esac
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(dirname -- "$script_dir")
if [ ! -f "$source_root/astraterm/__main__.py" ]; then
    printf '%s\n' 'Не найден каталог исходников astraterm.' >&2
    exit 1
fi
if [ "$system_install" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Для --system запустите: sudo sh scripts/install.sh --system [--deps]' >&2
    exit 1
fi

if [ "$install_dependencies" -eq 1 ]; then
    if ! command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' 'apt-get не найден. Установите зависимости средствами вашей системы.' >&2
        exit 1
    fi
    if [ "$(id -u)" -eq 0 ]; then
        apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 python3-paramiko openssh-client xauth
    else
        sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 python3-paramiko openssh-client xauth
    fi
    for alternatives in 'telnet' 'screen' 'freerdp2-x11 freerdp-x11' 'tigervnc-viewer xtightvncviewer'; do
        for package in $alternatives; do
            if apt-cache show "$package" >/dev/null 2>&1; then
                if [ "$(id -u)" -eq 0 ]; then
                    if apt-get install -y "$package"; then break; fi
                else
                    if sudo apt-get install -y "$package"; then break; fi
                fi
                printf 'Дополнительный пакет %s не установлен.\n' "$package" >&2
            fi
        done
    done
fi

if [ "$system_install" -eq 1 ]; then
    app_root=/usr/share/astraterm
    launcher=/usr/bin/astraterm
    applications=/usr/share/applications
    icons=/usr/share/icons/hicolor/scalable/apps
else
    app_root=$HOME/.local/opt/astraterm
    launcher=$HOME/.local/bin/astraterm
    applications=$HOME/.local/share/applications
    icons=$HOME/.local/share/icons/hicolor/scalable/apps
fi

install -d "$app_root/astraterm" "$app_root/scripts" "$app_root/share" \
    "$(dirname -- "$launcher")" "$applications" "$icons"
# Source is a flat package. Copy only code/assets, never development credentials,
# interpreter caches, user settings, .git or build output.
for source in "$source_root"/astraterm/*.py "$source_root"/astraterm/*.css; do
    if [ -f "$source" ]; then
        install -m 644 "$source" "$app_root/astraterm/$(basename -- "$source")"
    fi
done
install -m 755 "$source_root/scripts/astraterm" "$app_root/scripts/astraterm"
install -m 755 "$source_root/scripts/astraterm-askpass" "$app_root/scripts/astraterm-askpass"
install -m 644 "$source_root/share/astraterm.svg" "$icons/astraterm.svg"
install -m 644 "$source_root/share/astraterm.svg" "$app_root/share/astraterm.svg"
if [ -f "$source_root/README.md" ]; then
    install -m 644 "$source_root/README.md" "$app_root/README.md"
fi
if [ -d "$source_root/docs" ]; then
    install -d "$app_root/docs"
    for source in "$source_root"/docs/*.md; do
        if [ -f "$source" ]; then
            install -m 644 "$source" "$app_root/docs/$(basename -- "$source")"
        fi
    done
fi

if [ "$system_install" -eq 1 ]; then
    install -m 755 "$source_root/scripts/astraterm" "$launcher"
else
    # Replace our launcher file/symlink only; no recursive removal is needed.
    if [ -d "$launcher" ] && [ ! -L "$launcher" ]; then
        printf 'Путь занят каталогом: %s\n' "$launcher" >&2
        exit 1
    fi
    ln -sfn "$app_root/scripts/astraterm" "$launcher"
fi

desktop_file=$applications/astraterm.desktop
{
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            Exec=*) printf 'Exec="%s"\n' "$launcher" ;;
            Icon=*) printf 'Icon=%s\n' "$icons/astraterm.svg" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$source_root/share/astraterm.desktop"
} > "$desktop_file"
chmod 644 "$desktop_file"

if [ "$system_install" -eq 0 ]; then
    desktop_dir=
    if command -v xdg-user-dir >/dev/null 2>&1; then
        desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null || true)
    fi
    if [ -z "$desktop_dir" ]; then
        desktop_dir=$HOME/Desktop
    fi
    if [ "$desktop_dir" != "$HOME" ]; then
        install -d "$desktop_dir"
        install -m 755 "$desktop_file" "$desktop_dir/astraterm.desktop"
    fi
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications" >/dev/null 2>&1 || true
fi
printf 'AstraTerm установлен. Запуск: %s\n' "$launcher"
printf '%s\n' 'Сохранённые подключения и настройки пользователя сохранены.'
