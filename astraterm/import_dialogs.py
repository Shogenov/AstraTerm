"""GTK import preview and master-password lifecycle, separate from session data."""
import os
import re
import threading
from gi.repository import Gtk, GLib
from .dialogs import error, confirm
from .importers import parse_import


class CredentialController(object):
    def __init__(self, window, vault):
        self.window = window; self.vault = vault; self._waiting = []; self._busy = False

    def ensure(self, callback, create=False):
        self.vault.reload_if_changed()
        if self.vault.unlocked:
            callback(); return
        self._waiting.append(callback)
        if self._busy: return
        self._busy = True
        new = not self.vault.exists
        if new and not create:
            self._waiting = []; self._busy = False
            error(self.window, 'Хранилище не создано', 'Сначала импортируйте файл паролей.'); return
        dialog = Gtk.Dialog(title='Создать хранилище' if new else 'Разблокировать хранилище', transient_for=self.window, modal=True)
        dialog.add_buttons('Отмена', Gtk.ResponseType.CANCEL, 'Продолжить', Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area(); box.set_border_width(18); box.set_spacing(10)
        label = Gtk.Label(label=('Создайте мастер-пароль, не менее 10 символов. Он защищает импортированные пароли.' if new else
                                 'Введите мастер-пароль. Разблокировка действует до закрытия или блокировки AstraTerm.'),xalign=0)
        label.set_line_wrap(True); label.set_max_width_chars(65); box.add(label)
        entry = Gtk.Entry(); entry.set_visibility(False); entry.set_activates_default(True); box.add(entry)
        repeated = Gtk.Entry(); repeated.set_visibility(False); repeated.set_placeholder_text('Повторите мастер-пароль')
        repeated.set_activates_default(True)
        if new: box.add(repeated)
        dialog.show_all(); result = dialog.run(); master = entry.get_text(); same = master == repeated.get_text()
        entry.set_text(''); repeated.set_text(''); dialog.destroy()
        if result != Gtk.ResponseType.OK:
            self._waiting = []; self._busy = False; return
        if new and not same:
            self._waiting = []; self._busy = False; error(self.window,'Мастер-пароли не совпадают'); return
        self.window.set_status('Разблокировка хранилища…')
        def worker():
            message = None
            try: self.vault.unlock(master, create=create)
            except Exception as exc: message = str(exc)
            def done():
                self._busy = False; waiting = self._waiting; self._waiting = []
                if self.window.closed:
                    self.vault.lock(); return False
                if message: error(self.window,'Не удалось открыть хранилище AstraTerm',message)
                else:
                    self.window.set_status('Хранилище разблокировано')
                    for task in waiting:
                        try: task()
                        except Exception as exc: error(self.window,'Учётные данные',exc)
                return False
            GLib.idle_add(done)
        thread = threading.Thread(target=worker); thread.daemon = True; thread.start()

    def prepare(self, profile, callback):
        def ready():
            if self.window.closed: return
            try: credential = self.vault.get(profile['credential_profile'],profile.get('protocol'))
            except ValueError as exc:
                error(self.window,'Учётные данные недоступны',exc)
                return
            effective = dict(profile, username=credential['username'] or profile.get('username',''))
            if not effective.get('identity_profile'):effective['identity_profile']='system'
            effective['_credential_provider'] = lambda: self.password_for(effective)
            effective['_credential_prompt'] = lambda prompt: self.answer_prompt(effective,prompt)
            effective['_credential_gate'] = self.gate
            callback(effective)
        self.ensure(ready)

    def gate(self, callback):
        self.vault.reload_if_changed()
        if self.vault.unlocked:return True
        self.ensure(lambda:GLib.idle_add(callback))
        return False

    def password_for(self, profile):
        identifier = profile.get('credential_profile')
        if not identifier:return None
        self.vault.reload_if_changed()
        record=self.vault.get(identifier,profile.get('protocol','ssh'))
        if record['username'] and record['username'] != profile.get('username'):
            raise ValueError('Пользователь профиля изменился. Откройте сохранённое подключение заново.')
        return record['password']

    def answer_prompt(self, profile, prompt):
        # The target password is never reused for gateway, key or OTP prompts.
        if re.search(r'otp|token|code|passphrase|one.?time|verification|challenge|factor|\bpin\b',prompt,re.I): return None
        if re.search(r'password\s*:',prompt,re.I) and 'passphrase' not in prompt.lower():
            host = profile.get('host',''); username = profile.get('username','')
            if host.lower() in prompt.lower() and username and username in prompt:
                return self.password_for(profile)
            return None
        if 'are you sure you want to continue connecting' not in prompt.lower(): return None
        result = []; completed = threading.Event()
        def ask():
            try:
                result.append('yes' if not self.window.closed and confirm(self.window,'Новый ключ SSH-сервера',prompt) else None)
            finally: completed.set()
            return False
        GLib.idle_add(ask)
        if not completed.wait(120): return None
        return result[0] if result else None

    def manage(self, *_):
        if getattr(self,'manager',None) is not None:self.manager.present();return self.manager
        from .credential_dialogs import CredentialManager
        self.manager=CredentialManager(self.window,self)
        return self.manager

    def import_files(self, paths=None):
        if paths is None:
            chooser=Gtk.FileChooserDialog(title='Импорт AstraTerm / MobaXterm / Termius / Ásbrú',parent=self.window,
                action=Gtk.FileChooserAction.OPEN,buttons=('Отмена',Gtk.ResponseType.CANCEL,'Открыть',Gtk.ResponseType.OK))
            chooser.set_select_multiple(True)
            chooser.set_extra_widget(Gtk.Label(label='MobaXterm: выберите Sessions.mxtsessions и Stored Passwords.txt вместе.'))
            result=chooser.run(); paths=chooser.get_filenames(); chooser.destroy()
            if result!=Gtk.ResponseType.OK:return
        self.window.diagnostic_log.write('debug', 'import.preview')
        try: plan=parse_import(paths)
        except Exception as exc:
            self.window.diagnostic_log.write('warning', 'import.preview.failed')
            error(self.window,'Ошибка импорта',exc)
            return
        preview=plan.preview(self.vault.metadata())
        lines=['Подключений: {} · папок: {} · паролей: {}'.format(len(preview['sessions']),preview['groups'],preview['credentials'])]
        if preview['macros'] or preview['tunnels']:lines.append('Макросов: {} · туннелей: {}'.format(preview['macros'],preview['tunnels']))
        if preview['missing']:lines.append('Профилей без импортированного пароля: {}. Ссылки сохранятся для последующего импорта.'.format(preview['missing']))
        for row in preview['sessions']:
            lines.append('{} / {} · {} · {} · {}{}'.format(row['group'],row['name'],row['protocol'].upper(),row['host'],row['credential'] or row['username'],' [пароль отсутствует]' if row['missing'] else ''))
        lines.extend(preview['warnings'])
        dialog=Gtk.Dialog(title='Предпросмотр импорта',transient_for=self.window,modal=True)
        dialog.add_buttons('Отмена',Gtk.ResponseType.CANCEL,'Импортировать',Gtk.ResponseType.OK);dialog.set_default_size(780,460)
        view=Gtk.TextView();view.set_editable(False);view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR);view.get_buffer().set_text('\n'.join(lines))
        scroller=Gtk.ScrolledWindow();scroller.add(view);dialog.get_content_area().add(scroller);dialog.show_all()
        key_combo=Gtk.ComboBoxText();key_combo.append('','Сохранить исходные пути ключей')
        for item in self.window.identity_store.profiles if self.window.identity_store else []:
            key_combo.append(item['id'],item['name'])
        key_combo.set_active_id('');key_combo.set_tooltip_text('SSH-ключ для импортируемых SSH-сессий')
        dialog.get_content_area().pack_end(key_combo,False,False,10);key_combo.show()
        response=dialog.run(); selected_key_id=key_combo.get_active_id() or ''; dialog.destroy()
        if response!=Gtk.ResponseType.OK:return
        def apply():
            if self.window.closed:return
            key_id=selected_key_id
            if key_id:
                for session in plan.sessions:
                    if session['protocol']=='ssh':session.update(identity_profile=key_id,key_file='')
            document=plan.document(self.window.store)
            if plan.credentials:self.vault.merge(list(plan.credentials.values()))
            try:count=self.window.store.import_document(document)
            except Exception:
                if plan.credentials:raise ValueError('Пароли сохранены в хранилище, но подключения сохранить не удалось. Повторите импорт после перечитывания базы.') from None
                raise
            self.window._refresh_sessions();self.window.set_status('Импортировано: {} подключений, {} профилей паролей'.format(count,len(plan.credentials)))
            self.window.diagnostic_log.write('info', 'import.applied')
            plan.credentials.clear()
        if plan.credentials:self.ensure(apply,create=True)
        else:
            try:apply()
            except Exception as exc:error(self.window,'Ошибка импорта',exc)
