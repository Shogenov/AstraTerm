"""Small GTK 3.22 helpers for session icons and the tab strip."""
from __future__ import unicode_literals
from functools import lru_cache


import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

from ._icons import ICONS


def _theme_icon(*names):
    theme = Gtk.IconTheme.get_default()
    if theme is not None:
        for name in names:
            if theme.has_icon(name):
                return name
    return names[-1]


def protocol_icon(protocol):
    """Return an installed theme icon for the connection's actual protocol."""
    if protocol == "ssh":
        return _theme_icon("dialog-password-symbolic", "dialog-password", "network-server")
    if protocol in ("rdp", "vnc"):
        return _theme_icon("preferences-desktop-remote-desktop", "computer-symbolic", "computer")
    if protocol == "local":
        return _theme_icon("utilities-terminal-symbolic", "utilities-terminal", "application-x-executable")
    if protocol == "serial":
        return _theme_icon("network-wired-symbolic", "network-wired", "computer")
    return _theme_icon("network-server-symbolic", "network-server", "utilities-terminal")


_platform_icons = {}


def platform_pixbuf(protocol):
    """Colored platform marks in the saved connection list."""
    drawings = {
        'local': '<path fill="#18232d" stroke="#8194a5" d="M12 2c-4 0-5 3-5 7 0 3-4 5-4 9 0 2 4 3 9 3s9-1 9-3c0-4-4-6-4-9 0-4-1-7-5-7z"/>'
                 '<ellipse cx="12" cy="14" rx="5.2" ry="6.4" fill="#fff4e0"/>'
                 '<ellipse cx="8" cy="20" rx="4.4" ry="2.4" fill="#f8bb45"/>'
                 '<ellipse cx="16" cy="20" rx="4.4" ry="2.4" fill="#f8bb45"/>'
                 '<ellipse cx="10" cy="6.6" rx="1.7" ry="2.3" fill="#fff4e0"/><ellipse cx="14" cy="6.6" rx="1.7" ry="2.3" fill="#fff4e0"/>'
                 '<circle cx="10.5" cy="7" r=".8" fill="#18232d"/><circle cx="13.5" cy="7" r=".8" fill="#18232d"/>'
                 '<path fill="#f8bb45" d="M8.5 9.8 12 8l3.5 1.8L12 12z"/>',
        'rdp': '<rect x="3" y="4" width="18" height="12" rx="2" fill="none" stroke="#9cabb9" stroke-width="2"/>'
               '<path d="M12 16v4m-5 0h10" fill="none" stroke="#9cabb9" stroke-width="2" stroke-linecap="round"/>',
    }
    name = 'local' if protocol in ('ssh', 'mosh') else protocol
    return _colored_pixbuf(name, drawings.get(name))


def file_folder_pixbuf():
    return _colored_pixbuf('file-folder', ICONS['fileFolder'])


def _colored_pixbuf(name, drawing):
    if drawing is None:
        return None
    if name not in _platform_icons:
        try:
            svg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24">{}</svg>'
            loader = GdkPixbuf.PixbufLoader.new_with_type('svg')
            loader.write(svg.format(drawing).encode('utf-8'))
            loader.close()
            _platform_icons[name] = loader.get_pixbuf()
        except GLib.Error:
            _platform_icons[name] = None
    return _platform_icons[name]


def group_icon(expanded=False):
    if expanded:
        return _theme_icon("folder-open-symbolic", "folder-open", "folder")
    return _theme_icon("folder-symbolic", "folder")


def action_image(name):
    """Render the shared toolbar icon without relying on the desktop icon theme."""
    drawing = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
               'viewBox="0 0 24 24"><g fill="none" stroke="#768994" stroke-width="1.6" '
               'stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + '</g></svg>')
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.write(drawing.encode("utf-8"))
    loader.close()
    return Gtk.Image.new_from_pixbuf(loader.get_pixbuf())


def status_pixbuf(running):
    color = "#82b99a" if running else "#db9251"
    return _colored_pixbuf("status:" + color, '<circle cx="12" cy="12" r="4" fill="' + color + '"/>')


@lru_cache(maxsize=128)
def session_status_pixbuf(states):
    if not states:
        return None
    columns = 8
    width = min(len(states), columns) * 9
    height = ((len(states) + columns - 1) // columns) * 9
    dots = ['<circle cx="{}" cy="{}" r="2.5" fill="{}"/>'.format(
        4 + index % columns * 9, 4 + index // columns * 9,
        '#82b99a' if running else '#db9251') for index, running in enumerate(states)]
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}">{}</svg>'.format(
        width, height, ''.join(dots))
    loader = GdkPixbuf.PixbufLoader.new_with_type('svg')
    loader.write(svg.encode('ascii'))
    loader.close()
    return loader.get_pixbuf()


def _translated(widget, parent, x=0, y=0):
    result = widget.translate_coordinates(parent, x, y)
    if result is None or (len(result) == 3 and not result[0]):
        return None
    return result[-2], result[-1]


def _widget_root_origin(widget):
    toplevel = widget.get_toplevel()
    native = toplevel.get_window()
    translated = _translated(widget, toplevel)
    if native is None or translated is None:
        return None
    root_x, root_y = native.get_root_coords(0, 0)
    return root_x + translated[0], root_y + translated[1]


class TabStrip(Gtk.ScrolledWindow):
    """Scroll the tab labels independently of the selected notebook page."""

    def __init__(self):
        Gtk.ScrolledWindow.__init__(self)
        self.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        self.set_overlay_scrolling(False)
        self.set_kinetic_scrolling(False)
        self.labels = Gtk.Box(spacing=3)
        self.add(self.labels)
        self.connect("scroll-event", self._scroll)

    def _scroll(self, _, event):
        direction = event.direction
        if direction == Gdk.ScrollDirection.SMOOTH:
            valid, dx, dy = event.get_scroll_deltas()
            delta = (dy if abs(dy) >= abs(dx) else dx) if valid else 0
        else:
            delta = -1 if direction in (Gdk.ScrollDirection.UP, Gdk.ScrollDirection.LEFT) else 1
        adjustment = self.get_hadjustment()
        adjustment.set_value(adjustment.get_value() + delta * 96)
        return True

    def reveal(self, tab):
        if tab is not None and tab.get_parent() is self.labels:
            self.get_hadjustment().clamp_page(tab.get_allocation().x,
                                              tab.get_allocation().x + tab.get_allocated_width())
        return False
