#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Real GTK/VTE MultiExec checks; run in the Astra user's desktop session.

DISPLAY=:0 /usr/bin/python3.5 tests/smoke_multiexec.py

Only local capture children and temporary files are used. Children record raw
input bytes; they never interpret input as shell commands. Positive routing
checks use actual GDK key events and native clipboard actions, not feed_child.
"""
from __future__ import print_function, unicode_literals

import os
import re
import signal
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session


CAPTURE_CHILD = r'''
import binascii
import os
import sys
import tty

path, label, bracketed = sys.argv[1:]
tty.setraw(0)
descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
os.write(1, ("CAPTURE_READY_" + label + "\r\n").encode("ascii"))
os.write(1, b"\x1b[?2004h" if bracketed == "yes" else b"\x1b[?2004l")
with open(path + ".ready", "w") as ready:
    ready.write(str(os.getpid()))
try:
    while True:
        data = os.read(0, 4096)
        if not data:
            break
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.write(1, b"\r\nCAPTURE_HEX_" + binascii.hexlify(data) + b"\r\n")
        if b"\x1e" in data:
            # An actual terminal-generated DSR answer must stay local.
            os.write(1, b"\x1b[6n")
        if b"\x1f" in data:
            break
finally:
    os.close(descriptor)
'''


def pump(seconds=0.1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.005)


def wait_for(predicate, label, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump(0.03)
        if predicate():
            return
    raise AssertionError("Timed out: " + label)


def read_bytes(path):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except FileNotFoundError:
        return b""


def ready(pane, path):
    if pane.pid is None or pane.exited:
        return False
    try:
        with open(path + ".ready", "r") as stream:
            return stream.read() == str(pane.pid)
    except (OSError, ValueError):
        return False


def key_event(widget, keyval, state=0):
    """Build a real key event with keymap/device metadata for GTK 3.22."""
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.window = widget.get_window()
    event.send_event = True
    event.time = Gdk.CURRENT_TIME
    event.keyval = keyval
    event.hardware_keycode = 0
    event.group = 0
    keymap = Gdk.Keymap.get_default()
    found, entries = keymap.get_entries_for_keyval(keyval)
    if found and entries:
        entry = entries[0]
        event.hardware_keycode = entry.keycode
        event.group = entry.group
        if entry.level == 1:
            state |= Gdk.ModifierType.SHIFT_MASK
    event.state = state
    event.is_modifier = False
    display = Gdk.Display.get_default()
    seat = display.get_default_seat()
    if seat is not None and seat.get_keyboard() is not None:
        event.set_device(seat.get_keyboard())
    return event


def press(widget, keyval, state=0):
    widget.event(key_event(widget, keyval, state))
    pump(0.02)


def focus(window, pane):
    window.present()
    pane.terminal.grab_focus()
    pump(0.08)
    assert pane.terminal.get_mapped(), "Input source is not visible"
    assert window.get_focus() is pane.terminal, "Input source did not acquire GTK focus"


def workspace_for(window, pane):
    for index in range(window.notebook.get_n_pages()):
        workspace = window.notebook.get_nth_page(index)
        if pane in workspace.panes:
            return workspace
    raise AssertionError("Pane has no owning workspace")


def layout_shape(widget, panes):
    for index, pane in enumerate(panes):
        if widget is pane:
            return ("terminal", index)
    if isinstance(widget, Gtk.Paned):
        return ("paned", int(widget.get_orientation()),
                layout_shape(widget.get_child1(), panes),
                layout_shape(widget.get_child2(), panes))
    if isinstance(widget, Gtk.Box):
        return ("box", tuple(layout_shape(child, panes) for child in widget.get_children()))
    return (type(widget).__name__,)


def main():
    with tempfile.TemporaryDirectory(prefix="astraterm-multiexec-") as directory:
        child_path = os.path.join(directory, "capture.py")
        with open(child_path, "w", encoding="utf-8") as stream:
            stream.write(CAPTURE_CHILD)
        store = SessionStore(os.path.join(directory, "sessions.json"))
        window = MainWindow(store, start_local=False)
        records = []

        def make_profile(label, encoding="UTF-8", bracketed=True):
            path = os.path.join(directory, label + ".input")
            profile = default_session("local")
            profile.update(name=label, encoding=encoding)
            profile["_argv"] = [sys.executable, child_path, path, label,
                                "yes" if bracketed else "no"]
            return profile, path

        def add_capture(label, encoding="UTF-8", bracketed=True, split=False):
            profile, path = make_profile(label, encoding, bracketed)
            if split:
                pane = window.split_with_profile(profile, Gtk.Orientation.HORIZONTAL)
                assert pane is not None and hasattr(pane, "terminal"), "Cannot split capture child"
            else:
                workspace = window.open_session(profile)
                assert workspace is not None, "Cannot open raw capture child"
                pane = workspace.active
            records.append({"pane": pane, "path": path, "label": label})
            try:
                wait_for(lambda: ready(pane, path), label + " capture readiness")
            except AssertionError:
                status = window.status.get_text()
                history_path = os.path.join(directory, label + ".failure-history")
                pane.save_output(history_path)
                history = read_bytes(history_path).decode("utf-8", "replace")
                raise AssertionError(
                    "{} readiness: requested_argv={!r}, pane_argv={!r}, pid={!r}, "
                    "closed={!r}, exited={!r}, exit_code={!r}, state={!r}, status={!r}, "
                    "ready_file={!r}, input={!r}, mapped={!r}, realized={!r}, history={!r}".format(
                        label, profile.get("_argv"), pane.profile.get("_argv"), pane.pid,
                        pane.closed, pane.exited, pane.exit_code, pane.state_label.get_text(),
                        status, read_bytes(path + ".ready"), read_bytes(path),
                        pane.terminal.get_mapped(), pane.terminal.get_realized(), history))
            pump(0.15)
            return pane

        def snapshot():
            return {id(item["pane"]): len(read_bytes(item["path"])) for item in records}

        def assert_delta(before, expected, label):
            expected = {id(pane): payload for pane, payload in expected}
            def describe():
                focus_widget = window.get_focus()
                focused = [item["label"] for item in records
                           if item["pane"].terminal is focus_widget]
                selected = [item["label"] for item in records
                            if window.multiexec.is_selected(item["pane"])]
                result = ["focus={!r}, selected={!r}, enabled={!r}".format(
                    focused or type(focus_widget).__name__, selected, window.multiexec.enabled)]
                for item in records:
                    pane_id = id(item["pane"])
                    actual = read_bytes(item["path"])[before[pane_id]:]
                    result.append("{} expected {!r}, got {!r}".format(
                        item["label"], expected.get(pane_id, b""), actual))
                return "; ".join(result)
            def complete():
                return all(len(read_bytes(item["path"])) - before[id(item["pane"])] >=
                           len(expected.get(id(item["pane"]), b"")) for item in records)
            try:
                wait_for(complete, label)
            except AssertionError:
                raise AssertionError("{}: {}".format(label, describe()))
            # Catch duplicated or delayed fanout after all positive recipients.
            pump(0.18)
            for item in records:
                actual = read_bytes(item["path"])[before[id(item["pane"])]:]
                wanted = expected.get(id(item["pane"]), b"")
                assert actual == wanted, "{}: {}".format(label, describe())

        def select(*panes):
            window.clear_all_targets()
            for pane in panes:
                pane.multiexec_check.set_active(True)
            pump()
            assert set(map(id, window.multiexec.selected_panes())) == set(map(id, panes))

        try:
            a = add_capture("HostA", bracketed=True)
            b = add_capture("HostB", bracketed=False)
            c = add_capture("HostC", encoding="CP1251", bracketed=True)
            original_workspaces = [workspace_for(window, pane) for pane in (a, b, c)]
            assert len(set(map(id, original_workspaces))) == 3
            assert not window.multiexec.enabled
            assert window.multiexec.selected_panes() == []
            assert len(window.all_panes()) == 3
            window.set_tiled_view(True)
            pump(0.3)
            assert window.tiled.active
            assert all(pane.terminal.get_mapped() for pane in (a, b, c))
            assert all(pane.multiexec_check.get_visible() for pane in (a, b, c))
            window.set_multiexec(True)
            select(a, b)
            focus(window, a)

            before = snapshot()
            for character in "abЖ":
                press(a.terminal, Gdk.unicode_to_keyval(ord(character)))
            for keyval, state in ((Gdk.KEY_Return, 0), (Gdk.KEY_BackSpace, 0),
                                  (Gdk.KEY_Tab, 0),
                                  (Gdk.KEY_c, Gdk.ModifierType.CONTROL_MASK),
                                  (Gdk.KEY_space, Gdk.ModifierType.CONTROL_MASK)):
                press(a.terminal, keyval, state)
            typed = "abЖ".encode("utf-8") + b"\r\x7f\t\x03\x00"
            assert_delta(before, [(a, typed), (b, typed)], "selected pair keyboard exactly once")
            print("PASS live ASCII / Cyrillic / Enter / Backspace / Tab / Ctrl-C / NUL, excluded third", flush=True)

            before = snapshot()
            for keyval in (Gdk.KEY_Up, Gdk.KEY_Left, Gdk.KEY_Delete, Gdk.KEY_Home, Gdk.KEY_End):
                press(a.terminal, keyval)
            pump(0.15)
            navigation = read_bytes(records[0]["path"])[before[id(a)]:]
            assert navigation and navigation.startswith(b"\x1b"), "Navigation produced no terminal bytes"
            assert_delta(before, [(a, navigation), (b, navigation)], "navigation exactly once")
            print("PASS navigation keys preserved without duplicate source input", flush=True)

            select(a, c)
            focus(window, a)
            before = snapshot()
            press(a.terminal, Gdk.unicode_to_keyval(ord("Ж")))
            assert_delta(before, [(a, "Ж".encode("utf-8")), (c, "Ж".encode("cp1251"))],
                         "UTF-8 to CP1251")
            focus(window, c)
            before = snapshot()
            press(c.terminal, Gdk.unicode_to_keyval(ord("П")))
            assert_delta(before, [(a, "П".encode("utf-8")), (c, "П".encode("cp1251"))],
                         "CP1251 to UTF-8 raw commit bridge")
            print("PASS live mixed UTF-8 / CP1251 in both directions", flush=True)

            select(a, b)
            focus(window, c)
            before = snapshot()
            press(c.terminal, Gdk.KEY_x)
            assert_delta(before, [(c, b"x")], "unchecked source stays local")
            window.clear_all_targets()
            focus(window, a)
            before = snapshot()
            press(a.terminal, Gdk.KEY_y)
            assert_delta(before, [(a, b"y")], "clear all stays local")
            assert not any(pane.multiexec_check.get_active() for pane in (a, b, c))
            window.select_all_targets()
            assert set(map(id, window.multiexec.selected_panes())) == set(map(id, (a, b, c)))
            focus(window, a)
            before = snapshot()
            press(a.terminal, Gdk.KEY_s)
            assert_delta(before, [(a, b"s"), (b, b"s"), (c, b"s")],
                         "select all sends to three terminals exactly once")
            select(a, b)
            window.set_multiexec(False)
            focus(window, a)
            before = snapshot()
            press(a.terminal, Gdk.KEY_z)
            assert_delta(before, [(a, b"z")], "disabled mode stays local")
            window.set_multiexec(True)
            print("PASS positive checkboxes / select all / clear all / unchecked source / disabled mode", flush=True)

            focus(window, a)
            before = snapshot()
            a.send("PROGRAMMATIC_ONLY")
            assert_delta(before, [(a, b"PROGRAMMATIC_ONLY")], "programmatic send stays local")
            before = snapshot()
            a.send_bytes(b"\x1e")
            wait_for(lambda: re.search(br"\x1b\[[0-9]+;[0-9]+R",
                                      read_bytes(records[0]["path"])[before[id(a)]:]) is not None,
                     "terminal DSR answer")
            response = read_bytes(records[0]["path"])[before[id(a)]:]
            assert response.startswith(b"\x1e")
            assert_delta(before, [(a, response)], "terminal generated answer stays local")
            print("PASS programmatic input and real terminal DSR responses are not broadcast", flush=True)

            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text("PASTE_Ж\n", -1)
            pump()
            before = snapshot()
            press(a.terminal, Gdk.KEY_Insert,
                  Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
            paste = "PASTE_Ж".encode("utf-8") + b"\r"
            assert_delta(before, [(a, b"\x1b[200~" + paste + b"\x1b[201~"), (b, paste)],
                         "clipboard per-recipient bracketed paste")
            primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
            primary.set_text("PRIMARY", -1)
            pump()
            before = snapshot()
            press(a.terminal, Gdk.KEY_Insert, Gdk.ModifierType.SHIFT_MASK)
            assert_delta(before, [(a, b"\x1b[200~PRIMARY\x1b[201~"), (b, b"PRIMARY")],
                         "primary selection paste")
            print("PASS clipboard / PRIMARY selected recipients with independent bracketed modes", flush=True)

            before = snapshot()
            window.session_search.set_text("")
            window.session_search.grab_focus()
            pump()
            press(window.session_search, Gdk.KEY_q)
            assert window.session_search.get_text() == "q", "Search event did not insert local text"
            assert_delta(before, [], "search entry typing is not terminal input")
            window.session_search.set_text("")
            print("PASS search-entry focus does not fan out text", flush=True)

            window.set_multiexec(False)
            window.set_tiled_view(False)
            pump(0.25)
            original = {id(pane): (pane.pid, pane._drain_fd) for pane in (a, b, c)}
            workspace = workspace_for(window, a)
            window.move_pane_to_workspace(b, workspace, Gtk.Orientation.HORIZONTAL)
            window.move_pane_to_workspace(c, workspace, Gtk.Orientation.VERTICAL)
            pump(0.25)
            assert set(map(id, workspace.panes)) == set(map(id, (a, b, c)))
            assert all((pane.pid, pane._drain_fd) == original[id(pane)] for pane in (a, b, c))
            print("PASS moving existing panes into nested splits retains independent child processes", flush=True)

            focus(window, c)
            shape = layout_shape(workspace, (a, b, c))
            parents = {id(pane): pane.get_parent() for pane in (a, b, c)}
            before = snapshot()
            for cycle in range(3):
                window.set_tiled_view(True)
                pump(0.2)
                assert all(pane.terminal.get_mapped() for pane in (a, b, c))
                for pane in (a, b, c):
                    assert (pane.pid, pane._drain_fd) == original[id(pane)]
                    os.fstat(pane._drain_fd)
                window.set_tiled_view(False)
                pump(0.2)
                assert layout_shape(workspace, (a, b, c)) == shape, "Split topology changed"
                assert all(pane.get_parent() is parents[id(pane)] for pane in (a, b, c))
            assert_delta(before, [], "tiling does not send terminal input")
            assert window.get_focus() is c.terminal, "Focus was not restored after tiling"
            for index, pane in enumerate((a, b, c)):
                path = os.path.join(directory, "history-%d.txt" % index)
                pane.save_output(path)
                assert "CAPTURE_READY_" in read_bytes(path).decode("utf-8"), "Scrollback lost on reparent"
            print("PASS repeated tiles preserve PID / descriptors / scrollback / parent topology / focus", flush=True)

            window.set_multiexec(True)
            window.select_all_targets()
            d = add_capture("HostD", split=True)
            assert not window.multiexec.is_selected(d), "New split silently joined the group"
            assert len(window.all_panes()) == 4
            print("PASS new profile split stays excluded from an existing recipient selection", flush=True)

            window.multiexec.set_selected(c, True)
            c.send_bytes(b"\x1f")
            wait_for(lambda: c.exited and c.pid is None, "selected child exit")
            assert not window.multiexec.is_selected(c), "Exited child remains selected"
            assert c._drain_fd is None, "Exited child retained its duplicate PTY descriptor"
            path_c = records[2]["path"]
            os.unlink(path_c + ".ready")
            c.restart()
            wait_for(lambda: ready(c, path_c), "capture restart")
            assert not window.multiexec.is_selected(c), "Restarted child silently rejoined the group"
            print("PASS child exit / restart invalidates selection and releases duplicate master", flush=True)

            # VTE filters ESC from an unbracketed clipboard paste and emits no
            # commit. This is a real native request whose completion cannot be
            # inferred from commit, just like a delayed/empty external owner.
            clipboard.set_text("\x1b", -1)
            pump()
            window._paste_to_target(b)
            wait_for(lambda: b.terminal.native_paste_pending, "native paste without a commit")
            old_terminal, old_pid = b.terminal, b.pid
            original_ids = set(map(id, window.all_panes()))
            b.restart()
            wait_for(lambda: len([pane for pane in window.all_panes() if id(pane) not in original_ids]) == 1,
                     "isolated replacement pane")
            replacement = next(pane for pane in window.all_panes() if id(pane) not in original_ids)
            path_b = records[1]["path"]
            wait_for(lambda: ready(replacement, path_b), "replacement capture readiness")
            assert b.input_retired and b.terminal is old_terminal
            assert b.pid == old_pid and not b.exited, "Fallback terminated the old process"
            assert replacement.terminal is not old_terminal, "Replacement reused a pending VTE widget"
            assert not old_terminal.get_input_enabled(), "Retired widget still accepts native input"
            assert not window.multiexec.eligible(b)
            assert not window.multiexec.is_selected(b)
            assert not window.multiexec.is_selected(replacement), "Replacement silently joined MultiExec"
            before_history_input = read_bytes(path_b)
            b.send("MUST_NOT_REACH_HISTORY")
            b.send_bytes(b"MUST_NOT_REACH_HISTORY")
            old_terminal.send_bytes(b"LATE_NATIVE_INPUT")
            pump(0.15)
            assert read_bytes(path_b) == before_history_input, "Input leaked through the retired VTE"
            history = os.path.join(directory, "retired-history.txt")
            b.save_output(history)
            assert "CAPTURE_READY_HostB" in read_bytes(history).decode("utf-8"), "Fallback lost scrollback"
            os.kill(old_pid, signal.SIGTERM)
            wait_for(lambda: b.exited and b.pid is None, "retired child exit")
            b.start()
            assert b.pid is None and b.terminal is old_terminal, "History widget acquired a new PTY"
            window.close_pane(b)
            assert b.closed, "Retired history pane cannot be closed"
            print("PASS filtered native paste / isolated replacement / exact history / retired input guards", flush=True)
            print("MULTIEXEC_SMOKE_OK", flush=True)
        finally:
            window.set_multiexec(False)
            window.set_tiled_view(False)
            window.shutdown()
            window.destroy()
            pump(0.25)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
