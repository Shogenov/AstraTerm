"""Explicit, byte-preserving input routing without a dependency on GTK.

The source VTE already receives its input.  ``route`` only feeds the other
selected terminals; programmatic calls to a pane's send methods are never
intercepted.  Call the router only from the VTE input-commit signal.

Pane adapters expose ``closed``, ``pid``, ``profile`` and ``send_bytes(data)``.
An optional integer ``connection_generation`` changes on each spawn/exit.
Call ``connection_changed`` for lifecycle transitions even when a PID might be
reused.  A legacy ``send(text)`` adapter is supported with strict decoding using
the recipient's encoding.  Mixed encodings are transcoded strictly; equal
encodings preserve bytes as-is, including NUL and terminal control sequences.
No input payloads are retained or logged by the controller.
"""
from __future__ import unicode_literals

import codecs


TERMINAL_PROTOCOLS = frozenset(("local", "ssh", "telnet", "mosh", "serial"))


class MultiExecController(object):
    """Maintain an explicit recipient set and forward each input batch once.

    ``on_change(controller)`` refreshes the recipient UI.  Optional
    ``on_error(pane, exception)`` reports a failed delivery without stopping the
    remaining recipients.  Both callbacks are best-effort and must not retain
    input data.  Selection is independent of ``enabled`` so the mode can be
    paused and resumed, but new or reconnected panes must be selected again.

    The controller is intended for the GUI thread; its reentrancy guard handles
    a terminal adapter synchronously emitting input-commit while being fed.
    """

    def __init__(self, on_change=None, on_error=None):
        self.enabled = False
        self.on_change = on_change
        self.on_error = on_error
        # Identity keys avoid overloaded equality/hash on GUI objects.  Strong
        # references also prevent an unregistered id from accidentally matching.
        self._records = {}
        self._order = []
        self._selected = {}
        self._selection_serial = 0
        self._routing = False

    def _notify_change(self):
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:
                # A view callback must not interrupt a PTY input transaction.
                pass

    def _report_error(self, pane, exception):
        if self.on_error is not None:
            try:
                self.on_error(pane, exception)
            except Exception:
                pass

    def _record(self, pane):
        record = self._records.get(id(pane))
        return record if record is not None and record["pane"] is pane else None

    @staticmethod
    def eligible(pane):
        """Whether a pane is a live terminal, independent of registration."""
        if (getattr(pane, "closed", True) or getattr(pane, "exited", False) or
                getattr(pane, "input_retired", False)):
            return False
        pid = getattr(pane, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        profile = getattr(pane, "profile", None)
        protocol = profile.get("protocol") if isinstance(profile, dict) else None
        if not isinstance(protocol, str) or protocol not in TERMINAL_PROTOCOLS:
            return False
        return callable(getattr(pane, "send_bytes", None)) or callable(getattr(pane, "send", None))

    @staticmethod
    def _connection_token(record):
        pane = record["pane"]
        return (record["generation"], pane.pid, getattr(pane, "connection_generation", None))

    def _prune_selection(self, notify=True):
        changed = False
        for key, selection in list(self._selected.items()):
            record = self._records.get(key)
            if (record is None or not self.eligible(record["pane"]) or
                    selection[0] != self._connection_token(record)):
                del self._selected[key]
                changed = True
        if changed and notify:
            self._notify_change()
        return changed

    def register(self, pane):
        """Make a pane available for explicit selection; never auto-select it."""
        if self._record(pane) is not None:
            return False
        key = id(pane)
        self._records[key] = {"pane": pane, "generation": 0}
        self._order.append(key)
        self._notify_change()
        return True

    def unregister(self, pane):
        if self._record(pane) is None:
            return False
        key = id(pane)
        del self._records[key]
        self._order.remove(key)
        self._selected.pop(key, None)
        self._notify_change()
        return True

    def connection_changed(self, pane):
        """Invalidate selection on child exit, disconnect, restart or replacement."""
        record = self._record(pane)
        if record is None:
            return False
        record["generation"] += 1
        self._selected.pop(id(pane), None)
        self._notify_change()
        return True

    def set_selected(self, pane, selected):
        """Select only a registered live pane; return its resulting membership."""
        record = self._record(pane)
        if record is None:
            return False
        changed = self._prune_selection(notify=False)
        key = id(pane)
        if selected and self.eligible(pane):
            if key not in self._selected:
                self._selection_serial += 1
                self._selected[key] = (self._connection_token(record), self._selection_serial)
                changed = True
        elif key in self._selected:
            del self._selected[key]
            changed = True
        if changed:
            self._notify_change()
        return key in self._selected

    def select_all(self):
        """Explicitly include all currently registered live terminals."""
        changed = self._prune_selection(notify=False)
        for key in self._order:
            record = self._records[key]
            if key not in self._selected and self.eligible(record["pane"]):
                self._selection_serial += 1
                self._selected[key] = (self._connection_token(record), self._selection_serial)
                changed = True
        if changed:
            self._notify_change()
        return self.selected_panes()

    def clear_selection(self):
        if not self._selected:
            return False
        self._selected.clear()
        self._notify_change()
        return True

    def selected_panes(self):
        """Return current members in registration order; expire stale children."""
        self._prune_selection()
        return [self._records[key]["pane"] for key in self._order if key in self._selected]

    def is_selected(self, pane):
        self._prune_selection()
        return self._record(pane) is not None and id(pane) in self._selected

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self.enabled:
            self.enabled = enabled
            self._notify_change()
        return self.enabled

    def _selection_matches(self, pane, snapshot):
        record = self._record(pane)
        return (record is not None and self.eligible(pane) and
                self._selected.get(id(pane)) == snapshot and
                snapshot[0] == self._connection_token(record))

    @staticmethod
    def _encoded_for_target(source, target, payload):
        try:
            source_encoding = codecs.lookup(source.profile.get("encoding") or "UTF-8").name
            target_encoding = codecs.lookup(target.profile.get("encoding") or "UTF-8").name
        except (LookupError, TypeError):
            raise ValueError("MultiExec: неизвестная кодировка выбранного терминала") from None
        if source_encoding == target_encoding:
            return payload, target_encoding
        try:
            converted = payload.decode(source_encoding, "strict").encode(target_encoding, "strict")
        except UnicodeError:
            converted = None
        if converted is None:
            # UnicodeError's usual message/object can expose typed text.  The
            # callback only needs the incompatible codecs, never the payload.
            # Raise outside the except block so its context also has no payload.
            raise ValueError("MultiExec: ввод нельзя преобразовать из {} в {}".format(
                source_encoding, target_encoding))
        return converted, target_encoding

    def route(self, source, data):
        """Forward bytes to other members once; return successful deliveries.

        Bytes, bytearray and memoryview preserve control/NUL/non-UTF8 bytes for
        matching codecs.  Different codecs use strict decoding and encoding;
        incompatible recipients are skipped instead of corrupting their input.
        Newly selected or reconnected recipients never join an in-flight input
        batch.  Explicitly disabling stops the remaining sends.
        """
        if self._routing:
            return 0
        self._prune_selection()
        if not self.enabled or self._record(source) is None or id(source) not in self._selected:
            return 0
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("MultiExec routes bytes, not text or key names")
        payload = data if isinstance(data, bytes) else bytes(data)
        if not payload:
            return 0
        source_selection = self._selected[id(source)]
        recipients = [(self._records[key]["pane"], self._selected[key])
                      for key in self._order if key in self._selected and self._records[key]["pane"] is not source]
        delivered = 0
        self._routing = True
        try:
            for pane, selection in recipients:
                if not self.enabled or not self._selection_matches(source, source_selection):
                    break
                if not self._selection_matches(pane, selection):
                    continue
                try:
                    target_payload, target_encoding = self._encoded_for_target(source, pane, payload)
                    send_bytes = getattr(pane, "send_bytes", None)
                    if callable(send_bytes):
                        result = send_bytes(target_payload)
                    else:
                        try:
                            text = target_payload.decode(target_encoding, "strict")
                        except UnicodeError:
                            text = None
                        if text is None:
                            raise ValueError("MultiExec: текстовый терминал не принимает введённые байты")
                        result = pane.send(text)
                    if result is not False:
                        delivered += 1
                except Exception as exc:
                    self._report_error(pane, exc)
        finally:
            self._routing = False
            self._prune_selection()
        return delivered
