#!/usr/bin/python3.5
"""Real GTK folder/shared-settings checks with temporary metadata and no SSH.

Run DISPLAY=:0 /usr/bin/python3.5 tests/smoke_groups.py. Optional
ASTRA_GROUPS_SCREENSHOT saves the window and a shared-settings dialog PNG.
"""
from __future__ import print_function, unicode_literals

import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session
from astraterm.dialogs import SessionDialog
from astraterm.group_dialogs import GroupDialog
from astraterm.ui_helpers import platform_pixbuf
from smoke_multiexec import pump

KEYBOARD = json.loads((Path(__file__).resolve().parents[1] / "shared/contracts/keyboard-shortcuts.json")
                      .read_text(encoding="utf-8"))


def key_press(widget, key, modifiers=0):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.key.window = widget.get_window()
    event.key.keyval = Gdk.keyval_from_name(key)
    event.key.state = modifiers
    return widget.emit("key-press-event", event)


def rename_with_f2(tree, title, names):
    dialog_values = []

    def answer():
        dialog = next((widget for widget in Gtk.Window.list_toplevels()
                       if isinstance(widget, Gtk.Dialog) and widget.get_visible()), None)
        if dialog:
            entry = next((item for item in dialog.get_content_area().get_children()
                          if isinstance(item, Gtk.Entry)), None)
            dialog_values.append((dialog.get_title(), entry.get_text() if entry else None))
            if entry:
                entry.set_text(names[1])
            dialog.response(Gtk.ResponseType.OK if entry else Gtk.ResponseType.CANCEL)
        return False

    tree.grab_focus()
    source = GLib.idle_add(answer)
    assert key_press(tree, KEYBOARD["rename"]["key"])
    if not dialog_values:
        GLib.source_remove(source)
    assert dialog_values == [(title, names[0])], dialog_values


def rows(model):
    result = []
    def visit(unused_model, path, iterator, unused):
        result.append((path.to_string(), [model.get_value(iterator, column)
                                         for column in range(model.get_n_columns())]))
        return False
    model.foreach(visit, None)
    return result


def row_by_id(window, identifier):
    model = window.session_tree_store
    return next((path, values) for path, values in rows(model) if values[1] == identifier)


def set_setting(dialog, name, value):
    dialog.setting_checks[name].set_active(True)
    widget = dialog.ssh_fields[name]
    if name in ("compression", "x11"):
        widget.set_active_id("yes" if value else "no")
    elif isinstance(widget, Gtk.ComboBoxText):
        widget.set_active_id(value)
    else:
        widget.set_text(str(value))


def screenshot(widget, path):
    pump(0.2)
    native = widget.get_window()
    pixbuf = Gdk.pixbuf_get_from_window(native, 0, 0, native.get_width(), native.get_height())
    assert pixbuf is not None, "Could not capture GTK window"
    pixbuf.savev(path, "png", [], [])
    print("GROUPS_SCREENSHOT=" + path, flush=True)


def main():
    if not Gtk.init_check()[0]:
        raise RuntimeError("Astra desktop required; set DISPLAY")
    with tempfile.TemporaryDirectory(prefix="astraterm-groups-") as directory:
        store = SessionStore(os.path.join(directory, "sessions.json"))
        top = store.upsert_group({"name": "Работа", "ssh": {
            "username": "group-user", "port": 2222, "identity_profile": "work_key",
            "compression": False}})
        child = store.upsert_group({"name": "Работа/Prod", "ssh": {"port": 2200, "encoding": "CP1251"}})
        store.upsert_group({"name": "Пустая", "ssh": {}})
        store.upsert_group({"name": "Другое", "ssh": {
            "username": "other-user", "port": 2022, "identity_profile": "system"}})
        profile = default_session("ssh")
        profile.update(name="Приложение SSH", host="server.example", group=child["name"],
                       username="personal-user", port=2223, identity_profile="system",
                       inherit_group_ssh=True, remote_command="printf synthetic")
        profile = store.upsert(profile)
        rdp = default_session("rdp")
        rdp.update(name="Рабочий стол RDP", host="desktop.example", username="rdp-user",
                   group=child["name"], inherit_group_ssh=True)
        rdp = store.upsert(rdp)
        identities = [{"id": "work_key", "name": "Рабочий ключ", "key_file": "/not-read/test-key"}]
        window, dialogs = None, []
        try:
            window = MainWindow(store, start_local=False)
            window.present()
            pump(0.25)

            font = KEYBOARD["font"]
            control = Gdk.ModifierType.CONTROL_MASK
            assert window.font_size == font["default"]["linux"]
            assert key_press(window, "equal", control)
            assert window.font_size == font["default"]["linux"] + 1
            assert key_press(window, font["reset"], control)
            assert window.font_size == font["default"]["linux"]
            shortcut = KEYBOARD["rename"]
            group = store.upsert_group({"name": shortcut["group"][0], "ssh": {"port": 2201}})
            session = store.upsert(dict(default_session("ssh"), name=shortcut["session"][0],
                                        host="rename.example", group=group["name"]))
            window._refresh_sessions()
            tree = window.session_tree
            tree.get_selection().select_path(Gtk.TreePath.new_from_string(row_by_id(window, session["id"])[0]))
            rename_with_f2(tree, "Переименовать подключение", shortcut["session"])
            assert next(item for item in store.sessions if item["id"] == session["id"]) == dict(
                session, name=shortcut["session"][1])
            window._select_group(group["name"])
            rename_with_f2(tree, "Переименовать папку", shortcut["group"])
            assert next(item for item in store.groups if item["id"] == group["id"]) == dict(
                group, name=shortcut["group"][1])
            assert next(item for item in store.sessions if item["id"] == session["id"])["group"] == shortcut["group"][1]
            store.delete(session["id"])
            store.delete_group(shortcut["group"][1])
            window._refresh_sessions()
            print("PASS F2 sidebar rename and Ctrl+0 font reset", flush=True)

            source_path = Gtk.TreePath.new_from_string(row_by_id(window, profile["id"])[0])
            tree.expand_to_path(source_path)
            tree.scroll_to_cell(source_path, None, False, 0, 0)
            pump(.15)
            rectangle = tree.get_cell_area(source_path, tree.get_column(0))
            event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
            event.button.button = 3
            event.button.x, event.button.y = rectangle.x + rectangle.width / 2, rectangle.y + rectangle.height / 2
            hit = tree.get_path_at_pos(int(event.button.x), int(event.button.y))
            assert hit and hit[0].to_string() == source_path.to_string()
            menus = []
            with mock.patch.object(Gtk.Menu, "popup", lambda menu, *_: menus.append(menu)):
                assert window._session_context(tree, event.button)
            next(item for item in menus[0].get_children() if item.get_label() == "Дублировать").activate()
            copied = window._selected_session()
            assert copied["id"] != profile["id"]
            assert copied == dict(profile, id=copied["id"], name=profile["name"] + " (1)")
            assert window.notebook.get_n_pages() == 0
            store.delete(copied["id"])
            window._refresh_sessions()
            print("PASS sidebar context menu duplicates saved connection", flush=True)

            created = GroupDialog(window, store, parent_group=child["name"], identity_profiles=identities)
            dialogs.append(created)
            created.name_entry.set_text("Новая папка")
            added = created.get_group()
            assert added["name"] == "Работа/Prod/Новая папка" and added["ssh"] == {}
            assert not created.apply_to_sessions
            assert not any(item["name"] == added["name"] for item in store.groups), "Dialog saved before acceptance"
            created.name_entry.set_text("bad/name")
            try:
                created.get_group()
            except ValueError:
                pass
            else:
                raise AssertionError("A slash bypassed the explicit parent selector")
            created.destroy()
            store.upsert_group(added)

            editor = GroupDialog(window, store, group=child, identity_profiles=identities)
            dialogs.append(editor)
            editor.notebook.set_current_page(1)
            assert editor.parent_combo.get_active_id() == top["name"]
            assert not editor.setting_checks["username"].get_active()
            assert editor.ssh_fields["username"].get_text() == "group-user"
            assert not editor.ssh_fields["username"].get_sensitive()
            set_setting(editor, "username", "shared-user")
            editor.setting_checks["username"].set_active(False)
            assert editor.ssh_fields["username"].get_text() == "group-user"
            editor.setting_checks["username"].set_active(True)
            assert editor.ssh_fields["username"].get_text() == "shared-user", "Toggling lost the unsaved override"
            shared = {"username": "shared-user", "port": 2227, "identity_profile": "system",
                      "key_file": "~/.ssh/test-key", "jump_host": "jump@gateway:2222",
                      "x11": True, "compression": False, "encoding": "UTF-8"}
            for name, value in shared.items():
                set_setting(editor, name, value)
            editor.apply_existing.set_active(True)
            editor.parent_combo.set_active_id("Другое")
            editor.name_entry.set_text("Промышленные")
            edited = editor.get_group()
            assert edited["id"] == child["id"] and edited["name"] == "Другое/Промышленные"
            assert edited["ssh"] == shared and editor.apply_to_sessions
            assert next(item for item in store.groups if item["id"] == child["id"])["name"] == child["name"]
            destination = os.environ.get("ASTRA_GROUPS_SCREENSHOT")
            if destination:
                screenshot(editor, os.path.splitext(destination)[0] + "-dialog.png")
            editor.destroy()
            print("PASS folder parent selection / eight shared settings / explicit apply / unsaved override preservation", flush=True)

            session = SessionDialog(window, profile, identity_profiles=identities, session_store=store)
            dialogs.append(session)
            pump()
            assert session.fields["username"].get_text() == "group-user"
            assert session.fields["port"].get_text() == "2200"
            assert session.fields["identity_profile"].get_active_id() == "work_key"
            for name in ("username", "port", "identity_profile", "compression", "encoding"):
                assert not session.fields[name].get_sensitive(), "Inherited field is editable: " + name
            saved = session.get_session()
            assert saved["username"] == "personal-user" and saved["port"] == 2223
            assert saved["host"] == profile["host"] and saved["remote_command"] == profile["remote_command"]
            assert saved["inherit_group_ssh"]
            effective = store.resolve_session(saved)
            assert (effective["username"], effective["port"], effective["identity_profile"]) == ("group-user", 2200, "work_key")
            session.fields["inherit_group_ssh"].set_active(False)
            assert session.fields["username"].get_text() == "personal-user"
            assert session.fields["port"].get_text() == "2223"
            assert session.fields["identity_profile"].get_active_id() == "system"
            session.fields["group"].get_child().set_text("Другое")
            session.fields["inherit_group_ssh"].set_active(True)
            assert session.fields["username"].get_text() == "other-user"
            assert session.fields["port"].get_text() == "2022"
            assert session.fields["identity_profile"].get_active_id() == "system"
            session.fields["inherit_group_ssh"].set_active(False)
            assert session.get_session()["username"] == "personal-user"
            assert session.get_session()["port"] == 2223
            session.destroy()

            remote_desktop = SessionDialog(window, rdp, identity_profiles=identities, session_store=store)
            dialogs.append(remote_desktop)
            assert not remote_desktop.fields["inherit_group_ssh"].get_sensitive()
            assert remote_desktop.fields["username"].get_text() == "rdp-user"
            assert remote_desktop.fields["port"].get_text() == "3389"
            assert store.resolve_session(remote_desktop.get_session())["port"] == 3389
            remote_desktop.destroy()
            print("PASS session inherited display / individual values / group changes / SSH-only application", flush=True)

            window._refresh_sessions()
            pump()
            assert {"Работа", "Работа/Prod", "Работа/Prod/Новая папка", "Пустая"}.issubset(window._group_rows)
            model = window.session_tree_store
            folder_row = window._group_rows[child["name"]]
            assert model.get_value(folder_row, 0) == "Prod"
            parent_row = model.iter_parent(folder_row)
            assert model.get_value(parent_row, 4) == top["name"], "Sidebar did not nest the folder"
            ssh_path, ssh_values = row_by_id(window, profile["id"])
            unused_path, rdp_values = row_by_id(window, rdp["id"])
            assert ssh_values[3] and rdp_values[3] and ssh_values[3] != rdp_values[3], "SSH/RDP icons are not distinct"
            assert ssh_values[5] == platform_pixbuf('local'), "SSH row does not show the Linux mark"
            assert rdp_values[5] is not None, "RDP platform mark is missing"
            assert model.get_value(folder_row, 3) and model.get_value(folder_row, 3) != ssh_values[3]
            window.session_tree.get_selection().select_path(Gtk.TreePath.new_from_string(ssh_path))
            window._refresh_sessions()
            selected_model, selected_iter = window.session_tree.get_selection().get_selected()
            assert selected_iter is not None and selected_model.get_value(selected_iter, 1) == profile["id"], "Refresh lost the selected connection"
            window.session_search.set_text("Пустая")
            pump(0.3)
            assert "Пустая" in window._group_rows, "Search hid a matching empty folder"
            window.session_search.set_text("Работа")
            pump(0.3)
            assert row_by_id(window, profile["id"]) and row_by_id(window, rdp["id"])
            window.session_search.set_text("")
            pump(0.3)
            folder_path = window.session_tree_store.get_path(window._group_rows[child["name"]])
            assert window.session_tree.collapse_row(folder_path)
            window.session_search.set_text("Работа")
            pump(0.3)
            window.session_search.set_text("")
            pump(0.3)
            assert not window.session_tree.row_expanded(window.session_tree_store.get_path(window._group_rows[child["name"]]))
            reopened = MainWindow(SessionStore(store.path), start_local=False)
            try:
                path = reopened.session_tree_store.get_path(reopened._group_rows[child["name"]])
                assert not reopened.session_tree.row_expanded(path), "Collapsed folder was not restored"
            finally:
                reopened.shutdown()
                reopened.destroy()
            if destination:
                screenshot(window, destination)
            def set_logging():
                dialog = next(item for item in Gtk.Window.list_toplevels()
                              if isinstance(item, Gtk.Dialog) and item.get_title() == 'Настройки')
                controls = dialog.get_content_area().get_children()
                next(item for item in controls if isinstance(item, Gtk.CheckButton)).set_active(True)
                next(item for item in controls if isinstance(item, Gtk.ComboBoxText) and
                     item.get_active_id() in ('error', 'warning', 'info', 'debug')).set_active_id('debug')
                dialog.response(Gtk.ResponseType.OK)
                return False
            GLib.idle_add(set_logging)
            window.settings()
            assert window.diagnostic_log.enabled and window.diagnostic_log.level == 'debug'
            with open(window.diagnostic_log.filename, encoding='utf-8') as stream:
                assert 'logging.configured' in stream.read()
            print("PASS nested empty folders / SSH-RDP icons / selection restoration / folder search", flush=True)
            print("GROUPS_SMOKE_OK", flush=True)
        finally:
            for dialog in reversed(dialogs):
                dialog.destroy()
            if window is not None:
                window.shutdown()
                window.destroy()
            pump(0.2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
