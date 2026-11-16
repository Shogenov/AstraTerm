"""Native AstraTerm desktop workspace."""
import argparse
import json
import os
import shlex
import shutil
import sys
import threading
import uuid

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango

from . import __version__
from .core import (SessionStore, _atomic_json, _config_lock, default_session, validate_session, session_argv,
                   parse_ssh_config, detect_dependencies, tcp_probe, tunnel_argv)
from .terminal import TerminalWorkspace
from .floating import FloatingWorkspace
from .dialogs import SessionDialog, MacroDialog, error, confirm, name_dialog
from .files import FileBrowser, bind_file_drop
from .multiexec import MultiExecController
from .tiled import TiledTerminals
from .ssh_profiles import ProfileStore, SSHAgent, verify_terminal_identity
from .identity_dialogs import ensure_identity, IdentityCancelled
from .group_dialogs import GroupDialog
from .vault import CredentialVault
from .host_stats_ui import HostStatsBar
from .import_dialogs import CredentialController
from .ui_helpers import protocol_icon, platform_pixbuf, group_icon, TabStrip, action_image
from .update_ui import UpdateController
from .diagnostic_log import DiagnosticLog
from .backup_dialogs import BackupDialogs
from .profile_location import profile_root

DEFAULT_FONT_SIZE = 11


def button(label, callback, tooltip=None, primary=False):
    widget = Gtk.Button(label=label)
    widget.connect("clicked", callback)
    if tooltip:
        widget.set_tooltip_text(tooltip)
    if primary:
        widget.get_style_context().add_class("primary")
    return widget


def action_button(icon, callback, tooltip, toggle=False):
    widget = Gtk.ToggleButton() if toggle else Gtk.Button()
    widget.add(action_image(icon))
    widget.set_relief(Gtk.ReliefStyle.NONE)
    widget.get_style_context().add_class("icon-button")
    widget.set_tooltip_text(tooltip)
    widget.get_accessible().set_name(tooltip)
    widget.connect("toggled" if toggle else "clicked", callback)
    return widget


class MainWindow(Gtk.Window):
    def __init__(self, store=None, initial_session=None, start_local=True,
                 enable_identities=False, identity_store=None, ssh_agent=None, auto_sftp=None, backup_root=None):
        Gtk.Window.__init__(self, title="AstraTerm — терминалы и подключения")
        self.set_wmclass("astraterm", "AstraTerm")
        screen = Gdk.Screen.get_default()
        self.set_default_size(min(1320, max(860, screen.get_width() - 30)),
                              min(820, max(600, screen.get_height() - 80)))
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("utilities-terminal")
        self.store = store or SessionStore()
        directory = os.path.dirname(self.store.path)
        self.diagnostic_log = DiagnosticLog(os.path.dirname(self.store.path))
        self.diagnostic_log.write('info', 'app.start')
        self._sidebar_path = os.path.join(os.path.dirname(self.store.path), 'sidebar.json')
        self._collapsed_groups = self._read_collapsed_groups()
        self.credential_vault = CredentialVault(os.path.join(os.path.dirname(self.store.path), 'credentials.vault.json'))
        self.credentials = CredentialController(self, self.credential_vault)
        self.identity_store = identity_store or (ProfileStore(os.path.join(directory, 'ssh-profiles.json'))
                                                 if enable_identities else None)
        self.backups = BackupDialogs(self, backup_root or (directory if store is not None else profile_root()))
        self.ssh_agent = ssh_agent or (SSHAgent() if self.identity_store is not None else None)
        self.auto_sftp = bool(self.identity_store is not None) if auto_sftp is None else bool(auto_sftp)
        self.floating_windows = {}
        self.auth_generation = 0
        self._identity_jobs = set()
        self._identity_manager = None
        self._startup_local = start_local
        self.font_size = DEFAULT_FONT_SIZE
        self.tunnel_sessions = {}
        self.closed = False
        self.updates = UpdateController(self)
        self._tiles_wanted = False
        self._layout_input_suspended = False
        self._selection_update = False
        self._overview_active = None
        self._overview_files_visible = None
        self._dragged_workspace = None
        self._dragged_pane = None
        self._tab_context_menu = None
        self.multiexec = MultiExecController(on_change=self._multiexec_changed,
                                             on_error=self._multiexec_error)
        self.file_profile_id = None
        self._load_style()
        self.connect("delete-event", self._delete_event)
        self.connect("key-press-event", self._key_press)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        overlay = Gtk.Overlay()
        self.add(overlay)
        overlay.add(outer)
        self.notifications = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.notifications.set_halign(Gtk.Align.END)
        self.notifications.set_valign(Gtk.Align.END)
        self.notifications.set_margin_end(18)
        self.notifications.set_margin_bottom(34)
        overlay.add_overlay(self.notifications)
        outer.pack_start(self._toolbar(), False, False, 0)
        self.multiexec_bar = self._multiexec_bar()
        self.multiexec_bar.set_no_show_all(True)
        self.multiexec_bar.hide()
        outer.pack_start(self.multiexec_bar, False, False, 0)
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(body, True, True, 0)
        self.sidebar = self._sidebar()
        body.pack1(self.sidebar, False, False)
        body.set_position(210)
        self.content = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        body.pack2(self.content, True, False)
        self.notebook = Gtk.Notebook()
        self.notebook.get_style_context().add_class("workspace-tabs")
        self.notebook.set_show_tabs(False)
        self.notebook.set_show_border(False)
        self.tab_strip = TabStrip()
        self.tab_bar = Gtk.Box()
        self.tab_bar.get_style_context().add_class("workspace-tab-bar")
        self.tab_bar.pack_start(self.tab_strip, True, True, 0)
        self.tabs_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.tabs_view.pack_start(self.tab_bar, False, False, 0)
        self.tabs_view.pack_start(self.notebook, True, True, 0)
        self.notebook.connect("page-removed", self._remove_tab_header)
        self.notebook.connect("scroll-event", self._ignore_notebook_scroll)
        self.notebook.connect("switch-page", self._switch_page)
        self.tab_drop_target = Gtk.Button(label="+")
        self.tab_drop_target.set_relief(Gtk.ReliefStyle.NONE)
        self.tab_drop_target.set_tooltip_text("Новый терминал · перетащите сюда панель или объединённую вкладку для разъединения")
        self.tab_drop_target.connect("clicked", self.new_local)
        self.tab_bar.pack_end(self.tab_drop_target, False, False, 0)
        self.tab_drop_target.show()
        for destination in (self.tab_drop_target, self.tab_strip.labels):
            destination.drag_dest_set(Gtk.DestDefaults.ALL, self._tab_targets(), Gdk.DragAction.MOVE)
            destination.connect("drag-motion", self._separate_drag_motion)
            destination.connect("drag-leave", self._tab_drag_leave)
            destination.connect("drag-data-received", self._separate_drag_received)
        self.workspace_stack = Gtk.Stack()
        self.workspace_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.workspace_stack.add_named(self.tabs_view, "tabs")
        self.tiled = TiledTerminals()
        self.workspace_stack.add_named(self.tiled, "tiles")
        self.workspace_stack.set_visible_child_name("tabs")
        self.content.pack1(self.workspace_stack, True, False)
        self.file_browser = FileBrowser(on_status=self.set_status, on_notice=self.notify, auto_follow=self.auto_sftp,
                                        diagnostic_log=self.diagnostic_log)
        self.file_browser.password_provider = self.credentials.password_for
        self.file_browser.set_size_request(266, -1)
        self.content.pack2(self.file_browser, False, False)
        self.content.set_position(max(360, self.get_default_size()[0] - 210 - 270))
        for panel, control, label in (
                (self.sidebar, self.sidebar_toggle, 'панель подключений'),
                (self.file_browser, self.files_toggle, 'файловую панель')):
            panel.connect('notify::visible', self._sync_panel_toggle, control, label)
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status.set_border_width(0)
        status.get_style_context().add_class("statusbar")
        self.status = Gtk.Label(label="Готово", xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        status.pack_start(self.status, True, True, 6)
        self.status.set_max_width_chars(24)
        self.host_stats = HostStatsBar(self)
        status.pack_start(self.host_stats,True,True,6)
        self.connection_info = Gtk.Label(label="UTF-8")
        self.connection_info.get_style_context().add_class("muted")
        status.pack_end(self.connection_info, False, False, 6)
        self.cancel_drop = Gtk.EventBox()
        self.cancel_drop.add(Gtk.Label(label="Отмена перетаскивания"))
        self.cancel_drop.get_style_context().add_class("cancel-drop")
        self.cancel_drop.set_tooltip_text("Отпустите здесь вкладку, панель или файл для отмены")
        self.cancel_drop.drag_dest_set(Gtk.DestDefaults(0), self._tab_targets() +
                                       [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                                       Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        self.cancel_drop.connect("drag-motion", self._cancel_drag_motion)
        self.cancel_drop.connect("drag-leave", self._tab_drag_leave)
        self.cancel_drop.connect("drag-drop", self._cancel_drag_drop)
        status.pack_end(self.cancel_drop, False, False, 6)
        outer.pack_end(status, False, False, 0)
        self._refresh_sessions()
        if initial_session and not enable_identities:
            self.open_session(initial_session)
        elif start_local and not enable_identities:
            self.new_local()
        self.show_all()
        if enable_identities:
            GLib.idle_add(self._startup_identity, initial_session)
            GLib.timeout_add_seconds(15, lambda: self.updates.check(automatic=True))

    def _load_style(self):
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        provider = Gtk.CssProvider()
        provider.load_from_path(os.path.join(os.path.dirname(__file__), "style.css"))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _menu(self):
        bar = Gtk.Menu()
        groups = [
            ("Файл", [("Новое подключение…", self.new_session), ("Новая папка подключений…", self.new_group),
                       ("Локальный терминал", self.new_local),
                       ("Импорт подключений…", self.import_sessions), ("Импорт ~/.ssh/config…", self.import_ssh),
                       ("Экспорт подключений…", self.export_sessions),
                       ("Создать резервную копию…", self.backups.create),
                       ("Восстановить из резервной копии…", self.backups.restore),
                       ("Закрыть вкладку", self.close_tab),
                       ("Выход", lambda *_: self.close())]),
            ("Вид", [("Разделить по горизонтали", lambda *_: self.split(Gtk.Orientation.HORIZONTAL)),
                      ("Разделить по вертикали", lambda *_: self.split(Gtk.Orientation.VERTICAL)),
                      ("Все терминалы разных хостов", lambda *_: self.set_tiled_view(not self._tiles_wanted)),
                      ("MultiExec: включить / выключить", lambda *_: self.set_multiexec(not self.multiexec.enabled)),
                      ("Файловая панель", self.toggle_files), ("Панель подключений", self.toggle_sidebar),
                      ("Найти в терминале…", self.find), ("Увеличить шрифт", lambda *_: self.zoom(1)),
                      ("Уменьшить шрифт", lambda *_: self.zoom(-1))]),
            ("Инструменты", [("Настройки…", self.settings), ("Хранилище учётных данных…", self.credentials.manage),
                              ("SSH-туннели…", self.tunnels), ("Макросы…", self.macros),
                              ("Новая сессия с записью…", self.record_session),
                              ("Сохранить вывод терминала…", self.save_output),
                              ("Переподключить", self.reconnect), ("Диагностика…", self.diagnostics)]),
            ("Справка", [("Горячие клавиши", self.help_shortcuts),
                         ("Проверить обновления…", lambda *_: self.updates.check()), ("О программе", self.about)])]
        for label, items in groups:
            root = Gtk.MenuItem(label=label)
            menu = Gtk.Menu()
            for title, callback in items:
                item = Gtk.MenuItem(label=title)
                item.connect("activate", callback)
                menu.append(item)
            root.set_submenu(menu)
            bar.append(root)
        bar.show_all()
        return bar

    def _toolbar(self):
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        toolbar.set_hexpand(True)
        toolbar.set_border_width(0)
        toolbar.get_style_context().add_class("app-toolbar")
        mark = Gtk.Label(label="›_")
        mark.get_style_context().add_class("brand-mark")
        toolbar.pack_start(mark, False, False, 0)
        brand = Gtk.Label(label="AstraTerm", margin_right=16)
        brand.get_style_context().add_class("brand")
        toolbar.pack_start(brand, False, False, 0)
        toolbar.pack_start(button("+ Подключение", self.new_session, primary=True), False, False, 0)
        self.quick = Gtk.Entry()
        self.quick.set_placeholder_text("user@host:22")
        self.quick.set_width_chars(17)
        self.quick.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "network-server-symbolic")
        self.quick.set_tooltip_text("Быстрое SSH-подключение · введите адрес и нажмите Enter")
        self.quick.get_style_context().add_class("quick-connect")
        self.quick.connect("activate", self.quick_connect)
        toolbar.pack_start(self.quick, True, True, 6)
        toolbar.pack_start(action_button("terminal", self.new_local, "Локальный терминал · Ctrl+Shift+T"), False, False, 0)
        toolbar.pack_start(action_button("split", lambda *_: self.split(Gtk.Orientation.HORIZONTAL),
                                        "Разделить терминал · Ctrl+Shift+E"), False, False, 0)
        self.tiles_toggle = action_button("grid", lambda widget: self.set_tiled_view(widget.get_active()),
                                           "Все терминалы разных хостов", toggle=True)
        toolbar.pack_start(self.tiles_toggle, False, False, 4)
        self.sidebar_toggle = action_button("panel-left", self.toggle_sidebar, "Скрыть панель подключений")
        self.files_toggle = action_button("panel-right", self.toggle_files, "Скрыть файловую панель")
        for control in (self.sidebar_toggle, self.files_toggle):
            toolbar.pack_start(control, False, False, 0)
        self.multiexec_toggle = Gtk.ToggleButton(label="MultiExec")
        self.multiexec_toggle.get_style_context().add_class("multiexec-toggle")
        self.multiexec_toggle.set_tooltip_text("Живой ввод в отмеченные терминалы · Ctrl+Shift+M")
        self.multiexec_toggle.connect("toggled", lambda widget: self.set_multiexec(widget.get_active()))
        toolbar.pack_start(self.multiexec_toggle, False, False, 0)
        menu = Gtk.Button()
        menu.add(action_image("menu"))
        menu.set_relief(Gtk.ReliefStyle.NONE)
        menu.get_style_context().add_class("icon-button")
        menu.set_tooltip_text("Меню AstraTerm")
        menu.get_accessible().set_name("Меню AstraTerm")
        self.main_menu = self._menu()
        self.main_menu.hide()
        menu.connect("clicked", self._show_main_menu)
        toolbar.pack_start(menu, False, False, 0)
        toolbar.reorder_child(menu, 0)
        self.main_menu_button = menu
        return toolbar

    def _show_main_menu(self, widget):
        def position(menu, *unused):
            translated = widget.translate_coordinates(self, 0, 0)
            x, y = self.get_window().get_root_coords(*translated[-2:])
            return max(0, x), y + widget.get_allocated_height() + 4, True
        self.main_menu.popup(None, None, position, None, 0, Gtk.get_current_event_time())

    def _multiexec_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_border_width(0)
        bar.get_style_context().add_class("multiexec-bar")
        self.multiexec_label = Gtk.Label(label="Ввод раздельный · выбрано 0", xalign=0)
        self.multiexec_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.multiexec_label.set_hexpand(True)
        bar.pack_start(self.multiexec_label, True, True, 0)
        self.select_all_button = button("Выбрать все", lambda *_: self.select_all_targets())
        self.clear_all_button = button("Снять все", lambda *_: self.clear_all_targets())
        bar.pack_start(self.select_all_button, False, False, 0)
        bar.pack_start(self.clear_all_button, False, False, 0)
        self.multi_paste_button = button("Вставить", lambda *_: self.multi_paste(),
                                         "Вставить буфер в отмеченные панели · Ctrl+Shift+Insert")
        bar.pack_start(self.multi_paste_button, False, False, 0)
        bar.pack_end(action_button("close", lambda *_: self.set_tiled_view(False),
                                   "Вернуться к вкладкам и выключить MultiExec"), False, False, 0)
        bar.show_all()
        return bar

    def all_panes(self):
        if not hasattr(self, "notebook"):
            return []
        return [pane for i in range(self.notebook.get_n_pages())
                for pane in self.notebook.get_nth_page(i).panes if not pane.closed]

    def _workspace_for_pane(self, pane):
        for i in range(self.notebook.get_n_pages()):
            workspace = self.notebook.get_nth_page(i)
            if pane in workspace.panes:
                return workspace
        return None

    def _terminal_panes(self):
        return [pane for pane in self.all_panes()
                if self._workspace_for_pane(pane) not in self.floating_windows and
                pane.profile.get("protocol") not in ("rdp", "vnc", "tunnel")]

    def _register_pane(self, pane):
        pane.on_reconnect=lambda current:self.reconnect_pane(current)
        self.multiexec.register(pane)
        if not hasattr(pane, "pane_drag_id"):
            pane.pane_drag_id = uuid.uuid4().hex
            pane.heading.set_tooltip_text("Перетащите в строку вкладок для разъединения")
            pane.heading.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, self._tab_targets()[1:], Gdk.DragAction.MOVE)
            pane.heading.connect("drag-begin", self._pane_drag_begin, pane)
            pane.heading.connect("drag-end", self._tab_drag_end)
            pane.heading.connect("drag-data-get", self._pane_drag_data_get, pane)
            if pane.profile.get("protocol") == "ssh":
                bind_file_drop(pane.terminal, lambda paths: self._upload_to_pane(pane, paths))

    def _upload_to_pane(self, pane, paths):
        generation = pane.connection_generation

        def current():
            return not pane.closed and not pane.exited and pane.pid is not None and pane.connection_generation == generation

        if self._layout_input_suspended or not current():
            self.set_status("Для загрузки нужен работающий SSH-терминал.")
            return
        pane.terminal.grab_focus()
        pane.on_focus(pane)
        self.file_browser.upload_paths(paths, terminal_check=current)

    def _connection_changed(self, pane):
        if pane.closed:
            self.multiexec.unregister(pane)
        else:
            self.multiexec.connection_changed(pane)

    def _target_changed(self, pane, active):
        if not self._selection_update:
            self.multiexec.set_selected(pane, active)

    def _multiexec_changed(self, controller):
        if self._selection_update or not hasattr(self, "multiexec_label"):
            return
        self._selection_update = True
        try:
            for pane in self.all_panes():
                pane.show_multiexec_selection(self._tiles_wanted,
                                              controller.is_selected(pane), controller.eligible(pane))
            if self.multiexec_toggle.get_active() != controller.enabled:
                self.multiexec_toggle.set_active(controller.enabled)
            count = len(controller.selected_panes())
            text = ("MultiExec · выбрано {}".format(count) if count else "MultiExec · нет получателей") if controller.enabled else "Ввод раздельный · выбрано {}".format(count)
            pane = self.active_pane() if hasattr(self, "notebook") else None
            if controller.enabled and pane and not controller.is_selected(pane):
                text += " · текущая панель без рассылки"
            self.multiexec_label.set_text(text)
            self.multi_paste_button.set_sensitive(controller.enabled and count > 0)
            if hasattr(self, "multiexec_bar"):
                if self._tiles_wanted or controller.enabled:
                    self.multiexec_bar.show()
                else:
                    self.multiexec_bar.hide()
                context = self.multiexec_bar.get_style_context()
                context.add_class("broadcast-active") if controller.enabled else context.remove_class("broadcast-active")
        finally:
            self._selection_update = False

    def _multiexec_error(self, pane, exception):
        # Codec exceptions may contain typed text; never display their payload.
        self.set_status("MultiExec: ввод не передан в «{}»; проверьте кодировку и соединение".format(pane.identity))

    def _user_input(self, pane, data):
        owner = pane.terminal.get_toplevel()
        floating = self.floating_windows.get(self._workspace_for_pane(pane))
        owned = owner is self or (floating is not None and owner is floating.window)
        if (not self._layout_input_suspended and self._tiles_wanted and owned and
                owner.get_focus() is pane.terminal):
            self.multiexec.route(pane, data)

    def _paste_from_pane(self, pane, primary=False):
        if self._layout_input_suspended or not self.multiexec.eligible(pane):
            return
        if self.multiexec.enabled and self.multiexec.is_selected(pane):
            targets = self.multiexec.selected_panes()
        else:
            targets = [pane]
        for target in list(targets):
            if self.multiexec.eligible(target):
                self._paste_to_target(target, primary)

    def _paste_to_target(self, pane, primary=False):
        generation, pid = pane.connection_generation, pane.pid
        pane.terminal.native_paste(primary, guard=lambda: (
            not pane.closed and pane.pid == pid and pane.connection_generation == generation))

    def multi_paste(self):
        if not self.multiexec.enabled or self._layout_input_suspended:
            return
        for pane in self.multiexec.selected_panes():
            self._paste_to_target(pane, False)
        self._focus_active()

    def select_all_targets(self):
        self.set_tiled_view(True)
        self.multiexec.select_all()
        self._focus_active()

    def clear_all_targets(self):
        self.multiexec.clear_selection()
        self._focus_active()

    def set_multiexec(self, enabled):
        if self._selection_update:
            return
        if enabled:
            self.set_tiled_view(True)
        self.multiexec.set_enabled(bool(enabled))
        self._multiexec_changed(self.multiexec)
        self._focus_active()

    def _focus_active(self):
        pane = self.active_pane()
        requested_focus = self.get_focus()
        if pane and not pane.closed:
            def focus_when_ready():
                if self.closed or pane.closed:
                    return False
                current = self.get_focus()
                if current is not None and current is not requested_focus and current is not pane.terminal:
                    return False
                if current is not pane.terminal and any(current is item.terminal for item in self.all_panes()):
                    return False
                if self.active_pane() is pane:
                    self._pane_focus(pane)
                    pane.terminal.grab_focus()
                return False
            GLib.idle_add(focus_when_ready)

    def set_tiled_view(self, enabled):
        if self._layout_input_suspended:
            return
        enabled = bool(enabled)
        if enabled == self._tiles_wanted:
            return
        pane = self.active_pane()
        self._layout_input_suspended = True
        try:
            if enabled:
                self._overview_active = pane
                self._overview_files_visible = self.file_browser.get_visible()
                if not self.auto_sftp:
                    self.file_browser.set_no_show_all(True)
                    self.file_browser.hide()
                self.workspace_stack.set_visible_child_name("tiles")
                self._tiles_wanted = True
                self.tiled.enter(self._terminal_panes())
            else:
                self.multiexec.set_enabled(False)
                self.tiled.leave()
                self._tiles_wanted = False
                self.workspace_stack.set_visible_child_name("tabs")
                if self._overview_files_visible and not self.file_browser.get_visible():
                    self.file_browser.set_no_show_all(False)
                    self.file_browser.show_all()
                self._overview_files_visible = None
                workspace = self._workspace_for_pane(pane) if pane else None
                if workspace:
                    self.notebook.set_current_page(self.notebook.page_num(workspace))
        finally:
            self._layout_input_suspended = False
        if self.tiles_toggle.get_active() != enabled:
            self.tiles_toggle.set_active(enabled)
        self._multiexec_changed(self.multiexec)
        self._focus_active()

    def _suspend_tiling(self):
        wanted = self._tiles_wanted
        self._layout_input_suspended = True
        if self.tiled.active:
            self.tiled.leave()
            self.workspace_stack.set_visible_child_name("tabs")
        return wanted

    def _resume_tiling(self, wanted):
        try:
            if wanted and not self.closed:
                self.workspace_stack.set_visible_child_name("tiles")
                self.tiled.enter(self._terminal_panes())
        finally:
            self._layout_input_suspended = False
        self._multiexec_changed(self.multiexec)
        self._focus_active()

    def _sidebar(self):
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.set_size_request(208, -1)
        sidebar.set_border_width(14)
        sidebar.get_style_context().add_class("sidebar")
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(label="Подключения", xalign=0)
        label.get_style_context().add_class("section-title")
        row.pack_start(label, True, True, 0)
        self.session_count = Gtk.Label(label="0")
        self.session_count.set_valign(Gtk.Align.CENTER)
        self.session_count.get_style_context().add_class("muted")
        self.session_count.get_style_context().add_class("count-badge")
        row.pack_start(self.session_count, False, False, 0)
        row.pack_end(action_button("folder", self.new_group, "Создать папку подключений"), False, False, 0)
        sidebar.pack_start(row, False, False, 2)
        self.session_search = Gtk.SearchEntry()
        self.session_search.set_placeholder_text("Поиск")
        self.session_search.connect("search-changed", lambda *_: self._refresh_sessions())
        sidebar.pack_start(self.session_search, False, False, 0)
        self.session_tree_store = Gtk.TreeStore(str, str, str, str, str, GdkPixbuf.Pixbuf)
        self.session_tree = Gtk.TreeView(model=self.session_tree_store)
        self.session_tree.set_headers_visible(False)
        self.session_tree.get_style_context().add_class("session-list")
        self.session_tree.set_tooltip_column(2)
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer.set_property("ypad", 6)
        column = Gtk.TreeViewColumn("Подключения")
        icon = Gtk.CellRendererPixbuf()
        icon.set_property("xpad", 6)
        column.pack_start(icon, False)
        def session_icon(_column, cell, model, iterator, _data):
            pixbuf = model[iterator][5]
            cell.set_property('pixbuf' if pixbuf is not None else 'icon-name',
                              pixbuf if pixbuf is not None else model[iterator][3])
        column.set_cell_data_func(icon, session_icon, None)
        column.pack_start(renderer, True)
        column.add_attribute(renderer, "text", 0)
        self.session_tree.append_column(column)
        self.session_tree.connect("row-activated", self._session_activated)
        self.session_tree.connect("button-press-event", self._session_context)
        self.session_tree.connect("key-press-event", self._rename_session_key)
        self.session_tree.connect("row-expanded", self._group_expansion, True)
        self.session_tree.connect("row-collapsed", self._group_expansion, False)
        scroll = Gtk.ScrolledWindow()
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.session_tree)
        sidebar.pack_start(scroll, True, True, 0)
        return sidebar

    def set_status(self, text):
        if hasattr(self, "status") and not self.closed:
            self.status.set_text(str(text).replace("\n", " "))

    def notify(self, text):
        self.set_status(text)
        if self.closed:
            return
        row = Gtk.Box(spacing=10)
        row.get_style_context().add_class("toast")
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.set_max_width_chars(48)
        row.pack_start(label, True, True, 0)
        row.pack_end(action_button("close", lambda *_: row.destroy(), "Закрыть уведомление"), False, False, 0)
        self.notifications.pack_start(row, False, False, 0)
        row.show_all()

        def expire():
            timer[0] = 0
            row.destroy()
            return False

        timer = [GLib.timeout_add(6500, expire)]
        row.connect("destroy", lambda *_: GLib.source_remove(timer[0]) if timer[0] else None)

    def _ensure_identity(self, profile_id, completed):
        holder = [None]
        def ready(identity, failure):
            self._identity_jobs.discard(holder[0])
            if not self.closed:
                completed(identity, failure)
        holder[0] = ensure_identity(self, self.identity_store, self.ssh_agent,
                                    profile_id=profile_id, callback=ready)
        self._identity_jobs.add(holder[0])
        return holder[0]

    def _startup_identity(self, initial_session=None):
        if self.closed:
            return False
        def ready(identity, failure):
            if failure is not None:
                if not isinstance(failure, IdentityCancelled):
                    self.set_status("SSH-профиль не разблокирован: " + str(failure))
                if self._startup_local:
                    self.new_local()
                return
            def opened(environment=None, failure=None):
                if self.closed:
                    return False
                if environment:
                    os.environ.update(environment)
                if failure:
                    self.set_status("Не удалось подготовить ssh-agent")
                if initial_session:
                    self.open_session(initial_session)
                elif self._startup_local:
                    self.new_local()
                if identity is not None:
                    self.set_status("SSH-профиль готов: " + identity["name"])
                return False
            if identity is None:
                opened()
            else:
                def environment():
                    try:
                        result, failure = self.ssh_agent.environment(), None
                    except Exception as exc:
                        result, failure = None, exc
                    GLib.idle_add(opened, result, failure)
                worker = threading.Thread(target=environment, name="astraterm-agent-environment")
                worker.daemon = True
                worker.start()
        self._ensure_identity(None, ready)
        return False

    def identity_profiles(self, *_):
        return self.credentials.manage()


    def _requested_identity(self, profile):
        if profile.get('credential_profile') and not profile.get('_credential_provider'):
            return None
        if (self.identity_store is None or profile.get("_argv") or
                profile.get("protocol") not in ("ssh", "mosh")):
            return None
        selected = profile.get("identity_profile", "")
        if selected == "system" or (not selected and profile.get("key_file")):
            return None
        identity = self.identity_store.get(selected or None)
        if selected and identity is None:
            raise ValueError("SSH-профиль недоступен. Выберите профиль ключа в настройках подключения.")
        return identity

    def _prepare_identity_session(self, profile, identity, completed):
        self.set_status("Проверка SSH-профиля «{}»…".format(identity["name"]))
        def unlocked(selected, failure):
            if failure is not None or selected is None:
                if failure is not None and not isinstance(failure, IdentityCancelled):
                    error(self, "Не удалось разблокировать SSH-профиль", failure)
                return
            def resolve():
                try:
                    environment = self.ssh_agent.environment()
                    public = self.ssh_agent.loaded_identity(selected["key_file"], selected.get("fingerprint", ""))
                    if not public:
                        raise ValueError("Выбранный ключ отсутствует в ssh-agent")
                    public_file = self.ssh_agent.public_identity_file(
                        selected["key_file"], selected.get("fingerprint", ""))
                    effective = dict(profile)
                    effective.update(identity_profile=selected["id"], key_file=public_file)
                    effective["_sftp_endpoint"] = verify_terminal_identity(effective, public_file)
                    effective["_agent_key_blob"] = public["blob"]
                    effective["_ssh_agent_sock"] = environment["SSH_AUTH_SOCK"]
                    effective["_auth_token"] = (environment["SSH_AUTH_SOCK"], public["blob"])
                    result, failure = (effective, environment), None
                except Exception as exc:
                    result, failure = None, exc
                GLib.idle_add(finished, result, failure)
            def finished(result, failure):
                if self.closed:
                    return False
                if failure is not None:
                    error(self, "Не удалось подготовить SSH-профиль", failure)
                else:
                    effective, environment = result
                    os.environ.update(environment)
                    completed(effective)
                return False
            worker = threading.Thread(target=resolve, name="astraterm-identity")
            worker.daemon = True
            worker.start()
        self._ensure_identity(identity["id"], unlocked)

    def _sync_file_browser(self, pane):
        profile = pane.profile if pane is not None and pane.profile.get("protocol") == "ssh" else None
        if profile and profile.get("_sftp_endpoint"):
            profile = dict(profile)
            profile.update(profile["_sftp_endpoint"])
        token = (profile.get("_auth_token"), self.auth_generation, pane.pane_drag_id) if profile else self.auth_generation
        self.file_browser.set_profile(profile, auth_generation=token)
        if hasattr(self,'host_stats'):self.host_stats.refresh()

    def current_workspace(self):
        if self._tiles_wanted and self._overview_active and not self._overview_active.closed:
            workspace = self._workspace_for_pane(self._overview_active)
            if workspace:
                return workspace
        index = self.notebook.get_current_page()
        return self.notebook.get_nth_page(index) if index >= 0 else None

    def active_pane(self):
        if self._tiles_wanted:
            if self._overview_active and not self._overview_active.closed:
                return self._overview_active
            return next(iter(self._terminal_panes()), None)
        workspace = self.current_workspace()
        return workspace.active if workspace else None

    def _pane_focus(self, pane):
        if self._layout_input_suspended or pane.closed:
            return
        self._overview_active = pane
        floating_owner = self._workspace_for_pane(pane)
        if floating_owner in self.floating_windows:
            self.notebook.set_current_page(self.notebook.page_num(floating_owner))
        for child in self.all_panes():
            context = child.get_style_context()
            context.add_class("active-pane") if child is pane else context.remove_class("active-pane")
        if not hasattr(self, "file_browser"):
            return
        profile = pane.profile
        self._sync_file_browser(pane)
        self.connection_info.set_text("{}  •  {}  •  {} pt".format(
            profile.get("protocol", "local").upper(), profile.get("encoding", "UTF-8"), pane.font_size))
        self._multiexec_changed(self.multiexec)

    def _ignore_notebook_scroll(self, notebook, event):
        notebook.stop_emission_by_name("scroll-event")
        return False

    def _remove_tab_header(self, notebook, page, number):
        page.tab_header.destroy()

    def _switch_page(self, notebook, page, number):
        for tab in self.tab_strip.labels.get_children():
            tab.get_style_context().remove_class("active-tab")
        page.tab_header.get_style_context().add_class("active-tab")
        GLib.idle_add(self.tab_strip.reveal, page.tab_header)
        if not self._tiles_wanted and not self._layout_input_suspended and getattr(page, "active", None):
            def focus_after_switch():
                if (not self.closed and not self._tiles_wanted and not self._layout_input_suspended and
                        self.current_workspace() is page and page.active and not page.active.closed):
                    self._pane_focus(page.active)
                return False
            GLib.idle_add(focus_after_switch)

    def open_session(self, profile, on_open=None):
        self.diagnostic_log.write('debug', 'session.open')
        try:
            profile = self._resolve_group_session(profile)
            if profile.get('credential_profile') and not profile.get('_credential_provider'):
                self.credentials.prepare(profile, lambda ready:self.open_session(ready,on_open))
                return None
            identity = self._requested_identity(profile)
        except ValueError as exc:
            self.diagnostic_log.write('warning', 'session.profile-invalid')
            error(self, "Проверьте SSH-профиль", exc)
            return None
        if identity is not None:
            self._prepare_identity_session(profile, identity, lambda ready:self._open_session_ready(ready,on_open))
            return None
        return self._open_session_ready(profile,on_open)

    def _resolve_group_session(self, profile):
        if profile.get("_argv"):
            return dict(profile)
        effective = self.store.resolve_session(profile)
        for key in ('_credential_provider', '_credential_prompt', '_credential_gate'):
            if key in profile: effective[key] = profile[key]
        if profile.get("_recording_path"):
            effective["_recording_path"] = profile["_recording_path"]
        return effective

    def _open_session_ready(self, profile, on_open=None):
        if profile.get('import_warning') and 'заблокировано' in profile['import_warning']:
            error(self, 'Проверьте импортированное подключение', profile['import_warning'])
            return None
        if profile.get('credential_profile') and not profile.get('_credential_provider'):
            self.credentials.prepare(profile, lambda ready:self._open_session_ready(ready,on_open))
            return None
        if self._tiles_wanted and profile.get("protocol") in ("rdp", "vnc", "tunnel"):
            self.set_tiled_view(False)
        wanted = self._suspend_tiling()
        try:
            if not profile.get("_argv"):
                recording_path = profile.get("_recording_path")
                authentication = {key: profile[key] for key in ("_agent_key_blob", "_ssh_agent_sock", "_auth_token", "_sftp_endpoint", "_credential_provider", "_credential_prompt", "_credential_gate") if key in profile}
                profile = validate_session(profile)
                profile.update(authentication)
                if recording_path:
                    profile["_recording_path"] = recording_path
            argv = profile.get("_argv") or session_argv(profile)
            if not shutil.which(argv[0]) and not os.path.isfile(argv[0]):
                raise ValueError("Не установлен клиент «{}». Откройте Инструменты → Диагностика.".format(argv[0]))
            workspace=self._create_workspace(profile)
            self.diagnostic_log.write('info', 'session.opened')
            if on_open:on_open(workspace)
            return workspace
        except Exception as exc:
            self.diagnostic_log.write('error', 'session.open-failed')
            error(self, "Не удалось открыть подключение", exc)
            return None
        finally:
            self._resume_tiling(wanted)

    def _create_workspace(self, profile, existing_pane=None, position=-1):
        workspace = TerminalWorkspace(profile, on_focus=self._pane_focus, on_status=self.set_status,
                                      font_size=self.font_size,
                                      on_empty=lambda workspace: self.close_tab(workspace=workspace),
                                      on_input=self._user_input, on_paste=self._paste_from_pane,
                                      on_connection=self._connection_changed, on_select=self._target_changed,
                                      on_paste_pending_restart=self._restart_pending_paste,
                                      on_pane_added=self._register_pane, on_pane_close=self.close_pane,
                                      existing_pane=existing_pane)
        labelbox = Gtk.Box(spacing=8)
        labelbox.set_margin_start(8)
        labelbox.set_margin_end(8)
        labelbox.set_margin_top(4)
        labelbox.set_margin_bottom(4)
        label = Gtk.Label(label=profile.get("name", "Terminal"))
        workspace.tab_label = label
        label.set_width_chars(12)
        label.set_max_width_chars(20)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        labelbox.pack_start(label, True, True, 0)
        workspace.float_button = action_button("detach", lambda *_: self.toggle_window(workspace), "В отдельное окно")
        labelbox.pack_end(workspace.float_button, False, False, 0)
        close = action_button("close", lambda *_: self.close_tab(workspace=workspace), "Закрыть вкладку")
        close.get_style_context().add_class("flat")
        close.get_style_context().add_class("tab-close")
        close.set_relief(Gtk.ReliefStyle.NONE)
        labelbox.pack_end(close, False, False, 0)
        tab = Gtk.EventBox()
        tab.get_style_context().add_class("workspace-tab")
        tab.set_can_focus(True)
        tab.get_accessible().set_name(profile.get("name", "Терминал"))
        tab.add(labelbox)
        tab.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        tab.connect("button-press-event", self._tab_button_press, workspace)
        tab.connect("key-press-event", self._tab_key_press, workspace)
        workspace.tab_header = tab
        workspace.tab_drag_id = uuid.uuid4().hex
        targets = self._tab_targets()
        tab.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, targets[:1], Gdk.DragAction.MOVE)
        tab.drag_dest_set(Gtk.DestDefaults.ALL, targets, Gdk.DragAction.MOVE)
        tab.connect("drag-begin", self._tab_drag_begin, workspace)
        tab.connect("drag-end", self._tab_drag_end)
        tab.connect("drag-data-get", self._tab_drag_data_get, workspace)
        tab.connect("drag-motion", self._tab_drag_motion, workspace)
        tab.connect("drag-leave", self._tab_drag_leave)
        tab.connect("drag-data-received", self._tab_drag_received, workspace)
        tab.show_all()
        headers = self.tab_strip.labels
        headers.pack_start(tab, False, False, 0)
        if position >= 0:
            headers.reorder_child(tab, position)
        index = self.notebook.insert_page(workspace, None, position)
        workspace.show_all()
        self.notebook.set_current_page(index)
        self._overview_active = workspace.active
        self.set_status("Открыто: " + profile.get("name", "Terminal"))
        return workspace

    def toggle_window(self, workspace):
        return self.dock_window(workspace) if workspace in self.floating_windows else self.float_window(workspace)

    def float_window(self, workspace):
        if self.closed or self.notebook.page_num(workspace) < 0 or workspace in self.floating_windows:
            return False
        wanted = self._suspend_tiling()
        try:
            floating = FloatingWorkspace(self, workspace)
            self.floating_windows[workspace] = floating
            workspace.float_button.set_tooltip_text('Вернуть во вкладки')
        finally:
            self._resume_tiling(wanted)
        workspace.active.terminal.grab_focus()
        return True

    def dock_window(self, workspace):
        floating = self.floating_windows.pop(workspace, None)
        if floating is None:
            return False
        wanted = self._suspend_tiling()
        try:
            floating.dock()
            workspace.float_button.set_tooltip_text('В отдельное окно')
        finally:
            self._resume_tiling(wanted)
        self.notebook.set_current_page(self.notebook.page_num(workspace))
        self._pane_focus(workspace.active)
        self.present()
        workspace.active.terminal.grab_focus()
        return True

    def new_local(self, *_):
        profile = default_session("local")
        profile["name"] = "Локальный терминал"
        return self.open_session(profile)

    def _tab_button_press(self, widget, event, workspace):
        if event.button == 1:
            self.notebook.set_current_page(self.notebook.page_num(workspace))
        if event.button == 2:
            self.close_tab(workspace=workspace, ask=False)
            return True
        if event.button == 3:
            self.tab_context_menu(workspace, event)
            return True
        return False

    def _tab_key_press(self, widget, event, workspace):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.notebook.set_current_page(self.notebook.page_num(workspace))
            return True
        return False

    def tab_context_menu(self, workspace, event=None):
        if self.notebook.page_num(workspace) < 0:
            return None
        if self._tab_context_menu is not None:
            self._tab_context_menu.destroy()
        menu = Gtk.Menu()
        self._tab_context_menu = menu
        actions = [("Дублировать вкладку", "duplicate"), ("Переподключиться", "reconnect"),
                   ("Закрыть текущую вкладку", "close-current"), ("Закрыть все", "close-all"),
                   ("Закрыть все кроме текущей", "close-others")]
        actions.append(("Вернуть во вкладки" if workspace in self.floating_windows else "В отдельное окно",
                        "dock-window" if workspace in self.floating_windows else "float-window"))
        if len(workspace.panes) > 1:
            actions.append(("Разъединить все", "separate-all"))
        for label, kind in actions:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda item, action=kind: self.tab_action(action, workspace))
            menu.append(item)
        menu.show_all()
        if event is not None:
            menu.popup(None, None, None, None, event.button, event.time)
        return menu

    def tab_action(self, kind, workspace):
        if self.closed or self.notebook.page_num(workspace) < 0:
            return False
        if kind=='duplicate':
            try:profiles=[self._latest_pane_profile(pane) for pane in list(workspace.panes)]
            except ValueError as exc:error(self,'Не удалось дублировать вкладку',exc);return False
            source_panes=list(workspace.panes)
            def snapshot(widget):
                if isinstance(widget,Gtk.Paned):
                    size=widget.get_allocated_width() if widget.get_orientation()==Gtk.Orientation.HORIZONTAL else widget.get_allocated_height()
                    return (widget.get_orientation(),widget.get_position()/float(max(1,size)),snapshot(widget.get_child1()),snapshot(widget.get_child2()))
                return source_panes.index(widget)
            root=self.floating_windows[workspace].body.get_children()[0] if workspace in self.floating_windows else workspace.get_children()[0]
            layout=snapshot(root)
            active_index=source_panes.index(workspace.active)
            target=[None]
            def next_copy(index):
                if self.closed:return
                if index>=len(profiles):
                    target[0].restore_layout(layout)
                    target[0].active=target[0].panes[active_index];self._pane_focus(target[0].active)
                    return
                def opened(created):
                    if target[0] is None:target[0]=created
                    elif self.notebook.page_num(target[0])>=0:self.merge_tabs(created,target[0])
                    next_copy(index+1)
                self.open_session(profiles[index],on_open=opened)
            next_copy(0);return True
        if kind=='reconnect':
            panes=list(workspace.panes)
            if any(p.pid and not p.exited for p in panes) and not confirm(self,'Переподключить вкладку?','Текущие процессы этой вкладки будут завершены.'):return False
            for pane in panes:
                if not pane.closed:self.reconnect_pane(pane,ask=False)
            return True
        if kind == "float-window":
            return self.float_window(workspace)
        if kind == "dock-window":
            return self.dock_window(workspace)
        if kind == "separate-all":
            self.dock_window(workspace)
            return self.separate_all(workspace)
        if kind == "close-current":
            return self.close_tab(workspace=workspace)
        if kind not in ("close-all", "close-others"):
            return False
        targets = [self.notebook.get_nth_page(i) for i in range(self.notebook.get_n_pages())]
        if kind == "close-others":
            targets = [target for target in targets if target is not workspace]
        if any(pane.pid and not pane.exited for target in targets for pane in target.panes):
            if not confirm(self, "Закрыть выбранные вкладки и завершить их процессы?",
                           "Несохранённая работа в этих терминалах может быть потеряна."):
                return False
        if self.closed or self.notebook.page_num(workspace) < 0:
            return False
        for target in targets:
            self.close_tab(workspace=target, ask=False)
        return True

    @staticmethod
    def _tab_targets():
        return [Gtk.TargetEntry.new("application/x-astraterm-tab", Gtk.TargetFlags.SAME_APP, 0),
                Gtk.TargetEntry.new("application/x-astraterm-pane", Gtk.TargetFlags.SAME_APP, 1)]

    def _pane_drag_begin(self, widget, context, pane):
        self._dragged_pane = pane
        self._dragged_workspace = None

    def _pane_drag_data_get(self, widget, context, selection, info, timestamp, pane):
        selection.set(selection.get_target(), 8, pane.pane_drag_id.encode("ascii"))

    def _tab_drag_begin(self, widget, context, workspace):
        self._dragged_workspace = workspace
        self._dragged_pane = None

    def _tab_drag_end(self, *_):
        self._dragged_workspace = None
        self._dragged_pane = None
        self._tab_drag_leave(self.tab_drop_target)
        self._tab_drag_leave(self.tab_strip.labels)
        for index in range(self.notebook.get_n_pages()):
            self.notebook.get_nth_page(index).tab_header.get_style_context().remove_class("tab-drop-target")

    def _tab_drag_motion(self, widget, context, x, y, timestamp, target):
        source = self._workspace_for_pane(self._dragged_pane) if self._dragged_pane else self._dragged_workspace
        if self._can_merge_tabs(source, target):
            widget.get_style_context().add_class("tab-drop-target")
            return False
        Gdk.drag_status(context, Gdk.DragAction(0), timestamp)
        return True

    def _tab_drag_leave(self, widget, *_):
        widget.get_style_context().remove_class("tab-drop-target")

    def _cancel_drag_motion(self, widget, context, x, y, timestamp):
        if not widget.drag_dest_find_target(context, None):
            return False
        widget.get_style_context().add_class("tab-drop-target")
        action = Gdk.DragAction.MOVE if self._dragged_workspace or self._dragged_pane else Gdk.DragAction.COPY
        Gdk.drag_status(context, action, timestamp)
        return True

    def _cancel_drag_drop(self, widget, context, x, y, timestamp):
        self._tab_drag_leave(widget)
        Gtk.drag_finish(context, False, False, timestamp)
        self._tab_drag_end()
        return True

    def _tab_drag_data_get(self, widget, context, selection, info, timestamp, workspace):
        selection.set(selection.get_target(), 8, workspace.tab_drag_id.encode("ascii"))

    def _tab_drag_received(self, widget, context, x, y, selection, info, timestamp, target):
        self._tab_drag_leave(widget)
        pane, source = self._drag_payload(selection)
        valid = self._can_merge_tabs(source, target)
        Gtk.drag_finish(context, bool(valid), False, timestamp)
        self._tab_drag_end()
        if valid:
            def apply():
                if self._can_merge_tabs(source, target):
                    if pane is None:
                        self.merge_tabs(source, target)
                    elif pane in source.panes:
                        self.move_pane_to_workspace(pane, target)
                        self._pane_focus(pane)
                        pane.terminal.grab_focus()
                return False
            GLib.idle_add(apply)

    def _drag_payload(self, selection):
        pane, source = self._dragged_pane, self._dragged_workspace
        mime = selection.get_target().name()
        if pane is not None and mime == "application/x-astraterm-pane":
            if selection.get_data() == pane.pane_drag_id.encode("ascii"):
                return pane, self._workspace_for_pane(pane)
        if source is not None and mime == "application/x-astraterm-tab":
            if selection.get_data() == source.tab_drag_id.encode("ascii"):
                return None, source
        return None, None

    def _separate_drop_area(self, widget, x, y):
        if widget is self.tab_drop_target:
            return True
        for tab in self.tab_strip.labels.get_children():
            origin = tab.translate_coordinates(widget, 0, 0)
            if origin is not None and origin[0] <= x < origin[0] + tab.get_allocated_width():
                return False
        return True

    def _separate_drag_motion(self, widget, context, x, y, timestamp):
        source = self._workspace_for_pane(self._dragged_pane) if self._dragged_pane else self._dragged_workspace
        if (not self._tiles_wanted and source is not None and self.notebook.page_num(source) >= 0 and
                len(source.panes) > 1 and self._separate_drop_area(widget, x, y)):
            widget.get_style_context().add_class("tab-drop-target")
            return False
        Gdk.drag_status(context, Gdk.DragAction(0), timestamp)
        return True

    def _separate_drag_received(self, widget, context, x, y, selection, info, timestamp):
        pane, source = self._drag_payload(selection)
        valid = (not self.closed and not self._tiles_wanted and source is not None and
                 self.notebook.page_num(source) >= 0 and len(source.panes) > 1 and
                 self._separate_drop_area(widget, x, y))
        focused = source.active if valid else None
        Gtk.drag_finish(context, bool(valid), False, timestamp)
        self._tab_drag_end()
        if valid:
            def apply():
                if pane is not None:
                    self.detach_pane_to_tab(pane)
                elif self.separate_all(source):
                    owner = self._workspace_for_pane(focused)
                    if owner is not None:
                        self.notebook.set_current_page(self.notebook.page_num(owner))
                        self._pane_focus(focused)
                        focused.terminal.grab_focus()
                return False
            GLib.idle_add(apply)

    def _can_merge_tabs(self, source, target):
        return (not self.closed and not self._tiles_wanted and source is not None and
                target is not None and source is not target and
                self.notebook.page_num(source) >= 0 and self.notebook.page_num(target) >= 0 and
                bool(source.panes) and bool(target.panes) and
                all(pane.profile.get("protocol") not in ("rdp", "vnc", "tunnel")
                    for pane in source.panes + target.panes))

    def merge_tabs(self, source, target):
        """Move a complete tab into another tab without replacing any PTY."""
        if not self._can_merge_tabs(source, target):
            return False
        self.dock_window(source)
        self.dock_window(target)
        focused = source.active
        for pane in list(source.panes):
            self.move_pane_to_workspace(pane, target, append=True)
        target.balance_horizontal()
        target.active = focused
        self._pane_focus(focused)
        focused.terminal.grab_focus()
        return True

    def new_session(self, *_):
        self._session_dialog(None)

    def _session_dialog(self, profile):
        if profile is None:
            profile = default_session()
            profile["group"] = self._selected_group_path()
        dialog = SessionDialog(self, profile, identity_profiles=self.identity_store.profiles if self.identity_store else None,
                               session_store=self.store, credential_profiles=self.credential_vault.metadata())
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                result = dialog.get_session()
                self.store.upsert(result)
                self._refresh_sessions()
                self.set_status("Подключение сохранено: " + result["name"])
                break
            except Exception as exc:
                error(dialog, "Проверьте параметры подключения", exc)
        dialog.destroy()

    def _selected_session(self):
        model, iter_ = self.session_tree.get_selection().get_selected()
        if iter_:
            identifier = model[iter_][1]
            return next((item for item in self.store.sessions if item["id"] == identifier), None)
        return None

    def edit_session(self, *_):
        profile = self._selected_session()
        if profile:
            self._session_dialog(profile)
        elif self._selected_group_path():
            self.edit_group()

    def _rename_session_key(self, _tree, event):
        if event.keyval != Gdk.KEY_F2 or event.state & Gtk.accelerator_get_default_mod_mask():
            return False
        profile = self._selected_session()
        group = self._selected_group_path() if profile is None else ""
        if profile is None and not group:
            return True
        initial = profile["name"] if profile else group.rsplit("/", 1)[-1]
        title = "Переименовать подключение" if profile else "Переименовать папку"
        name = name_dialog(self, title, initial)
        if name is None or name.strip() == initial:
            return True
        name = name.strip()
        try:
            if not name:
                raise ValueError("Введите название.")
            if profile:
                current = next((item for item in self.store.sessions if item["id"] == profile["id"]), None)
                if current is None:
                    raise ValueError("Подключение уже удалено.")
                self.store.upsert(dict(current, name=name))
            else:
                if "/" in name or "\\" in name:
                    raise ValueError("Введите название папки без разделителей.")
                parent = group.rpartition("/")[0]
                renamed = self.store.rename_group(group, "/".join(filter(None, (parent, name))))
            self._refresh_sessions()
            if not profile:
                self._select_group(renamed["name"], expand=False)
        except (OSError, ValueError) as exc:
            error(self, title, exc)
        return True

    def _selected_group_path(self):
        model, iterator = self.session_tree.get_selection().get_selected()
        return model[iterator][4] if iterator is not None else ""

    def new_group(self, *_):
        self._group_dialog(parent_group=self._selected_group_path())

    def edit_group(self, *_, ssh_settings=False):
        name = self._selected_group_path()
        group = next((item for item in self.store.groups if item["name"] == name), None)
        if group is not None:
            self._group_dialog(group, ssh_settings=ssh_settings)

    def _group_dialog(self, group=None, parent_group="", ssh_settings=False):
        dialog = GroupDialog(self, self.store, group=group, parent_group=parent_group,
                             identity_profiles=self.identity_store.profiles if self.identity_store else None)
        if ssh_settings:
            dialog.notebook.set_current_page(1)
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                record = self.store.upsert_group(dialog.get_group(), apply_to_sessions=dialog.apply_to_sessions)
                self._refresh_sessions()
                self._select_group(record["name"])
                self.set_status("Папка сохранена: " + record["name"])
                break
            except Exception as exc:
                error(dialog, "Проверьте настройки папки", exc)
        dialog.destroy()

    def _select_group(self, name, expand=True):
        iterator = self._group_rows.get(name)
        if iterator is not None:
            path = self.session_tree_store.get_path(iterator)
            if expand:
                self.session_tree.expand_to_path(path)
            self.session_tree.get_selection().select_iter(iterator)
            self.session_tree.scroll_to_cell(path, None, False, 0, 0)

    def _read_collapsed_groups(self):
        try:
            with open(self._sidebar_path, encoding='utf-8') as stream:
                saved = json.load(stream)
            collapsed = saved.get('collapsed') if isinstance(saved, dict) else None
            if isinstance(collapsed, list):
                return {item for item in collapsed if isinstance(item, str)}
        except (OSError, ValueError):
            pass
        return set()

    def _group_expansion(self, tree, iterator, path, expanded):
        if not tree.get_model()[iterator][1]:
            tree.get_model()[iterator][3] = group_icon(expanded)
            if not getattr(self, "_rebuilding_sessions", False) and not self.session_search.get_text().strip():
                name = tree.get_model()[iterator][4]
                identifier = self._group_ids.get(name, '')
                try:
                    with _config_lock(self._sidebar_path):
                        collapsed = self._read_collapsed_groups()
                        if expanded:
                            collapsed.discard(identifier)
                        else:
                            collapsed.add(identifier)
                        _atomic_json(self._sidebar_path, {'collapsed': sorted(collapsed)})
                    self._collapsed_groups = collapsed
                except OSError as exc:
                    self.set_status('Не удалось запомнить состояние папки · ' + str(exc))

    def _refresh_sessions(self, select_id=None):
        selected = self._selected_session()
        selected_group = self._selected_group_path()
        self._rebuilding_sessions = True
        self.session_tree_store.clear()
        query = self.session_search.get_text().lower()
        self._group_rows, session_rows = {}, {}
        self._group_ids = {group['name']: group['id'] for group in self.store.groups}
        def folder(name):
            if name in self._group_rows:
                return self._group_rows[name]
            parent_name = name.rsplit("/", 1)[0] if "/" in name else None
            parent = folder(parent_name) if parent_name is not None else None
            iterator = self.session_tree_store.append(parent, [name.rsplit("/", 1)[-1] if name else "Без группы",
                "", name or "Подключения без группы", group_icon(False), name, None])
            self._group_rows[name] = iterator
            return iterator
        for group in sorted(self.store.groups, key=lambda item: item["name"].lower()):
            if not query or query in group["name"].lower():
                folder(group["name"])
        for item in self.store.sessions:
            if query not in " ".join(str(item.get(key, "")) for key in ("name", "host", "group", "username")).lower():
                continue
            effective = self.store.resolve_session(item)
            group = folder(item.get("group") or "")
            tooltip = "{} · {}@{}:{}".format(effective["protocol"].upper(), effective.get("username") or "default",
                                            effective.get("host", ""), effective.get("port", ""))
            session_rows[item["id"]] = self.session_tree_store.append(group,
                [item["name"], item["id"], tooltip, protocol_icon(item["protocol"]), item.get("group") or "",
                 platform_pixbuf(item["protocol"])])
        self.session_tree.expand_all()
        if not query:
            for name in sorted(self._group_rows, key=lambda value: value.count('/'), reverse=True):
                if self._group_ids.get(name, '') in self._collapsed_groups:
                    self.session_tree.collapse_row(self.session_tree_store.get_path(self._group_rows[name]))
        if select_id in session_rows:
            iterator = session_rows[select_id]
            self.session_tree.get_selection().select_iter(iterator)
            self.session_tree.scroll_to_cell(self.session_tree_store.get_path(iterator), None, False, 0, 0)
        elif selected and selected["id"] in session_rows:
            self.session_tree.get_selection().select_iter(session_rows[selected["id"]])
        elif selected_group in self._group_rows:
            self.session_tree.get_selection().select_iter(self._group_rows[selected_group])
        self._rebuilding_sessions = False
        self.session_count.set_text(str(len(self.store.sessions)))

    def _session_activated(self, tree, path, column):
        profile = self._selected_session()
        if profile:
            self.open_session(profile)
        else:
            tree.collapse_row(path) if tree.row_expanded(path) else tree.expand_row(path, False)

    def _session_context(self, tree, event):
        if event.button != 3:
            return False
        location = tree.get_path_at_pos(int(event.x), int(event.y))
        if location:
            tree.get_selection().select_path(location[0])
        else:
            tree.get_selection().unselect_all()
        profile = self._selected_session()
        menu = Gtk.Menu()
        def delete(*_):
            if confirm(self, "Удалить подключение «{}»?".format(profile["name"])):
                try:
                    self.store.delete(profile["id"])
                    self._refresh_sessions()
                except Exception as exc:
                    error(self, "Не удалось удалить подключение", exc)
        def duplicate(*_):
            try:
                saved = self.store.duplicate(profile["id"])
                self._refresh_sessions(select_id=saved["id"])
                self.set_status("Создано подключение «{}»".format(saved["name"]))
            except (OSError, ValueError) as exc:
                error(self, "Не удалось дублировать подключение", exc)
        if profile:
            actions = [("Подключиться", lambda *_: self.open_session(profile)), ("Изменить…", self.edit_session),
                       ("Переместить в папку…", lambda *_: self.move_session_to_group(profile)),
                       ("Дублировать", duplicate), ("Удалить", delete)]
        else:
            actions = [("Новое подключение…", self.new_session), ("Новая папка…", self.new_group)]
            if self._selected_group_path():
                actions += [("Настройки папки / переименовать…", self.edit_group),
                            ("Общие SSH-настройки…", lambda *_: self.edit_group(ssh_settings=True)),
                            ("Удалить папку…", self.delete_group)]
        for label, callback in actions:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def delete_group(self, *_):
        name = self._selected_group_path()
        if name and confirm(self, "Удалить папку «{}»?".format(name),
                            "Вложенные папки будут удалены. Подключения сохранятся в родительской папке со своими SSH-настройками."):
            try:
                self.store.delete_group(name)
                self._refresh_sessions()
            except Exception as exc:
                error(self, "Не удалось удалить папку", exc)

    def move_session_to_group(self, profile):
        dialog = Gtk.Dialog(title="Переместить подключение", transient_for=self, modal=True)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Переместить", Gtk.ResponseType.OK)
        choices = Gtk.ComboBoxText()
        choices.append("", "Без группы")
        for group in self.store.groups:
            choices.append(group["name"], group["name"])
        choices.set_active_id(profile.get("group") or "")
        dialog.get_content_area().set_border_width(16)
        dialog.get_content_area().pack_start(choices, False, False, 0)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                moved = dict(profile)
                moved["group"] = choices.get_active_id() or ""
                self.store.upsert(moved)
                self._refresh_sessions()
            except Exception as exc:
                error(dialog, "Не удалось переместить подключение", exc)
        dialog.destroy()

    def quick_connect(self, *_):
        raw = self.quick.get_text().strip()
        if not raw:
            return
        try:
            from urllib.parse import urlsplit
            url = urlsplit(raw if raw.startswith("ssh://") else "ssh://" + raw)
            if url.scheme != "ssh" or url.password is not None or url.path not in ("", "/") or url.query or url.fragment:
                raise ValueError("Введите user@host:22 или ssh://user@host:22 без пароля")
            profile = default_session("ssh")
            profile.update(host=url.hostname or "", username=url.username or "", port=url.port or 22,
                           name=raw, x11=False)
            profile = validate_session(profile)
            self.open_session(profile)
        except Exception as exc:
            error(self, "Не удалось разобрать адрес", exc)

    def split(self, orientation):
        workspace = self.current_workspace()
        if workspace:
            if workspace.active.profile.get("protocol") in ("rdp", "vnc", "tunnel"):
                error(self, "Для этого подключения разделение недоступно", "Откройте локальный или SSH-терминал.")
                return
            dialog = Gtk.Dialog(title="Добавить терминал рядом", transient_for=self, modal=True)
            dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Разделить", Gtk.ResponseType.OK)
            content = dialog.get_content_area()
            content.set_border_width(16)
            content.set_spacing(10)
            content.pack_start(Gtk.Label(label="Выберите открытый терминал или подключение другого хоста:", xalign=0), False, False, 0)
            choices = Gtk.ComboBoxText()
            actions = []
            active = self.active_pane()
            for pane in self._terminal_panes():
                if pane is not active:
                    key = str(len(actions))
                    choices.append(key, "Открытый: " + pane.identity)
                    actions.append(("existing", pane))
            for profile in self.store.sessions:
                if profile.get("protocol") not in ("rdp", "vnc"):
                    key = str(len(actions))
                    choices.append(key, "Подключение: {} · {}".format(profile["name"], profile.get("host", "local")))
                    actions.append(("profile", profile))
            choices.append(str(len(actions)), "Новый локальный терминал")
            actions.append(("profile", default_session("local")))
            choices.append(str(len(actions)), "Новое подключение к текущему хосту")
            actions.append(("profile", dict(active.profile)))
            choices.set_active(0)
            content.pack_start(choices, False, False, 0)
            note = Gtk.Label(label="Открытый терминал будет перенесён без переподключения и потери истории.", xalign=0)
            note.set_line_wrap(True)
            content.pack_start(note, False, False, 0)
            dialog.show_all()
            response = dialog.run()
            selected = choices.get_active()
            dialog.destroy()
            if response == Gtk.ResponseType.OK and selected >= 0:
                kind, item = actions[selected]
                if kind == "existing":
                    self.move_pane_to_workspace(item, workspace, orientation)
                else:
                    self.split_with_profile(item, orientation)
        else:
            self.new_local()

    def split_with_profile(self, profile, orientation=Gtk.Orientation.HORIZONTAL):
        try:
            profile = self._resolve_group_session(profile)
            identity = self._requested_identity(profile)
        except ValueError as exc:
            error(self, "Проверьте SSH-профиль", exc)
            return None
        if identity is not None:
            workspace = self.current_workspace()
            def prepared(effective):
                if workspace is None or self.notebook.page_num(workspace) < 0:
                    self._open_session_ready(effective)
                else:
                    self._split_ready(effective, orientation, workspace)
            self._prepare_identity_session(profile, identity, prepared)
            return None
        return self._split_ready(profile, orientation)

    def _split_ready(self, profile, orientation=Gtk.Orientation.HORIZONTAL, workspace=None):
        if profile.get('import_warning') and 'заблокировано' in profile['import_warning']:
            error(self, 'Проверьте импортированное подключение', profile['import_warning'])
            return None
        if profile.get('credential_profile') and not profile.get('_credential_provider'):
            self.credentials.prepare(profile, lambda ready: self._split_ready(ready, orientation, workspace))
            return None
        workspace = workspace or self.current_workspace()
        if workspace is None:
            return self._open_session_ready(profile)
        self.dock_window(workspace)
        wanted = self._suspend_tiling()
        try:
            authentication = {key: profile[key] for key in ("_agent_key_blob", "_ssh_agent_sock", "_auth_token", "_sftp_endpoint", "_credential_provider", "_credential_prompt", "_credential_gate") if key in profile}
            profile = dict(profile) if profile.get("_argv") else validate_session(profile)
            profile.update(authentication)
            pane = workspace.split(orientation, profile=profile)
            self._overview_active = pane
            self._update_tab_label(workspace)
            return pane
        finally:
            self._resume_tiling(wanted)

    def move_pane_to_workspace(self, pane, workspace, orientation=Gtk.Orientation.HORIZONTAL, append=False):
        origin = self._workspace_for_pane(pane)
        if origin is None or pane is workspace.active:
            return pane
        self.dock_window(origin)
        self.dock_window(workspace)
        wanted = self._suspend_tiling()
        try:
            origin.detach_pane(pane)
            workspace.split(orientation, existing_pane=pane, append=append)
            if not origin.panes:
                index = self.notebook.page_num(origin)
                if index >= 0:
                    self.notebook.remove_page(index)
                origin.destroy()
            else:
                self._update_tab_label(origin)
            self.notebook.set_current_page(self.notebook.page_num(workspace))
            self._overview_active = pane
            self._update_tab_label(workspace)
            return pane
        finally:
            self._resume_tiling(wanted)

    def detach_pane_to_tab(self, pane, position=None):
        origin = self._workspace_for_pane(pane)
        if self.closed or self._tiles_wanted or origin is None or len(origin.panes) < 2:
            return None
        self.dock_window(origin)
        if position is None:
            position = self.notebook.page_num(origin) + 1
        wanted = self._suspend_tiling()
        try:
            origin.detach_pane(pane)
            workspace = self._create_workspace(pane.profile, existing_pane=pane, position=position)
            self._update_tab_label(origin)
            self._pane_focus(pane)
            pane.terminal.grab_focus()
            return workspace
        finally:
            self._resume_tiling(wanted)

    def separate_all(self, workspace):
        if (self.closed or self._tiles_wanted or self.notebook.page_num(workspace) < 0 or
                len(workspace.panes) < 2):
            return False
        focused = self.active_pane()
        position = self.notebook.page_num(workspace)
        for offset, pane in enumerate(list(workspace.panes)[1:], 1):
            self.detach_pane_to_tab(pane, position=position + offset)
        owner = self._workspace_for_pane(focused)
        if owner is not None:
            self.notebook.set_current_page(self.notebook.page_num(owner))
            self._pane_focus(focused)
            focused.terminal.grab_focus()
        return True

    def _update_tab_label(self, workspace):
        if workspace.panes and hasattr(workspace, "tab_label"):
            name = workspace.panes[0].profile.get("name", "Терминал")
            if len(workspace.panes) > 1:
                name += " +{}".format(len(workspace.panes) - 1)
            workspace.tab_label.set_text(name)
            workspace.tab_header.get_accessible().set_name(name)
            workspace.tab_label.set_tooltip_text("\n".join(pane.identity for pane in workspace.panes))

    def close_pane(self, pane):
        workspace = self._workspace_for_pane(pane)
        if not workspace:
            return
        if len(workspace.panes) == 1:
            self.close_tab(workspace=workspace)
            return
        wanted = self._suspend_tiling()
        try:
            workspace.close_pane(pane)
            self._overview_active = workspace.active
            self._update_tab_label(workspace)
        finally:
            self._resume_tiling(wanted)

    def close_tab(self, *_, workspace=None, ask=True):
        workspace = workspace or self.current_workspace()
        if self.closed or workspace is None or self.notebook.page_num(workspace) < 0:
            return
        if ask and any(pane.pid for pane in workspace.panes):
            if not confirm(self, "Закрыть вкладку и завершить её процессы?",
                           "Несохранённая работа в терминалах этой вкладки может быть потеряна."):
                return
        self.dock_window(workspace)
        index = self.notebook.page_num(workspace)
        wanted = self._suspend_tiling()
        try:
            workspace.close()
            if index >= 0:
                self.notebook.remove_page(index)
            workspace.destroy()
            self._overview_active = None
        finally:
            self._resume_tiling(wanted)
        self._sync_file_browser(self.active_pane())

    def send_text(self, text):
        pane = self.active_pane()
        if pane is not None and pane.pid and pane.profile.get("protocol") not in ("rdp", "vnc", "tunnel"):
            pane.send(text)
            pane.terminal.grab_focus()
        else:
            self.set_status("Сначала откройте активный терминал")

    def find(self, *_):
        pane = self.active_pane()
        if pane:
            pane.show_search()

    def zoom(self, delta):
        self.font_size = max(7, min(28, self.font_size + delta))
        for i in range(self.notebook.get_n_pages()):
            for pane in self.notebook.get_nth_page(i).panes:
                pane.set_font_size(self.font_size)
        if self.active_pane():
            self._pane_focus(self.active_pane())

    def save_output(self, *_):
        if self.active_pane():
            self.active_pane().save_output()

    def record_session(self, *_):
        pane = self.active_pane()
        profile = dict(pane.profile) if pane else default_session("local")
        if profile.get("protocol") in ("tunnel", "rdp", "vnc"):
            error(self, "Запись доступна для терминальных сессий")
            return
        if not shutil.which("script"):
            error(self, "Не установлена утилита script из util-linux")
            return
        path = self._choose_file("Запись новой сессии: файл журнала", save=True)
        if path:
            profile["id"] = uuid.uuid4().hex
            profile["name"] = (profile.get("name") or "Терминал") + " · запись"
            profile["_recording_path"] = path
            self.open_session(profile)

    def reconnect(self, *_):
        if self.active_pane():
            self.reconnect_pane(self.active_pane())

    def _latest_pane_profile(self,pane):
        source=next((s for s in self.store.sessions if s['id']==pane.profile.get('id')),pane.profile)
        if source.get('protocol')!=pane.profile.get('protocol'):raise ValueError('Протокол подключения изменён. Откройте новую вкладку из списка подключений.')
        result=dict(source) if source.get('_argv') else validate_session(source)
        if source is pane.profile:
            for key in ('_credential_provider','_credential_prompt','_credential_gate'):
                if key in source:result[key]=source[key]
        result.pop('_recording_path',None)
        return result

    def reconnect_pane(self,pane,ask=True):
        if self.closed or pane.closed:return
        generation=pane.connection_generation
        try:
            profile=self._latest_pane_profile(pane)
            if profile.get('protocol')!=pane.profile.get('protocol'):raise ValueError('Протокол подключения изменён. Откройте новую вкладку из списка подключений.')
            profile=self._resolve_group_session(profile)
            if pane.profile.get('_recording_path'):profile['_recording_path']=pane.profile['_recording_path']
        except ValueError as exc:error(self,'Не удалось переподключиться',exc);return
        def prepared(ready):
            if not self.closed and not pane.closed and pane.connection_generation==generation:
                pane.restart(profile=ready,ask=ask)
                workspace=self._workspace_for_pane(pane)
                if workspace:workspace.tab_label.set_text(ready.get('name','Terminal'))
        def authenticate(ready):
            if self.closed or pane.closed or pane.connection_generation!=generation:return
            try:
                identity=self._requested_identity(ready)
                if identity:self._prepare_identity_session(ready,identity,prepared)
                else:prepared(ready)
            except ValueError as exc:error(self,'Проверьте SSH-профиль',exc)
        if profile.get('credential_profile'):self.credentials.prepare(profile,authenticate)
        else:authenticate(profile)

    def _restart_pending_paste(self, pane):
        # VTE 0.46 has no public cancellation API for its clipboard request.
        # Keep that widget isolated from the replacement connection.
        profile = dict(getattr(pane,'_replacement_profile',None) or pane.profile)
        profile.pop("_recording_path", None)
        workspace = self.open_session(profile)
        if workspace is not None:
            self.set_status("Открыто новое подключение; прежняя панель сохранена для истории, отложенная вставка отменена")
        return workspace

    def toggle_files(self, *_):
        if self._tiles_wanted:
            self._overview_files_visible = None
        self._toggle(self.file_browser)

    def toggle_sidebar(self, *_):
        self._toggle(self.sidebar)

    @staticmethod
    def _toggle(widget):
        visible = not widget.get_visible()
        widget.set_no_show_all(not visible)
        widget.set_visible(visible)

    @staticmethod
    def _sync_panel_toggle(panel, _property, control, label):
        visible = panel.get_visible()
        description = ('Скрыть ' if visible else 'Показать ') + label
        control.set_tooltip_text(description)
        control.get_accessible().set_name(description)
        context = control.get_style_context()
        if visible:
            context.add_class('panel-visible')
        else:
            context.remove_class('panel-visible')

    def macros(self, *_):
        dialog = MacroDialog(self, self.store, self.send_text)
        dialog.run()
        dialog.destroy()

    def tunnels(self, *_):
        from .tunnels import TunnelDialog
        dialog = TunnelDialog(self, self.store, self.start_tunnel, self.stop_tunnel, self.tunnel_active)
        dialog.run()
        dialog.destroy()

    def tunnel_active(self, identifier):
        pane = self.tunnel_sessions.get(identifier)
        return bool(pane and pane.pid and not pane.closed)

    def start_tunnel(self, item):
        if self.tunnel_active(item["id"]):
            self.set_status("Туннель уже запущен: " + item["name"])
            return
        argv = tunnel_argv(item)
        profile = {"id": "tunnel-" + item["id"], "protocol": "tunnel", "name": "Туннель · " + item["name"],
                   "_argv": argv, "host": item["host"], "encoding": "UTF-8"}
        workspace = self.open_session(profile)
        if workspace:
            self.tunnel_sessions[item["id"]] = workspace.active

    def stop_tunnel(self, identifier):
        pane = self.tunnel_sessions.get(identifier)
        if pane:
            pane._hangup()
            self.set_status("Туннель останавливается")

    def _choose_file(self, title, save=False):
        dialog = Gtk.FileChooserDialog(title=title, transient_for=self,
                                       action=Gtk.FileChooserAction.SAVE if save else Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить" if save else "Открыть", Gtk.ResponseType.OK)
        if save:
            dialog.set_do_overwrite_confirmation(True)
            dialog.set_current_name("astraterm-connections.json")
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        return path

    def import_sessions(self, *_):
        self.credentials.import_files()

    def export_sessions(self, *_):
        path = self._choose_file("Экспорт подключений", save=True)
        if path:
            try:
                self.store.export_file(path)
                self.notify("Подключения экспортированы: " + path)
            except Exception as exc:
                error(self, "Ошибка экспорта", exc)

    def import_ssh(self, *_):
        path = self._choose_file("Импорт OpenSSH config")
        if not path:
            return
        try:
            items = parse_ssh_config(path)
            if not items:
                self.set_status("В файле нет подходящих записей Host")
                return
            if not confirm(self, "Импортировать {} подключений из OpenSSH?".format(len(items)),
                           "Host с шаблонами, Include, Match и ProxyCommand не импортируются."):
                return
            previous = list(self.store.sessions)
            self.store.sessions += items
            try:
                self.store.save()
            except Exception:
                self.store.sessions = previous
                raise
            self._refresh_sessions()
            self.set_status("Импортировано: {}".format(len(items)))
        except Exception as exc:
            error(self, "Ошибка импорта OpenSSH", exc)

    def settings(self, *_):
        diagnostic_log = self.diagnostic_log
        dialog = Gtk.Dialog(title='Настройки', transient_for=self, modal=True)
        dialog.add_buttons('Отмена', Gtk.ResponseType.CANCEL, 'Сохранить', Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)
        enabled = Gtk.CheckButton(label='Записывать диагностический журнал', active=diagnostic_log.enabled)
        levels = Gtk.ComboBoxText()
        for key, title in (('error', 'Ошибки'), ('warning', 'Предупреждения'), ('info', 'Информация'), ('debug', 'Отладка')):
            levels.append(key, title)
        levels.set_active_id(diagnostic_log.level)
        levels.set_sensitive(enabled.get_active())
        enabled.connect('toggled', lambda button: levels.set_sensitive(button.get_active()))
        path = Gtk.Label(label=diagnostic_log.filename, xalign=0, selectable=True)
        path.set_line_wrap(True)
        box.add(enabled)
        box.add(Gtk.Label(label='Уровень логирования', xalign=0))
        box.add(levels)
        box.add(path)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                diagnostic_log.configure(enabled.get_active(), levels.get_active_id())
                diagnostic_log.write('info', 'logging.configured')
                self.notify('Настройки журнала сохранены')
                break
            except (OSError, ValueError):
                error(dialog, 'Не удалось сохранить настройки журнала')
        dialog.destroy()

    def diagnostics(self, *_):
        dialog = Gtk.Dialog(title="Диагностика AstraTerm", transient_for=self, modal=True)
        dialog.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(640, 470)
        content = dialog.get_content_area()
        content.set_border_width(14)
        content.set_spacing(10)
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        content.pack_start(scroll, True, True, 0)
        deps = detect_dependencies()
        lines = ["AstraTerm " + __version__, "Python " + sys.version.split()[0],
                 "DISPLAY: " + (os.environ.get("DISPLAY") or "не задан"),
                 "Конфигурация: " + str(self.store.path), "", "Системные клиенты:"]
        lines += ["  {}: {}".format(name, path or "не установлен") for name, path in sorted(deps.items())]
        lines += ["", "Клиенты RDP/VNC запускаются в отдельных окнах.",
                  "X11 требует xauth и X11Forwarding на SSH-сервере.",
                  "Serial требует доступа пользователя к устройству /dev/tty*."]
        view.get_buffer().set_text("\n".join(lines))
        row = Gtk.Box(spacing=8)
        host = Gtk.Entry(placeholder_text="Сервер для проверки")
        port = Gtk.Entry(text="22", width_chars=5)
        pane = self.active_pane()
        if pane:
            host.set_text(pane.profile.get("host", ""))
            port.set_text(str(pane.profile.get("port") or 22))
        row.pack_start(host, True, True, 0)
        row.pack_start(port, False, False, 0)
        result = Gtk.Label(label="", xalign=0)
        result.set_line_wrap(True)
        def probe(*_):
            hostname, port_text = host.get_text(), port.get_text()
            result.set_text("Проверка DNS и TCP…")
            check.set_sensitive(False)
            def run():
                try:
                    message = str(tcp_probe(hostname, port_text))
                except Exception as exc:
                    message = str(exc)
                def finish():
                    if not destroyed[0]:
                        result.set_text(message)
                        check.set_sensitive(True)
                    return False
                GLib.idle_add(finish)
            threading.Thread(target=run, daemon=True).start()
        check = button("Проверить TCP", probe)
        row.pack_end(check, False, False, 0)
        content.pack_start(row, False, False, 0)
        content.pack_start(result, False, False, 0)
        destroyed = [False]
        dialog.show_all()
        dialog.run()
        destroyed[0] = True
        dialog.destroy()

    def help_shortcuts(self, *_):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.CLOSE, text="Горячие клавиши")
        dialog.format_secondary_text("Ctrl+Shift+T — локальный терминал\nCtrl+Shift+W — закрыть вкладку\n"
            "Ctrl+Shift+E — разделить по горизонтали\nCtrl+Shift+O — разделить по вертикали\n"
            "Ctrl+Shift+F — поиск по выводу\nCtrl+Shift+C / V — копировать / вставить\n"
            "Ctrl+Shift+M — MultiExec\nCtrl+Shift+Insert — вставить в отмеченные панели\n"
            "Ctrl + плюс / минус — размер шрифта\nCtrl+0 — сброс размера шрифта\n"
            "Alt + 1…9 — перейти к вкладке\n"
            "F2 — переименовать выбранное подключение, файл или папку")
        dialog.run()
        dialog.destroy()

    def about(self, *_):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True, program_name="AstraTerm", version=__version__)
        dialog.set_comments("Python · GTK 3 · PyGObject · VTE\nOpenSSH · Paramiko · FreeRDP · PyYAML · cryptography")
        dialog.set_website("https://astraterminal.ru")
        notices = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "THIRD_PARTY_NOTICES.md")
        if not os.path.isfile(notices):
            notices = "/usr/share/doc/astraterm/copyright"
        with open(notices, encoding="utf-8") as stream:
            dialog.set_license(stream.read())
        dialog.set_wrap_license(True)
        dialog.run()
        dialog.destroy()

    def _key_press(self, window, event):
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        key = (Gdk.keyval_name(event.keyval) or "").lower()
        if control or alt:
            # Keep shortcuts usable with the Russian keyboard layout.
            translated = Gdk.Keymap.get_default().translate_keyboard_state(event.hardware_keycode, event.state, 0)
            if translated[0]:
                key = (Gdk.keyval_name(translated[1]) or key).lower()
        if control and shift:
            if key == "m":
                self.set_multiexec(not self.multiexec.enabled)
                return True
            if key in ("insert", "kp_insert"):
                pane = self.active_pane()
                if pane and self.get_focus() is pane.terminal:
                    self.multi_paste()
                    return True
            callbacks = {"t": self.new_local, "w": self.close_tab,
                         "e": lambda: self.split(Gtk.Orientation.HORIZONTAL),
                         "o": lambda: self.split(Gtk.Orientation.VERTICAL), "f": self.find}
            if key in callbacks:
                callbacks[key]()
                return True
            pane = self.active_pane()
            if pane and pane.profile.get('protocol') != 'rdp' and self.get_focus() is pane.terminal and key in ("c", "v"):
                pane.terminal.copy_clipboard() if key == "c" else self._paste_from_pane(pane)
                return True
        if control and key in ("plus", "equal", "kp_add", "minus", "kp_subtract"):
            self.zoom(-1 if key in ("minus", "kp_subtract") else 1)
            return True
        if control and not shift and not alt and key in ("0", "kp_0"):
            self.zoom(DEFAULT_FONT_SIZE - self.font_size)
            return True
        if alt and key in "123456789" and len(key) == 1:
            self.notebook.set_current_page(int(key) - 1)
            return True
        return False

    def _delete_event(self, *_):
        if not self.file_browser.request_close():
            return True
        active = any(pane.pid for i in range(self.notebook.get_n_pages()) for pane in self.notebook.get_nth_page(i).panes)
        if active and not confirm(self, "Закрыть AstraTerm и завершить подключения?",
                                  "Все терминалы и SSH-туннели этого окна будут закрыты."):
            return True
        self.shutdown()
        Gtk.main_quit()
        return False

    def shutdown(self):
        if self.closed:
            return
        for workspace in list(self.floating_windows):
            self.dock_window(workspace)
        self.closed = True
        self.host_stats.close()
        self.credential_vault.lock()
        for request in list(self._identity_jobs):
            request.cancel()
        self._identity_jobs.clear()
        if self._identity_manager is not None:
            self._identity_manager.destroy()
        self.main_menu.destroy()
        if self._tab_context_menu is not None:
            self._tab_context_menu.destroy()
        self.multiexec.set_enabled(False)
        if self.tiled.active:
            self.tiled.leave()
        self.file_browser.shutdown()
        for i in range(self.notebook.get_n_pages()):
            self.notebook.get_nth_page(i).close()


def main():
    parser = argparse.ArgumentParser(description="AstraTerm — терминалы и подключения для Astra Linux")
    parser.add_argument("--local", action="store_true", help="открыть локальный терминал")
    parser.add_argument("--session", help="идентификатор сохранённого подключения")
    args = parser.parse_args()
    try:
        store = SessionStore()
        profile = None
        if args.session:
            profile = next((item for item in store.sessions if item["id"] == args.session), None)
            if profile is None:
                parser.error("подключение с таким идентификатором не найдено")
        window = MainWindow(store, profile, enable_identities=True, backup_root=profile_root())
        window.present()
        Gtk.main()
    except Exception as exc:
        print("AstraTerm: " + str(exc), file=sys.stderr)
        if Gtk.init_check()[0]:
            error(None, "Не удалось запустить AstraTerm", exc)
        raise SystemExit(1)
