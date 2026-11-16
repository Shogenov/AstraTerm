"""GTK update workflow; network and package installation stay off the UI thread."""
import os
import shutil
import subprocess
import threading
from gi.repository import Gtk, GLib
from . import updates
from .dialogs import confirm


class UpdateController(object):
    def __init__(self, window):
        self.window = window
        self.dialog = None
        self.busy = False

    def check(self, automatic=False):
        if self.busy or self.window.closed:
            return False
        if self.dialog:
            self.dialog.present()
            return False
        if not automatic:
            self._show('Проверяем обновления…')
        self.busy = True
        def work():
            try:
                result, failure = updates.check(), None
            except Exception:
                result, failure = None, 'Не удалось получить проверенный список обновлений. Проверьте подключение к сайту и повторите попытку.'
            GLib.idle_add(self._checked, result, failure, automatic)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _show(self, message):
        self.dialog = Gtk.Dialog(title='Обновления AstraTerm', transient_for=self.window, modal=True,
                                 destroy_with_parent=True)
        self.dialog.set_default_size(480, 220)
        box = self.dialog.get_content_area()
        box.set_border_width(22)
        self.label = Gtk.Label(label=message)
        self.label.set_line_wrap(True)
        self.label.set_max_width_chars(60)
        self.label.set_selectable(True)
        box.pack_start(self.label, True, True, 10)
        self.progress = Gtk.ProgressBar(show_text=True, no_show_all=True)
        box.pack_start(self.progress, False, False, 8)
        self.action = self.dialog.add_button('Скачать обновление', Gtk.ResponseType.ACCEPT)
        self.action.set_sensitive(False)
        self.close_button = self.dialog.add_button('Закрыть', Gtk.ResponseType.CLOSE)
        self.dialog.connect('response', self._response)
        self.dialog.show_all()

    def _checked(self, item, failure, automatic):
        self.busy = False
        if self.window.closed:
            return False
        if automatic and (failure or not item['available']):
            return False
        if not self.dialog:
            if not automatic:
                return False
            self._show('')
        self.item, self.filename = item, None
        self.label.set_text(failure or ('Доступна версия ' + item['version'] + '\n\n' + item['notes'] if item['available'] else 'Установлена актуальная версия AstraTerm ' + updates.VERSION + '.'))
        self.action.set_sensitive(bool(item and item['available']))
        return False

    def _response(self, dialog, response):
        if response != Gtk.ResponseType.ACCEPT:
            if not self.close_button.get_sensitive():
                return
            self._remove_package()
            dialog.destroy()
            self.dialog = None
            return
        if self.busy:
            return
        if self.filename:
            if not confirm(self.window, 'Установить AstraTerm ' + self.item['version'] + '?',
                           'Системный установщик запросит права администратора. После установки перезапустите AstraTerm, завершив активные подключения.'):
                return
            installer = shutil.which('gdebi-gtk')
            command = [installer, self.filename] if installer else [shutil.which('pkexec') or '/usr/bin/pkexec', '/usr/bin/dpkg', '-i', self.filename]
            self._job(lambda: subprocess.check_call(command), installing=True)
        else:
            directory = os.path.join(os.path.dirname(self.window.store.path), 'updates')
            self._download(directory)

    def _download(self, directory):
        self.progress.set_fraction(0)
        self.progress.set_text('0 %')
        self.progress.show()
        self.close_button.set_sensitive(False)
        last = [0]

        def progress(received, total):
            percent = received * 100 // total
            if percent != last[0]:
                last[0] = percent
                GLib.idle_add(self._show_progress, percent)

        item = self.item
        self._job(lambda: updates.download(item, directory, progress=progress))

    def _show_progress(self, percent):
        if not self.window.closed and self.dialog and self.progress.get_visible():
            self.progress.set_fraction(percent / 100)
            self.progress.set_text('{} %'.format(percent))
            if percent == 100:
                self.label.set_text('Проверяем размер и SHA-256 пакета…')
        return False

    def _job(self, operation, installing=False):
        self.busy = True
        self.action.set_sensitive(False)
        self.close_button.set_sensitive(False)
        self.label.set_text('Устанавливаем обновление…' if installing else 'Скачиваем AstraTerm ' + self.item['version'] + '…')
        def work():
            try:
                result, failure = operation(), None
            except Exception:
                result, failure = None, ('Установка отменена или не выполнена. Можно повторить попытку.' if installing else 'Не удалось скачать и проверить обновление. Повторите попытку.')
            GLib.idle_add(self._finished, result, failure, installing)
        threading.Thread(target=work, daemon=True).start()

    def _finished(self, result, failure, installing):
        self.busy = False
        if self.window.closed or not self.dialog:
            if not installing and result and os.path.isfile(result):
                os.unlink(result)
            return False
        self.progress.hide()
        self.close_button.set_sensitive(True)
        if failure:
            self.label.set_text(failure)
            self.action.set_sensitive(True)
        elif installing:
            self.label.set_text('Обновление установлено. Закройте AstraTerm и откройте его снова, чтобы использовать новую версию.')
            self._remove_package()
            self.action.set_sensitive(False)
        else:
            self.filename = result
            self.label.set_text('Версия ' + self.item['version'] + ' скачана. Подпись списка релизов и SHA-256 пакета проверены.\n\nУстановка потребует системного подтверждения.')
            self.action.set_label('Запустить установщик')
            self.action.set_sensitive(True)
        return False

    def _remove_package(self):
        filename, self.filename = getattr(self, 'filename', None), None
        if filename:
            try:
                os.unlink(filename)
            except OSError:
                pass
