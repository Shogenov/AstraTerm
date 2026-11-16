#!/usr/bin/python3.5
"""Notebook header wheel checks against real GTK; no terminals or network.

Run in the user's desktop: DISPLAY=:0 /usr/bin/python3.5 tests/smoke_ui_helpers.py
"""
from __future__ import print_function, unicode_literals

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk

from astraterm.ui_helpers import (TabStrip, protocol_icon, group_icon,
                                 rename_image, _widget_root_origin)


def pump(seconds=0.08):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.005)


def wheel(strip, direction, dx=0.0, dy=0.0):
    x, y = strip.get_allocated_width() // 2, strip.get_allocated_height() // 2
    origin = _widget_root_origin(strip)
    event = Gdk.Event.new(Gdk.EventType.SCROLL)
    # PyGObject 3.22's Gdk.Event convenience forwarding omits SCROLL. Writing
    # event.direction would only create a Python attribute; GTK would still
    # receive the native zero-filled UP event. Populate the actual union member.
    scroll = event.scroll
    scroll.window = strip.get_window()
    window_root = scroll.window.get_root_coords(0, 0)
    scroll.x_root, scroll.y_root = origin[0] + x, origin[1] + y
    scroll.x, scroll.y = scroll.x_root - window_root[0], scroll.y_root - window_root[1]
    scroll.direction = direction
    scroll.delta_x, scroll.delta_y = dx, dy
    scroll.time = Gdk.CURRENT_TIME
    scroll.send_event = True
    if direction == Gdk.ScrollDirection.SMOOTH:
        native = event.get_scroll_deltas()
        assert native[0] and native[-2:] == (dx, dy), "Synthetic native smooth deltas were not set"
    else:
        assert event.get_scroll_direction()[-1] == direction, "Synthetic native scroll direction was not set"
    seat = Gdk.Display.get_default().get_default_seat()
    if seat is not None and seat.get_pointer() is not None:
        event.set_device(seat.get_pointer())
    handled = strip.event(event)
    pump(0.025)
    return handled


def main():
    if not Gtk.init_check()[0]:
        raise RuntimeError("A GTK desktop is required; set DISPLAY")
    window = Gtk.Window(title="AstraTerm notebook wheel check")
    window.set_default_size(600, 380)
    notebook = Gtk.Notebook()
    notebook.set_show_tabs(False)
    contents = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    strip = TabStrip()
    contents.pack_start(strip, False, False, 0)
    contents.pack_start(notebook, True, True, 0)
    window.add(contents)
    for index in range(8):
        content = Gtk.Label(label="Body %d" % index)
        tab = Gtk.Label(label="Terminal %d" % index)
        tab.set_size_request(140, 28)
        strip.labels.pack_start(tab, False, False, 0)
        notebook.append_page(content, None)
    window.show_all()
    window.present()
    pump(0.25)
    try:
        adjustment = strip.get_hadjustment()
        assert adjustment.get_upper() > adjustment.get_page_size()
        assert wheel(strip, Gdk.ScrollDirection.DOWN)
        assert adjustment.get_value() > 0 and notebook.get_current_page() == 0
        offset = adjustment.get_value()
        assert wheel(strip, Gdk.ScrollDirection.SMOOTH, dx=0.5)
        assert adjustment.get_value() > offset and notebook.get_current_page() == 0
        for unused in range(18):
            wheel(strip, Gdk.ScrollDirection.LEFT)
        assert adjustment.get_value() == 0 and notebook.get_current_page() == 0
        for tab in strip.labels.get_children()[2:]:
            tab.destroy()
        pump()
        wheel(strip, Gdk.ScrollDirection.DOWN)
        assert adjustment.get_value() == 0 and notebook.get_current_page() == 0
        print("PASS wheel scrolls overflowing tab labels and never selects a page", flush=True)

        theme = Gtk.IconTheme.get_default()
        for protocol in ("ssh", "rdp", "vnc", "local", "serial", "telnet", "mosh"):
            icon = protocol_icon(protocol)
            assert theme.has_icon(icon), "Missing %s icon: %s" % (protocol, icon)
        assert theme.has_icon(group_icon()) and theme.has_icon(group_icon(True))
        pencil = rename_image()
        assert pencil.get_storage_type() == Gtk.ImageType.PIXBUF, "Inline pencil SVG did not render"
        assert pencil.get_pixbuf().get_width() == 20
        print("PASS folder / SSH / RDP theme icons and explicit pencil rendering", flush=True)
        print("UI_HELPERS_SMOKE_OK", flush=True)
    finally:
        window.destroy()
        pump(0.05)


if __name__ == "__main__":
    main()
