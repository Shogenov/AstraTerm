"""User-input boundaries and byte-safe commit handling for VTE 0.46.

The GObject commit signal carries terminal-encoded bytes in a G_TYPE_STRING.
PyGObject would decode that string as UTF-8, so the native signal is required
for legacy charsets. Only actual keyboard input is routed; terminal replies,
mouse reports and programmatic feed_child calls are never broadcast.
"""
import ctypes
import ctypes.util

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gtk, Vte


_CommitCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_uint, ctypes.c_void_p)
_gobject = ctypes.CDLL(ctypes.util.find_library("gobject-2.0"))
_gobject.g_signal_connect_data.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         _CommitCallback, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_int]
_gobject.g_signal_connect_data.restype = ctypes.c_ulong
_gobject.g_signal_handler_disconnect.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_gobject.g_signal_handler_disconnect.restype = None
_capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
_capsule_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
_capsule_pointer.restype = ctypes.c_void_p


class InputTerminal(Vte.Terminal):
    """Route keyboard commits, while each recipient retains native paste modes."""
    __gtype_name__ = "AstraTermInputTerminal"

    def __init__(self):
        Vte.Terminal.__init__(self)
        self.on_user_input = None
        self.on_user_paste = None
        self.on_input_error = None
        self._user_input_depth = 0
        self._programmatic_depth = 0
        self._input_disconnected = False
        self._paste_serial = 0
        self._pending_pastes = {}
        self._active_native_paste = None
        self._selection_anchor = None
        self._native_pointer = _capsule_pointer(self.__gpointer__, None)
        self._commit_callback = _CommitCallback(self._native_commit)
        self._commit_handler = _gobject.g_signal_connect_data(
            self._native_pointer, b"commit", self._commit_callback, None, None, 0)
        if not self._commit_handler:
            raise RuntimeError("Не удалось подключить обработчик ввода VTE")

    def _native_commit(self, instance, pointer, size, user_data):
        try:
            if self._pending_pastes:
                token = self._active_native_paste
                if token is None and not self._programmatic_depth and not self._user_input_depth:
                    event = Gtk.get_current_event()
                    if event is not None and event.type in (Gdk.EventType.SELECTION_NOTIFY, Gdk.EventType.PROPERTY_NOTIFY):
                        token = next((key for key, phase in self._pending_pastes.items() if phase == "native"), None)
                if token is not None:
                    self._pending_pastes.pop(token, None)
            if (self._input_disconnected or self._programmatic_depth or
                    not self._user_input_depth or not self.on_user_input or not size):
                return
            # G_TYPE_STRING can be copied by GLib up to its first NUL. Never
            # read beyond that allocation using the separate original length.
            prefix = ctypes.string_at(pointer) if pointer else b""
            if len(prefix) == size:
                data = prefix
            elif size == 1 and not prefix:
                data = b"\x00"  # Ctrl+@ / Ctrl+Space.
            else:
                raise ValueError("Неподдерживаемый формат терминального ввода")
            self.on_user_input(data)
        except BaseException:
            # Never include typed content or native callback exceptions in logs.
            self._report_input_error()

    def _report_input_error(self):
        if self.on_input_error:
            try:
                self.on_input_error()
            except BaseException:
                pass

    def do_key_press_event(self, event):
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if shift and event.keyval in (Gdk.KEY_Insert, Gdk.KEY_KP_Insert):
            self.request_user_paste(primary=not control)
            return True
        self._user_input_depth += 1
        try:
            return Vte.Terminal.do_key_press_event(self, event)
        finally:
            self._user_input_depth -= 1

    def do_button_press_event(self, event):
        if event.button == 2:
            if event.type == Gdk.EventType.BUTTON_PRESS:
                self._select_between_clicks(event)
            return True
        if event.button == 1:
            self._selection_anchor = None
        return Vte.Terminal.do_button_press_event(self, event)

    def do_button_release_event(self, event):
        return event.button == 2 or Vte.Terminal.do_button_release_event(self, event)

    def _select_between_clicks(self, event):
        # VTE 0.46 exposes native selection through mouse events. Complete a
        # single native drag at the second click, without holding PTY reads.
        adjustment = self.get_vadjustment()
        viewport = adjustment.get_value()
        size = (self.get_row_count(), self.get_column_count(), self.get_char_height(), self.get_char_width())
        anchor = self._selection_anchor
        self._selection_anchor = None
        self.grab_focus()
        if anchor is None or anchor[3] != size or anchor[2] < adjustment.get_lower():
            self.unselect_all()
            self._selection_anchor = (event.x, event.y, viewport, size)
            return
        x, y, original_viewport, _ = anchor
        if x == event.x and y == event.y and original_viewport == viewport:
            return

        press = event.copy()
        press.button.button = 1
        press.button.state = Gdk.ModifierType.SHIFT_MASK
        press.button.x, press.button.y = x, y
        motion = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
        motion.motion.window = event.window
        motion.motion.time = event.time
        motion.motion.state = Gdk.ModifierType.BUTTON1_MASK | Gdk.ModifierType.SHIFT_MASK
        motion.set_device(event.get_device())
        release = Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE)
        release.button.window = event.window
        release.button.time = event.time
        release.button.x, release.button.y = event.x, event.y
        release.button.button = 1
        release.button.state = motion.motion.state
        release.set_device(event.get_device())
        adjustment.set_value(original_viewport)
        Vte.Terminal.do_button_press_event(self, press.button)
        try:
            # Pass GTK's drag threshold even for two adjacent cells.
            motion.motion.x, motion.motion.y = x + 16, y
            Vte.Terminal.do_motion_notify_event(self, motion.motion)
            adjustment.set_value(viewport)
            motion.motion.x, motion.motion.y = event.x, event.y
            Vte.Terminal.do_motion_notify_event(self, motion.motion)
        finally:
            Vte.Terminal.do_button_release_event(self, release.button)

    def do_paste_clipboard(self):
        if self._programmatic_depth:
            Vte.Terminal.do_paste_clipboard(self)
        else:
            self.request_user_paste(primary=False)

    def request_user_paste(self, primary=False):
        if self.on_user_paste:
            self.on_user_paste(primary)
        else:
            self.native_paste(primary)

    @property
    def paste_pending(self):
        return bool(self._pending_pastes)

    @property
    def native_paste_pending(self):
        # Completion cannot always be observed: a native paste can produce no
        # commit (filtered controls, failed conversion, empty external owner).
        # Treat such a request conservatively; never reuse this widget's PTY.
        return any(phase == "native" for phase in self._pending_pastes.values())

    def cancel_pending_fetches(self):
        """Invalidate our own callbacks before they initiate a VTE request."""
        changed = False
        for token, phase in list(self._pending_pastes.items()):
            if phase == "fetch":
                self._pending_pastes.pop(token, None)
                changed = True
        return changed

    def _clipboard_for(self, primary):
        return Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY if primary else Gdk.SELECTION_CLIPBOARD)

    def native_paste(self, primary=False, guard=None):
        # Keep the current PTY generation stable while an external clipboard
        # owner responds; never paste its delayed answer into a new connection.
        if self._input_disconnected:
            return False
        # A newer preflight safely supersedes older Python-owned callbacks.
        # Keep an existing native marker until VTE actually replaces its request.
        self.cancel_pending_fetches()
        self._paste_serial += 1
        token = self._paste_serial
        self._pending_pastes[token] = "fetch"

        def received(clipboard, text):
            try:
                if (self._input_disconnected or token not in self._pending_pastes or
                        not text or (guard is not None and not guard())):
                    self._pending_pastes.pop(token, None)
                    return
                # VTE has one native request slot; its next paste cancels the
                # previous request. Do not retain canceled native tokens forever.
                for key, phase in list(self._pending_pastes.items()):
                    if phase == "native":
                        self._pending_pastes.pop(key, None)
                self._pending_pastes[token] = "native"
                previous = self._active_native_paste
                self._active_native_paste = token
                self._programmatic_depth += 1
                try:
                    if primary:
                        Vte.Terminal.paste_primary(self)
                    else:
                        Vte.Terminal.do_paste_clipboard(self)
                finally:
                    self._programmatic_depth -= 1
                    self._active_native_paste = previous
            except BaseException:
                # Once the native call begins, keep its marker even if the call
                # raises: there may still be a live VTE-owned asynchronous request.
                if self._pending_pastes.get(token) == "fetch":
                    self._pending_pastes.pop(token, None)
                self._report_input_error()
        try:
            self._clipboard_for(primary).request_text(received)
        except Exception:
            if self._pending_pastes.get(token) == "fetch":
                self._pending_pastes.pop(token, None)
            self._report_input_error()
            return False
        return True

    def send_text(self, text):
        self._programmatic_depth += 1
        try:
            self.feed_child(text, -1)
        finally:
            self._programmatic_depth -= 1

    def send_bytes(self, data):
        self._programmatic_depth += 1
        try:
            self.feed_child_binary(list(data))
        finally:
            self._programmatic_depth -= 1

    def disconnect_input(self):
        if self._input_disconnected:
            return
        self._input_disconnected = True
        self._pending_pastes.clear()
        self.on_user_input = None
        self.on_user_paste = None
        self.on_input_error = None
        if self._commit_handler:
            _gobject.g_signal_handler_disconnect(self._native_pointer, self._commit_handler)
            self._commit_handler = None
