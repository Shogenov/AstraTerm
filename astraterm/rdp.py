"""FreeRDP X11 child surface for GTK 3.22. No password in argv, files or logs."""
import ctypes
import ctypes.util
import errno
import fcntl
import os
import pty
import shutil
import signal
import subprocess
import sys
import termios

from gi.repository import Gtk, Gdk, GLib


class XChildren(object):
    """Checked XCB requests cannot abort GTK when a remote child disappears mid-resize."""
    def __init__(self):
        class Cookie(ctypes.Structure):
            _fields_ = [('sequence', ctypes.c_uint)]
        self.lib = ctypes.CDLL(ctypes.util.find_library('xcb'))
        self.free = ctypes.CDLL(None).free
        self.free.argtypes = [ctypes.c_void_p]
        pointer, xid = ctypes.c_void_p, ctypes.c_uint32
        self.lib.xcb_connect.restype = pointer
        self.lib.xcb_connect.argtypes = [ctypes.c_char_p, pointer]
        self.display = self.lib.xcb_connect(None, None)
        if not self.display: raise ValueError('RDP требует графический сеанс X11.')
        self.lib.xcb_query_tree.argtypes = [pointer, xid]; self.lib.xcb_query_tree.restype = Cookie
        self.lib.xcb_query_tree_reply.argtypes = [pointer, Cookie, ctypes.POINTER(pointer)]; self.lib.xcb_query_tree_reply.restype = pointer
        self.lib.xcb_query_tree_children.argtypes = [pointer]; self.lib.xcb_query_tree_children.restype = ctypes.POINTER(xid)
        self.lib.xcb_query_tree_children_length.argtypes = [pointer]
        self.lib.xcb_configure_window_checked.argtypes = [pointer, xid, ctypes.c_uint16, ctypes.POINTER(xid)]
        self.lib.xcb_configure_window_checked.restype = Cookie
        self.lib.xcb_set_input_focus_checked.argtypes = [pointer, ctypes.c_uint8, xid, xid]
        self.lib.xcb_set_input_focus_checked.restype = Cookie
        self.lib.xcb_request_check.argtypes = [pointer, Cookie]; self.lib.xcb_request_check.restype = pointer
        self.lib.xcb_disconnect.argtypes = [pointer]

    def children(self, parent):
        error = ctypes.c_void_p()
        reply = self.lib.xcb_query_tree_reply(self.display, self.lib.xcb_query_tree(self.display, parent), ctypes.byref(error))
        if error: self.free(error)
        if not reply: return []
        children = self.lib.xcb_query_tree_children(reply)
        values = [children[index] for index in range(self.lib.xcb_query_tree_children_length(reply))]
        self.free(reply)
        return values

    def checked(self, cookie):
        error = self.lib.xcb_request_check(self.display, cookie)
        if error: self.free(error)

    def resize(self, child, width, height):
        values = (ctypes.c_uint32 * 2)(max(1, width), max(1, height))
        self.checked(self.lib.xcb_configure_window_checked(self.display, child, 12, values))

    def focus(self, child):
        self.checked(self.lib.xcb_set_input_focus_checked(self.display, 1, child, 0))

    def close(self):
        if self.display: self.lib.xcb_disconnect(self.display); self.display = None


class RdpPane(Gtk.Box):
    def __init__(self, profile, on_focus=None, on_status=None, on_close=None, on_connection=None, font_size=11, **unused):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self.profile = dict(profile)
        self.on_focus = on_focus or (lambda pane: None)
        self.on_status = on_status or (lambda message: None)
        self.on_close, self.on_connection = on_close, on_connection or (lambda pane: None)
        self.font_size, self.pid, self.process = font_size, None, None
        self.closed, self.exited, self.input_retired = False, False, False
        self.connection_generation, self.exit_code, self.child_xid = 0, None, None
        self.fd, self.watch, self.monitor, self.buffer, self.dialog = None, None, None, '', None
        self.x11 = XChildren()
        self.set_hexpand(True); self.set_vexpand(True)
        self.get_style_context().add_class('terminal-pane')
        self.heading = Gtk.EventBox(); self.heading.set_visible_window(False)
        bar = Gtk.Box(spacing=8); bar.set_border_width(4)
        self.heading.add(bar)
        self.title_label = Gtk.Label(label=profile.get('name', 'RDP'), xalign=0)
        bar.pack_start(self.title_label, True, True, 4)
        self.state_label = Gtk.Label(label='RDP')
        bar.pack_start(self.state_label, False, False, 4)
        from .ui_helpers import action_image
        close = Gtk.Button()
        close.add(action_image('close'))
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect('clicked', lambda *_: self.on_close(self) if self.on_close else None)
        bar.pack_end(close, False, False, 0)
        self.pack_start(self.heading, False, False, 0)
        self.heading.set_no_show_all(True)
        self.message = Gtk.Label(label='Подключение RDP…', wrap=True)
        self.pack_start(self.message, False, False, 4)
        self.message.set_no_show_all(True)
        self.message.show()
        self.terminal = Gtk.Socket()
        self.terminal.set_hexpand(True); self.terminal.set_vexpand(True); self.terminal.set_can_focus(True)
        self.pack_start(self.terminal, True, True, 0)
        self.terminal.connect('realize', lambda *_: GLib.idle_add(self.start))
        self.terminal.connect('focus-in-event', self._focus)
        self.terminal.connect('plug-removed', lambda *_: True)
        self.terminal.connect('size-allocate', lambda *_: self._resize())
        self.terminal.connect('map', lambda *_: self._resize())

    def _focus(self, *_):
        self.on_focus(self)
        if self.child_xid and self.terminal.get_mapped() and self.get_toplevel().is_active():
            self.x11.focus(self.child_xid)
        return False

    def _show_message(self, text):
        self.message.set_text(text)
        self.message.show()

    def start(self):
        if self.closed or self.process is not None: return False
        if self.profile.get('_credential_gate') and not self.profile['_credential_gate'](self.start): return False
        try:
            if self.profile.get('jump_host'):
                raise ValueError('Шлюзы RDP пока не поддерживаются.')
            executable = shutil.which('xfreerdp') or shutil.which('xfreerdp3')
            if not executable: raise ValueError('Установите freerdp2-x11 для встроенного RDP.')
            master, slave = pty.openpty()
            settings = termios.tcgetattr(slave); settings[3] &= ~termios.ECHO; termios.tcsetattr(slave, termios.TCSANOW, settings)
            self.fd = master
            fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)
            profile = self.profile
            host = '[' + profile['host'] + ']' if ':' in profile['host'] else profile['host']
            # Allocate the first remote desktop for the whole pane, including the
            # space the connecting message will release once the X11 child maps.
            width = self.terminal.get_allocated_width()
            height = self.get_allocated_height()
            resolution = profile['rdp_resolution']
            if self.terminal.get_mapped() and width >= 200 and height >= 200:
                resolution = '{}x{}'.format(min(8192, width - width % 2), min(8192, height))
            argv = [executable, '/v:{}:{}'.format(host, profile['port']), '/u:' + profile['username'],
                    '/d:' + profile.get('rdp_domain', ''), '/size:' + resolution,
                    '/parent-window:' + str(self.terminal.get_id()), '/dynamic-resolution', '+clipboard',
                    '+grab-keyboard', '/log-level:WARN']
            # A separate exec helper sets the controlling TTY, without Python preexec_fn in a threaded GTK process.
            try:
                self.process = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), 'rdp_launch.py'), '--'] + argv,
                                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            finally: os.close(slave)
            self.pid = self.process.pid; self.connection_generation += 1; self.exited = False
            self.buffer = ''; self.child_xid = None
            self._show_message('Подключение RDP…')
            self.on_connection(self)
            self.watch = GLib.io_add_watch(master, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read)
            self.monitor = GLib.timeout_add(200, self._poll)
        except Exception:
            self._stop(); self.exited = True
            self._show_message('Не удалось запустить встроенный RDP. Проверьте freerdp2-x11 и настройки подключения.')
        return False

    def _read(self, descriptor, condition):
        try:
            data = os.read(descriptor, 8192)
        except OSError as exc:
            if exc.errno == errno.EAGAIN: return True
            data = b''
        if not data: self.watch = None; return False
        self.buffer = (self.buffer + data.decode('utf-8', 'replace'))[-16384:]
        if 'Do you trust the above certificate?' in self.buffer:
            detail = self.buffer[-10000:]; self.buffer = ''
            # Once-only trust is an explicit UI choice. Changed certificates are never auto-accepted.
            self.dialog = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE,
                text='Проверка сертификата RDP · ' + self.profile['host'])
            self.dialog.format_secondary_text(detail)
            self.dialog.add_button('Отмена', Gtk.ResponseType.CANCEL)
            self.dialog.add_button('Доверять один раз', Gtk.ResponseType.OK)
            accepted = self.dialog.run() == Gtk.ResponseType.OK
            self.dialog.destroy(); self.dialog = None
            self._answer('T' if accepted else 'N')
        elif 'Username:' in self.buffer or 'Password:' in self.buffer:
            asking_username = 'Username:' in self.buffer
            self.buffer = ''
            password = None
            if not asking_username and self.profile.get('_credential_provider'):
                try: password = self.profile['_credential_provider']()
                except Exception: pass
            else:
                self.dialog = Gtk.Dialog(title=('Пользователь RDP · ' if asking_username else 'Пароль RDP · ') + self.profile['host'], transient_for=self.get_toplevel(), modal=True)
                entry = Gtk.Entry(); entry.set_visibility(asking_username); entry.set_activates_default(True)
                self.dialog.get_content_area().pack_start(entry, False, False, 12)
                self.dialog.add_button('Отмена', Gtk.ResponseType.CANCEL); self.dialog.add_button('Подключиться', Gtk.ResponseType.OK)
                self.dialog.set_default_response(Gtk.ResponseType.OK); self.dialog.show_all()
                if self.dialog.run() == Gtk.ResponseType.OK: password = entry.get_text()
                entry.set_text(''); self.dialog.destroy(); self.dialog = None
            if password is None:
                self._stop(); self.exited = True
                self._show_message('Подключение RDP отменено. Для новой попытки выберите «Переподключить».')
                self.state_label.set_text('RDP · отключено'); self.on_connection(self)
            else: self._answer(password)
            password = None
        return not self.closed and self.fd is not None

    def _answer(self, value):
        try:
            if not isinstance(value, str) or any(char in value for char in '\r\n\0') or len(value.encode('utf-8')) > 511:
                raise ValueError()
        except (ValueError, UnicodeError):
            self._stop(); self._show_message('RDP: используйте пароль без переводов строк, до 511 байт UTF-8.'); return
        if self.fd is not None:
            try: os.write(self.fd, (value + '\n').encode('utf-8'))
            except OSError: pass

    def _resize(self):
        if self.child_xid and self.terminal.get_mapped():
            allocation = self.terminal.get_allocation()
            self.x11.resize(self.child_xid, allocation.width, allocation.height)

    def _poll(self):
        if self.closed or self.process is None: self.monitor = None; return False
        status = self.process.poll()
        if status is not None:
            self.exit_code = status; self.exited = True; self.pid = None; self.process = None
            self.child_xid = None; self.connection_generation += 1; self.on_connection(self)
            self._show_message('RDP отключён (код {}). Переподключение — в меню.'.format(status))
            self.buffer = ''; self.monitor = None; self._close_io(); return False
        children = self.x11.children(self.terminal.get_id())
        if children and self.child_xid is None:
            self.child_xid = children[0]
            self.message.hide()
            self._resize()
            self.state_label.set_text('RDP · подключено')
            if self.terminal.has_focus():
                self._focus()
        return True

    def _close_io(self):
        if self.watch is not None: GLib.source_remove(self.watch); self.watch = None
        if self.fd is not None: os.close(self.fd); self.fd = None

    def _stop(self):
        process = self.process; self.process = None; self.pid = None; self.child_xid = None
        if self.dialog: self.dialog.response(Gtk.ResponseType.CANCEL)
        self._close_io()
        if self.monitor is not None: GLib.source_remove(self.monitor); self.monitor = None
        if process is not None and process.poll() is None:
            try: process.terminate()
            except ProcessLookupError: pass
            def reap():
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            import threading
            threading.Thread(target=reap, daemon=True).start()

    def restart(self,profile=None,ask=True):
        if profile is None and getattr(self,'on_reconnect',None):return self.on_reconnect(self)
        if profile is not None:self.profile=dict(profile);self.title_label.set_text(self.profile.get('name','RDP'))
        self.connection_generation += 1; self._stop(); self.on_connection(self); self.start()

    def close(self):
        if self.closed: return
        self.closed = True; self.connection_generation += 1; self._stop(); self.on_connection(self); self.x11.close(); self.destroy()

    def set_layout_heading(self, visible): self.heading.set_visible(visible)
    def show_multiexec_selection(self, *unused): pass
    def set_font_size(self, size): self.font_size = size
    def send(self, text): pass
    def send_bytes(self, data): pass
    def paste(self, *unused): self.on_status('Используйте буфер обмена рабочего стола RDP.')
    def show_search(self): self.on_status('Поиск доступен в терминальных сессиях.')
    def save_output(self): self.on_status('Сохранение текстового вывода доступно в терминальных сессиях.')
