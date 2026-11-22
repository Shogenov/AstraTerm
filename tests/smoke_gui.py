"""Integration checks against a real Astra GTK/VTE desktop.

Run: DISPLAY=:0 /usr/bin/python3.5 tests/smoke_gui.py
Uses temporary config/files; does not alter saved connections or start remote sessions.
"""
from __future__ import print_function, unicode_literals

import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

from astraterm.core import SessionStore, default_session
from astraterm.app import MainWindow
from astraterm.dialogs import SessionDialog, MacroDialog
from astraterm.tunnels import TunnelDialog


def pump(seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.05)
        if predicate():
            return
    raise AssertionError("Timed out waiting for GUI condition")


def terminal_text(pane):
    return pane.terminal.get_text(lambda *_: True, None)[0]


def main():
    with tempfile.TemporaryDirectory(prefix="astraterm-gui-") as directory:
        store = SessionStore(os.path.join(directory, "sessions.json"))
        profile = default_session("ssh")
        profile.update(name="Smoke SSH", host="127.0.0.1", username="test", group="Проверка")
        store.upsert(profile)
        window = MainWindow(store, start_local=True)
        try:
            pane = window.active_pane()
            wait_for(lambda: pane.pid is not None)
            pane.send("printf '\\033[32mASTRA_UTF8_Проверка_OK\\033[0m\\n'\n")
            wait_for(lambda: "ASTRA_UTF8_Проверка_OK" in terminal_text(pane))
            pump(0.2)
            print("PASS local shell / UTF-8 / ANSI terminal")
            output = os.path.join(directory, "output.txt")
            pane.save_output(output)
            assert "ASTRA_UTF8_Проверка_OK" in open(output, encoding="utf-8").read()
            print("PASS save scrollback")
            pane.show_search()
            pane.search_entry.set_text("ASTRA_UTF8")
            pump()
            assert pane.search_bar.get_visible()
            pane.search_bar.hide()
            print("PASS terminal search")
            workspace = window.current_workspace()
            second = workspace.split(Gtk.Orientation.HORIZONTAL)
            wait_for(lambda: second.pid is not None)
            second.send("printf 'SPLIT_OK\\n'\n")
            wait_for(lambda: "SPLIT_OK" in terminal_text(second))
            third = workspace.split(Gtk.Orientation.VERTICAL)
            wait_for(lambda: third.pid is not None)
            pump(0.3)
            assert len(workspace.panes) == 3
            print("PASS horizontal / vertical splits")
            second.send("stty size\n")
            pump(0.2)
            assert second.terminal.get_column_count() > 10
            print("PASS PTY resize ({}x{})".format(second.terminal.get_column_count(), second.terminal.get_row_count()))
            session_dialog = SessionDialog(window, profile)
            assert session_dialog.get_session()["host"] == "127.0.0.1"
            session_dialog.destroy()
            local_dialog = SessionDialog(window, default_session("local"))
            assert local_dialog.get_session()["port"] == 0
            local_dialog.destroy()
            macros = MacroDialog(window, store, window.send_text)
            macros.destroy()
            tunnels = TunnelDialog(window, store, lambda *_: None, lambda *_: None, lambda *_: False)
            tunnels.destroy()
            print("PASS session / macros / tunnel dialogs")
            window.file_browser.open_local(directory)
            wait_for(lambda: not window.file_browser._busy)
            pump(0.2)
            assert window.file_browser._mode == "local"
            print("PASS asynchronous local file browser")
            edit_path = os.path.join(directory, "editor.txt")
            with open(edit_path, "w", encoding="utf-8") as stream:
                stream.write("initial")
            browser = window.file_browser
            browser._edit(edit_path)
            wait_for(lambda: len(browser._editors) == 1 and not browser._busy)
            editor = browser._editors[0]
            editor._astraterm_buffer.set_text("Изменено в AstraTerm\n")
            assert editor._astraterm_buffer.get_modified()
            editor.response(Gtk.ResponseType.APPLY)
            wait_for(lambda: not browser._busy and not editor._astraterm_buffer.get_modified())
            assert open(edit_path, encoding="utf-8").read() == "Изменено в AstraTerm\n"
            editor.response(Gtk.ResponseType.CLOSE)
            print("PASS real local editor / atomic save / modified tracking")
            third.send("exit\n")
            wait_for(lambda: third.exited)
            assert third.pid is None and third.exit_code == 0
            print("PASS child exit / lifecycle")
            workspace.close_pane(third)
            assert len(workspace.panes) == 2
            assert not second.closed
            print("PASS close split / retain sibling")
            third = workspace.split(Gtk.Orientation.VERTICAL)
            wait_for(lambda: third.pid is not None)
            third.send("exit\n")
            wait_for(lambda: third.exited)
            third.restart()
            wait_for(lambda: third.pid is not None)
            print("PASS restart ended terminal")
            assert third.exit_code is None
            assert "ЗАВЕРШЕНО" not in third.state_label.get_text()
            recording = default_session("local")
            recording["name"] = "Проверка записи"
            recording["_recording_path"] = os.path.join(directory, "recording.log")
            recording_workspace = window.open_session(recording)
            recorded = recording_workspace.active
            wait_for(lambda: recorded.pid is not None)
            recorded.send("printf 'REC_FIRST_TOKEN\\n'; exit\n")
            wait_for(lambda: recorded.exited)
            assert "REC_FIRST_TOKEN" in open(recording["_recording_path"], encoding="utf-8").read()
            recorded.restart()
            wait_for(lambda: recorded.pid is not None)
            recorded.send("printf 'REC_SECOND_TOKEN\\n'; exit\n")
            wait_for(lambda: recorded.exited)
            content = open(recording["_recording_path"], encoding="utf-8").read()
            assert "REC_FIRST_TOKEN" in content and "REC_SECOND_TOKEN" in content
            assert os.stat(recording["_recording_path"]).st_mode & 0o777 == 0o600
            recorded.restart()
            wait_for(lambda: recorded.pid is not None)
            recorded.send("exit 7\n")
            wait_for(lambda: recorded.exited)
            assert recorded.exit_code == 7
            print("PASS continuous recording / reconnect append / private mode")
            for index in range(5):
                fast = default_session("local")
                fast["name"] = "Fast output test"
                fast["_argv"] = ["/bin/sh", "-c", "printf 'FAST_CHILD_{}\\n'".format(index)]
                fast_workspace = window.open_session(fast)
                fast_pane = fast_workspace.active
                wait_for(lambda: fast_pane.exited)
                pump(0.15)  # VTE.feed() schedules parsing on its idle source.
                fast_output = os.path.join(directory, "fast-{}.txt".format(index))
                fast_pane.save_output(fast_output)
                content = open(fast_output, encoding="utf-8").read()
                assert "FAST_CHILD_{}".format(index) in content, repr(content)
                assert fast_pane._drain_fd is None
                window.close_tab(workspace=fast_workspace)
            print("PASS fast-child final PTY output / descriptor cleanup")
            if os.environ.get("ASTRA_SCREENSHOT"):
                window.notebook.set_current_page(0)
                pump(0.3)
                width, height = window.get_size()
                image = Gdk.pixbuf_get_from_window(window.get_window(), 0, 0, width, height)
                image.savev(os.environ["ASTRA_SCREENSHOT"], "png", [], [])
            pump(0.2)
            print("GUI_SMOKE_OK")
        finally:
            window.shutdown()
            window.destroy()
            pump(0.2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
