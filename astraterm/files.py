# -*- coding: utf-8 -*-
"""GTK file browser and explicit, host-key-checked SFTP connections.

The module intentionally supports Python 3.5 and GTK 3.22 shipped with Astra 1.6.
Network and filesystem work runs off the GTK thread; passwords live in memory only.
"""
from __future__ import unicode_literals

import base64
from collections import OrderedDict
import getpass
import hashlib
import os
import posixpath
import shutil
import stat
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, Gtk, GLib, Pango

from .core import validate_jump_host, validate_session
from .file_metadata import permissions, remote_birth_times
from .file_sort import sort_rows
from .terminal_directory import REMOTE_HOSTNAME, UNKNOWN_DIRECTORY, resolve_terminal_directory

try:
    import paramiko
except ImportError:
    paramiko = None


MAX_EDIT_SIZE = 2 * 1024 * 1024
MAX_REMOTE_CONNECTIONS = 8


def bind_file_drop(widget, receive):
    targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]
    widget.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.HIGHLIGHT,
                         targets, Gdk.DragAction.COPY)

    def request(widget, context, x, y, timestamp):
        source = Gtk.drag_get_source_widget(context)
        if getattr(source, '_astraterm_sftp_source', False):
            Gtk.drag_finish(context, False, False, timestamp)
            return True
        target = widget.drag_dest_find_target(context, None)
        if not target:
            return False
        widget.drag_get_data(context, target, timestamp)
        return True

    def received(widget, context, x, y, selection, info, timestamp):
        # VTE normally pastes dropped URIs. SFTP receives them instead.
        widget.stop_emission_by_name("drag-data-received")
        uris = selection.get_uris() or []
        paths = []
        try:
            for uri in uris:
                location = urlsplit(uri)
                if location.scheme != "file" or location.netloc not in ("", "localhost"):
                    break
                paths.append(Gio.File.new_for_uri(uri).get_path())
        except ValueError:
            paths = []
        accepted = bool(paths and len(paths) == len(uris) and all(paths))
        Gtk.drag_finish(context, accepted, False, timestamp)
        if accepted:
            receive(paths)

    widget.connect("drag-drop", request)
    widget.connect("drag-data-received", received)


class SSHIdentityUnavailable(Exception):
    """A specifically selected identity must not fall back to unrelated keys."""


def _sftp_profile_key(profile, auth_generation=None):
    """Separate cached transports by endpoint, identity and unlock generation."""
    if profile.get("protocol") != "ssh":
        return None
    return (profile.get("host"), profile.get("port", 22),
            profile.get("username") or os.environ.get("USER") or getpass.getuser(),
            os.path.expanduser(profile.get("key_file") or ""), profile.get("jump_host") or "",
            bool(profile.get("compression", False)), profile.get("_agent_key_blob") or "",
            bool(profile.get("_identity_required", False)),
            profile.get("identity_profile") or "",
            auth_generation)


def _connection_alive(client, sftp):
    if client is None or sftp is None:
        return False
    try:
        transport = client.get_transport()
        return bool(transport is not None and transport.is_active() and transport.is_authenticated()
                    and not sftp.get_channel().closed)
    except Exception:
        return False


def _authentication_error(error):
    if paramiko is None:
        return False
    kinds = tuple(kind for kind in (getattr(paramiko, "AuthenticationException", None),
                                    getattr(paramiko, "PasswordRequiredException", None))
                  if isinstance(kind, type))
    # Paramiko 1.17 raises the base SSHException when no method was available.
    return isinstance(error, kinds) or (isinstance(error, getattr(paramiko, "SSHException", ()))
                                        and str(error) == "No authentication methods available")


def _public_key_only_error(error):
    kind = getattr(paramiko, "BadAuthenticationType", None)
    return isinstance(kind, type) and isinstance(error, kind) and set(error.allowed_types) == {"publickey"}


def _legacy_key_format_error(error):
    kind = getattr(paramiko, "SSHException", None)
    return isinstance(kind, type) and isinstance(error, kind) and str(error) in (
        "not a valid RSA private key file", "not a valid DSA private key file",
        "not a valid EC private key file")


def _safe_name(name):
    """A single path component, also safe for a local download destination."""
    return (isinstance(name, str) and name not in ("", ".", "..") and
            not any(c in name for c in ("/", "\\", "\x00", "\n", "\r")))


def _size(value):
    value = float(value or 0)
    for suffix in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or suffix == "ТБ":
            return ("%d %s" % (value, suffix) if suffix == "Б"
                    else "%.1f %s" % (value, suffix))
        value /= 1024


def _snapshot(path):
    info = os.stat(path)
    return (info.st_size, info.st_mtime, info.st_ino)


def _parse_jump_hosts(value, default_user=None):
    """Parse validated ProxyJump endpoints without interpreting a local shell."""
    value = validate_jump_host(value)
    if not value:
        return []
    default_user = default_user or os.environ.get("USER") or getpass.getuser()
    result = []
    for index, item in enumerate(value.split(",")):
        if "@" in item:
            username, endpoint = item.rsplit("@", 1)
        else:
            username, endpoint = default_user, item
        port = 22
        if endpoint.startswith("["):
            closing = endpoint.index("]")
            host = endpoint[1:closing]
            if endpoint[closing + 1:]:
                port = int(endpoint[closing + 2:])
        elif ":" in endpoint:
            host, raw_port = endpoint.rsplit(":", 1)
            port = int(raw_port)
        else:
            host = endpoint
        result.append({"host": host, "port": port, "username": username,
                       "role": "SSH-шлюз %d" % (index + 1)})
    return result


def _endpoint_label(endpoint):
    host = endpoint["host"]
    if ":" in host:
        host = "[" + host + "]"
    return "%s@%s:%d" % (endpoint["username"], host, endpoint["port"])


def _close_clients(clients):
    """Close downstream transports first, even if an individual close fails."""
    seen = set()
    for client in reversed(clients):
        if id(client) in seen:
            continue
        seen.add(id(client))
        try:
            client.close()
        except Exception:
            pass


def _native_host_key_required(error):
    """Only retry a failed algorithm negotiation, never failed host trust/auth."""
    kind = getattr(paramiko, "SSHException", None)
    return (isinstance(kind, type) and isinstance(error, kind)
            and "no acceptable host key" in str(error).lower())


def _connect_openssh_sftp(profile, secret, policy, known_hosts,
                          register_client=None, cancelled=None, public_key_only=False):
    from .openssh_sftp import connect_openssh_sftp
    return connect_openssh_sftp(profile, secret, policy, known_hosts,
                                register_client=register_client, cancelled=cancelled,
                                public_key_only=public_key_only)


class _PasswordSSHClient(paramiko.SSHClient if paramiko is not None else object):
    """Saved account passwords must not be replayed into an arbitrary OTP prompt."""
    def _auth(self, username, password, *args, **kwargs):
        self._transport.auth_password(username, password, fallback=False)


def _connect_sftp_chain(profile, target_secret, jump_secrets, policy_factory,
                        register_client=None, cancelled=None):
    """Create one verified SSH transport per jump using direct-tcpip channels.

    ``policy_factory(endpoint)`` supplies the UI host-key confirmation policy.
    Returns target client, SFTP client, remote home, and gateway clients in order.
    All partially established connections are closed on failure or cancellation.
    """
    agent_blob = profile.get("_agent_key_blob")
    agent_socket = profile.get("_ssh_agent_sock")
    identity_required = bool(profile.get("_identity_required", False) or
                             profile.get("identity_profile") not in (None, "", "system"))
    profile = validate_session(profile)
    if profile["protocol"] != "ssh":
        raise ValueError("SFTP требует SSH-профиль.")
    local_user = os.environ.get("USER") or getpass.getuser()
    jumps = _parse_jump_hosts(profile.get("jump_host"), local_user)
    if len(jumps) != len(jump_secrets):
        raise ValueError("Число паролей SSH-шлюзов не соответствует цепочке.")
    target = {"host": profile["host"], "port": profile["port"],
              "username": profile.get("username") or local_user, "role": "Сервер SFTP"}
    endpoints = jumps + [target]
    secrets = list(jump_secrets) + [target_secret]
    clients, channels = [], []
    agent, selected_key = None, None
    known = os.path.expanduser("~/.ssh/known_hosts")
    key_file = os.path.expanduser(profile["key_file"]) if profile.get("key_file") else None
    saved_password = bool(profile.get("credential_profile"))
    target_password_only = saved_password and not agent_blob and not key_file
    authenticating = False

    def check_cancelled():
        if cancelled and cancelled():
            raise RuntimeError("Подключение отменено.")

    try:
        if identity_required and not agent_blob:
            raise SSHIdentityUnavailable("Разблокируйте ключ выбранного SSH-профиля в «Профили SSH».")
        if agent_blob:
            agent = paramiko.Agent()
            selected_key = next((key for key in agent.get_keys() if key.get_base64() == agent_blob), None)
            if selected_key is None:
                raise SSHIdentityUnavailable("Ключ SSH-профиля не разблокирован в ssh-agent. Откройте «Профили SSH».")
        previous = None
        for endpoint, secret in zip(endpoints, secrets):
            check_cancelled()
            channel = None
            if previous is not None:
                channel = previous.get_transport().open_channel(
                    "direct-tcpip", (endpoint["host"], endpoint["port"]),
                    ("127.0.0.1", 0), timeout=12)
                channels.append(channel)
                check_cancelled()
            password_only = target_password_only and endpoint is target
            client = _PasswordSSHClient() if password_only else paramiko.SSHClient()
            clients.append(client)
            if register_client:
                register_client(client)
            check_cancelled()
            client.load_system_host_keys()
            if os.path.exists(known):
                client.load_host_keys(known)
            client.set_missing_host_key_policy(policy_factory(endpoint))
            arguments = dict(hostname=endpoint["host"], port=endpoint["port"],
                             username=endpoint["username"], password=secret or None,
                             key_filename=None if selected_key is not None else key_file,
                             allow_agent=selected_key is None and not (profile.get('credential_profile') and endpoint is target),
                             look_for_keys=selected_key is None and not (profile.get('credential_profile') and endpoint is target),
                             timeout=12, banner_timeout=12, sock=channel,
                             compress=bool(profile.get("compression", False)))
            if selected_key is not None:
                arguments["pkey"] = selected_key
            authenticating = True
            client.connect(**arguments)
            authenticating = False
            check_cancelled()
            client.get_transport().set_keepalive(30)
            previous = client
        sftp = previous.open_sftp()
        sftp.get_channel().settimeout(20)
        home = sftp.normalize(".")
        check_cancelled()
        return previous, sftp, home, clients[:-1]
    except Exception as error:
        _close_clients(clients)
        for channel in reversed(channels):
            try:
                channel.close()
            except Exception:
                pass
        retry_key = authenticating and not target_password_only and (
            (_authentication_error(error) and
             bool(agent_blob or key_file or not target_secret or _public_key_only_error(error))) or
            (key_file is not None and _legacy_key_format_error(error)))
        if not jumps and (_native_host_key_required(error) or retry_key):
            check_cancelled()
            native_profile = dict(profile)
            if agent_blob:
                native_profile["_agent_key_blob"] = agent_blob
            if agent_socket:
                native_profile["_ssh_agent_sock"] = agent_socket
            if identity_required:
                native_profile["_identity_required"] = True
            # A vault password belongs to the target account, never to a key.
            native_secret = target_secret
            if retry_key and (saved_password or not key_file or not target_secret):
                native_secret = None
            return _connect_openssh_sftp(
                native_profile, native_secret, policy_factory(target), known,
                register_client=register_client, cancelled=cancelled, public_key_only=retry_key)
        raise
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass


class FileBrowser(Gtk.Box):
    """Browser pane with shared-agent authentication and optional active-host follow.

    Public API: set_profile(dict), open_local(path), connect_remote(), shutdown().
    ``on_status`` is an optional callable taking one status string on the GTK thread.
    """

    def __init__(self, profile=None, terminal=None, on_status=None, auto_follow=False, diagnostic_log=None, on_notice=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_border_width(14)
        self.get_style_context().add_class("files-panel")
        self.profile = dict(profile or {})
        self.terminal = terminal
        self.on_status = on_status
        self.on_notice = on_notice
        self.diagnostic_log = diagnostic_log
        self.password_provider = None
        self._client = None
        self._gateway_clients = []
        self._connecting_clients = []
        self._connection_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._sftp = None
        self._mode = "local"
        self._path = os.path.expanduser("~")
        self._local_path = self._path
        self._remote_path = None
        self._remote_home = None
        self._busy = False
        self._closed = False
        self._generation = 0
        self._progress_time = 0
        self._editors = []
        self._pending_profile = None
        self._pending_auth_generation = None
        self._pending_upload = None
        self.auto_follow = bool(auto_follow)
        self._auth_generation = None
        self._follow_source = None
        self._follow_failures = set()
        self._remote_cache = OrderedDict()
        self._remote_directories = OrderedDict()
        self._operation_kind = None
        self._drag_root = None
        self._drag_gesture = None
        self._drag_ready = None
        self._drag_file = None
        self._drag_timers = set()

        heading = Gtk.Box(spacing=10)
        heading.get_style_context().add_class("files-header")
        self.mode_label = Gtk.Label(label="Файлы")
        self.mode_label.set_xalign(0)
        self.mode_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.mode_label.set_max_width_chars(22)
        self.mode_label.get_style_context().add_class("section-title")
        heading.pack_start(self.mode_label, True, True, 0)
        modes = Gtk.Box(spacing=0)
        modes.get_style_context().add_class("linked")
        modes.get_style_context().add_class("files-modes")
        self.local_button = Gtk.Button(label="Локально")
        self.local_button.get_style_context().add_class("mode-button")
        self.local_button.set_tooltip_text("Открыть локальные файлы")
        self.local_button.connect("clicked", lambda unused: self.open_local(self._local_path))
        modes.pack_start(self.local_button, False, False, 0)
        self.connect_button = Gtk.Button(label="SFTP")
        self.connect_button.get_style_context().add_class("mode-button")
        self.connect_button.set_tooltip_text("Открыть или обновить SFTP активного SSH-подключения")
        self.connect_button.connect("clicked", lambda unused: self.connect_remote())
        modes.pack_start(self.connect_button, False, False, 0)
        heading.pack_end(modes, False, False, 0)
        self.pack_start(heading, False, False, 0)

        navigation = Gtk.Box(spacing=4)
        navigation.get_style_context().add_class("files-navigation")
        self.up_button = self._button("go-up-symbolic", "На уровень выше", self._up)
        navigation.pack_start(self.up_button, False, False, 0)
        self.path_entry = Gtk.Entry()
        self.path_entry.get_style_context().add_class("files-path")
        self.path_entry.set_placeholder_text("Путь к каталогу")
        self.path_entry.connect("activate", lambda entry: self._navigate(entry.get_text()))
        navigation.pack_start(self.path_entry, True, True, 0)
        self.refresh_button = self._button("view-refresh-symbolic", "Обновить", self.refresh)
        navigation.pack_start(self.refresh_button, False, False, 0)
        self.pack_start(navigation, False, False, 0)

        actions = Gtk.Box(spacing=4)
        actions.get_style_context().add_class("files-toolbar")
        self.upload_button = self._button("go-up-symbolic", "Загрузить файл на сервер", self._upload)
        self.download_button = self._button("go-down-symbolic", "Скачать выбранный файл", self._download)
        self.mkdir_button = self._button("folder-new-symbolic", "Создать каталог", self._mkdir)
        self.rename_button = self._button("edit-rename-symbolic", "Переименовать · F2", self._rename)
        from .ui_helpers import rename_image
        self.rename_button.set_image(rename_image())
        self.delete_button = self._button("edit-delete-symbolic", "Удалить файл или пустой каталог", self._delete)
        self.delete_button.get_style_context().add_class("destructive-action")
        for button in (self.upload_button, self.download_button, self.mkdir_button,
                       self.rename_button, self.delete_button):
            actions.pack_start(button, False, False, 0)
        self.spinner = Gtk.Spinner()
        actions.pack_end(self.spinner, False, False, 4)
        self.pack_start(actions, False, False, 0)

        # icon, name, size, modified, directory, symlink, path, permissions, created, ordinary, tooltip, raw sort values
        self.model = Gtk.ListStore(str, str, str, str, bool, bool, str, str, str, bool, str, object)
        self._sort = ('name', False)
        self.tree = Gtk.TreeView(model=self.model)
        self.tree.get_style_context().add_class("files-list")
        self.tree.set_headers_visible(False)
        self.tree.set_grid_lines(Gtk.TreeViewGridLines.NONE)
        self.tree.set_rules_hint(False)
        self.tree.set_enable_search(True)
        self.tree.set_search_column(1)
        self.tree.set_tooltip_column(1)
        name_column = Gtk.TreeViewColumn("Имя")
        icon = Gtk.CellRendererPixbuf()
        icon.set_property("ypad", 6)
        name_column.pack_start(icon, False)
        from .ui_helpers import file_folder_pixbuf
        folder_image = file_folder_pixbuf()
        def row_icon(_column, cell, model, iterator, _data):
            if model[iterator][4] and folder_image is not None:
                cell.set_property('pixbuf', folder_image)
            else:
                cell.set_property('icon-name', model[iterator][0])
        name_column.set_cell_data_func(icon, row_icon, None)
        name = Gtk.CellRendererText()
        name.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        name.set_property("ypad", 6)
        name_column.pack_start(name, True)
        name_column.add_attribute(name, "text", 1)
        name_column.set_expand(True)
        name_column.set_min_width(120)
        self.tree.append_column(name_column)
        size = Gtk.CellRendererText()
        size.set_property("xalign", 1.0)
        size.set_property("ypad", 6)
        self.tree.append_column(Gtk.TreeViewColumn("Размер", size, text=2))
        self.metadata_columns = []
        for title, index in (("Права", 7), ("Создан", 8), ("Изменён", 3)):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index)
            column.set_visible(False)
            self.tree.append_column(column)
            self.metadata_columns.append(column)
        self._sort_columns = dict(zip(('name', 'size', 'permissions', 'created', 'mtime'), self.tree.get_columns()))
        for key, column in self._sort_columns.items():
            column.set_clickable(True)
            column.connect('clicked', lambda _column, key=key: self._sort_column(key))
        self._show_sort_indicator()
        self.tree.connect("row-activated", self._activated)
        self.tree.connect("button-press-event", self._context_menu)
        self.tree.connect("key-press-event", self._rename_key)
        self.tree._astraterm_sftp_source = True
        self.tree.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.tree.connect("button-press-event", self._drag_press)
        self.tree.connect("motion-notify-event", self._drag_motion)
        self.tree.connect("button-release-event", self._drag_release)
        self.tree.connect("drag-data-get", self._drag_data)
        self.tree.connect("drag-end", self._drag_end)
        self._drag_targets = Gtk.TargetList.new([Gtk.TargetEntry.new("text/uri-list", 0, 0)])
        bind_file_drop(self.tree, self.upload_paths)
        bind_file_drop(self, self.upload_paths)
        scroll = Gtk.ScrolledWindow()
        scroll.get_style_context().add_class("files-scroll")
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree)
        self.pack_start(scroll, True, True, 0)

        self.status_label = Gtk.Label(label="Двойной щелчок: открыть каталог или текстовый файл")
        self.status_label.set_xalign(0)
        self.status_label.set_line_wrap(True)
        self.status_label.set_max_width_chars(38)
        self.status_label.get_style_context().add_class("dim-label")
        self.status_label.get_style_context().add_class("files-status")
        self.pack_start(self.status_label, False, False, 0)
        self._update_controls()
        if self.auto_follow and self.profile.get("protocol") == "ssh":
            initial = self.profile
            self.profile = {}
            GLib.idle_add(self.set_profile, initial)
        else:
            GLib.idle_add(self.refresh)

    @staticmethod
    def _button(icon, tooltip, callback):
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("icon-button")
        button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda unused: callback())
        return button

    def _parent(self):
        top = self.get_toplevel()
        return top if isinstance(top, Gtk.Window) else None

    def _status(self, text):
        if self._closed:
            return False
        self.status_label.set_text(text)
        if self.on_status:
            self.on_status(text)
        return False

    def _error(self, text):
        if self._closed:
            return
        self._status(text)
        dialog = Gtk.MessageDialog(transient_for=self._parent(), modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE,
                                   text="Операция с файлами не выполнена")
        dialog.format_secondary_text(str(text))
        dialog.run()
        dialog.destroy()

    def _confirm(self, title, detail):
        if self._closed:
            return False
        dialog = Gtk.MessageDialog(transient_for=self._parent(), modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        dialog.format_secondary_text(detail)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL,
                           "Подтвердить", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        result = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return result

    def _confirm_from_worker(self, title, detail, generation):
        answered = threading.Event()
        result = [False]

        def ask():
            if not self._closed and generation == self._generation:
                result[0] = self._confirm(title, detail)
            answered.set()
            return False

        GLib.idle_add(ask)
        while not answered.wait(0.2):
            if self._closed or generation != self._generation:
                return False
        return result[0]

    def _update_controls(self):
        idle = not self._busy and not self._closed
        editable = idle and self._mode in ("local", "remote")
        for widget in (self.path_entry,
                       self.up_button, self.refresh_button, self.mkdir_button,
                       self.rename_button, self.delete_button):
            widget.set_sensitive(editable)
        self.tree.set_sensitive(editable or self._operation_kind == 'drag-export')
        self.local_button.set_sensitive(idle)
        self.upload_button.set_sensitive(editable and self._mode == "remote")
        self.download_button.set_sensitive(editable and self._mode == "remote")
        if self._busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        remote_mode = self._mode in ("remote", "waiting")
        self.tree.set_headers_visible(remote_mode)
        self.tree.set_tooltip_column(10 if remote_mode else 1)
        for column in self.metadata_columns:
            column.set_visible(remote_mode)
        self.mode_label.set_tooltip_text("SFTP · " + self.profile.get("host", "")
                                         if remote_mode else "Локальные файлы")
        for button, active in ((self.local_button, not remote_mode),
                               (self.connect_button, remote_mode)):
            context = button.get_style_context()
            if active:
                context.add_class("active")
            else:
                context.remove_class("active")
        self.connect_button.set_sensitive(idle and self.profile.get("protocol") == "ssh")

    def _submit(self, label, work, complete=None, cancel=None, failed=None, kind=None):
        if self._closed or self._busy:
            return False
        diagnostic_log = self.diagnostic_log
        event = 'files.' + (kind or 'operation')
        if diagnostic_log:
            diagnostic_log.write('debug', event)
        self._busy = True
        self._operation_kind = kind
        self._update_controls()
        self._status(label)
        generation = self._generation

        def finish(result, error):
            if self._closed or generation != self._generation:
                if cancel and result is not None:
                    cancel(result)
                return False
            self._busy = False
            self._operation_kind = None
            self._update_controls()
            if error:
                if diagnostic_log:
                    diagnostic_log.write('warning', event + '.failed')
                if failed:
                    failed(error)
                else:
                    self._error(str(error))
            elif complete:
                complete(result)
            if self._pending_profile is not None and not self._busy:
                pending = self._pending_profile
                auth_generation = self._pending_auth_generation
                self._pending_profile = None
                self._pending_auth_generation = None
                self.set_profile(pending, auth_generation)
            if self._pending_upload:
                self._resume_upload()
            return False

        def run():
            try:
                with self._io_lock:
                    result = work()
                error = None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(finish, result, error)

        worker = threading.Thread(target=run, name="astraterm-files")
        worker.daemon = True
        worker.start()
        return False

    def set_profile(self, profile, auth_generation=None):
        if self._closed:
            return
        profile = dict(profile or {})
        old_key = _sftp_profile_key(self.profile, self._auth_generation)
        new_key = _sftp_profile_key(profile, auth_generation)
        changed = old_key != new_key or self.profile.get("protocol") != profile.get("protocol")
        if not changed:
            self._pending_profile = None
            self._pending_auth_generation = None
            self.profile = profile
            self._update_controls()
            return
        if self._busy and self._operation_kind not in ("connect", "list"):
            self._pending_profile = profile
            self._pending_auth_generation = auth_generation
            self.model.clear()
            self.path_entry.set_text("")
            self._status("Сессия файлов сменится после завершения текущей операции.")
            return
        self._cancel_follow()
        self._release_connection(cache=self.auto_follow and not self._busy)
        self.profile = profile
        self._auth_generation = auth_generation
        self._pending_profile = None
        self._pending_auth_generation = None
        self.model.clear()
        self.path_entry.set_text("")
        if self.auto_follow and new_key is not None:
            self._mode = "waiting"
            if not self._restore_cached_connection(new_key):
                self._status("Ожидание SFTP · " + profile.get("host", ""))
                if new_key not in self._follow_failures:
                    generation = self._generation
                    def follow():
                        self._follow_source = None
                        if not self._closed and generation == self._generation:
                            self.connect_remote(automatic=True)
                        return False
                    self._follow_source = GLib.timeout_add(200, follow)
                else:
                    self._status("SFTP не подключён. Для повторной попытки нажмите SFTP.")
        else:
            self._mode = "local"
            self._path = self._local_path
            self.refresh()
        self._update_controls()

    def open_local(self, path=None):
        if self._busy or self._closed:
            return
        self._cancel_follow()
        self._mode = "local"
        self._navigate(path or self._local_path)

    def _cancel_follow(self):
        if self._follow_source is not None:
            GLib.source_remove(self._follow_source)
            self._follow_source = None

    @staticmethod
    def _close_remote_record(record):
        _close_clients(record["gateways"] + [record["client"]])

    def _drop_cached_connections(self):
        records = list(self._remote_cache.values())
        self._remote_cache.clear()
        for record in records:
            self._close_remote_record(record)

    def _release_connection(self, cache=False):
        self._generation += 1
        self._pending_upload = None
        self._busy = False
        self._operation_kind = None
        retained = False
        key = _sftp_profile_key(self.profile, self._auth_generation)
        if key is not None and self._remote_path:
            self._remote_directories.pop(key, None)
            self._remote_directories[key] = self._remote_path
            while len(self._remote_directories) > 100:
                self._remote_directories.popitem(last=False)
        if cache and key is not None and _connection_alive(self._client, self._sftp):
            previous = self._remote_cache.pop(key, None)
            if previous is not None:
                self._close_remote_record(previous)
            self._remote_cache[key] = {"client": self._client, "sftp": self._sftp,
                                       "gateways": self._gateway_clients,
                                       "home": self._remote_home,
                                       "path": self._remote_path or self._remote_home}
            retained = True
            while len(self._remote_cache) >= MAX_REMOTE_CONNECTIONS:
                unused, oldest = self._remote_cache.popitem(last=False)
                self._close_remote_record(oldest)
        with self._connection_lock:
            clients = [] if retained else self._gateway_clients + ([self._client] if self._client else [])
            clients += self._connecting_clients
            self._gateway_clients = []
            self._connecting_clients = []
        self._sftp = None
        self._client = None
        self._remote_path = None
        self._remote_home = None
        _close_clients(clients)

    def _restore_cached_connection(self, key):
        record = self._remote_cache.pop(key, None)
        if record is None:
            return False
        if not _connection_alive(record["client"], record["sftp"]):
            self._close_remote_record(record)
            return False
        self._client, self._sftp = record["client"], record["sftp"]
        self._gateway_clients = record["gateways"]
        self._remote_home, self._remote_path = record["home"], record["path"]
        self._mode, self._path = "remote", self._remote_path
        self.refresh()
        return True

    def _disconnect(self):
        self._cancel_follow()
        self._release_connection()
        self._drop_cached_connections()

    def request_close(self):
        """Guard main-window closure against in-flight writes and dirty editors."""
        if self._busy:
            self._error("Дождитесь завершения текущей операции с файлами перед закрытием AstraTerm.")
            return False
        dirty = [editor.get_title() for editor in self._editors
                 if getattr(editor, "_astraterm_buffer", None) is not None and
                 editor._astraterm_buffer.get_modified()]
        if dirty:
            return self._confirm("Закрыть несохранённые файлы?",
                                 "Изменения будут потеряны:\n\n" + "\n".join(dirty))
        return True

    def shutdown(self):
        """Stop callbacks, release sockets, and unblock any host-key wait."""
        self._closed = True
        self._disconnect()
        for source in self._drag_timers:
            GLib.source_remove(source)
        self._drag_timers.clear()
        if self._drag_root is not None:
            self._drag_root.cleanup()
            self._drag_root = None
        for editor in list(self._editors):
            editor.destroy()
        self._editors = []

    def _request_credentials(self, profile, jumps):
        dialog = Gtk.Dialog(title="Подключение SFTP", transient_for=self._parent(), modal=True)
        dialog.set_default_size(500, -1)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL,
                           "Подключиться", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_border_width(16)
        user = profile.get("username") or os.environ.get("USER") or getpass.getuser()
        host = profile.get("host", "")
        info = Gtk.Label(label="SFTP · %s@%s:%s" % (user, host, profile.get("port", 22)))
        info.set_xalign(0)
        content.pack_start(info, False, False, 0)
        notice = Gtk.Label(label="Автоматический вход с ключом или ssh-agent не выполнен.\n"
                                 "Введите пароль пользователя или ключа для повторной попытки.\n"
                                 "Для каждого SSH-шлюза можно ввести отдельный пароль.")
        notice.set_line_wrap(True)
        notice.set_xalign(0)
        content.pack_start(notice, False, False, 0)
        credential_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        jump_entries = []
        for endpoint in jumps:
            label = Gtk.Label(label=endpoint["role"] + " · " + _endpoint_label(endpoint))
            label.set_xalign(0)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            credential_box.pack_start(label, False, False, 0)
            entry = Gtk.Entry()
            entry.set_visibility(False)
            entry.set_placeholder_text("Пароль шлюза / пароль ключа")
            entry.set_activates_default(True)
            credential_box.pack_start(entry, False, False, 0)
            jump_entries.append(entry)
        if jumps:
            label = Gtk.Label(label="Сервер SFTP · " + _endpoint_label(
                {"host": host, "port": profile["port"], "username": user}))
            label.set_xalign(0)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            credential_box.pack_start(label, False, False, 0)
        password = Gtk.Entry()
        password.set_visibility(False)
        password.set_placeholder_text("Пароль пользователя / пароль ключа")
        password.set_activates_default(True)
        credential_box.pack_start(password, False, False, 0)
        if len(jumps) > 2:
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_size_request(-1, 330)
            scroll.add(credential_box)
            content.pack_start(scroll, True, True, 0)
        else:
            content.pack_start(credential_box, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        secret = password.get_text() if response == Gtk.ResponseType.OK else ""
        jump_secrets = [entry.get_text() for entry in jump_entries] if response == Gtk.ResponseType.OK else []
        password.set_text("")
        for entry in jump_entries:
            entry.set_text("")
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return secret, jump_secrets

    def connect_remote(self, automatic=False, credentials=None):
        if self._busy or self._closed:
            return
        self._cancel_follow()
        if self.profile.get("protocol") != "ssh":
            if not automatic:
                self._error("Выберите SSH-сессию, чтобы подключить SFTP.")
            return
        if paramiko is None:
            message = "Не установлен python3-paramiko для Python 3.5. Установите зависимости через install.sh."
            self._status(message) if automatic else self._error(message)
            return
        key = _sftp_profile_key(self.profile, self._auth_generation)
        if automatic and key in self._follow_failures:
            return
        if not automatic:
            self._follow_failures.discard(key)
        if _connection_alive(self._client, self._sftp):
            self._mode = "remote"
            self._path = self._remote_path or self._remote_home
            self.refresh()
            return
        if self._sftp is not None or self._client is not None:
            self._release_connection()
        self._mode = "waiting"
        self.model.clear()
        self.path_entry.set_text("")
        self._update_controls()
        try:
            profile = validate_session(self.profile)
            if self.profile.get("_agent_key_blob"):
                profile["_agent_key_blob"] = self.profile["_agent_key_blob"]
            if self.profile.get("_identity_required"):
                profile["_identity_required"] = True
            if self.profile.get("_ssh_agent_sock"):
                profile["_ssh_agent_sock"] = self.profile["_ssh_agent_sock"]
            jumps = _parse_jump_hosts(profile.get("jump_host"))
        except ValueError as exc:
            self._status(str(exc)) if automatic else self._error(str(exc))
            return
        if credentials is None and profile.get('credential_profile') and self.password_provider is not None:
            try:
                credentials = (self.password_provider(profile), [''] * len(jumps))
            except ValueError as exc:
                self._status(str(exc)) if automatic else self._error(str(exc))
                return
        secret, jump_secrets = credentials if credentials is not None else ("", [""] * len(jumps))
        host = profile["host"]
        generation = self._generation
        browser = self

        class ConfirmHostKey(paramiko.MissingHostKeyPolicy):
            def __init__(self, endpoint):
                self.endpoint = endpoint

            def confirm_openssh_host_key(self, prompt):
                detail = ("%s: %s\n\n%s\n\nСверьте отпечаток с администратором сервера. "
                          "При подтверждении OpenSSH сохранит ключ в ~/.ssh/known_hosts." %
                          (self.endpoint["role"], _endpoint_label(self.endpoint), prompt))
                return browser._confirm_from_worker("Новый ключ SSH-сервера", detail, generation)

            def missing_host_key(self, client, hostname, key):
                digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
                detail = ("%s: %s\nТип ключа: %s\nSHA256:%s\n\n"
                          "Сверьте отпечаток ключа с администратором сервера. "
                          "После подтверждения ключ будет записан в ~/.ssh/known_hosts." %
                          (self.endpoint["role"], hostname, key.get_name(), digest))
                if not browser._confirm_from_worker("Новый ключ SSH-сервера", detail, generation):
                    raise paramiko.SSHException("Ключ сервера не подтверждён.")
                ssh_dir = os.path.expanduser("~/.ssh")
                if not os.path.isdir(ssh_dir):
                    os.makedirs(ssh_dir, mode=0o700)
                known = os.path.join(ssh_dir, "known_hosts")
                line = "%s %s %s\n" % (hostname, key.get_name(), key.get_base64())
                descriptor = os.open(known, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "a") as stream:
                    stream.write(line)
                client.get_host_keys().add(hostname, key.get_name(), key)

        created = []

        def register(client):
            with self._connection_lock:
                if self._closed or generation != self._generation:
                    client.close()
                    raise RuntimeError("Подключение отменено.")
                created.append(client)
                self._connecting_clients.append(client)

        def unregister():
            with self._connection_lock:
                self._connecting_clients = [client for client in self._connecting_clients
                                            if client not in created]

        def connect():
            try:
                return _connect_sftp_chain(
                    profile, secret, jump_secrets, ConfirmHostKey,
                    register_client=register,
                    cancelled=lambda: self._closed or generation != self._generation)
            except Exception:
                unregister()
                raise

        def cancel_result(result):
            _close_clients(result[3] + [result[0]])
            unregister()

        def connected(result):
            self._client, self._sftp, self._remote_home, self._gateway_clients = result
            unregister()
            self._mode = "remote"
            self._path = self._remote_directories.get(key, self._remote_home)
            self._remote_path = self._path
            self._follow_failures.discard(key)
            self._update_controls()
            self.refresh()

        def failed(error):
            self._follow_failures.add(key)
            # Never prompt for a previous endpoint after a focus change, even
            # if its authentication error arrives while another operation ends.
            if self._pending_profile is not None or key != _sftp_profile_key(self.profile, self._auth_generation):
                return
            if _public_key_only_error(error):
                self._status("Сервер принимает только SSH-ключи. Проверьте ключ подключения или ssh-agent.")
            elif _authentication_error(error) and credentials is None:
                supplied = self._request_credentials(profile, jumps)
                if (supplied is not None and not self._closed and generation == self._generation
                        and key == _sftp_profile_key(self.profile, self._auth_generation)):
                    self.connect_remote(automatic=False, credentials=supplied)
                    return
                self._status("SFTP не подключён. Для повторной попытки нажмите SFTP.")
            elif isinstance(error, SSHIdentityUnavailable):
                self._status(str(error))
            elif automatic:
                self._status("SFTP не подключён: " + str(error))
            else:
                self._error(str(error))

        self._submit("Подключение SFTP к %s…" % host, connect, connected,
                     cancel=cancel_result, failed=failed, kind="connect")

    def _navigate(self, path):
        if self._busy or self._closed or self._mode == "waiting":
            return
        if self._mode == "remote":
            path = path.strip() or self._remote_home
            if path == "~" or path.startswith("~/"):
                path = posixpath.join(self._remote_home, path[2:]) if path != "~" else self._remote_home
            if not posixpath.isabs(path):
                path = posixpath.join(self._path, path)
            path = posixpath.normpath(path)
        else:
            path = os.path.abspath(os.path.expanduser(path or self._local_path))
        self._list_directory(path)

    def refresh(self):
        if self._closed:
            return False
        self._list_directory(self._path)
        return False

    def _list_directory(self, path):
        mode, sftp, client = self._mode, self._sftp, self._client
        if mode == "waiting":
            return
        if mode == "remote" and sftp is None:
            return

        def read():
            rows = []
            if mode == "remote":
                canonical = sftp.normalize(path)
                entries = sftp.listdir_attr(canonical)
                births = remote_birth_times(client, canonical) if entries else {}
                for attr in entries:
                    if not _safe_name(attr.filename):
                        continue
                    is_dir = stat.S_ISDIR(attr.st_mode or 0)
                    is_link = stat.S_ISLNK(attr.st_mode or 0)
                    full = posixpath.join(canonical, attr.filename)
                    rows.append(self._row(attr.filename, attr, is_dir, is_link, full, births.get(full)))
            else:
                canonical = os.path.abspath(path)
                for name in os.listdir(canonical):
                    full = os.path.join(canonical, name)
                    try:
                        attr = os.lstat(full)
                        is_link = stat.S_ISLNK(attr.st_mode)
                        is_dir = os.path.isdir(full)
                        rows.append(self._row(name, attr, is_dir, is_link, full))
                    except OSError:
                        continue
            return canonical, rows

        def listed(result):
            self._path, rows = result
            if mode == "local":
                self._local_path = self._path
            else:
                self._remote_path = self._path
            self.path_entry.set_text(self._path)
            self._drag_ready = None
            self.model.clear()
            for row in sort_rows(rows, *self._sort) if mode == 'remote' else sort_rows(rows):
                self.model.append(row)
            self._update_controls()
            self._status("%d объектов · %s" % (len(rows), "SFTP" if mode == "remote" else "локально"))

        self._submit("Чтение каталога…", read, listed, kind="list")

    @staticmethod
    def _row(name, attr, is_dir, is_link, path, created=None):
        icon = "folder-symbolic" if is_dir else "text-x-generic-symbolic"
        if is_link and not is_dir:
            icon = "emblem-symbolic-link-symbolic"
        def display_date(value):
            if value is not None:
                try:
                    return time.strftime("%d.%m.%Y %H:%M", time.localtime(value))
                except (OverflowError, OSError, ValueError):
                    pass
            return "—"
        rights = permissions(attr.st_mode)
        modified_text, created_text = display_date(attr.st_mtime), display_date(created)
        tooltip = GLib.markup_escape_text("{}\nПрава · {}\nСоздан · {}\nИзменён · {}".format(
            name, rights or "—", created_text, modified_text))
        return (icon, name, "—" if is_dir else _size(attr.st_size), modified_text, bool(is_dir), bool(is_link),
                path, rights or "—", created_text, stat.S_ISREG(attr.st_mode or 0), tooltip,
                (None if is_dir else attr.st_size, rights, created, attr.st_mtime))

    def _show_sort_indicator(self):
        key, descending = self._sort
        for name, column in self._sort_columns.items():
            column.set_sort_indicator(name == key)
            column.set_sort_order(Gtk.SortType.DESCENDING if descending else Gtk.SortType.ASCENDING)

    def _sort_column(self, key):
        if self._mode != 'remote' or self._busy or self._drag_file:
            return
        self._sort = (key, key == self._sort[0] and not self._sort[1])
        selected = self._selected()
        rows = sort_rows([tuple(row) for row in self.model], *self._sort)
        self.model.clear()
        for row in rows:
            iterator = self.model.append(row)
            if selected and selected[6] == row[6]:
                self.tree.get_selection().select_iter(iterator)
        self._show_sort_indicator()

    def _up(self):
        self._navigate(posixpath.dirname(self._path) if self._mode == "remote"
                       else os.path.dirname(self._path))

    def _selected(self):
        model, tree_iter = self.tree.get_selection().get_selected()
        return tuple(model[tree_iter]) if tree_iter is not None else None

    def _activated(self, tree, tree_path, column):
        row = tuple(self.model[tree_path])
        if row[4]:
            self._navigate(row[6])
        elif self._mode == "remote" and row[5]:
            sftp = self._sftp

            def resolve():
                return stat.S_ISDIR(sftp.stat(row[6]).st_mode)

            self._submit("Проверка ссылки…", resolve,
                         lambda is_dir: self._navigate(row[6]) if is_dir else self._edit(row[6]))
        else:
            self._edit(row[6])

    def _context_menu(self, tree, event):
        if event.button != 3 or self._busy:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit:
            tree.get_selection().select_path(hit[0])
        menu = Gtk.Menu()
        items = [("Открыть", self._open_selected), ("Переименовать", self._rename),
                 ("Удалить…", self._delete), ("Создать каталог…", self._mkdir),
                 ("Обновить", self.refresh)]
        if self._mode == "remote":
            items[1:1] = [("Скачать…", self._download), ("Загрузить…", self._upload)]
        for label, callback in items:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda unused, cb=callback: cb())
            menu.append(item)
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def _open_selected(self):
        selection = self.tree.get_selection().get_selected()
        if selection[1] is not None:
            self._activated(self.tree, self.model.get_path(selection[1]), None)

    def _name_dialog(self, title, initial=""):
        from .dialogs import name_dialog
        answer = name_dialog(self._parent(), title, initial)
        if answer is not None and not _safe_name(answer):
            self._error("Введите одно имя без /, \\, перевода строки; имена . и .. недопустимы.")
            return None
        return answer

    def _mkdir(self):
        name = self._name_dialog("Новый каталог")
        if name is None:
            return
        mode, sftp = self._mode, self._sftp
        path = posixpath.join(self._path, name) if mode == "remote" else os.path.join(self._path, name)
        self._submit("Создание каталога…", lambda: sftp.mkdir(path) if mode == "remote" else os.mkdir(path),
                     lambda unused: self.refresh())

    def _rename(self):
        row = self._selected()
        if row is None or self._busy or self._mode not in ("local", "remote"):
            return
        mode, sftp, directory, generation = self._mode, self._sftp, self._path, self._generation
        name = self._name_dialog("Переименовать", row[1])
        if name is None or name == row[1]:
            return
        if ((mode, sftp, directory, generation) != (self._mode, self._sftp, self._path, self._generation)
                or self._pending_profile is not None):
            self._error("Файловая панель переключилась. Выберите объект заново.")
            return
        destination = (posixpath if mode == "remote" else os.path).join(directory, name)

        def rename():
            if mode == "remote":
                try:
                    sftp.lstat(destination)
                except IOError as exc:
                    if getattr(exc, "errno", None) != 2:
                        raise
                else:
                    raise IOError("Объект с таким именем уже существует.")
                sftp.rename(row[6], destination)
            else:
                if os.path.lexists(destination):
                    raise IOError("Объект с таким именем уже существует.")
                os.rename(row[6], destination)

        self._submit("Переименование…", rename, lambda unused: self.refresh())

    def _rename_key(self, _tree, event):
        if event.keyval != Gdk.KEY_F2 or event.state & Gtk.accelerator_get_default_mod_mask():
            return False
        self._rename()
        return True

    def _delete(self):
        row = self._selected()
        if row is None:
            return
        detail = "%s\n\nКаталоги удаляются только пустыми. Восстановление через корзину не предусмотрено." % row[6]
        if not self._confirm("Удалить выбранный объект?", detail):
            return
        mode, sftp = self._mode, self._sftp

        def remove():
            if mode == "remote":
                info = sftp.lstat(row[6])
                if stat.S_ISDIR(info.st_mode):
                    sftp.rmdir(row[6])
                else:
                    sftp.remove(row[6])
            elif os.path.islink(row[6]) or not os.path.isdir(row[6]):
                os.unlink(row[6])
            else:
                os.rmdir(row[6])

        self._submit("Удаление…", remove, lambda unused: self.refresh())

    def _progress(self, transferred, total):
        now = time.monotonic()
        if now - self._progress_time >= 0.15 or transferred == total:
            self._progress_time = now
            GLib.idle_add(self._status, "Передано %s / %s" % (_size(transferred), _size(total)))

    @staticmethod
    def _remote_stat(sftp, path):
        try:
            return sftp.lstat(path)
        except IOError as exc:
            if getattr(exc, "errno", None) == 2:
                return None
            raise

    def _remote_replace(self, sftp, temporary, target, original):
        """Use OpenSSH's atomic extension when present, otherwise keep a backup.

        Paramiko 2.0 predates posix_rename(). The backup fallback never truncates
        the original and attempts rollback if publishing the new file fails.
        """
        if original is None:
            sftp.rename(temporary, target)
            return
        if stat.S_ISLNK(original.st_mode) or not stat.S_ISREG(original.st_mode):
            raise IOError("Замена ссылок и специальных файлов не поддерживается.")
        current = sftp.stat(temporary)
        if (getattr(original, "st_uid", None) is not None and
                getattr(original, "st_gid", None) is not None and
                (current.st_uid, current.st_gid) != (original.st_uid, original.st_gid)):
            # Refuse replacement when ownership cannot be preserved.
            sftp.chown(temporary, original.st_uid, original.st_gid)
        sftp.chmod(temporary, stat.S_IMODE(original.st_mode))
        if hasattr(sftp, "posix_rename"):
            try:
                sftp.posix_rename(temporary, target)
                return
            except IOError as exc:
                # A server can lack the extension although the client has it.
                if "unsupported" not in str(exc).lower():
                    raise
        backup = target + ".astraterm-backup-" + uuid.uuid4().hex
        sftp.rename(target, backup)
        try:
            sftp.rename(temporary, target)
        except Exception:
            try:
                sftp.rename(backup, target)
            except Exception:
                raise IOError("Не удалось восстановить исходный файл. Резервная копия: " + backup)
            raise
        try:
            sftp.remove(backup)
        except Exception:
            raise IOError("Файл сохранён, но не удалось удалить резервную копию: " + backup)

    def _upload(self):
        if self._mode != "remote" or self._sftp is None:
            return
        generation, directory = self._generation, self._path
        dialog = Gtk.FileChooserDialog(title="Загрузить файл на сервер", parent=self._parent(),
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Загрузить", Gtk.ResponseType.OK)
        response = dialog.run()
        source = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if source and generation == self._generation:
            self.upload_paths([source], directory=directory)

    def upload_paths(self, sources, terminal_check=None, directory=None, terminal_directory=None):
        if self._closed:
            return
        if self._pending_upload or self._pending_profile is not None or (
                self._busy and self._operation_kind not in ("connect", "list")):
            self._status("Дождитесь завершения текущей файловой операции.")
            return
        if terminal_check is None and self._mode != "remote":
            self._status("Для загрузки выберите SSH-сессию и откройте SFTP.")
            return
        if terminal_check is not None and terminal_directory is None:
            self._error(UNKNOWN_DIRECTORY)
            return
        if not 0 < len(sources) <= 100:
            self._error("Перетащите от 1 до 100 файлов.")
            return
        self._pending_upload = (tuple(sources), directory or self._remote_path, terminal_check,
                                terminal_check is not None and self._operation_kind != "connect", terminal_directory)
        self._resume_upload()

    def _resume_upload(self):
        if self._busy:
            return
        sources, directory, terminal_check, may_connect, terminal_directory = self._pending_upload
        self._pending_upload = None
        if terminal_check is not None and not terminal_check():
            self._status("SSH-подключение изменилось. Повторите загрузку.")
            return
        if self._sftp is None:
            if may_connect:
                generation = self._generation
                self.connect_remote()
                if self._busy and generation == self._generation and terminal_check():
                    self._pending_upload = (sources, directory, terminal_check, False, terminal_directory)
                    return
            self._status("SFTP недоступен. Подключите SFTP и повторите загрузку.")
            return
        sftp, directory = self._sftp, directory or self._remote_path or self._remote_home
        generation = self._generation
        host, user, home = self.profile.get("host", ""), self.profile.get("username"), self._remote_home

        def check_binding():
            if self._closed or generation != self._generation or (terminal_check and not terminal_check()):
                raise IOError("SSH-подключение изменилось. Загрузка отменена.")

        def upload():
            target_directory = directory
            if terminal_directory is not None:
                def read_hostname():
                    try:
                        with sftp.open(REMOTE_HOSTNAME, "rb") as stream:
                            return stream.read(256).decode("ascii", "replace").strip()
                    except IOError:
                        return ""
                check_binding()
                target_directory = resolve_terminal_directory(
                    terminal_directory, host, user or os.environ.get("USER") or getpass.getuser(), home, read_hostname)
                target_directory = sftp.normalize(target_directory)
                if not posixpath.isabs(target_directory) or not stat.S_ISDIR(sftp.stat(target_directory).st_mode):
                    raise IOError("Текущий каталог терминала недоступен в SFTP.")
                check_binding()
            for source in sources:
                if not os.path.isabs(source) or not stat.S_ISREG(os.lstat(source).st_mode):
                    raise IOError("Можно загружать только обычные локальные файлы.")
                if not _safe_name(os.path.basename(source)):
                    raise IOError("Недопустимое имя файла.")
            uploaded = 0
            for source in sources:
                check_binding()
                target = posixpath.join(target_directory, os.path.basename(source))
                before = os.lstat(source)
                if not stat.S_ISREG(before.st_mode):
                    raise IOError("Исходный файл больше не является обычным файлом.")
                source_revision = _snapshot(source)
                original = self._remote_stat(sftp, target)
                if original is not None:
                    if not stat.S_ISREG(original.st_mode):
                        raise IOError("Замена ссылок и специальных файлов не поддерживается.")
                    if not self._confirm_from_worker("Заменить файл на сервере?", target, generation):
                        continue
                check_binding()
                temporary = posixpath.join(target_directory, ".astraterm-upload-" + uuid.uuid4().hex)
                try:
                    sftp.put(source, temporary, callback=self._progress)
                    check_binding()
                    if not stat.S_ISREG(os.lstat(source).st_mode) or _snapshot(source) != source_revision:
                        raise IOError("Исходный файл изменился во время передачи.")
                    current = self._remote_stat(sftp, target)
                    expected = None if original is None else (original.st_size, original.st_mtime, original.st_mode)
                    actual = None if current is None else (current.st_size, current.st_mtime, current.st_mode)
                    if actual != expected:
                        raise IOError("Файл назначения изменился во время передачи.")
                    self._remote_replace(sftp, temporary, target, original)
                    uploaded += 1
                finally:
                    try:
                        sftp.remove(temporary)
                    except IOError:
                        pass
            return uploaded

        def refresh_upload():
            if self._mode == "remote" and self._pending_profile is None:
                self.refresh()

        def upload_failed(error):
            self._error(str(error))
            refresh_upload()

        def upload_complete(uploaded):
            if uploaded and self.on_notice:
                self.on_notice("Загружено файлов: {}".format(uploaded))
            refresh_upload()

        self._submit("Загрузка файлов…", upload, upload_complete, failed=upload_failed)

    def _drag_press(self, tree, event):
        self._drag_gesture = None
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS or self._mode != 'remote':
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit:
            row = tuple(self.model[hit[0]])
            if row[9] and not row[5]:
                self._drag_gesture = {'row': row, 'generation': self._generation,
                                      'x': int(event.x), 'y': int(event.y), 'moved': False}
        return False

    def _drag_release(self, _tree, _event):
        self._drag_gesture = None
        return False

    def _drag_motion(self, tree, event):
        gesture = self._drag_gesture
        if (not gesture or gesture['moved'] or self._busy or self._sftp is None or
                gesture['generation'] != self._generation or not event.state & Gdk.ModifierType.BUTTON1_MASK or
                not tree.drag_check_threshold(gesture['x'], gesture['y'], int(event.x), int(event.y))):
            return False
        gesture['moved'] = True
        row = gesture['row']
        if not _safe_name(row[1]):
            return False
        capture = event.copy()
        sftp = self._sftp
        binding = (self._generation, row[6], row[2], row[3])

        def begin(filename):
            window = tree.get_bin_window()
            device = capture.get_device()
            if (self._drag_gesture is gesture and self._pending_profile is None and
                    window is not None and device is not None and
                    window.get_device_position(device)[-1] & Gdk.ModifierType.BUTTON1_MASK):
                self._drag_file = filename
                context = tree.drag_begin_with_coordinates(self._drag_targets, Gdk.DragAction.COPY, 1, capture, -1, -1)
                if context is not None:
                    self._drag_ready = None
                    self._status("Файл готов к копированию")
                    return
                self._drag_file = None
            self._status("Файл готов. Перетащите его ещё раз.")

        if self._drag_ready and self._drag_ready[0] == binding and os.path.isfile(self._drag_ready[1]):
            begin(self._drag_ready[1])
            return True
        if self._drag_root is None:
            self._drag_root = tempfile.TemporaryDirectory(prefix='astraterm-drag-')
        root = self._drag_root.name

        def prepare():
            directory = tempfile.mkdtemp(dir=root)
            filename = os.path.join(directory, row[1])
            try:
                self._receive_file(sftp, row[6], filename)
                return filename
            except Exception:
                self._discard_drag_file(filename)
                raise

        def ready(filename):
            self._drag_ready = (binding, filename)

            def expire():
                if self._drag_file == filename:
                    return True
                self._drag_timers.discard(source)
                self._discard_drag_file(filename)
                return False
            source = GLib.timeout_add_seconds(600, expire)
            self._drag_timers.add(source)
            begin(filename)

        self._submit("Подготовка файла для перетаскивания…", prepare, ready,
                     cancel=self._discard_drag_file, kind='drag-export')
        return True

    @staticmethod
    def _discard_drag_file(filename):
        try:
            os.unlink(filename)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(os.path.dirname(filename))
        except FileNotFoundError:
            pass

    def _drag_data(self, _tree, _context, selection, _info, _timestamp):
        if self._drag_file:
            selection.set_uris([GLib.filename_to_uri(self._drag_file, None)])

    def _drag_end(self, _tree, _context):
        self._drag_file = None
        self._drag_gesture = None

    def _receive_file(self, sftp, source, filename):
        before = sftp.lstat(source)
        if not stat.S_ISREG(before.st_mode or 0):
            raise IOError("Выберите обычный файл.")
        sftp.get(source, filename, callback=self._progress)
        after = sftp.lstat(source)
        if ((before.st_size, before.st_mtime, before.st_mode) != (after.st_size, after.st_mtime, after.st_mode)
                or before.st_size != os.path.getsize(filename)):
            raise IOError("Удалённый файл изменился во время передачи.")

    def _download(self):
        if self._mode != "remote" or self._sftp is None:
            return
        row = self._selected()
        if row is None:
            return
        if row[4] or row[5]:
            self._error("Выберите обычный файл. Скачивание каталогов и ссылок пока не поддерживается.")
            return
        if not _safe_name(row[1]):
            self._error("Сервер передал недопустимое имя файла.")
            return
        dialog = Gtk.FileChooserDialog(title="Скачать файл", parent=self._parent(),
                                       action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Скачать", Gtk.ResponseType.OK)
        dialog.set_current_name(row[1])
        dialog.set_do_overwrite_confirmation(True)
        response = dialog.run()
        destination = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not destination:
            return
        sftp = self._sftp
        original = _snapshot(destination) if os.path.exists(destination) else None
        generation = self._generation

        def download():
            descriptor, temporary = tempfile.mkstemp(prefix=".astraterm-download-", dir=os.path.dirname(destination))
            os.close(descriptor)
            try:
                self._receive_file(sftp, row[6], temporary)
                current = _snapshot(destination) if os.path.exists(destination) else None
                if current != original and not self._confirm_from_worker(
                        "Файл назначения изменился. Заменить?", destination, generation):
                    return False
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return True

        self._submit("Скачивание файла…", download,
                     lambda done: self._status("Сохранено: " + destination if done else "Скачивание отменено."))

    def _edit(self, path):
        mode, sftp = self._mode, self._sftp
        encoding = self.profile.get("encoding") or "utf-8"

        def load():
            if mode == "remote":
                actual = sftp.normalize(path)
                info = sftp.stat(actual)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EDIT_SIZE:
                    raise IOError("Редактор открывает обычные текстовые файлы не больше 2 МБ.")
                with sftp.open(actual, "rb") as stream:
                    data = stream.read(MAX_EDIT_SIZE + 1)
                snapshot = (info.st_size, info.st_mtime)
            else:
                actual = os.path.realpath(path)
                info = os.stat(actual)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EDIT_SIZE:
                    raise IOError("Редактор открывает обычные текстовые файлы не больше 2 МБ.")
                with open(actual, "rb") as stream:
                    data = stream.read(MAX_EDIT_SIZE + 1)
                snapshot = _snapshot(actual)
            if len(data) > MAX_EDIT_SIZE or b"\x00" in data:
                raise IOError("Файл слишком большой или имеет двоичный формат.")
            try:
                text = data.decode(encoding)
            except (UnicodeError, LookupError):
                raise IOError("Не удалось прочитать текст в кодировке %s. "
                              "Выберите подходящую кодировку в свойствах сессии." % encoding)
            return actual, text, snapshot, stat.S_IMODE(info.st_mode)

        self._submit("Открытие файла…", load,
                     lambda result: self._show_editor(mode, sftp, encoding, result))

    def _show_editor(self, mode, sftp, encoding, result):
        path, text, snapshot, permissions = result
        dialog = Gtk.Dialog(title=("SFTP · " if mode == "remote" else "") + path,
                            transient_for=self._parent(), modal=False)
        dialog.set_default_size(860, 600)
        dialog.add_buttons("Закрыть", Gtk.ResponseType.CLOSE,
                           "Сохранить на сервер" if mode == "remote" else "Сохранить", Gtk.ResponseType.APPLY)
        self._editors.append(dialog)
        body = dialog.get_content_area()
        body.set_spacing(8)
        body.set_border_width(10)
        label = Gtk.Label(label=("Удалённый файл · " if mode == "remote" else "Локальный файл · ") + encoding)
        label.set_xalign(0)
        body.pack_start(label, False, False, 0)
        view = Gtk.TextView()
        view.modify_font(Pango.FontDescription("DejaVu Sans Mono 10"))
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.set_left_margin(10)
        view.set_right_margin(10)
        buffer = view.get_buffer()
        dialog._astraterm_buffer = buffer
        buffer.set_text(text)
        buffer.set_modified(False)
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        body.pack_start(scroll, True, True, 0)
        current_snapshot = [snapshot]
        saving = [False]
        generation = self._generation

        def close():
            if saving[0]:
                return False
            if buffer.get_modified() and not self._confirm("Закрыть без сохранения?", path):
                return False
            dialog.destroy()
            return True

        def response(unused, answer):
            if answer != Gtk.ResponseType.APPLY:
                close()
                return
            if saving[0] or self._busy:
                self._status("Дождитесь завершения текущей операции с файлами.")
                return
            if mode == "remote" and (sftp is not self._sftp or generation != self._generation):
                self._error("SFTP-подключение изменилось. Сохранение в прежнюю сессию недоступно.")
                return
            content = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            try:
                data = content.encode(encoding)
            except (UnicodeError, LookupError) as exc:
                self._error(str(exc))
                return
            if len(data) > MAX_EDIT_SIZE:
                self._error("Размер файла после редактирования превышает 2 МБ.")
                return

            def save():
                if mode == "remote":
                    original = sftp.stat(path)
                    seen = (original.st_size, original.st_mtime)
                else:
                    original = os.stat(path)
                    seen = _snapshot(path)
                if seen != current_snapshot[0] and not self._confirm_from_worker(
                        "Файл был изменён другим приложением. Перезаписать?", path, generation):
                    return None
                if mode == "remote":
                    temporary = posixpath.join(posixpath.dirname(path), ".astraterm-edit-" + uuid.uuid4().hex)
                    try:
                        with sftp.open(temporary, "wb") as stream:
                            stream.write(data)
                        self._remote_replace(sftp, temporary, path, original)
                    finally:
                        try:
                            sftp.remove(temporary)
                        except IOError:
                            pass
                    saved = sftp.stat(path)
                    return saved.st_size, saved.st_mtime
                descriptor, temporary = tempfile.mkstemp(prefix=".astraterm-edit-", dir=os.path.dirname(path))
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    created = os.stat(temporary)
                    if (created.st_uid, created.st_gid) != (original.st_uid, original.st_gid):
                        os.chown(temporary, original.st_uid, original.st_gid)
                    shutil.copystat(path, temporary)
                    os.utime(temporary, None)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return _snapshot(path)

            def saved(updated):
                saving[0] = False
                dialog.set_response_sensitive(Gtk.ResponseType.APPLY, True)
                view.set_editable(True)
                if updated is not None:
                    current_snapshot[0] = updated
                    buffer.set_modified(False)
                    label.set_text("Сохранено · " + encoding)
                    self._status("Сохранено: " + path)
                    if mode == self._mode:
                        self.refresh()

            # Completion must also restore editor controls after an error.
            def guarded_save():
                try:
                    return save()
                except Exception:
                    GLib.idle_add(restore)
                    raise

            def restore():
                saving[0] = False
                if dialog in self._editors:
                    dialog.set_response_sensitive(Gtk.ResponseType.APPLY, True)
                    view.set_editable(True)
                return False

            saving[0] = True
            dialog.set_response_sensitive(Gtk.ResponseType.APPLY, False)
            view.set_editable(False)
            self._submit("Сохранение файла…", guarded_save, saved)

        def destroyed(unused):
            if dialog in self._editors:
                self._editors.remove(dialog)

        dialog.connect("response", response)
        dialog.connect("delete-event", lambda unused, event: not close())
        dialog.connect("destroy", destroyed)
        dialog.show_all()

