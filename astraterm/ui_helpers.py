"""Small GTK 3.22 helpers for session icons and the tab strip."""
from __future__ import unicode_literals


import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib


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
    return _colored_pixbuf('file-folder',
                           '<path fill="#e2aa45" d="M2 6a2 2 0 0 1 2-2h5l3 3h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/>'
                           '<path fill="#ffd273" d="M3 10h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>')


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


def rename_icon():
    return _theme_icon("document-edit-symbolic", "accessories-text-editor-symbolic",
                       "accessories-text-editor", "gtk-edit")


def rename_image():
    """A recognizable pencil even when Astra's theme lacks edit-rename."""
    drawing = b'''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24">
      <path d="M4 15.5L3 21l5.5-1L20 8.5 15.5 4z" fill="none" stroke="#d8dee9" stroke-width="1.8" stroke-linejoin="round"/>
      <path d="M14 5.5l4.5 4.5M4 15.5L8.5 20M3 21l3-0.5-2.5-2.5z" fill="none" stroke="#78d6bd" stroke-width="1.8" stroke-linejoin="round"/>
      <path d="M16.5 3l1-1c0.7-0.7 1.6-0.7 2.3 0l2.2 2.2c0.7 0.7 0.7 1.6 0 2.3l-1 1z" fill="#78d6bd"/>
    </svg>'''
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(drawing)
        loader.close()
        return Gtk.Image.new_from_pixbuf(loader.get_pixbuf())
    except Exception:
        return Gtk.Image.new_from_icon_name(rename_icon(), Gtk.IconSize.BUTTON)


def action_image(name):
    """Small consistent line icons, independent of the installed desktop theme."""
    paths = {
        "terminal": '<path d="m4 6 5 5-5 5m8 0h7"/>',
        "split": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>',
        "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
        "panel-left": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m-4-12h2m-2 4h2m-2 4h2"/>',
        "panel-right": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16m2-12h2m-2 4h2m-2 4h2"/>',
        "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
        "plus": '<path d="M12 5v14M5 12h14"/>',
        "folder": '<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7zM12 10v7m-3-3.5h6"/>',
        "detach": '<path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/>',
        "close": '<path d="m6 6 12 12M18 6 6 18"/>',
        "arrow": '<path d="M4 12h15m-5-5 5 5-5 5"/>',
    }
    drawing = ('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
               'viewBox="0 0 24 24"><g fill="none" stroke="#9ba8b3" stroke-width="1.65" '
               'stroke-linecap="round" stroke-linejoin="round">' + paths[name] + '</g></svg>')
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.write(drawing.encode("utf-8"))
    loader.close()
    return Gtk.Image.new_from_pixbuf(loader.get_pixbuf())


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
