"""Saved OpenSSH forwarding profiles, compatible with GTK 3.22/Python 3.5."""
import uuid

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .core import tunnel_argv, validate_tunnel


KINDS = (("local", "Локальный (-L)"), ("remote", "Удалённый (-R)"),
         ("dynamic", "SOCKS-прокси (-D)"))
KIND_LABELS = dict(KINDS)


class TunnelDialog(Gtk.Dialog):
    def __init__(self, parent, store, on_start, on_stop, is_active):
        Gtk.Dialog.__init__(self, title="SSH-туннели", transient_for=parent,
                            modal=True, destroy_with_parent=True)
        self.store = store
        self.on_start = on_start
        self.on_stop = on_stop
        self.is_active = is_active
        self.selected_id = None
        self.fields = {}
        self.set_default_size(920, 600)
        self.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        content = self.get_content_area()
        content.set_spacing(10)
        content.set_border_width(12)
        description = Gtk.Label(xalign=0)
        description.set_text("Локальный: порт этого компьютера → сервер.  "
                             "Удалённый: порт сервера → этот компьютер.\n"
                             "SOCKS: динамический прокси. Пароль и проверка ключа — в терминале SSH.")
        description.set_line_wrap(True)
        content.pack_start(description, False, False, 0)
        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content.pack_start(panes, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.model = Gtk.ListStore(str, str, str, str)
        self.tree = Gtk.TreeView(model=self.model)
        for index, title in ((1, "Туннель"), (2, "Тип"), (3, "Состояние")):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(index == 1)
            self.tree.append_column(column)
        self.tree.get_selection().connect("changed", self._selection_changed)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        left.pack_start(scroller, True, True, 0)
        actions = Gtk.Box(spacing=6)
        new_button = Gtk.Button(label="Новый")
        new_button.connect("clicked", self._new)
        actions.pack_start(new_button, True, True, 0)
        self.delete_button = Gtk.Button(label="Удалить")
        self.delete_button.connect("clicked", self._delete)
        actions.pack_start(self.delete_button, True, True, 0)
        left.pack_start(actions, False, False, 0)
        panes.pack1(left, resize=True, shrink=False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        right.set_border_width(8)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        right.pack_start(grid, False, False, 0)
        field_specs = (("name", "Название", "entry"),
                       ("type", "Тип", "kind"),
                       ("host", "SSH-сервер", "entry"),
                       ("username", "Пользователь", "entry"),
                       ("port", "Порт SSH", "port"),
                       ("key_file", "Файл приватного ключа", "entry"),
                       ("jump_host", "SSH-шлюз", "entry"),
                       ("bind_host", "Адрес прослушивания", "entry"),
                       ("listen_port", "Порт прослушивания", "port"),
                       ("destination_host", "Сервер назначения", "entry"),
                       ("destination_port", "Порт назначения", "port"))
        for row, (name, label, kind) in enumerate(field_specs):
            label_widget = Gtk.Label(label=label, xalign=0)
            grid.attach(label_widget, 0, row, 1, 1)
            if kind == "kind":
                widget = Gtk.ComboBoxText()
                for key, text in KINDS:
                    widget.append(key, text)
                widget.connect("changed", self._kind_changed)
            elif kind == "port":
                widget = Gtk.SpinButton.new_with_range(1, 65535, 1)
                widget.set_numeric(True)
            else:
                widget = Gtk.Entry()
                widget.set_hexpand(True)
            self.fields[name] = widget
            grid.attach(widget, 1, row, 1, 1)
        self.fields["host"].set_placeholder_text("server.example.org")
        self.fields["key_file"].set_placeholder_text("~/.ssh/id_ed25519 (необязательно)")
        self.fields["jump_host"].set_placeholder_text("user@gateway:22 (необязательно)")
        self.note = Gtk.Label(xalign=0)
        self.note.set_line_wrap(True)
        right.pack_start(self.note, False, False, 0)
        buttons = Gtk.Box(spacing=6)
        self.save_button = Gtk.Button(label="Сохранить")
        self.save_button.connect("clicked", self._save)
        buttons.pack_start(self.save_button, True, True, 0)
        self.start_button = Gtk.Button(label="Запустить")
        self.start_button.connect("clicked", self._start)
        self.start_button.get_style_context().add_class("suggested-action")
        buttons.pack_start(self.start_button, True, True, 0)
        self.stop_button = Gtk.Button(label="Остановить")
        self.stop_button.connect("clicked", self._stop)
        buttons.pack_start(self.stop_button, True, True, 0)
        right.pack_end(buttons, False, False, 0)
        panes.pack2(right, resize=True, shrink=False)
        panes.set_position(350)
        self._reload()
        if len(self.model):
            self.tree.get_selection().select_path(Gtk.TreePath.new_from_indices([0]))
        else:
            self._new()
        self.show_all()

    def _error(self, message):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE,
                                   text="Не удалось выполнить действие")
        dialog.format_secondary_text(str(message))
        dialog.run()
        dialog.destroy()

    def _reload(self, select_id=None):
        self.model.clear()
        selected_path = None
        for index, tunnel in enumerate(self.store.tunnels):
            active = self.is_active(tunnel["id"])
            self.model.append((tunnel["id"], tunnel["name"],
                               KIND_LABELS.get(tunnel["type"], tunnel["type"]),
                               "Активен" if active else "Остановлен"))
            if tunnel["id"] == select_id:
                selected_path = index
        if selected_path is not None:
            self.tree.get_selection().select_path(Gtk.TreePath.new_from_indices([selected_path]))

    def _selection_changed(self, selection):
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        selected_id = model[iterator][0]
        tunnel = next((item for item in self.store.tunnels if item["id"] == selected_id), None)
        if tunnel is not None:
            self.selected_id = selected_id
            self._fill(tunnel)

    def _fill(self, tunnel):
        for name, widget in self.fields.items():
            value = tunnel.get(name, "")
            if isinstance(widget, Gtk.ComboBoxText):
                widget.set_active_id(value)
            elif isinstance(widget, Gtk.SpinButton):
                widget.set_value(value or 1)
            else:
                widget.set_text(str(value))
        self._kind_changed()
        active = bool(self.selected_id and self.is_active(self.selected_id))
        self.delete_button.set_sensitive(bool(self.selected_id) and not active)
        self.save_button.set_sensitive(not active)
        self.start_button.set_sensitive(not active)
        self.stop_button.set_sensitive(active)
        self.note.set_text("Сначала остановите активный туннель для изменения его настроек."
                           if active else "Адрес 127.0.0.1 ограничивает доступ локальным компьютером. "
                           "Запуск откроет терминал SSH; оставьте его открытым, пока нужен туннель.")

    def _kind_changed(self, *_):
        enabled = self.fields["type"].get_active_id() != "dynamic"
        for name in ("destination_host", "destination_port"):
            if name in self.fields:
                self.fields[name].set_sensitive(enabled)

    def _new(self, *_):
        self.tree.get_selection().unselect_all()
        self.selected_id = None
        self._fill({"name": "", "type": "local", "host": "", "username": "", "port": 22,
                    "key_file": "", "jump_host": "", "bind_host": "127.0.0.1", "listen_port": 8080,
                    "destination_host": "127.0.0.1", "destination_port": 80})
        self.fields["name"].grab_focus()

    def _read(self):
        result = {"id": self.selected_id or uuid.uuid4().hex}
        for name, widget in self.fields.items():
            if isinstance(widget, Gtk.ComboBoxText):
                result[name] = widget.get_active_id()
            elif isinstance(widget, Gtk.SpinButton):
                result[name] = widget.get_value_as_int()
            else:
                result[name] = widget.get_text()
        result = validate_tunnel(result)
        tunnel_argv(result)
        return result

    def _persist(self, tunnels):
        previous = self.store.tunnels
        self.store.tunnels = tunnels
        try:
            self.store.save()
        except Exception:
            self.store.tunnels = previous
            raise

    def _save_current(self):
        if self.selected_id and self.is_active(self.selected_id):
            raise ValueError("Сначала остановите активный туннель")
        tunnel = self._read()
        tunnels = [dict(item) for item in self.store.tunnels]
        for index, previous in enumerate(tunnels):
            if previous["id"] == tunnel["id"]:
                tunnels[index] = tunnel
                break
        else:
            tunnels.append(tunnel)
        self._persist(tunnels)
        self.selected_id = tunnel["id"]
        self._reload(tunnel["id"])
        return tunnel

    def _save(self, *_):
        try:
            self._save_current()
        except Exception as exc:
            self._error(exc)

    def _delete(self, *_):
        if not self.selected_id:
            return
        if self.is_active(self.selected_id):
            self._error("Сначала остановите активный туннель")
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL,
                                   text="Удалить сохранённый туннель?")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            self._persist([dict(item) for item in self.store.tunnels if item["id"] != self.selected_id])
            self._reload()
            self._new()
        except Exception as exc:
            self._error(exc)

    def _start(self, *_):
        try:
            tunnel = self._save_current()
            self.on_start(tunnel)
            self.response(Gtk.ResponseType.CLOSE)
        except Exception as exc:
            self._error(exc)

    def _stop(self, *_):
        if not self.selected_id or not self.is_active(self.selected_id):
            return
        try:
            self.on_stop(self.selected_id)
            self.response(Gtk.ResponseType.CLOSE)
        except Exception as exc:
            self._error(exc)
