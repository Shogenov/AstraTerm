"""Per-user SSH identity dialogs; private-key passphrases stay in ssh-agent.

All ssh-agent/ssh-add work runs in a worker.  GTK callbacks receive only public
profile metadata or a short error; a passphrase is never retained in a profile.
"""
from __future__ import unicode_literals

import os
import threading
import uuid

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk, GLib, Pango

from .ssh_profiles import discover_keys, generate_profile, read_public_key


class IdentityCancelled(Exception):
    """The user canceled identity selection or unlocking."""


class IdentityLocked(Exception):
    """The selected identity needs an interactive passphrase."""


_pending = {}


def _run_worker(work, completed):
    def run():
        try:
            result, failure = work(), None
        except Exception as exc:
            result, failure = None, exc
        GLib.idle_add(completed, result, failure)
    worker = threading.Thread(target=run)
    worker.daemon = True
    worker.start()


def _notice(text):
    label = Gtk.Label(label=text, xalign=0)
    label.set_line_wrap(True)
    label.set_max_width_chars(68)
    return label


class IdentityProfileDialog(Gtk.Dialog):
    """Edit public identity metadata and select a file in the user's ~/.ssh."""
    def __init__(self, parent, store, profile=None, first_setup=False, on_saved=None):
        Gtk.Dialog.__init__(self, title="SSH-профиль", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        if first_setup:
            self.add_button("Без профиля", Gtk.ResponseType.REJECT)
        self.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(580, 280)
        self.store = store
        self.profile = dict(profile or {})
        self.on_saved = on_saved
        self._alive = True
        self.connect("destroy", lambda *_: setattr(self, "_alive", False))
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)
        content.pack_start(_notice("Выберите свой приватный SSH-ключ. Разблокированный ключ будет доступен "
                                   "терминалам SSH и SFTP через ssh-agent."), False, False, 0)
        grid = Gtk.Grid(column_spacing=10, row_spacing=9)
        content.pack_start(grid, False, False, 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(self.profile.get("name", "Мой SSH-ключ"))
        grid.attach(Gtk.Label(label="Название", xalign=0), 0, 0, 1, 1)
        grid.attach(self.name_entry, 1, 0, 2, 1)
        self.key_entry = Gtk.Entry()
        self.key_entry.set_hexpand(True)
        self.key_entry.set_text(self.profile.get("key_file", ""))
        self.key_entry.set_placeholder_text("~/.ssh/id_ed25519")
        grid.attach(Gtk.Label(label="Приватный ключ", xalign=0), 0, 1, 1, 1)
        grid.attach(self.key_entry, 1, 1, 1, 1)
        browse = Gtk.Button(label="Обзор…")
        browse.connect("clicked", self._browse)
        grid.attach(browse, 2, 1, 1, 1)
        self.key_combo = Gtk.ComboBoxText()
        self.key_combo.set_hexpand(True)
        self.key_combo.connect("changed", self._selected_key)
        grid.attach(Gtk.Label(label="Ключи ~/.ssh", xalign=0), 0, 2, 1, 1)
        grid.attach(self.key_combo, 1, 2, 2, 1)
        self.default_check = Gtk.CheckButton(label="Использовать по умолчанию для новых подключений")
        self.default_check.set_active(not store.profiles or store.default_id == self.profile.get("id"))
        content.pack_start(self.default_check, False, False, 0)
        self.message = _notice("")
        content.pack_start(self.message, False, False, 0)
        self.connect("response", self._response)
        self.show_all()
        _run_worker(discover_keys, self._keys_found)

    def _keys_found(self, paths, failure):
        if not self._alive:
            return False
        for path in paths or []:
            self.key_combo.append(path, os.path.basename(path))
        if paths and not self.key_entry.get_text().strip():
            self.key_combo.set_active(0)
        if failure:
            self.message.set_text("Не удалось прочитать список ~/.ssh. Выберите файл кнопкой «Обзор». ")
        return False

    def _selected_key(self, combo):
        path = combo.get_active_id()
        if path:
            self.key_entry.set_text(path)

    def _browse(self, *_):
        chooser = Gtk.FileChooserDialog(title="Выберите приватный SSH-ключ", transient_for=self,
                                       action=Gtk.FileChooserAction.OPEN)
        chooser.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Выбрать", Gtk.ResponseType.OK)
        directory = os.path.expanduser("~/.ssh")
        chooser.set_current_folder(directory if os.path.isdir(directory) else os.path.expanduser("~"))
        chooser.set_show_hidden(True)
        current = os.path.expanduser(self.key_entry.get_text().strip())
        if current and os.path.isfile(current):
            chooser.set_filename(current)
        if chooser.run() == Gtk.ResponseType.OK:
            self.key_entry.set_text(chooser.get_filename())
        chooser.destroy()

    def _response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            value = dict(self.profile)
            value.update(id=value.get("id") or uuid.uuid4().hex,
                         name=self.name_entry.get_text().strip(),
                         key_file=self.key_entry.get_text().strip())
            if value["key_file"] != self.profile.get("key_file"):
                value.pop("fingerprint", None)
            try:
                old_default = self.store.default_id
                result = self.store.upsert(value, make_default=self.default_check.get_active())
                if old_default == result["id"] and not self.default_check.get_active():
                    self.store.set_default("")
            except Exception as exc:
                self.message.set_text(str(exc))
                return
            callback = self.on_saved
            self.destroy()
            if callback:
                callback(result, None)
        elif response == Gtk.ResponseType.REJECT:
            try:
                self.store.skip_setup()
            except Exception as exc:
                self.message.set_text(str(exc))
                return
            callback = self.on_saved
            self.destroy()
            if callback:
                callback(None, None)
        else:
            callback = self.on_saved
            self.destroy()
            if callback:
                callback(None, IdentityCancelled())


class GenerateKeyDialog(Gtk.Dialog):
    def __init__(self, parent, store, on_saved, directory=None):
        Gtk.Dialog.__init__(self, title="Создать SSH-ключ", transient_for=parent, modal=True,
                            destroy_with_parent=True)
        self.store, self.on_saved, self.directory = store, on_saved, directory
        self.busy, self.alive = False, True
        self.set_default_size(510, 270)
        self.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Создать", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        content.pack_start(grid, True, True, 0)
        self.algorithm = Gtk.ComboBoxText()
        self.algorithm.append("ed25519", "Ed25519")
        self.algorithm.append("rsa", "RSA 3072")
        self.algorithm.set_active_id("ed25519")
        self.profile_name = Gtk.Entry(text="Мой SSH-ключ", max_length=160)
        self.key_name = Gtk.Entry(text="id_astraterm", max_length=78)
        self.password = Gtk.Entry(visibility=False)
        self.repeat = Gtk.Entry(visibility=False)
        for index, (label, field) in enumerate((("Тип", self.algorithm), ("Название профиля", self.profile_name),
                ("Имя файла в ~/.ssh", self.key_name), ("Парольная фраза", self.password), ("Повтор фразы", self.repeat))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, index, 1, 1)
            field.set_hexpand(True)
            grid.attach(field, 1, index, 1, 1)
        self.password.set_placeholder_text("Рекомендуется для защиты приватного ключа")
        self.message = _notice("")
        content.pack_start(self.message, False, False, 0)
        self.response_handler = self.connect("response", self._respond)
        self.connect("delete-event", lambda *_: self.busy)
        self.connect("destroy", lambda *_: setattr(self, "alive", False))
        self.show_all()

    def _respond(self, dialog, response):
        if self.busy:
            return
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        secret = self.password.get_text()
        if secret != self.repeat.get_text():
            self.message.set_text("Парольные фразы не совпадают.")
            return
        name, filename = self.profile_name.get_text().strip(), self.key_name.get_text().strip()
        if not name:
            self.message.set_text("Введите название SSH-профиля.")
            return
        algorithm = self.algorithm.get_active_id()
        self.password.set_text("")
        self.repeat.set_text("")
        self.busy = True
        self.set_sensitive(False)
        self.message.set_text("Создаём SSH-ключ…")
        _run_worker(lambda: generate_profile(self.store, name, filename, algorithm, secret, self.directory),
                    self._created)

    def _created(self, profile, failure):
        if not self.alive:
            return False
        self.busy = False
        self.set_sensitive(True)
        if failure:
            self.message.set_text(str(failure) if isinstance(failure, ValueError)
                                  else "Не удалось создать SSH-ключ в ~/.ssh.")
            return False
        self.on_saved(profile)
        self.message.set_text("Ключ создан. Добавьте публичный ключ на нужные серверы.")
        public = self.add_button("Копировать публичный ключ", Gtk.ResponseType.APPLY)
        self.get_widget_for_response(Gtk.ResponseType.OK).hide()
        self.get_widget_for_response(Gtk.ResponseType.CANCEL).set_label("Закрыть")
        self.disconnect(self.response_handler)

        def complete(dialog, response):
            if response == Gtk.ResponseType.APPLY:
                try:
                    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(read_public_key(profile["key_file"]), -1)
                    self.message.set_text("Публичный ключ скопирован.")
                except (OSError, ValueError) as exc:
                    self.message.set_text(str(exc))
            else:
                self.destroy()

        self.connect("response", complete)
        public.show()
        return False


class _IdentityRequest(object):
    def __init__(self, parent, store, agent, profile_id, interactive, pending_key):
        self.parent, self.store, self.agent = parent, store, agent
        self.profile_id = profile_id
        self.interactive = interactive
        self.pending_key = pending_key
        self.profile = None
        self.callbacks = []
        self.dialog = None
        self.finished = False
        self._parent_destroy = parent.connect("destroy", lambda *_: self.cancel()) if parent else None

    def start(self):
        self.profile = self.store.get(self.profile_id)
        if self.profile is None:
            if self.profile_id:
                self._finish(None, ValueError("Выбранный SSH-профиль больше не существует."))
            elif self.interactive and not self.store.configured:
                self.dialog = IdentityProfileDialog(self.parent, self.store, first_setup=True,
                                                    on_saved=self._chosen)
            else:
                self._finish(None, None)
            return False
        self._check()
        return False

    def _chosen(self, profile, failure):
        self.dialog = None
        if failure is not None or profile is None:
            self._finish(None, failure)
        else:
            self.profile = profile
            key = (id(self.parent), id(self.store), profile["id"])
            if key != self.pending_key and key not in _pending:
                _pending.pop(self.pending_key, None)
                self.pending_key = key
                _pending[key] = self
            self._check()

    def _check(self):
        profile = dict(self.profile)
        def work():
            self.agent.ensure()
            loaded = self.agent.loaded_identity(profile["key_file"], profile.get("fingerprint", ""))
            if loaded is not None:
                return loaded
            if not os.path.isfile(os.path.expanduser(profile["key_file"])):
                raise ValueError("Файл SSH-ключа не найден. Измените путь в профиле.")
            try:
                return self.agent.unlock(profile["key_file"], "")
            except Exception:
                return None
        _run_worker(work, self._checked)

    def _checked(self, identity, failure):
        if self.finished:
            return False
        if failure is not None:
            self._finish(None, failure)
        elif identity is not None:
            self._unlocked(identity, None)
        elif not self.interactive:
            self._finish(None, IdentityLocked("SSH-ключ нужно разблокировать в профиле."))
        else:
            self._prompt()
        return False

    def _prompt(self, retry=False):
        if self.finished:
            return
        dialog = Gtk.Dialog(title="Разблокировать SSH-ключ", transient_for=self.parent, modal=True)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Разблокировать", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)
        content.pack_start(_notice(self.profile["name"] + "\n" + self.profile["key_file"]), False, False, 0)
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_placeholder_text("Passphrase приватного ключа")
        entry.set_activates_default(True)
        content.pack_start(entry, False, False, 0)
        content.pack_start(_notice("Passphrase вводится один раз, пока ключ загружен в ssh-agent."),
                           False, False, 0)
        if retry:
            content.pack_start(_notice("Ключ не разблокирован. Проверьте passphrase и выбранный файл."),
                               False, False, 0)
        self.dialog = dialog
        self.passphrase_entry = entry
        def response(unused, result):
            secret = entry.get_text() if result == Gtk.ResponseType.OK else ""
            entry.set_text("")
            self.dialog = None
            dialog.destroy()
            if self.finished:
                return
            if result != Gtk.ResponseType.OK:
                self._finish(None, IdentityCancelled())
                return
            key_file = self.profile["key_file"]
            _run_worker(lambda: self.agent.unlock(key_file, secret), self._unlocked)
        dialog.connect("response", response)
        dialog.show_all()
        entry.grab_focus()

    def _unlocked(self, identity, failure):
        if self.finished:
            return False
        if failure is not None:
            # Never present a backend exception from a passphrase transaction.
            self._prompt(retry=True)
            return False
        try:
            value = dict(self.profile)
            if identity and identity.get("fingerprint"):
                value["fingerprint"] = identity["fingerprint"]
            if value != self.profile:
                self.profile = self.store.upsert(value)
        except Exception:
            self._finish(None, ValueError("Ключ разблокирован, но данные профиля не удалось сохранить."))
        else:
            self._finish(dict(self.profile), None)
        return False

    def _finish(self, profile, failure):
        if self.finished:
            return
        self.finished = True
        if self.dialog is not None:
            dialog, self.dialog = self.dialog, None
            dialog.destroy()
        if self._parent_destroy is not None:
            try:
                self.parent.disconnect(self._parent_destroy)
            except (TypeError, RuntimeError):
                pass
            self._parent_destroy = None
        _pending.pop(self.pending_key, None)
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            if callback:
                callback(profile, failure)

    def cancel(self):
        self._finish(None, IdentityCancelled())


def ensure_identity(parent, store, agent, profile_id=None, callback=None, interactive=True):
    """Resolve/unlock one identity asynchronously, coalescing duplicate requests.

    The callback receives ``(profile, error)``. ``(None, None)`` means the user
    chose system SSH settings. An IdentityCancelled error aborts the pending
    connection. The returned request has cancel() and its current GTK dialog.
    """
    key = (id(parent), id(store), profile_id or store.default_id or "")
    pending = _pending.get(key)
    if pending is not None:
        pending.interactive = pending.interactive or interactive
        pending.callbacks.append(callback)
        return pending
    request = _IdentityRequest(parent, store, agent, profile_id, interactive, key)
    request.callbacks.append(callback)
    _pending[key] = request
    GLib.idle_add(request.start)
    return request


class ProfileManager(Gtk.Dialog):
    def __init__(self, parent, store, agent, on_changed=None):
        Gtk.Dialog.__init__(self, title="Профили SSH-ключей", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        self.set_default_size(690, 380)
        self.store, self.agent, self.on_changed = store, agent, on_changed
        self._alive = True
        self.connect("destroy", lambda *_: setattr(self, "_alive", False))
        self.connect("response", lambda *_: self.destroy())
        content = self.get_content_area()
        content.set_border_width(14)
        content.set_spacing(10)
        content.pack_start(_notice("Профили принадлежат текущему пользователю Linux. "
                                   "SSH и SFTP используют ключи, разблокированные в ssh-agent."),
                           False, False, 0)
        self.model = Gtk.ListStore(str, str, str, str)
        self.tree = Gtk.TreeView(model=self.model)
        for title, column in (("Профиль", 1), ("Приватный ключ", 2), ("", 3)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
            view_column = Gtk.TreeViewColumn(title, renderer, text=column)
            view_column.set_expand(column == 2)
            self.tree.append_column(view_column)
        self.tree.connect("row-activated", lambda *_: self._edit())
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree)
        content.pack_start(scroll, True, True, 0)
        actions = Gtk.Box(spacing=6)
        for label, callback in (("Добавить", self._add), ("Изменить", self._edit),
                                ("Удалить", self._delete), ("По умолчанию", self._default),
                                ("Разблокировать", self._unlock)):
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            actions.pack_start(button, False, False, 0)
        content.pack_start(actions, False, False, 0)
        system = Gtk.Button(label="Использовать системный ssh-agent без профиля по умолчанию")
        system.connect("clicked", self._system)
        content.pack_start(system, False, False, 0)
        self.message = _notice("")
        content.pack_start(self.message, False, False, 0)
        self.refresh()
        self.show_all()

    def refresh(self, selected=None):
        if not self._alive:
            return
        self.model.clear()
        for item in self.store.profiles:
            row = self.model.append([item["id"], item["name"], item["key_file"],
                                     "По умолчанию" if self.store.default_id == item["id"] else ""])
            if item["id"] == selected:
                self.tree.get_selection().select_iter(row)

    def _selected(self):
        model, iterator = self.tree.get_selection().get_selected()
        return self.store.get(model[iterator][0]) if iterator is not None else None

    def _changed(self, profile=None, failure=None):
        if not self._alive:
            return
        if failure is not None:
            if not isinstance(failure, IdentityCancelled):
                self.message.set_text(str(failure))
            return
        self.refresh(profile["id"] if profile else None)
        if self.on_changed:
            self.on_changed()

    def _add(self, *_):
        IdentityProfileDialog(self, self.store, on_saved=self._changed)

    def _edit(self, *_):
        selected = self._selected()
        if selected:
            IdentityProfileDialog(self, self.store, selected, on_saved=self._changed)

    def _delete(self, *_):
        selected = self._selected()
        if selected:
            try:
                self.store.delete(selected["id"])
            except Exception as exc:
                self.message.set_text(str(exc))
            else:
                self._changed()

    def _default(self, *_):
        selected = self._selected()
        if selected:
            try:
                self.store.set_default(selected["id"])
            except Exception as exc:
                self.message.set_text(str(exc))
            else:
                self._changed(selected)

    def _system(self, *_):
        try:
            self.store.set_default("")
            self.store.skip_setup()
        except Exception as exc:
            self.message.set_text(str(exc))
        else:
            self._changed()

    def _unlock(self, *_):
        selected = self._selected()
        if selected:
            self.message.set_text("Проверка SSH-ключа…")
            def completed(profile, failure):
                if not self._alive:
                    return
                self.message.set_text("Ключ готов для SSH и SFTP." if failure is None else "")
                self._changed(profile, failure)
            ensure_identity(self, self.store, self.agent, selected["id"], completed)


def manage_profiles(parent, store, agent, on_changed=None):
    """Show a nonblocking modal profile manager and return its dialog."""
    return ProfileManager(parent, store, agent, on_changed)
