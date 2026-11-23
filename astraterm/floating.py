"""Presentation-only GTK windows: retain widgets, native sockets and live transports."""
from gi.repository import Gtk


class FloatingWorkspace(object):
    def __init__(self, owner, workspace):
        self.owner, self.workspace = owner, workspace
        self.window = Gtk.Window(title=workspace.tab_label.get_text() + ' · AstraTerm')
        self.window.set_default_size(1000, 720)
        self.window.set_icon_name('utilities-terminal')
        self.window.connect('delete-event', self._delete)
        self.window.connect('key-press-event', owner._key_press)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(spacing=12);toolbar.set_border_width(8)
        toolbar.pack_start(Gtk.Label(label=workspace.tab_label.get_text(), xalign=0), True, True, 0)
        back = Gtk.Button(label='Вернуть во вкладки')
        back.connect('clicked', lambda *_: owner.dock_window(workspace))
        toolbar.pack_end(back, False, False, 0)
        outer.pack_start(toolbar, False, False, 0)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self.body, True, True, 0);self.window.add(outer)
        self.placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.placeholder.set_valign(Gtk.Align.CENTER);self.placeholder.set_halign(Gtk.Align.CENTER)
        back = Gtk.Button(label='Вернуть вкладку')
        back.get_style_context().add_class('primary')
        back.connect('clicked', lambda *_: owner.dock_window(workspace))
        self.placeholder.pack_start(back, False, False, 0)
        # Realize both parents before reparenting; remove/add would destroy Gtk.Socket's XID.
        self.window.show_all()
        root = workspace.get_children()[0]
        self._move_content(root, self.body)
        workspace.pack_start(self.placeholder, True, True, 0);self.placeholder.show_all()

    @staticmethod
    def _move_content(root, destination):
        if isinstance(root, Gtk.Paned):
            # GTK 3.22's reparent fixup can flatten the GdkWindow hierarchy
            # of nested paned children. Realize this terminal tree in its new
            # window so the pane windows are created with their correct parents.
            root.get_parent().remove(root)
            destination.pack_start(root, True, True, 0)
        else:
            root.reparent(destination)
            destination.set_child_packing(root, True, True, 0, Gtk.PackType.START)

    def _delete(self, *_):
        self.owner.dock_window(self.workspace)
        return True

    def dock(self):
        self.placeholder.destroy()
        for root in self.body.get_children():
            self._move_content(root, self.workspace)
        self.window.destroy()
        self.workspace.show_all()
