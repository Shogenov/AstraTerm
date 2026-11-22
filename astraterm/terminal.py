"""A real VTE terminal, compatible with Astra's VTE 0.46."""
import os
import errno
import fcntl
import re
import shlex
import signal
import sys
import tempfile
import shutil

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, Vte

from .core import session_argv
from .vte_input import InputTerminal
from .terminal_directory import directory_report


PALETTE = ["#242a30", "#df7d85", "#90bf8f", "#d6bc83", "#8faedb", "#b69dcf", "#85bfc2", "#d9e1e8",
           "#65717d", "#eea0a5", "#add6a1", "#e3cf9f", "#afc8ed", "#ccb5e3", "#a3d5d7", "#f0f3f6"]
LIGHT_PALETTE = ["#26333e", "#a73145", "#217446", "#785700", "#285aa0", "#794592", "#176b73", "#52616d",
                 "#60717d", "#ae3b50", "#286c41", "#806312", "#305fa3", "#8a4291", "#16676f", "#465562"]


def rgba(value):
    color = Gdk.RGBA()
    color.parse(value)
    return color


class TerminalPane(Gtk.Box):
    def __init__(self, profile, on_focus=None, on_status=None, on_title=None, font_size=11, on_close=None,
                 on_input=None, on_paste=None, on_connection=None, on_select=None,
                 on_paste_pending_restart=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.profile = dict(profile)
        self.on_focus = on_focus or (lambda pane: None)
        self.on_status = on_status or (lambda text: None)
        self.on_title = on_title or (lambda pane, title: None)
        self.on_close = on_close
        self.on_input = on_input or (lambda pane, data: None)
        self.on_paste = on_paste
        self.on_connection = on_connection or (lambda pane: None)
        self.on_select = on_select or (lambda pane, active: None)
        self.on_paste_pending_restart = on_paste_pending_restart
        self.input_retired = False
        self.connection_generation = 0
        self.directory_report = None
        self.pid = None
        self.closed = False
        self.exited = False
        self.font_size = font_size
        self.exit_code = None
        self._restart_handler = None
        self._record_initialized = False
        self._drain_fd = None
        self._credential_broker = None
        self._credential_runtime = None
        self.layout_heading_visible = True
        self.get_style_context().add_class("terminal-pane")
        self.set_hexpand(True)
        self.set_vexpand(True)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.heading = Gtk.EventBox()
        self.heading.set_visible_window(False)
        self.heading.add(bar)
        bar.get_style_context().add_class("terminal-heading")
        bar.set_border_width(4)
        marker = Gtk.Label(label="●")
        marker.get_style_context().add_class("terminal-focus-marker")
        bar.pack_start(marker, False, False, 4)
        self.title_label = Gtk.Label(label=self.profile.get("name", "Terminal"), xalign=0)
        self.title_label.get_style_context().add_class("terminal-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        bar.pack_start(self.title_label, True, True, 0)
        self.state_label = Gtk.Label(label=self.profile.get("protocol", "local").upper())
        self.state_label.get_style_context().add_class("muted")
        self.state_label.get_style_context().add_class("terminal-state")
        self.state_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.state_label.set_max_width_chars(12)
        bar.pack_start(self.state_label, False, False, 2)
        self.multiexec_check = Gtk.CheckButton()
        self.multiexec_check.get_style_context().add_class("multiexec-target")
        self.multiexec_check.set_no_show_all(True)
        self.multiexec_check.connect("toggled", self._selection_changed)
        bar.pack_start(self.multiexec_check, False, False, 0)
        close_button = Gtk.Button()
        from .ui_helpers import action_image
        close_button.add(action_image("close"))
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.get_style_context().add_class("flat")
        close_button.get_style_context().add_class("pane-close")
        close_button.set_tooltip_text("Закрыть эту панель терминала")
        close_button.connect("clicked", lambda *_: self.on_close(self) if self.on_close else None)
        bar.pack_start(close_button, False, False, 0)
        self.heading.show_all()
        self.heading.set_no_show_all(True)
        self.pack_start(self.heading, False, False, 0)
        identity = self.profile.get("name") or self.profile.get("host") or "Терминал"
        if self.profile.get("host"):
            identity += " · {}@{}:{}".format(self.profile.get("username") or "default",
                                             self.profile["host"], self.profile.get("port", 22))
        self.identity = identity
        self.title_label.set_tooltip_text(identity)
        self.multiexec_check.set_tooltip_text("Получать ввод MultiExec · " + identity)
        self.multiexec_check.get_accessible().set_name("Получать ввод MultiExec · " + identity)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.get_style_context().add_class("terminal-body")
        self.terminal = InputTerminal()
        self.terminal.get_style_context().add_class("terminal-surface")
        self.terminal.on_user_input = lambda data: self.on_input(self, data)
        self.terminal.on_user_paste = lambda primary: self.paste(primary)
        self.terminal.on_input_error = lambda: self.on_status("MultiExec: формат ввода не поддерживается; проверьте получателей")
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        self.terminal.set_scrollback_lines(20000)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_encoding(self.profile.get("encoding", "UTF-8"))
        self.terminal.set_rewrap_on_resize(True)
        self.terminal.set_allow_hyperlink(True) if hasattr(self.terminal, "set_allow_hyperlink") else None
        self.set_theme('dark')
        self.set_font_size(font_size)
        body.pack_start(self.terminal, True, True, 0)
        body.pack_end(Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL,
                                   adjustment=self.terminal.get_vadjustment()), False, False, 0)
        self.pack_start(body, True, True, 0)
        self.search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_bar.set_border_width(6)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Найти в терминале…")
        self.search_entry.connect("changed", self._search_changed)
        self.search_entry.connect("activate", lambda *_: self.terminal.search_find_next())
        self.search_bar.pack_start(self.search_entry, True, True, 0)
        for label, callback in [("↑", lambda *_: self.terminal.search_find_previous()),
                                ("↓", lambda *_: self.terminal.search_find_next()),
                                ("×", lambda *_: self.search_bar.hide())]:
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            self.search_bar.pack_start(button, False, False, 0)
        self.pack_end(self.search_bar, False, False, 0)
        self.search_bar.set_no_show_all(True)
        self.terminal.connect("child-exited", self._child_exited)
        self.terminal.connect("window-title-changed", self._title_changed)
        self.terminal.connect("current-directory-uri-changed", self._directory_changed)
        self.terminal.connect("focus-in-event", self._focused)
        self.terminal.connect("key-press-event", self._ended_key)
        self.terminal.connect("button-press-event", self._button_press)
        self.terminal.connect("bell", lambda *_: self.on_status("Звуковой сигнал терминала"))
        GLib.idle_add(self.start)

    def start(self):
        if self.closed or self.pid is not None:
            return False
        if self.input_retired:
            self.on_status("Эта панель доступна только для просмотра истории")
            return False
        self.terminal.cancel_pending_fetches()
        if self.terminal.native_paste_pending:
            self._restart_with_pending_paste()
            return False
        if self.profile.get('_credential_gate') and not self.profile['_credential_gate'](self.start):
            return False
        try:
            self.directory_report = None
            argv = self.profile.get("_argv") or session_argv(self.profile)
            if (self.profile.get('_credential_provider') and self.profile.get('protocol') == 'ssh' and
                    not self.profile.get('key_file') and not self.profile.get('_agent_key_blob')):
                argv = argv[:1] + ['-o', 'PreferredAuthentications=password,keyboard-interactive',
                                  '-o', 'PubkeyAuthentication=no'] + argv[1:]
            if self.profile.get('_credential_prompt') and self.profile.get('protocol') == 'ssh':
                argv = [sys.executable,os.path.join(os.path.dirname(__file__),'credential_launch.py'),'--'] + argv
            if self.profile.get("_recording_path"):
                path = self.profile["_recording_path"]
                flags = os.O_CREAT | os.O_WRONLY | (os.O_APPEND if self._record_initialized else os.O_TRUNC)
                descriptor = os.open(path, flags, 0o600)
                os.fchmod(descriptor, 0o600)
                os.close(descriptor)
                self._record_initialized = True
                argv = ["script", "-q", "-f", "-a", "-e", "-c", " ".join(shlex.quote(arg) for arg in argv), path]
            cwd = os.path.expanduser(self.profile.get("startup_directory") or "~")
            if not os.path.isdir(cwd):
                cwd = os.path.expanduser("~")
            environment = dict(os.environ)
            if self.profile.get("_ssh_agent_sock"):
                environment["SSH_AUTH_SOCK"] = self.profile["_ssh_agent_sock"]
            environment["TERM"] = "xterm-256color"
            environment["COLORTERM"] = "truecolor"
            if self.profile.get('_credential_prompt') and self.profile.get('protocol') == 'ssh':
                from .openssh_sftp import _PromptBroker
                self._close_credentials()
                runtime = self._credential_runtime = tempfile.mkdtemp(prefix='astraterm-credential-')
                used_password = [False]

                def use_saved():
                    if used_password[0]:
                        return None
                    used_password[0] = True
                    return self.profile['_credential_provider']()

                def answer(prompt):
                    return self.profile['_credential_prompt'](self.profile, prompt, use_saved,
                                                               lambda: self.closed or self._credential_runtime != runtime)
                self._credential_broker = _PromptBroker(self._credential_runtime, answer)
                environment.update(SSH_ASKPASS=os.path.abspath(os.path.join(os.path.dirname(__file__),'../scripts/astraterm-askpass')),
                                   ASTRATERM_ASKPASS_SOCKET=self._credential_broker.path,ASTRATERM_ASKPASS_REQUEST='1',
                                   SSH_ASKPASS_REQUIRE='force')
                if not environment.get('DISPLAY'):environment['DISPLAY']=':0'
            envv = ["{}={}".format(k, v) for k, v in environment.items()]
            # spawn_async is unavailable in VTE 0.46. spawn_sync already watches the child.
            ok, pid = self.terminal.spawn_sync(Vte.PtyFlags.DEFAULT, cwd, argv, envv,
                                                GLib.SpawnFlags.SEARCH_PATH, None, None, None)
            if not ok:
                raise RuntimeError("VTE не смог запустить процесс")
            self.pid = pid
            self.connection_generation += 1
            # VTE 0.46 reaps with higher priority than its PTY reader and can
            # close the master before consuming the last bytes of a fast child.
            # Retain a nonblocking master until child-exited so we can drain
            # those bytes after VTE has processed its own incoming buffer.
            self._close_master()
            self._drain_fd = os.dup(self.terminal.get_pty().get_fd())
            flags = fcntl.fcntl(self._drain_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._drain_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self.exited = False
            self.exit_code = None
            self.on_connection(self)
            self.state_label.set_text(self.profile.get("protocol", "local").upper() +
                                      (" · REC" if self.profile.get("_recording_path") else ""))
            self.on_status("Подключение: " + self.profile.get("name", "Terminal"))
            self.terminal.grab_focus()
        except Exception as exc:
            self._close_credentials()
            self.exited = True
            self.state_label.set_text("ОШИБКА")
            self.terminal.feed(("\r\nНе удалось запустить сессию: {}\r\n".format(exc)).encode("utf-8"))
            self.on_status(str(exc))
        return False

    def _child_exited(self, terminal, status):
        self._close_credentials()
        self._drain_master()
        self.directory_report = None
        self.pid = None
        self.connection_generation += 1
        self.exited = True
        self.on_connection(self)
        self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
        if not self.closed:
            self.state_label.set_text("ИСТОРИЯ · {}".format(self.exit_code) if self.input_retired else
                                      "ЗАВЕРШЕНО · {}".format(self.exit_code))
            message = "[Сессия завершена, код {}. R — переподключиться, другая клавиша — закрыть.]".format(self.exit_code)
            terminal.feed(("\r\n" + message + "\r\n").encode("utf-8"))
            self.on_status("Сессия завершена: " + self.profile.get("name", "Terminal"))

    def _ended_key(self, _terminal, event):
        if self.closed or not self.exited or event.is_modifier:
            return False
        if (Gdk.keyval_to_lower(event.keyval) in (Gdk.KEY_r, Gdk.KEY_Cyrillic_ka) and
                not event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK)):
            self.restart(ask=False)
        elif self.on_close:
            self.on_close(self)
        return True

    def _close_master(self):
        if self._drain_fd is not None:
            try:
                os.close(self._drain_fd)
            except OSError:
                pass
            self._drain_fd = None

    def _close_credentials(self):
        broker, self._credential_broker = self._credential_broker, None
        runtime, self._credential_runtime = self._credential_runtime, None
        if broker is not None:
            broker.close()
        if runtime is not None:
            shutil.rmtree(runtime, ignore_errors=True)

    def _drain_master(self):
        if self._drain_fd is None:
            return
        try:
            # A background grandchild must not keep the UI in an endless read.
            for _ in range(128):
                try:
                    data = os.read(self._drain_fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EIO):
                        break
                    raise
                if not data:
                    break
                # VTE enables TIOCPKT: every master read starts with a status
                # byte, which is not terminal output (including header-only reads).
                if not self.closed and data[0] == 0 and len(data) > 1:
                    self.terminal.feed(data[1:])
        except OSError as exc:
            self.on_status("Ошибка чтения завершённой сессии: " + str(exc))
        finally:
            self._close_master()

    def _title_changed(self, terminal):
        reported = terminal.get_window_title()
        if self.pid is not None and self.profile.get("protocol") == "ssh":
            self.directory_report = directory_report("title", reported)
        title = reported or self.profile.get("name", "Terminal")
        self.title_label.set_text(title)
        self.on_title(self, title)

    def _directory_changed(self, terminal):
        if self.pid is not None and self.profile.get("protocol") == "ssh":
            self.directory_report = directory_report("osc7", terminal.get_current_directory_uri())

    def _focused(self, *_):
        self.on_focus(self)
        return False

    def _selection_changed(self, check):
        self.on_select(self, check.get_active())

    def set_layout_heading(self, visible):
        """Show a host heading only when the layout needs to distinguish panes."""
        self.layout_heading_visible = bool(visible)
        self.heading.set_visible(self.layout_heading_visible or self.multiexec_check.get_visible())

    def show_multiexec_selection(self, visible, selected=False, eligible=True):
        self.multiexec_check.set_sensitive(eligible)
        if self.multiexec_check.get_active() != selected:
            self.multiexec_check.set_active(selected)
        self.multiexec_check.set_visible(visible)
        self.set_layout_heading(self.layout_heading_visible)
        style = self.get_style_context()
        style.add_class("multiexec-selected") if selected else style.remove_class("multiexec-selected")

    def _button_press(self, terminal, event):
        if event.button != 3:
            return False
        self.on_focus(self)
        menu = Gtk.Menu()
        for label, callback in [("Копировать", lambda *_: terminal.copy_clipboard()),
                                ("Вставить", lambda *_: self.paste()),
                                ("Выделить всё", lambda *_: terminal.select_all()),
                                ("Найти…", lambda *_: self.show_search()),
                                ("Сохранить вывод…", lambda *_: self.save_output()),
                                ("Переподключить", lambda *_: self.restart()),
                                ("Сбросить терминал", lambda *_: terminal.reset(True, True))]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def send(self, text):
        if self.pid is not None and not self.closed and not self.input_retired:
            self.terminal.send_text(text)

    def send_bytes(self, data):
        if self.pid is not None and not self.closed and not self.input_retired:
            self.terminal.send_bytes(data)

    def paste(self, primary=False):
        if self.input_retired:
            return
        if self.on_paste:
            self.on_paste(self, primary)
        elif self.pid is not None and not self.closed:
            self.terminal.native_paste(primary)

    def show_search(self):
        self.search_bar.show_all()
        self.search_bar.show()
        self.search_entry.grab_focus()

    def _search_changed(self, entry):
        pattern = entry.get_text()
        if not pattern:
            self.terminal.search_set_gregex(None, 0)
            return
        regex = GLib.Regex.new(re.escape(pattern),
                              GLib.RegexCompileFlags.CASELESS | GLib.RegexCompileFlags.MULTILINE, 0)
        self.terminal.search_set_gregex(regex, 0)
        self.terminal.search_set_wrap_around(True)
        self.terminal.search_find_next()

    def set_font_size(self, size):
        self.font_size = max(7, min(28, size))
        self.terminal.set_font(Pango.FontDescription("DejaVu Sans Mono {}".format(self.font_size)))

    def set_theme(self, theme):
        light = theme == 'light'
        colors = LIGHT_PALETTE if light else PALETTE
        self.terminal.set_colors(rgba("#21303a" if light else "#d9e1e8"),
                                 rgba("#ffffff" if light else "#101316"), [rgba(color) for color in colors])
        self.terminal.set_color_cursor(rgba("#246752" if light else "#82d9bd"))

    def save_output(self, path=None):
        if path is None:
            dialog = Gtk.FileChooserDialog(title="Сохранить вывод терминала", transient_for=self.get_toplevel(),
                                           action=Gtk.FileChooserAction.SAVE)
            dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
            dialog.set_do_overwrite_confirmation(True)
            dialog.set_current_name("astraterm-session.txt")
            response = dialog.run()
            path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
            dialog.destroy()
            if not path:
                return
        try:
            stream = Gio.File.new_for_path(path).replace(None, False, Gio.FileCreateFlags.PRIVATE, None)
            try:
                self.terminal.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
            finally:
                stream.close(None)
            self.on_status("Вывод сохранён: " + path)
        except Exception as exc:
            self.on_status("Ошибка сохранения: " + str(exc))

    def restart(self,profile=None,ask=True):
        if self.closed:
            return
        if profile is None and getattr(self,'on_reconnect',None):return self.on_reconnect(self)
        self.terminal.cancel_pending_fetches()
        if self.input_retired or self.terminal.native_paste_pending:
            self._replacement_profile=profile
            self._restart_with_pending_paste()
            return
        if self._restart_handler is not None:
            return
        if self.pid is not None and ask:
            dialog = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                       message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                                       text="Переподключить активную сессию?")
            dialog.format_secondary_text("Текущий процесс и выполняемые в нём команды будут завершены.")
            result = dialog.run()
            dialog.destroy()
            if result != Gtk.ResponseType.OK:
                return
        if self.closed:return
        if profile is not None:
            if profile.get('_recording_path')!=self.profile.get('_recording_path'):self._record_initialized=False
            self.profile=dict(profile)
            self.terminal.set_encoding(self.profile.get('encoding','UTF-8'))
            self.title_label.set_text(self.profile.get('name','Terminal'))
        if self.pid is not None:
            # Wait for child-exited before starting another child on the same widget.
            self._restart_handler = self.terminal.connect("child-exited", self._restart_after_exit)
            self._hangup()
        else:
            self.start()

    def _restart_with_pending_paste(self):
        """Keep a possibly delayed native paste isolated from every future PTY."""
        if self.on_paste_pending_restart is None:
            self.on_status("Вставка ещё ожидается: откройте новое подключение в отдельной панели")
            return
        if not self.input_retired:
            # Input-disabled also drops VTE's late native clipboard callback.
            # Preserve the widget and its exact scrollback; never attach a new
            # PTY to this widget, even if the old child subsequently exits.
            self.input_retired = True
            self.terminal.set_input_enabled(False)
            self.terminal.disconnect_input()
            self.connection_generation += 1
            self.on_connection(self)
            self.state_label.set_text("ИСТОРИЯ · ВСТАВКА ОТМЕНЕНА")
        self.on_status("Старая панель оставлена для истории; новое подключение откроется отдельно")
        self.on_paste_pending_restart(self)

    def _restart_after_exit(self, *_):
        self.terminal.disconnect(self._restart_handler)
        self._restart_handler = None
        GLib.idle_add(self.start)

    def _hangup(self):
        if self.pid is not None:
            try:
                os.killpg(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    os.kill(self.pid, signal.SIGHUP)
                except OSError:
                    pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.connection_generation += 1
        self.on_connection(self)
        self._hangup()
        self._close_credentials()
        self._close_master()
        self.terminal.disconnect_input()
        self.terminal.destroy()
        self.destroy()


class TerminalWorkspace(Gtk.Box):
    @staticmethod
    def _position_split(paned, ratio):
        handler = getattr(paned, '_position_handler', None)
        if handler is not None:
            paned.disconnect(handler)

        def allocate(widget, allocation):
            horizontal = widget.get_orientation() == Gtk.Orientation.HORIZONTAL
            measure = Gtk.Widget.get_allocated_width if horizontal else Gtk.Widget.get_allocated_height
            extent = measure(widget.get_child1()) + measure(widget.get_child2())
            if extent > 10:
                widget.disconnect(widget._position_handler)
                widget._position_handler = None
                widget.set_position(int(round(extent * ratio)))

        paned._position_handler = paned.connect_after('size-allocate', allocate)
        paned.queue_resize()

    def balance_horizontal(self):
        """Give each column equal space, keeping existing vertical splits together."""
        def balance(widget):
            if not isinstance(widget, Gtk.Paned) or widget.get_orientation() != Gtk.Orientation.HORIZONTAL:
                return 1
            left = balance(widget.get_child1())
            right = balance(widget.get_child2())
            self._position_split(widget, left / float(left + right))
            return left + right

        balance(self.get_children()[0])

    def capture_layout(self, root=None):
        def snapshot(widget):
            if widget in self.panes:
                return self.panes.index(widget)
            horizontal = widget.get_orientation() == Gtk.Orientation.HORIZONTAL
            extent = widget.get_allocated_width() if horizontal else widget.get_allocated_height()
            ratio = max(.01, min(.99, widget.get_position() / float(extent))) if extent > 1 else .5
            return ['horizontal' if horizontal else 'vertical', ratio,
                    snapshot(widget.get_child1()), snapshot(widget.get_child2())]

        return snapshot(root if root is not None else self.get_children()[0])

    def restore_layout(self,layout):
        """Arrange new panes using a captured split tree."""
        root=self.get_children()[0]
        for pane in self.panes:pane.get_parent().remove(pane)
        if root not in self.panes:self.remove(root);root.destroy()
        def build(node):
            if isinstance(node,int):return self.panes[node]
            direction, ratio, left, right = node
            orientation = Gtk.Orientation.HORIZONTAL if direction == 'horizontal' else Gtk.Orientation.VERTICAL
            split = Gtk.Paned(orientation=orientation)
            split.pack1(build(left),True,False);split.pack2(build(right),True,False)
            def position(widget,allocation):
                size=allocation.width if orientation==Gtk.Orientation.HORIZONTAL else allocation.height
                if size>10:widget.set_position(int(size*ratio));widget.disconnect(widget._copy_layout_handler)
            split._copy_layout_handler=split.connect('size-allocate',position)
            return split
        self.pack_start(build(layout),True,True,0);self.show_all();self._sync_headings()

    def __init__(self, profile, on_focus=None, on_status=None, font_size=11, on_empty=None,
                 on_input=None, on_paste=None, on_connection=None, on_select=None,
                 on_pane_added=None, on_pane_close=None, on_paste_pending_restart=None, existing_pane=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self.panes = []
        self.active = None
        self.focus_callback = on_focus or (lambda pane: None)
        self.status_callback = on_status
        self.font_size = font_size
        self.on_empty = on_empty
        self.on_input = on_input
        self.on_paste = on_paste
        self.on_connection = on_connection
        self.on_select = on_select
        self.on_pane_added = on_pane_added
        self.on_pane_close = on_pane_close
        self.on_paste_pending_restart = on_paste_pending_restart
        pane = self._adopt_pane(existing_pane) if existing_pane is not None else self._new_pane(profile)
        self.pack_start(pane, True, True, 0)

    def _adopt_pane(self, pane):
        pane.on_focus = self._on_focus
        pane.on_close = self.on_pane_close or self.close_pane
        pane.on_paste_pending_restart = self.on_paste_pending_restart
        self.panes.append(pane)
        self.active = pane
        self._sync_headings()
        if self.on_pane_added:
            self.on_pane_added(pane)
        return pane

    def _new_pane(self, profile):
        pane_class = TerminalPane
        if profile.get('protocol') == 'rdp':
            from .rdp import RdpPane
            pane_class = RdpPane
        pane = pane_class(profile, on_focus=self._on_focus, on_status=self.status_callback,
                            font_size=self.font_size,
                            on_close=self.on_pane_close or self.close_pane,
                            on_input=self.on_input, on_paste=self.on_paste,
                            on_connection=self.on_connection, on_select=self.on_select,
                            on_paste_pending_restart=self.on_paste_pending_restart)
        self.panes.append(pane)
        self.active = pane
        self._sync_headings()
        if self.on_pane_added:
            self.on_pane_added(pane)
        return pane

    def _sync_headings(self):
        for pane in self.panes:
            pane.set_layout_heading(len(self.panes) > 1)

    def _on_focus(self, pane):
        self.active = pane
        for child in self.panes:
            style = child.get_style_context()
            style.add_class("active-pane") if child is pane else style.remove_class("active-pane")
        self.focus_callback(pane)

    def split(self, orientation=Gtk.Orientation.HORIZONTAL, profile=None, existing_pane=None, append=False):
        previous = self.active
        branch = self.get_children()[0] if append else previous
        parent = branch.get_parent()
        position = 0
        if isinstance(parent, Gtk.Paned):
            position = 1 if parent.get_child1() is branch else 2
        parent.remove(branch)
        paned = Gtk.Paned(orientation=orientation)
        if position == 1:
            parent.pack1(paned, True, False)
        elif position == 2:
            parent.pack2(paned, True, False)
        else:
            parent.pack_start(paned, True, True, 0)
        split_profile = dict(profile or previous.profile)
        # A recording path belongs to exactly one child; a split must never truncate it.
        split_profile.pop("_recording_path", None)
        self.font_size = previous.font_size
        if existing_pane is None:
            pane = self._new_pane(split_profile)
        else:
            pane = self._adopt_pane(existing_pane)
        paned.pack1(branch, True, False)
        paned.pack2(pane, True, False)
        self.show_all()
        if not append:
            self._position_split(paned, .5)
        pane.terminal.grab_focus()
        return pane

    def detach_pane(self, pane):
        """Remove one existing pane from the layout without closing its process."""
        if pane not in self.panes:
            raise ValueError("Панель не принадлежит этому рабочему пространству")
        parent = pane.get_parent()
        if len(self.panes) == 1:
            parent.remove(pane)
        else:
            sibling = parent.get_child2() if parent.get_child1() is pane else parent.get_child1()
            ancestor = parent.get_parent()
            first = isinstance(ancestor, Gtk.Paned) and ancestor.get_child1() is parent
            parent.remove(sibling)
            parent.remove(pane)
            ancestor.remove(parent)
            if isinstance(ancestor, Gtk.Paned):
                ancestor.pack1(sibling, True, False) if first else ancestor.pack2(sibling, True, False)
            else:
                ancestor.pack_start(sibling, True, True, 0)
            parent.destroy()
        self.panes.remove(pane)
        self._sync_headings()
        if self.active is pane:
            self.active = self.panes[-1] if self.panes else None
        return pane

    def close_pane(self, pane):
        if len(self.panes) == 1:
            if self.on_empty:
                self.on_empty(self)
            return
        if pane.pid is not None:
            from .dialogs import confirm
            if not confirm(self.get_toplevel(), "Закрыть панель и завершить её процесс?"):
                return
        parent = pane.get_parent()
        sibling = parent.get_child2() if parent.get_child1() is pane else parent.get_child1()
        ancestor = parent.get_parent()
        first = isinstance(ancestor, Gtk.Paned) and ancestor.get_child1() is parent
        parent.remove(sibling)
        ancestor.remove(parent)
        pane.close()
        self.panes.remove(pane)
        self._sync_headings()
        if isinstance(ancestor, Gtk.Paned):
            ancestor.pack1(sibling, True, False) if first else ancestor.pack2(sibling, True, False)
        else:
            ancestor.pack_start(sibling, True, True, 0)
        parent.destroy()
        self.active = self.panes[-1]
        self.show_all()
        self.active.terminal.grab_focus()

    def close(self):
        for pane in list(self.panes):
            pane.close()
        self.panes = []
