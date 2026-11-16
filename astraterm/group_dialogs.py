"""Folder creation and shared SSH settings; no credentials are collected here."""
from __future__ import unicode_literals

import copy

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from .core import default_session, normalize_group_name, validate_group


_FIELDS = (("username", "Пользователь"), ("port", "Порт SSH"),
           ("identity_profile", "Профиль ключа"), ("key_file", "Файл ключа"),
           ("jump_host", "SSH-шлюз"), ("x11", "Проброс X11"),
           ("compression", "Сжатие SSH"), ("encoding", "Кодировка"))


def _note(text):
    label = Gtk.Label(label=text, xalign=0)
    label.set_line_wrap(True)
    label.set_max_width_chars(66)
    label.get_style_context().add_class("muted")
    return label


class GroupDialog(Gtk.Dialog):
    """Return a group record for the caller's single atomic store update.

    ``get_group()`` validates public metadata only. ``apply_to_sessions`` is
    explicit consent to enable group inheritance on existing SSH/Mosh sessions
    in this folder and its descendants; the default leaves that choice alone.
    """
    def __init__(self, parent, store, group=None, parent_group="", identity_profiles=None):
        Gtk.Dialog.__init__(self, title="Настройки папки" if group else "Новая папка",
                            transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(590, min(650, max(480, self.get_screen().get_height() - 100)))
        self.store = store
        self.group = copy.deepcopy(group or {})
        self.setting_checks, self.ssh_fields = {}, {}
        self._saved_overrides = dict(self.group.get("ssh", {}))
        self._defaults = default_session("ssh")
        original_name = self.group.get("name", "")
        original_parent, unused, leaf = original_name.rpartition("/")
        selected_parent = original_parent if group else parent_group
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(12)
        notebook = Gtk.Notebook()
        self.notebook = notebook
        content.pack_start(notebook, True, True, 0)

        folder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        folder.set_border_width(15)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        folder.pack_start(grid, False, False, 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(leaf if group else "")
        self.name_entry.set_placeholder_text("Например: Рабочие серверы")
        self.name_entry.set_activates_default(True)
        self.name_entry.set_hexpand(True)
        grid.attach(Gtk.Label(label="Название", xalign=0), 0, 0, 1, 1)
        grid.attach(self.name_entry, 1, 0, 1, 1)
        self.parent_combo = Gtk.ComboBoxText()
        self.parent_combo.append("", "Верхний уровень")
        names = {item["name"] for item in store.groups}
        if selected_parent:
            names.add(selected_parent)
        for name in sorted(names):
            if original_name and (name == original_name or name.startswith(original_name + "/")):
                continue
            self.parent_combo.append(name, name)
        self.parent_combo.set_active_id(selected_parent or "")
        renderer = self.parent_combo.get_cells()[0]
        renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        renderer.set_property("max-width-chars", 40)
        grid.attach(Gtk.Label(label="В папке", xalign=0), 0, 1, 1, 1)
        grid.attach(self.parent_combo, 1, 1, 1, 1)
        folder.pack_start(_note("Создайте папку на верхнем уровне или внутри другой папки. "
                                "При переименовании и перемещении подключения сохраняются."),
                          False, False, 0)
        notebook.append_page(folder, Gtk.Label(label="Папка"))

        ssh_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        ssh_box.set_border_width(12)
        ssh_box.pack_start(_note("Отметьте настройки, общие для этой папки. "
                                 "Неотмеченные значения наследуются из родительской папки. "
                                 "Адрес сервера и команды остаются отдельными у каждого подключения."),
                           False, False, 0)
        settings = Gtk.Grid(column_spacing=12, row_spacing=8)
        ssh_box.pack_start(settings, False, False, 0)
        own_settings = self.group.get("ssh", {})
        for row, (key, caption) in enumerate(_FIELDS):
            check = Gtk.CheckButton(label=caption)
            check.set_active(key in own_settings)
            self.setting_checks[key] = check
            settings.attach(check, 0, row, 1, 1)
            if key in ("x11", "compression"):
                widget = Gtk.ComboBoxText()
                widget.append("yes", "Включено")
                widget.append("no", "Выключено")
            elif key == "identity_profile":
                widget = Gtk.ComboBoxText()
                widget.append("", "Профиль по умолчанию")
                widget.append("system", "Системные ключи / свой файл")
                known = {"", "system"}
                for identity in identity_profiles or []:
                    widget.append(identity["id"], identity["name"])
                    known.add(identity["id"])
                selected = own_settings.get(key, "")
                if selected not in known:
                    widget.append(selected, "Профиль недоступен: " + selected)
                renderer = widget.get_cells()[0]
                renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
                renderer.set_property("max-width-chars", 35)
            else:
                widget = Gtk.Entry()
                widget.set_activates_default(True)
                widget.set_placeholder_text({"port": "22", "key_file": "~/.ssh/id_rsa",
                                             "jump_host": "user@gateway:22",
                                             "encoding": "UTF-8"}.get(key, ""))
            widget.set_hexpand(True)
            self.ssh_fields[key] = widget
            settings.attach(widget, 1, row, 1, 1)
            self._set_value(key, own_settings.get(key, self._defaults.get(key)))
            check.connect("toggled", lambda unused, field=key: self._toggle_override(field))

        existing = sum(1 for session in store.sessions
                       if original_name and session.get("protocol") in ("ssh", "mosh") and
                       (session.get("group") == original_name or
                        session.get("group", "").startswith(original_name + "/")))
        self.apply_existing = Gtk.CheckButton(label="Включить наследование у существующих подключений ({})".format(existing))
        self.apply_existing.set_active(False)
        self.apply_existing.set_sensitive(existing > 0)
        self.apply_existing.set_tooltip_text("SSH и Mosh в этой папке и вложенных папках будут использовать общие настройки.")
        content.pack_start(self.apply_existing, False, False, 0)
        content.pack_start(_note("Настройки применяются при новом подключении. "
                                 "В отдельной сессии наследование можно отключить."), False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
        scroller.add(ssh_box)
        notebook.append_page(scroller, Gtk.Label(label="Общие SSH-настройки"))
        self.parent_combo.connect("changed", lambda unused: self._refresh_inherited())
        self._refresh_inherited()
        self.show_all()
        self.name_entry.grab_focus()

    def _set_value(self, key, value):
        widget = self.ssh_fields[key]
        if key in ("x11", "compression"):
            widget.set_active_id("yes" if value else "no")
        elif isinstance(widget, Gtk.ComboBoxText):
            selected = value or ""
            widget.set_active_id(selected)
            if widget.get_active() < 0:
                widget.append(selected, "Профиль недоступен: " + selected)
                widget.set_active_id(selected)
        else:
            widget.set_text(str(value) if value is not None else "")

    def _get_value(self, key):
        widget = self.ssh_fields[key]
        if key in ("x11", "compression"):
            return widget.get_active_id() == "yes"
        if isinstance(widget, Gtk.ComboBoxText):
            return widget.get_active_id() or ""
        return widget.get_text().strip()

    def _toggle_override(self, key):
        if self.setting_checks[key].get_active():
            if key in self._saved_overrides:
                self._set_value(key, self._saved_overrides[key])
        else:
            self._saved_overrides[key] = self._get_value(key)
        self._refresh_inherited()

    def _refresh_inherited(self):
        parent = self.parent_combo.get_active_id() or ""
        inherited = self.store.group_ssh_settings(parent) if parent else {}
        for key, check in self.setting_checks.items():
            widget = self.ssh_fields[key]
            enabled = check.get_active()
            widget.set_sensitive(enabled)
            if not enabled:
                self._set_value(key, inherited.get(key, self._defaults.get(key)))
                widget.set_tooltip_text("Из родительской папки" if key in inherited else "Обычное значение подключения")
            else:
                widget.set_tooltip_text("Общее значение для этой папки")

    @property
    def apply_to_sessions(self):
        return self.apply_existing.get_active()

    def get_group(self):
        leaf = self.name_entry.get_text().strip()
        if not leaf or "/" in leaf:
            raise ValueError("Введите название папки без символа /; родительскую папку выберите в списке.")
        parent = self.parent_combo.get_active_id() or ""
        name = normalize_group_name(parent + "/" + leaf if parent else leaf)
        result = dict(self.group)
        result.update(name=name, ssh={key: self._get_value(key) for key, check in self.setting_checks.items()
                                      if check.get_active()})
        return validate_group(result)
