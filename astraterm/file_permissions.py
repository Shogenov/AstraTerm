"""Edit one SFTP object's POSIX permissions."""
import re
import shlex
import stat
import threading

from ._shared import SFTP_CHMOD_SCRIPT


def parse_mode(text):
    if not re.fullmatch("[0-7]{3,4}", text):
        raise ValueError("Введите три или четыре восьмеричные цифры, например 644.")
    return int(text, 8)


def permission_revision(attributes):
    mode = attributes.st_mode or 0
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise IOError("Права доступны для обычных файлов и папок.")
    return (mode, attributes.st_uid, attributes.st_gid, attributes.st_size, attributes.st_mtime)


def _execute(client, command, password):
    if hasattr(client, 'execute_sudo_command'):
        return client.execute_sudo_command(command, password)
    transport = client.get_transport() if client else None
    if transport is None or not hasattr(transport, 'open_session'):
        raise IOError("Этот SFTP-транспорт не поддерживает команды sudo.")
    channel = transport.open_session(timeout=8)
    timer = threading.Timer(20, channel.close)
    timer.daemon = True
    timer.start()
    try:
        channel.set_combine_stderr(True)
        channel.exec_command(command)
        if password is not None:
            channel.sendall((password + '\n').encode('utf-8'))
        channel.shutdown_write()
        # Sudo and the fixed helper send their diagnostics to stderr.
        output = bytearray()
        while True:
            block = channel.recv(4096)
            if not block:
                return channel.recv_exit_status(), bytes(output)
            output.extend(block)
            if len(output) > 16384:
                raise IOError("Сервер вернул слишком большой ответ sudo.")
    finally:
        timer.cancel()
        channel.close()


def sudo_chmod(client, filename, mode, revision, ask_password, check):
    if any(type(value) is not int for value in revision):
        raise IOError("Сервер не сообщил свойства объекта для проверки sudo.")
    tail = ' '.join(shlex.quote(part) for part in
                    ['/usr/bin/python3', '-IS', '-c', SFTP_CHMOD_SCRIPT, filename, format(mode, '04o')] +
                    [str(value) for value in revision])
    check()
    status, output = _execute(client, 'LC_ALL=C sudo -n -- ' + tail, None)
    if status != 0 and b'sudo: a password is required' in output:
        password = ask_password()
        if password is None:
            return False
        if len(password) > 4096 or re.search('[\x00\r\n]', password):
            raise ValueError("Недопустимый пароль sudo.")
        check()
        status, output = _execute(client, "LC_ALL=C sudo -S -p '' -- " + tail, password)
    if status:
        if status == 55:
            raise IOError("Объект изменился. Откройте права заново.")
        raise IOError("Не удалось применить sudo. Проверьте пароль, разрешение на /usr/bin/python3 и доступ к пути.")
    return True


def permissions_dialog(parent, name, mode):
    from gi.repository import Gtk
    dialog = Gtk.Dialog(title="Права · " + name, transient_for=parent, modal=True)
    dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Применить", Gtk.ResponseType.OK)
    content = dialog.get_content_area()
    content.set_border_width(16)
    content.set_spacing(10)
    grid = Gtk.Grid(column_spacing=12, row_spacing=6)
    content.add(grid)
    for column, title in enumerate(("Чтение", "Запись", "Выполнение"), 1):
        grid.attach(Gtk.Label(label=title), column, 0, 1, 1)
    checks = []
    for row, label in enumerate(("Владелец", "Группа", "Остальные"), 1):
        grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
        for column in range(1, 4):
            check = Gtk.CheckButton()
            check.set_tooltip_text(label + " · " + ("Чтение", "Запись", "Выполнение")[column - 1])
            checks.append(check)
            grid.attach(check, column, row, 1, 1)
    value = Gtk.Entry(text=format(mode, '04o' if mode > 0o777 else '03o'), max_length=4)
    value.set_width_chars(5)
    sudo = Gtk.CheckButton(label="Выполнить через sudo, если у вас есть права sudo")
    symbolic = Gtk.Label(xalign=0)
    grid.attach(Gtk.Label(label="Восьмеричный режим", xalign=0), 0, 4, 2, 1)
    grid.attach(value, 2, 4, 1, 1)
    grid.attach(symbolic, 3, 4, 1, 1)
    content.add(sudo)
    updating = [False]

    def render(*_):
        try:
            current = parse_mode(value.get_text())
        except ValueError:
            symbolic.set_text("—")
            dialog.set_response_sensitive(Gtk.ResponseType.OK, False)
            return
        updating[0] = True
        symbolic.set_text(stat.filemode(stat.S_IFREG | current)[1:])
        for index, check in enumerate(checks):
            check.set_active(bool(current & (1 << (8 - index))))
        updating[0] = False
        dialog.set_response_sensitive(Gtk.ResponseType.OK, True)

    def toggle(*_):
        if updating[0]:
            return
        try:
            special = parse_mode(value.get_text()) & 0o7000
        except ValueError:
            special = mode & 0o7000
        current = special | sum(1 << (8 - index) for index, check in enumerate(checks) if check.get_active())
        value.set_text(format(current, '04o' if special else '03o'))

    value.connect("changed", render)
    for check in checks:
        check.connect("toggled", toggle)
    render()
    dialog.show_all()
    try:
        if dialog.run() == Gtk.ResponseType.OK:
            return parse_mode(value.get_text()), sudo.get_active()
    finally:
        dialog.destroy()
