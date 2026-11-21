#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Check the compact GTK workspace and optionally capture demonstration PNGs.

DISPLAY=:0 /usr/bin/python3.5 tests/smoke_design.py
ASTRA_DESIGN_SCREENSHOT=/tmp/design-preview.png also saves
/tmp/design-preview-multiexec.png. Every terminal is a local Python fixture;
the saved SSH/RDP examples use documentation addresses and are never opened.
All sessions and files live in a temporary directory.
"""
from __future__ import print_function, unicode_literals

import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session
from smoke_multiexec import pump, wait_for


FIXTURE = r'''
import os
import signal
import sys
import tty

name, role, address = sys.argv[1:]
tty.setraw(0)
content = ("\x1b[2J\x1b[H\r\n"
           "\x1b[38;5;245m  ASTRA LINUX 1.6\x1b[0m\r\n"
           "\x1b[1;38;5;157m  " + name + "\x1b[0m\r\n"
           "\x1b[38;5;245m  " + role + "\r\n"
           "  " + address + "\x1b[0m\r\n\r\n"
           "\x1b[38;5;157m$\x1b[0m systemctl --failed\r\n"
           "\x1b[38;5;245m0 loaded units listed.\x1b[0m\r\n\r\n"
           "\x1b[38;5;157m$\x1b[0m uptime\r\n"
           "up 12d  \x1b[38;5;245mload 0.12\x1b[0m\r\n\r\n"
           "\x1b[38;5;157mops@" + name + ":~$\x1b[0m ")
os.write(1, content.encode("utf-8"))
# Keep a real PTY alive without evaluating input or touching the network.
while True:
    try:
        data = os.read(0, 4096)
    except OSError:
        break
    if not data:
        break
'''


def screenshot(window, path):
    pump(0.25)
    native = window.get_window()
    pixbuf = Gdk.pixbuf_get_from_window(native, 0, 0,
                                      native.get_width(), native.get_height())
    assert pixbuf is not None, "Could not capture the GTK window"
    pixbuf.savev(path, "png", [], [])
    print("DESIGN_SCREENSHOT=" + path, flush=True)


def fixture_profile(name, role, address):
    profile = default_session("local")
    profile.update(name=name, _argv=[sys.executable, "-u", "-c", FIXTURE,
                                     name, role, address])
    return profile


def populate_store(store):
    for name in ("Инфраструктура", "Инфраструктура/Production",
                 "Инфраструктура/Сеть", "Разработка"):
        store.upsert_group({"name": name, "ssh": {}})
    examples = [("prod-app-01", "ssh", "192.0.2.11", "Инфраструктура/Production"),
                ("prod-db-01", "ssh", "192.0.2.12", "Инфраструктура/Production"),
                ("edge-gw-01", "ssh", "192.0.2.21", "Инфраструктура/Сеть"),
                ("dev-app-01", "ssh", "192.0.2.31", "Разработка"),
                ("Windows · RDP", "rdp", "192.0.2.41", "Разработка")]
    for name, protocol, host, group in examples:
        profile = default_session(protocol)
        profile.update(name=name, host=host, group=group, username="ops")
        store.upsert(profile)


def populate_files(directory):
    for name in ("deploy", "nginx", "services"):
        os.mkdir(os.path.join(directory, name))
    for name, contents in (("README.md", "# Infrastructure\n"),
                           ("app.conf", "environment=production\n"),
                           ("inventory.yml", "# Demonstration inventory\n"),
                           ("deploy.sh", "#!/bin/sh\n"),
                           ("healthcheck.py", "# Health check\n"),
                           ("access.log", "# Demonstration log\n"),
                           ("notes.md", "# Operations notes\n")):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as stream:
            stream.write(contents)


def check_side_panels(window, pane):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'shared', 'contracts', 'tabs.json'), encoding='utf-8') as stream:
        steps = json.load(stream)['side_panels']
    controls = {'left': window.sidebar_toggle, 'right': window.files_toggle}
    initial = pane.pid, pane.connection_generation, pane.terminal
    width = window.workspace_stack.get_allocated_width()
    for step in steps:
        controls[step['toggle']].clicked()
        pump(.15)
        new_width = window.workspace_stack.get_allocated_width()
        assert window.sidebar.get_visible() == step['left']
        assert window.file_browser.get_visible() == step['right']
        assert (new_width > width) if step['grows'] else (new_width < width)
        assert all(control.get_mapped() for control in controls.values())
        assert (pane.pid, pane.connection_generation, pane.terminal) == initial
        width = new_width
    window.set_tiled_view(True)
    if not window.file_browser.get_visible():
        window.files_toggle.clicked()
    window.files_toggle.clicked()
    window.set_tiled_view(False)
    assert not window.file_browser.get_visible(), 'Overview overwrote the explicit panel choice'
    window.files_toggle.clicked()
    pump(.15)


def check_terminal_viewport(window, pane):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'shared', 'contracts', 'tabs.json'), encoding='utf-8') as stream:
        contract = json.load(stream)['terminal_viewport']
    original_size = window.get_size()
    original_font = pane.font_size
    terminal = pane.terminal
    footer = window.status.get_parent()
    try:
        for steps in contract['zoom_steps']:
            pane.set_font_size(original_font + steps)
            for width, height in contract['sizes']:
                window.resize(width, height)
                pump(.2)
                _, terminal_y = terminal.translate_coordinates(window, 0, 0)[-2:]
                _, footer_y = footer.translate_coordinates(window, 0, 0)[-2:]
                allocation = terminal.get_allocated_height()
                padding = terminal.get_style_context().get_padding(terminal.get_state_flags())
                row_height = terminal.get_char_height()
                rows = terminal.get_row_count()
                assert terminal_y + allocation <= footer_y, 'Terminal overlaps status bar'
                free = allocation - padding.top - padding.bottom - rows * row_height
                assert 0 <= free < row_height, 'VTE rows do not fit the viewport: {}'.format(free)
    finally:
        pane.set_font_size(original_font)
        window.resize(*original_size)
        pump(.2)


def check_theme(window, panes):
    assert window.theme == 'dark'
    processes = [pane.pid for pane in panes]
    for theme, minimum, maximum in (('light', .9, 1), ('dark', 0, .25)):
        def choose_theme():
            dialogs = [item for item in Gtk.Window.list_toplevels()
                       if isinstance(item, Gtk.Dialog) and item.get_title() == 'Настройки']
            if not dialogs:
                return True
            dialog = dialogs[0]
            combo = next(item for item in dialog.get_content_area().get_children()
                         if isinstance(item, Gtk.ComboBoxText) and item.get_active_id() in ('light', 'dark'))
            combo.set_active_id(theme)
            dialog.response(Gtk.ResponseType.OK)
            return False

        GLib.timeout_add(80, choose_theme)
        window.settings()
        pump(.2)
        color = window.get_style_context().get_background_color(Gtk.StateFlags.NORMAL)
        assert minimum <= color.red <= maximum, color
        with open(window.appearance_path, encoding='utf-8') as stream:
            assert json.load(stream) == {'theme': theme}
        assert window.theme == theme and [pane.pid for pane in panes] == processes


def check_menu(window):
    button = window.main_menu_button
    menu = window.main_menu
    assert isinstance(menu, Gtk.Menu), "Main actions have no popup menu"
    expected = {"Файл": 10, "Вид": 9, "Инструменты": 8, "Справка": 3}
    roots = menu.get_children()
    assert {item.get_label() for item in roots} == set(expected)
    button.clicked()
    pump(0.15)
    assert menu.get_visible(), "The menu button did not open its popup"
    try:
        for item in roots:
            title = item.get_label()
            assert item.get_visible(), "Hidden main menu section: " + title
            submenu = item.get_submenu()
            actions = submenu.get_children()
            assert len(actions) == expected[title], "Lost actions in " + title
            if title == "Инструменты":
                assert any(action.get_label() == "Настройки…" for action in actions)
            assert all(action.get_visible() for action in actions), "Hidden actions in " + title
            # Show each native submenu without activating any command. This
            # protects the complete old menu behind the new compact button.
            menu.select_item(item)
            submenu.popup(None, None, None, None, 0, Gdk.CURRENT_TIME)
            pump(0.05)
            assert submenu.get_mapped(), "Could not show menu section: " + title
            submenu.popdown()
    finally:
        menu.popdown()
        pump(0.1)


def main():
    if not Gtk.init_check()[0]:
        raise RuntimeError("Astra GTK desktop required; set DISPLAY")
    with tempfile.TemporaryDirectory(prefix="astraterm-design-") as directory:
        store = SessionStore(os.path.join(directory, "sessions.json"))
        populate_store(store)
        files = os.path.join(directory, "projects")
        os.mkdir(files)
        populate_files(files)
        window = None
        try:
            # Following LOCAL fixtures never performs authentication or opens
            # saved remote examples, but retains the production file-pane layout.
            window = MainWindow(store, start_local=False, auto_sftp=True)
            window.resize(1024, 690)
            window.present()
            pump(0.3)
            workspaces = []
            examples = [("prod-app-01", "Production", "192.0.2.11"),
                        ("prod-db-01", "Production", "192.0.2.12"),
                        ("edge-gw-01", "Infrastructure", "192.0.2.21"),
                        ("dev-app-01", "Development", "192.0.2.31")]
            for name, role, address in examples:
                workspace = window.open_session(fixture_profile(name, role, address))
                assert workspace is not None
                workspaces.append(workspace)
            panes = [workspace.active for workspace in workspaces]
            wait_for(lambda: all(pane.pid is not None for pane in panes), "four local preview PTYs")
            window.notebook.set_current_page(0)
            window._pane_focus(panes[0])
            window.file_browser.open_local(files)
            wait_for(lambda: not window.file_browser._busy and
                     window.file_browser._path == files, "temporary local files")
            pump(0.25)
            assert window.get_allocated_width() <= 1024, "Toolbar widened the window past 1024 px"
            assert not window.multiexec_bar.get_visible(), "Idle MultiExec strip consumes normal workspace"
            assert all(not pane.heading.get_visible() for pane in panes), "Single-terminal headings duplicate tabs"
            assert not any(pane.multiexec_check.get_visible() for pane in panes)
            assert [pane for pane in panes if pane.get_style_context().has_class("active-pane")] == [panes[0]], "Active-pane styling is not unique"
            check_menu(window)
            check_side_panels(window, panes[0])
            print("PASS independent side panel toggles enlarge the workspace and preserve the PTY", flush=True)
            print("PASS compact 1024 px workspace / idle controls hidden / complete native menu", flush=True)

            window.session_tree.expand_all()
            if os.environ.get('ASTRA_WEBSITE_SHOT'):
                from astraterm.host_stats_ui import fields
                window.host_stats.close()
                demo = {'hostname':'demo-app-01', 'cpu':4, 'memory_used':6*1024**3, 'memory_total':8*1024**3,
                        'upload':123000, 'download':456000, 'uptime':18*86400, 'interfaces':['eth0'],
                        'users':[{'name':'demo','count':2}], 'disks':[{'path':'/','percent':24},{'path':'/data','percent':48}]}
                for name, text, tooltip in fields(demo):
                    window.host_stats.labels[name].set_text(text)
                    window.host_stats.labels[name].show()
                window.host_stats.show()
            for row in window._group_rows.values():
                if window.session_tree_store.get_value(row, 4) == "Инфраструктура/Production":
                    window.session_tree.get_selection().select_iter(row)
                    break
            window.set_status("Демонстрация · 4 локальных терминала")
            destination = os.environ.get("ASTRA_DESIGN_SCREENSHOT")
            if destination:
                screenshot(window, destination)

            processes = [pane.pid for pane in panes]
            window.set_tiled_view(True)
            pump(0.2)
            assert window.multiexec_bar.get_visible(), "Overview has no recipient controls"
            assert all(pane.heading.get_visible() and pane.multiexec_check.get_visible()
                       for pane in panes), "Overview hides host/recipient identity"
            assert all(not pane.multiexec_check.get_active() for pane in panes)
            for button in (window.select_all_button, window.clear_all_button, window.multi_paste_button):
                assert button.get_visible() and button.get_mapped(), "Overview has hidden bulk actions"
            assert not window.multi_paste_button.get_sensitive()
            window.multiexec.set_selected(panes[0], True)
            window.multiexec.set_selected(panes[1], True)
            window.set_multiexec(True)
            window._pane_focus(panes[0])
            panes[0].terminal.grab_focus()
            pump(0.2)
            assert [pane.multiexec_check.get_active() for pane in panes] == [True, True, False, False]
            assert [pane for pane in panes if pane.get_style_context().has_class("active-pane")] == [panes[0]], "Tiled focus styling highlights multiple hosts"
            assert len(window.multiexec.selected_panes()) == 2 and window.multiexec.enabled
            assert window.multi_paste_button.get_sensitive()
            if destination:
                screenshot(window, os.path.splitext(destination)[0] + "-multiexec.png")

            window.set_multiexec(False)
            assert window.multiexec_bar.get_visible(), "Pausing hid recipient controls"
            assert len(window.multiexec.selected_panes()) == 2
            assert all(pane.multiexec_check.get_visible() for pane in panes)
            window.set_tiled_view(False)
            pump(0.2)
            assert not window.multiexec_bar.get_visible()
            assert all(not pane.heading.get_visible() and not pane.multiexec_check.get_visible()
                       for pane in panes), "Leaving overview did not restore compact headings"
            assert [pane.pid for pane in panes] == processes, "Visual mode replaced terminal processes"
            assert all(pane.get_parent() is workspace for pane, workspace in zip(panes, workspaces))
            print("PASS visible positive recipients / two selected / pause / original headings and PTYs restored", flush=True)
            check_terminal_viewport(window, panes[0])
            print("PASS complete VTE rows above the status bar after resize and font changes", flush=True)
            check_theme(window, panes)
            print("PASS dark default and live theme changes with persistent preferences", flush=True)
            print("DESIGN_SMOKE_OK", flush=True)
        finally:
            if window is not None:
                window.shutdown()
                window.destroy()
            pump(0.15)


if __name__ == "__main__":
    callback_errors = []
    def callback_exception(kind, value, frames):
        # PyGObject prints signal-handler errors without failing the process.
        # Count them so a broken menu-position callback cannot produce a pass.
        callback_errors.append(value)
        traceback.print_exception(kind, value, frames)
    sys.excepthook = callback_exception
    try:
        main()
        if callback_errors:
            raise AssertionError("Unexpected GTK callback exceptions: {}".format(len(callback_errors)))
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
