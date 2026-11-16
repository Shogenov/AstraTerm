"""GTK file and password dialogs for portable profile backups."""
import datetime
import os

from gi.repository import Gtk

from .backup import EXTENSION, capture_profile, read_backup, restore_profile, write_backup
from .dialogs import confirm, error


def _choose_file(window, saving):
    title = 'Создать резервную копию' if saving else 'Восстановить из резервной копии'
    action = Gtk.FileChooserAction.SAVE if saving else Gtk.FileChooserAction.OPEN
    dialog = Gtk.FileChooserDialog(title=title, transient_for=window, action=action)
    dialog.add_buttons('Отмена', Gtk.ResponseType.CANCEL,
                       'Сохранить' if saving else 'Открыть', Gtk.ResponseType.OK)
    file_filter = Gtk.FileFilter()
    file_filter.set_name('Резервная копия AstraTerm')
    file_filter.add_pattern('*.' + EXTENSION)
    dialog.add_filter(file_filter)
    if saving:
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        dialog.set_current_name('AstraTerm-{}.{}'.format(timestamp, EXTENSION))
    try:
        if dialog.run() != Gtk.ResponseType.OK:
            return None
        filename = dialog.get_filename()
        if saving and not filename.lower().endswith('.' + EXTENSION):
            filename += '.' + EXTENSION
        return filename
    finally:
        dialog.destroy()


def _password(window, fresh):
    dialog = Gtk.Dialog(title='Пароль резервной копии', transient_for=window, modal=True)
    dialog.add_buttons('Отмена', Gtk.ResponseType.CANCEL, 'Продолжить', Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    content = dialog.get_content_area()
    content.set_border_width(16)
    content.set_spacing(10)
    explanation = ('Задайте пароль копии, не менее 10 символов. Он понадобится при восстановлении.'
                   if fresh else 'Введите пароль, указанный при создании резервной копии.')
    note = Gtk.Label(label=explanation, xalign=0)
    note.set_line_wrap(True)
    content.pack_start(note, False, False, 0)
    entry = Gtk.Entry(visibility=False, activates_default=True, placeholder_text='Пароль резервной копии')
    content.pack_start(entry, False, False, 0)
    if fresh:
        repeated = Gtk.Entry(visibility=False, activates_default=True, placeholder_text='Повторите пароль')
        content.pack_start(repeated, False, False, 0)
    dialog.show_all()
    entry.grab_focus()
    try:
        while dialog.run() == Gtk.ResponseType.OK:
            value = entry.get_text()
            if not value or (fresh and len(value) < 10):
                error(dialog, 'Введите пароль резервной копии.',
                      'Используйте не менее 10 символов.' if fresh else '')
            elif fresh and value != repeated.get_text():
                error(dialog, 'Пароли не совпадают.')
            else:
                return value
        return None
    finally:
        dialog.destroy()


def _missing_text(missing):
    lines = missing[:8]
    if len(missing) > len(lines):
        lines = lines + ['Ещё файлов: {}'.format(len(missing) - len(lines))]
    return '\n'.join(lines)


class BackupDialogs(object):
    def __init__(self, window, root):
        self.window = window
        self.root = root
        self.directory = os.path.dirname(window.store.path)

    def create(self, *_):
        try:
            filename = _choose_file(self.window, True)
            if filename is None:
                return
            payload = capture_profile(self.directory)
            if payload['missing'] and not confirm(
                    self.window, 'Сохранить копию без недоступных файлов SSH-ключей?',
                    _missing_text(payload['missing'])):
                return
            master = _password(self.window, True)
            if master is None:
                return
            write_backup(filename, payload, master, self.root)
            self.window.notify('Резервная копия сохранена.')
        except (ValueError, OSError) as exc:
            error(self.window, 'Не удалось создать резервную копию', exc)

    def restore(self, *_):
        try:
            filename = _choose_file(self.window, False)
            if filename is None:
                return
            password = _password(self.window, False)
            if password is None:
                return
            payload = read_backup(filename, password)
            connections = len(payload['documents']['sessions.json']['sessions'])
            detail = ('Подключений: {}. Файлов SSH-ключей: {}.\n\n'
                      'При следующем запуске AstraTerm откроет профиль из копии. '
                      'Текущий профиль сохранится отдельно.').format(connections, len(payload['keys']))
            if 'credentials.vault.json' in payload['documents']:
                detail += '\nДля хранилища понадобится мастер-пароль с исходного компьютера.'
            if payload['missing']:
                detail += '\n\nВ копии отсутствуют файлы SSH-ключей:\n' + _missing_text(payload['missing'])
            if not confirm(self.window, 'Восстановить профиль из резервной копии?', detail):
                return
            restore_profile(self.root, self.directory, payload)
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.INFO,
                                       buttons=Gtk.ButtonsType.CLOSE, text='Профиль восстановлен.')
            dialog.format_secondary_text('Закройте и снова откройте AstraTerm, чтобы загрузить его.')
            try:
                dialog.run()
            finally:
                dialog.destroy()
        except (ValueError, OSError) as exc:
            error(self.window, 'Не удалось восстановить резервную копию', exc)
