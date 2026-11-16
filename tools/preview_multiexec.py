#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Temporary visual preview; deliberately excluded from the release allowlist.

Set ASTRA_PREVIEW_PNG to the screenshot destination. ASTRA_PREVIEW_EXIT=1 closes
the preview one second after capture; otherwise it remains available as a demo.
Only local shells and a temporary SessionStore are created. No input is sent.
"""
from __future__ import print_function, unicode_literals

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session


def main():
    destination = os.environ.get("ASTRA_PREVIEW_PNG")
    if not destination:
        raise SystemExit("ASTRA_PREVIEW_PNG is required")
    destination = os.path.abspath(os.path.expanduser(destination))
    if not Gtk.init_check()[0]:
        raise SystemExit("A graphical GTK session is required; set DISPLAY")
    with tempfile.TemporaryDirectory(prefix="astraterm-preview-") as directory:
        store = SessionStore(os.path.join(directory, "sessions.json"))
        window = MainWindow(store, start_local=False)
        panes = []
        failed = [False]
        for number in range(1, 4):
            profile = default_session("local")
            profile["name"] = "Терминал%d" % number
            workspace = window.open_session(profile)
            if workspace is None:
                window.shutdown()
                window.destroy()
                raise RuntimeError("Cannot create preview terminal")
            panes.append(workspace.active)
        window.present()

        def finish():
            window.shutdown()
            window.destroy()
            Gtk.main_quit()
            return False

        def capture():
            try:
                native = window.get_window()
                if native is None:
                    raise RuntimeError("Preview window is not realized")
                width, height = native.get_width(), native.get_height()
                pixbuf = Gdk.pixbuf_get_from_window(native, 0, 0, width, height)
                if pixbuf is None:
                    raise RuntimeError("Cannot capture preview window")
                parent = os.path.dirname(destination)
                if not os.path.isdir(parent):
                    os.makedirs(parent)
                pixbuf.savev(destination, "png", [], [])
                print("PREVIEW_PNG=%s (%dx%d)" % (destination, width, height), flush=True)
                if os.environ.get("ASTRA_PREVIEW_EXIT") == "1":
                    GLib.timeout_add(1000, finish)
            except Exception as exc:
                failed[0] = True
                print("Preview failed: " + str(exc), file=sys.stderr, flush=True)
                GLib.idle_add(finish)
            return False

        attempts = [0]

        def configure():
            attempts[0] += 1
            if any(pane.pid is None for pane in panes):
                if attempts[0] < 50 and not any(pane.exited for pane in panes):
                    return True
                failed[0] = True
                print("Preview local shells did not start", file=sys.stderr, flush=True)
                GLib.idle_add(finish)
                return False
            window.set_tiled_view(True)
            window.multiexec.set_selected(panes[0], True)
            window.multiexec.set_selected(panes[1], True)
            window.set_multiexec(True)
            window._pane_focus(panes[0])
            GLib.idle_add(lambda: panes[0].terminal.grab_focus())
            GLib.timeout_add(1500, capture)
            return False

        GLib.timeout_add(100, configure)
        try:
            Gtk.main()
        finally:
            window.shutdown()
            window.destroy()
        if failed[0]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
