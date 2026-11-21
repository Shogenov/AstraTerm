"""Session and command dialogs. No credentials are persisted."""
import copy
import os
import uuid

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from .core import default_session, validate_session


PROTOCOLS = [("ssh", "SSH / SFTP"), ("local", "Локальный терминал"), ("telnet", "Telnet"),
             ("serial", "Serial / COM"), ("rdp", "RDP"), ("vnc", "VNC"), ("mosh", "Mosh")]


def error(parent, text, detail=""):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.ERROR,
                               buttons=Gtk.ButtonsType.CLOSE, text=text)
    if detail:
        dialog.format_secondary_text(str(detail))
    dialog.run()
    dialog.destroy()


def confirm(parent, text, detail=""):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
    if detail:
        dialog.format_secondary_text(detail)
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK


def name_dialog(parent, title, initial="", secret=False):
    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    entry = Gtk.Entry(text=initial, activates_default=True, visibility=not secret)
    if secret:
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    content = dialog.get_content_area()
    content.set_border_width(16)
    content.add(entry)
    dialog.show_all()
    entry.grab_focus()
    entry.select_region(0, -1)
    answer = entry.get_text() if dialog.run() == Gtk.ResponseType.OK else None
    if secret:
        entry.set_text("")
    dialog.destroy()
    return answer


class SessionDialog(Gtk.Dialog):
    def __init__(self, parent, session=None, identity_profiles=None, session_store=None, credential_profiles=None):
        Gtk.Dialog.__init__(self, title="Настройка подключения", transient_for=parent, modal=True)
        self.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(560, 570)
        self.profile = copy.deepcopy(session or default_session())
        self.session_store = session_store
        self._inherited_fields = set()
        self._own_ssh_values = {}
        self.fields = {}
        content = self.get_content_area()
        content.set_spacing(12)
        content.set_border_width(18)
        title = Gtk.Label(label="ПОДКЛЮЧЕНИЕ", xalign=0)
        title.get_style_context().add_class("eyebrow")
        content.pack_start(title, False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        self._entry(grid, 0, "Название", "name")
        groups = Gtk.ComboBoxText.new_with_entry()
        for group in session_store.groups if session_store is not None else []:
            groups.append_text(group["name"])
        groups.get_child().set_text(self.profile.get("group", ""))
        groups.get_child().set_placeholder_text("Выберите папку или введите новую")
        groups.set_hexpand(True)
        self.fields["group"] = groups
        grid.attach(Gtk.Label(label="Группа", xalign=0), 0, 1, 1, 1)
        grid.attach(groups, 1, 1, 1, 1)
        combo = Gtk.ComboBoxText()
        for key, name in PROTOCOLS:
            combo.append(key, name)
        combo.set_active_id(self.profile.get("protocol", "ssh"))
        self.fields["protocol"] = combo
        grid.attach(Gtk.Label(label="Протокол", xalign=0), 0, 2, 1, 1)
        grid.attach(combo, 1, 2, 1, 1)
        self._entry(grid, 3, "Сервер", "host", "server.example.org")
        self._entry(grid, 4, "Порт", "port")
        self._entry(grid, 5, "Пользователь", "username")
        inherit = Gtk.CheckButton(label="Использовать общие SSH-настройки группы")
        inherit.set_active(bool(self.profile.get("inherit_group_ssh", False)))
        inherit.set_tooltip_text("Сервер и команда остаются индивидуальными. Общие параметры задаются в настройках папки.")
        self.fields["inherit_group_ssh"] = inherit
        notebook = Gtk.Notebook()
        self.notebook = notebook
        content.pack_start(notebook, True, True, 0)
        general = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)
        general.pack_start(grid, False, False, 0)
        general.pack_start(inherit, False, False, 0)
        notebook.append_page(general, Gtk.Label(label="Подключение"))
        ssh = Gtk.Grid(column_spacing=12, row_spacing=9, margin=14)
        self._entry(ssh, 0, "SSH-ключ", "key_file", "Из профиля или ~/.ssh/id_rsa")
        self._entry(ssh, 1, "Jump host", "jump_host", "user@gateway:22")
        self._entry(ssh, 2, "Команда", "remote_command", "Необязательно")
        for row, key, label in [(3, "x11", "Проброс X11 (-X)"), (4, "compression", "Сжатие SSH (-C)")]:
            button = Gtk.CheckButton(label=label)
            button.set_active(bool(self.profile.get(key)))
            self.fields[key] = button
            ssh.attach(button, 0, row, 2, 1)
        note = Gtk.Label(label="Профиль разблокирует ключ через ssh-agent один раз за сеанс Linux.\nКлючи серверов проверяются OpenSSH.", xalign=0)
        note.get_style_context().add_class("muted")
        ssh.attach(note, 0, 5, 2, 1)
        identities = Gtk.ComboBoxText()
        identities.append("", "Профиль по умолчанию")
        identities.append("system", "Системные ключи / свой файл")
        available = {"", "system"}
        for identity in identity_profiles or []:
            identities.append(identity["id"], identity["name"])
            available.add(identity["id"])
        selected = self.profile.get("identity_profile", "")
        if selected not in available:
            identities.append(selected, "Профиль недоступен: " + selected)
        identities.set_active_id(selected)
        self.fields["identity_profile"] = identities
        ssh.attach(Gtk.Label(label="Профиль ключа", xalign=0), 0, 6, 1, 1)
        ssh.attach(identities, 1, 6, 1, 1)
        self._identity_ids = available
        credentials = Gtk.ComboBoxText()
        self._credential_profiles = credential_profiles or []
        self._credential_names = {item['id']:item['name'] for item in credential_profiles or []}
        credentials.append('', 'Без профиля пароля')
        credential_ids = {''}
        for credential in credential_profiles or []:
            credentials.append(credential['id'],credential['name']+' · '+credential['username'])
            credential_ids.add(credential['id'])
        selected_credential = self.profile.get('credential_profile','')
        if selected_credential not in credential_ids:
            credentials.append(selected_credential,self.profile.get('credential_label') or 'Профиль в хранилище')
        credentials.set_active_id(selected_credential)
        self.fields['credential_profile'] = credentials
        grid.attach(Gtk.Label(label='Учётные данные',xalign=0),0,6,1,1)
        grid.attach(credentials,1,6,1,1)
        if self.profile.get('import_warning'):
            warning=Gtk.Label(label=self.profile['import_warning'],xalign=0); warning.set_line_wrap(True); warning.set_max_width_chars(65)
            other_warning=Gtk.CheckButton(label='Параметры проверены; снять блокировку импорта')
            other_warning.connect('toggled',lambda widget:self.profile.update(import_warning='' if widget.get_active() else session.get('import_warning','')))
            ssh.attach(warning,0,8,2,1);ssh.attach(other_warning,0,9,2,1)
        def identity_changed(widget):
            self.fields["key_file"].set_sensitive(widget.get_active_id() in ("", "system") and
                                                   "key_file" not in self._inherited_fields)
            selected=widget.get_active_id()
            item=next((p for p in identity_profiles or [] if p['id']==selected),None)
            self.identity_note.set_text('Используется ключ профиля: '+(item['key_file'] if item else 'профиль недоступен') if selected not in ('','system',None) else
                                       'Используется свой файл: '+self.fields['key_file'].get_text() if self.fields['key_file'].get_text() else
                                       'Используется системный SSH-agent.' if selected=='system' else 'Используется профиль SSH по умолчанию.')
        self.identity_note=Gtk.Label(xalign=0);self.identity_note.set_line_wrap(True);self.identity_note.set_max_width_chars(64)
        ssh.attach(self.identity_note,0,7,2,1)
        identities.connect("changed", identity_changed)
        self.fields['key_file'].connect('changed',lambda *_:identity_changed(identities))
        identity_changed(identities)
        notebook.append_page(ssh, Gtk.Label(label="SSH"))
        other = Gtk.Grid(column_spacing=12, row_spacing=9, margin=14)
        self._entry(other, 0, "Рабочая папка", "startup_directory", "~")
        self._entry(other, 1, "Устройство", "serial_device", "/dev/ttyUSB0")
        self._entry(other, 2, "Скорость serial", "baud", "115200")
        self._entry(other, 3, "Домен RDP", "rdp_domain")
        self._entry(other, 4, "Размер RDP", "rdp_resolution", "1280x800")
        self._entry(other, 5, "Кодировка", "encoding", "UTF-8")
        note = Gtk.Label(label="RDP открывается во вкладке, VNC — в системном клиенте.\nMosh требует установленного клиента и сервера.", xalign=0)
        note.get_style_context().add_class("muted")
        other.attach(note, 0, 6, 2, 1)
        notebook.append_page(other, Gtk.Label(label="Дополнительно"))
        self.old_protocol = self.profile.get("protocol", "ssh")
        combo.connect("changed", self._protocol_changed)
        self._protocol_changed(combo)
        groups.get_child().connect("changed", self._group_settings_changed)
        inherit.connect("toggled", self._group_settings_changed)
        self._group_settings_changed()
        self.show_all()

    def _entry(self, grid, row, label, key, placeholder=""):
        widget = Gtk.Entry()
        widget.set_hexpand(True)
        value = self.profile.get(key)
        widget.set_text(str(value) if value is not None else "")
        widget.set_placeholder_text(placeholder)
        widget.set_activates_default(True)
        self.fields[key] = widget
        grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _protocol_changed(self, combo):
        for name in self._inherited_fields:
            self._write_field(name, self._own_ssh_values[name])
        self._inherited_fields.clear()
        key = combo.get_active_id()
        defaults = {"ssh": 22, "mosh": 22, "telnet": 23, "rdp": 3389, "vnc": 5900}
        previous = defaults.get(self.old_protocol, 22)
        if self.fields["port"].get_text() in (str(previous), ""):
            self.fields["port"].set_text(str(defaults.get(key, 22)))
        self.old_protocol = key
        remote = key not in ("local", "serial")
        self.fields["host"].set_sensitive(remote)
        self.fields["port"].set_sensitive(remote)
        self.fields["username"].set_sensitive(key in ("ssh", "mosh", "rdp"))
        credentials=self.fields['credential_profile'];selected=credentials.get_active_id() or ''
        credentials.remove_all();credentials.append('','Без профиля пароля');available={''}
        for item in self._credential_profiles:
            if item.get('protocol','both') in ('both',key):
                credentials.append(item['id'],('SSH/RDP' if item.get('protocol','both')=='both' else item['protocol'].upper())+' · '+item['name']+' · '+item['username']);available.add(item['id'])
        # Preserve a missing/locked profile only for the original protocol.
        if selected and selected not in available and key==self.profile.get('protocol') and selected not in {item['id'] for item in self._credential_profiles}:
            credentials.append(selected,self.profile.get('credential_label') or 'Профиль в хранилище');available.add(selected)
        credentials.set_active_id(selected if selected in available else '')
        credentials.set_sensitive(key in ('ssh','rdp'))
        if "encoding" in self.fields:
            self._group_settings_changed()

    def _read_field(self, name):
        widget = self.fields[name]
        if name == "group":
            return widget.get_child().get_text().strip()
        if isinstance(widget, Gtk.CheckButton):
            return widget.get_active()
        if isinstance(widget, Gtk.ComboBoxText):
            return widget.get_active_id() or ""
        return widget.get_text().strip()

    def _write_field(self, name, value):
        widget = self.fields[name]
        if isinstance(widget, Gtk.CheckButton):
            widget.set_active(bool(value))
        elif isinstance(widget, Gtk.ComboBoxText):
            if name == "identity_profile" and value not in self._identity_ids:
                widget.append(value, "Профиль недоступен: " + value)
                self._identity_ids.add(value)
            widget.set_active_id(value)
        else:
            widget.set_text(str(value))

    def _group_settings_changed(self, *_):
        names = ("username", "port", "key_file", "identity_profile", "jump_host", "x11", "compression", "encoding")
        for name in names:
            if name not in self._inherited_fields:
                self._own_ssh_values[name] = self._read_field(name)
        group = self._read_field("group")
        protocol = self._read_field("protocol")
        active = bool(group) and protocol in ("ssh", "mosh")
        self.fields["inherit_group_ssh"].set_sensitive(active)
        inherited = {}
        if active and self.fields["inherit_group_ssh"].get_active() and self.session_store is not None:
            inherited = self.session_store.group_ssh_settings(group)
        self._inherited_fields = set(inherited)
        for name in names:
            self._write_field(name, inherited.get(name, self._own_ssh_values[name]))
            allowed = protocol in ("ssh", "mosh") or name not in ("username", "port")
            if name == "username":
                allowed = protocol in ("ssh", "mosh", "rdp")
            elif name == "port":
                allowed = protocol not in ("local", "serial")
            self.fields[name].set_sensitive(allowed and name not in inherited)
            self.fields[name].set_tooltip_text("Из настроек группы «{}»".format(group) if name in inherited else None)
        if self._read_field("identity_profile") not in ("", "system"):
            self.fields["key_file"].set_sensitive(False)

    def get_session(self):
        result = dict(self.profile)
        for key in self.fields:
            result[key] = self._own_ssh_values[key] if key in self._inherited_fields else self._read_field(key)
        identifier=result.get('credential_profile','')
        result['credential_label']=self._credential_names.get(identifier,
            self.profile.get('credential_label','') if identifier==self.profile.get('credential_profile') else '') if identifier else ''
        return validate_session(result)


class MacroDialog(Gtk.Dialog):
    def __init__(self, parent, store, send_callback):
        Gtk.Dialog.__init__(self, title="Макросы", transient_for=parent, modal=True)
        self.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        self.set_default_size(660, 420)
        self.store = store
        self.send_callback = send_callback
        box = self.get_content_area()
        box.set_border_width(14)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(paned, True, True, 0)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.listbox = Gtk.ListBox()
        self.listbox.set_size_request(200, -1)
        self.listbox.connect("row-selected", self._selected)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.listbox)
        left.pack_start(scroll, True, True, 0)
        controls = Gtk.Box(spacing=4)
        for label, callback in [("+", self._new), ("Удалить", self._delete)]:
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            controls.pack_start(button, True, True, 0)
        left.pack_end(controls, False, False, 0)
        paned.pack1(left, False, False)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_left=12)
        self.name = Gtk.Entry()
        self.name.set_placeholder_text("Название макроса")
        right.pack_start(self.name, False, False, 0)
        self.text = Gtk.TextView()
        self.text.set_monospace(True)
        self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.text)
        right.pack_start(scroll, True, True, 0)
        self.newline = Gtk.CheckButton(label="Добавить Enter после команды")
        self.newline.set_active(True)
        right.pack_start(self.newline, False, False, 0)
        controls = Gtk.Box(spacing=6)
        for label, callback in [("Сохранить", self._save), ("Выполнить…", self._run)]:
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            controls.pack_start(button, True, True, 0)
        right.pack_end(controls, False, False, 0)
        paned.pack2(right, True, False)
        self.current_id = None
        self._refresh()
        self.show_all()

    def _refresh(self):
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        for macro in self.store.macros:
            row = Gtk.ListBoxRow()
            row.macro = macro
            label = Gtk.Label(label=macro["name"], xalign=0, margin=8)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            row.add(label)
            self.listbox.add(row)
        self.listbox.show_all()

    def _selected(self, _, row):
        if row is None:
            return
        item = row.macro
        self.current_id = item["id"]
        self.name.set_text(item["name"])
        self.text.get_buffer().set_text(item["text"])
        self.newline.set_active(item.get("append_newline", True))

    def _new(self, *_):
        self.listbox.unselect_all()
        self.current_id = None
        self.name.set_text("")
        self.text.get_buffer().set_text("")
        self.newline.set_active(True)
        self.name.grab_focus()

    def _current(self):
        buffer_ = self.text.get_buffer()
        return {"id": self.current_id or uuid.uuid4().hex, "name": self.name.get_text().strip(),
                "text": buffer_.get_text(buffer_.get_start_iter(), buffer_.get_end_iter(), True),
                "append_newline": self.newline.get_active()}

    def _save(self, *_):
        item = self._current()
        if not item["name"] or not item["text"]:
            error(self, "Укажите название и команду")
            return
        original = list(self.store.macros)
        self.store.macros = [x for x in self.store.macros if x["id"] != item["id"]] + [item]
        try:
            self.store.save()
        except Exception as exc:
            self.store.macros = original
            error(self, "Не удалось сохранить макрос", exc)
            return
        self.current_id = item["id"]
        self._refresh()

    def _delete(self, *_):
        if self.current_id and confirm(self, "Удалить макрос?"):
            original = list(self.store.macros)
            self.store.macros = [x for x in self.store.macros if x["id"] != self.current_id]
            try:
                self.store.save()
            except Exception as exc:
                self.store.macros = original
                error(self, "Ошибка сохранения", exc)
            self._new()
            self._refresh()

    def _run(self, *_):
        item = self._current()
        if not item["text"]:
            return
        if confirm(self, "Выполнить макрос в активном терминале?", item["text"]):
            self.send_callback(item["text"] + ("\n" if item["append_newline"] else ""))
