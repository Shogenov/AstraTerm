# -*- coding: utf-8 -*-
"""Temporarily tile existing terminal widgets without changing their processes.

Call ``leave()`` before closing/splitting workspaces or destroying this view.
All methods run on the GTK thread. The owning window controls which terminals
are eligible and keeps its workspace lists and notebook selection unchanged.
"""
from __future__ import unicode_literals

import math

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


class TiledTerminals(Gtk.Box):
    """A resizable, scrollable presentation of existing TerminalPane widgets.

    ``enter(panes)`` retains stable input order, with two columns for three/four
    panes and three columns for five through nine. ``leave()`` restores original
    Gtk.Box packing, Gtk.Paned slots/flags, divider proportions, and focus.
    No terminal is spawned, closed, reset, or destroyed by this class.
    """

    MIN_TILE_WIDTH = 160
    MIN_TILE_HEIGHT = 120

    def __init__(self):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.get_style_context().add_class("tiled-terminals")
        self.active = False
        self.panes = []
        self._records = []
        self._parent_states = []
        self._tree = None
        self._tile_handlers = []
        self._restore_handlers = []
        self._focus_widget = None
        self._focus_generation = 0
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        # VTE must not be a GtkScrolledWindow's direct scrollable child. The
        # viewport scrolls our paned layout, while each VTE keeps its own history.
        self.viewport = Gtk.Viewport()
        self.viewport.set_shadow_type(Gtk.ShadowType.NONE)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._content.set_hexpand(True)
        self._content.set_vexpand(True)
        self.viewport.add(self._content)
        self.scroller.add(self.viewport)
        self.pack_start(self.scroller, True, True, 0)
        self.connect("destroy", self._destroyed)

    @staticmethod
    def _axis_size(widget, allocation=None):
        horizontal = widget.get_orientation() == Gtk.Orientation.HORIZONTAL
        if allocation is not None:
            return allocation.width if horizontal else allocation.height
        return widget.get_allocated_width() if horizontal else widget.get_allocated_height()

    @staticmethod
    def _disconnect_handlers(handlers):
        for widget, identifier in list(handlers):
            try:
                if widget.handler_is_connected(identifier):
                    widget.disconnect(identifier)
            except (TypeError, RuntimeError):
                pass
        handlers[:] = []

    def _destroyed(self, unused):
        self._focus_generation += 1
        self._disconnect_handlers(self._tile_handlers)
        self._disconnect_handlers(self._restore_handlers)

    def _initial_divider(self, paned, ratio):
        token = [None]

        def allocate(widget, allocation):
            extent = self._axis_size(widget, allocation)
            if extent <= 20:
                return
            widget.set_position(int(round(extent * ratio)))
            widget.disconnect(token[0])
            pair = (widget, token[0])
            if pair in self._tile_handlers:
                self._tile_handlers.remove(pair)

        token[0] = paned.connect("size-allocate", allocate)
        self._tile_handlers.append((paned, token[0]))

    def _balanced(self, children, orientation):
        if len(children) == 1:
            return children[0]
        middle = (len(children) + 1) // 2
        paned = Gtk.Paned(orientation=orientation)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_wide_handle(True)
        paned.pack1(self._balanced(children[:middle], orientation), True, False)
        paned.pack2(self._balanced(children[middle:], orientation), True, False)
        self._initial_divider(paned, float(middle) / len(children))
        return paned

    def _build_tree(self, holders):
        columns = int(math.ceil(math.sqrt(len(holders))))
        rows = [self._balanced(holders[index:index + columns], Gtk.Orientation.HORIZONTAL)
                for index in range(0, len(holders), columns)]
        return self._balanced(rows, Gtk.Orientation.VERTICAL)

    @staticmethod
    def _snapshot(pane):
        parent = pane.get_parent()
        record = {"pane": pane, "parent": parent, "placeholder": None,
                  "holder": None, "moved": False, "visible": pane.get_visible(),
                  "layout_heading": pane.layout_heading_visible}
        if isinstance(parent, Gtk.Paned):
            record.update(kind="paned", first=parent.get_child1() is pane,
                          resize=parent.child_get_property(pane, "resize"),
                          shrink=parent.child_get_property(pane, "shrink"))
        elif isinstance(parent, Gtk.Box):
            if parent.get_center_widget() is pane:
                raise ValueError("Центральная панель Gtk.Box не поддерживает плиточный перенос.")
            expand, fill, padding, pack_type = parent.query_child_packing(pane)
            record.update(kind="box", expand=expand, fill=fill, padding=padding,
                          pack_type=pack_type,
                          position=parent.child_get_property(pane, "position"))
        else:
            raise ValueError("Терминал должен находиться в Gtk.Box или Gtk.Paned.")
        return record

    @staticmethod
    def _apply_packing(record, widget):
        parent = record["parent"]
        if record["kind"] == "box":
            parent.set_child_packing(widget, record["expand"], record["fill"],
                                     record["padding"], record["pack_type"])
            parent.reorder_child(widget, record["position"])
        else:
            actual_first = parent.get_child1() is widget
            if actual_first != record["first"]:
                raise RuntimeError("Изменился исходный слот разделённого терминала.")
            parent.child_set_property(widget, "resize", record["resize"])
            parent.child_set_property(widget, "shrink", record["shrink"])

    def _put_placeholder(self, record):
        parent, placeholder = record["parent"], record["placeholder"]
        if record["kind"] == "box":
            parent.pack_start(placeholder, record["expand"], record["fill"], record["padding"])
        elif record["first"]:
            parent.pack1(placeholder, record["resize"], record["shrink"])
        else:
            parent.pack2(placeholder, record["resize"], record["shrink"])
        self._apply_packing(record, placeholder)
        placeholder.show()

    def _remember_focus(self, panes):
        window = self.get_toplevel()
        focus = window.get_focus() if isinstance(window, Gtk.Window) else None
        ancestor = focus
        while ancestor is not None:
            if any(ancestor is pane for pane in panes):
                return focus
            ancestor = ancestor.get_parent()
        return None

    def enter(self, panes):
        """Move the given existing widgets into a balanced temporary layout."""
        if self.active:
            self.leave()
        unique, seen = [], set()
        for pane in panes:
            if id(pane) not in seen:
                seen.add(id(pane))
                unique.append(pane)
        if not unique:
            return
        # Capture all slots before moving any widget. Multiple children can
        # share one parent, so snapshots taken during mutation would be wrong.
        records = [self._snapshot(pane) for pane in unique]
        self._focus_generation += 1
        self._disconnect_handlers(self._restore_handlers)
        parent_states, parent_ids = [], set()
        for record in records:
            parent = record["parent"]
            if isinstance(parent, Gtk.Paned) and id(parent) not in parent_ids:
                parent_ids.add(id(parent))
                parent_states.append({"parent": parent, "position": parent.get_position(),
                                      "position_set": parent.get_property("position-set"),
                                      "extent": self._axis_size(parent)})
            record["placeholder"] = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.set_hexpand(True)
            holder.set_vexpand(True)
            holder.set_size_request(self.MIN_TILE_WIDTH, self.MIN_TILE_HEIGHT)
            holder.get_style_context().add_class("tiled-terminal")
            record["holder"] = holder
        self._focus_widget = self._remember_focus(unique)
        self._records, self._parent_states = records, parent_states
        self.panes = unique
        self.active = True
        try:
            self._tree = self._build_tree([record["holder"] for record in records])
            self._content.pack_start(self._tree, True, True, 0)
            self._tree.show_all()
            for record in records:
                holder, pane = record["holder"], record["pane"]
                # GTK3 reparent() retains the widget and preserves realization
                # when both parents are realized in the same toplevel window.
                if self.get_realized() and not holder.get_realized():
                    holder.realize()
                pane.reparent(holder)
                record["moved"] = True
                pane.set_layout_heading(True)
                holder.set_child_packing(pane, True, True, 0, Gtk.PackType.START)
                self._put_placeholder(record)
                pane.show()
            self._content.show()
            self.viewport.show()
            self.scroller.show()
        except Exception:
            self.leave()
            raise

    def _restore_dividers(self):
        for state in self._parent_states:
            parent = state["parent"]
            parent.set_position(state["position"])
            parent.set_property("position-set", state["position_set"])
            if not state["position_set"] or state["extent"] <= 1:
                continue
            token = [None]

            def allocate(widget, allocation, saved=state, identifier=token):
                extent = self._axis_size(widget, allocation)
                if extent <= 20 or self.active:
                    return
                if extent == saved["extent"]:
                    position = saved["position"]
                else:
                    position = int(round(float(saved["position"]) * extent / saved["extent"]))
                widget.set_position(position)
                widget.set_property("position-set", saved["position_set"])
                widget.disconnect(identifier[0])
                pair = (widget, identifier[0])
                if pair in self._restore_handlers:
                    self._restore_handlers.remove(pair)

            token[0] = parent.connect("size-allocate", allocate)
            self._restore_handlers.append((parent, token[0]))

    def leave(self):
        """Restore original parents/packing without restarting or closing a pane."""
        if not self.active:
            return
        self._disconnect_handlers(self._tile_handlers)
        failures = []
        for record in self._records:
            if not record["moved"]:
                continue
            pane, parent, placeholder = record["pane"], record["parent"], record["placeholder"]
            try:
                if placeholder.get_parent() is parent:
                    parent.remove(placeholder)
                if parent.get_toplevel().get_realized() and not parent.get_realized():
                    parent.realize()
                if pane.get_parent() is not parent:
                    pane.reparent(parent)
                if pane.get_parent() is not parent:
                    raise RuntimeError("Не удалось восстановить родительский контейнер терминала.")
                self._apply_packing(record, pane)
                pane.set_layout_heading(record["layout_heading"])
                pane.set_visible(record["visible"])
                record["moved"] = False
                placeholder.destroy()
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            # Never destroy a temporary tree while it still owns a terminal.
            # The caller can restore its workspace invariants and retry leave().
            raise RuntimeError("Не удалось восстановить раскладку: " + "; ".join(failures))
        self.active = False
        self._restore_dividers()
        if self._tree is not None:
            self._content.remove(self._tree)
            self._tree.destroy()
            self._tree = None
        self._records = []
        self._parent_states = []
        self.panes = []
        focus = self._focus_widget
        self._focus_widget = None
        generation = self._focus_generation

        def restore_focus():
            if (not self.active and generation == self._focus_generation and
                    focus is not None and focus.get_mapped()):
                focus.grab_focus()
            return False

        if focus is not None:
            GLib.idle_add(restore_focus)

