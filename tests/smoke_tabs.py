"""Native GTK tab gestures on an isolated display; synthetic local PTYs only."""
from __future__ import print_function, unicode_literals
import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk
from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session
from astraterm.ui_helpers import _widget_root_origin
from smoke_multiexec import CAPTURE_CHILD, pump, wait_for, ready, read_bytes


class Pointer(object):
    def __init__(self):
        self.x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
        self.xtest = ctypes.CDLL(ctypes.util.find_library('Xtst'))
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xtest.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.xtest.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.display = self.x11.XOpenDisplay(None)
        assert self.display

    def move(self, x, y):
        self.xtest.XTestFakeMotionEvent(self.display, -1, int(x), int(y), 0)
        self.x11.XFlush(self.display)
        pump(.08)

    def button(self, button, pressed):
        self.xtest.XTestFakeButtonEvent(self.display, button, int(pressed), 0)
        self.x11.XFlush(self.display)
        pump(.1)

    @staticmethod
    def center(widget):
        x, y = _widget_root_origin(widget)
        return x + widget.get_allocated_width() // 2, y + widget.get_allocated_height() // 2

    def drag(self, source, target):
        start = self.center(source)
        self.move(*start)
        self.button(1, True)
        if not target.get_visible():
            self.move(start[0] + 16, start[1] + 10)
            start = self.center(target.get_toplevel())
            self.move(*start)
            wait_for(target.get_visible, 'cancellation target appears on drag')
            assert target.get_allocated_width() >= 250
        end = self.center(target)
        for step in range(1, 11):
            self.move(start[0] + (end[0]-start[0])*step/10, start[1] + (end[1]-start[1])*step/10)
        self.button(1, False)

    def close(self):
        self.x11.XCloseDisplay(self.display)


def management(pointer, contract):
    with tempfile.TemporaryDirectory(prefix='astraterm-tab-management-') as directory:
        window = MainWindow(SessionStore(os.path.join(directory, 'sessions.json')), start_local=False)
        try:
            window.resize(1100, 740); window.present()
            panes = {}; paths = {}; target = None
            for name in contract['panes'] + ['survivor']:
                paths[name] = os.path.join(directory, name)
                profile = default_session('local')
                profile.update(name=name, _argv=[sys.executable, '-u', '-c', CAPTURE_CHILD, paths[name], name, 'no'])
                if target is not None and name != 'survivor':
                    panes[name] = window._split_ready(profile, workspace=target)
                else:
                    owner = window.open_session(profile)
                    panes[name] = owner.active
                    if target is None: target = owner
            survivor = window._workspace_for_pane(panes['survivor'])
            wait_for(lambda: all(ready(pane, paths[name]) for name,pane in panes.items()), 'management PTYs ready')
            original = {name:(pane.pid,pane.terminal,pane.connection_generation) for name,pane in panes.items()}
            window.multiexec.set_selected(panes['b'], True)
            window.notebook.set_current_page(window.notebook.page_num(target));pump(.2)
            pointer.drag(panes[contract['detached']].heading, window.tab_drop_target)
            wait_for(lambda: window.notebook.get_n_pages()==3, 'pane header drop detaches one tab')
            assert target.panes == [panes[name] for name in contract['remaining']]
            detached = window._workspace_for_pane(panes[contract['detached']])
            assert detached.panes == [panes[contract['detached']]]
            assert window.active_pane() is panes[contract['detached']]
            # Merged tab -> + separates all; the single survivor is untouched.
            window.notebook.set_current_page(window.notebook.page_num(target));pump(.2)
            pointer.drag(target.tab_header, window.tab_drop_target)
            wait_for(lambda: window.notebook.get_n_pages()==4, 'merged header drop separates all')
            assert all(len(window._workspace_for_pane(pane).panes)==1 for pane in panes.values())
            for name,pane in panes.items():
                assert (pane.pid,pane.terminal,pane.connection_generation)==original[name]
                assert 'CAPTURE_READY_'+name in pane.terminal.get_text(lambda *_:True,None)[0].replace('\n','').replace('\r','')
            assert window.multiexec.selected_panes()==[panes['b']]
            floating_owner = window._workspace_for_pane(panes['b'])
            window.notebook.set_current_page(window.notebook.page_num(floating_owner));pump(.2)
            assert window.float_window(floating_owner);pump(.5)
            floating = window.floating_windows[floating_owner]
            assert panes['b'].get_toplevel() is floating.window
            survivor = window._workspace_for_pane(panes['a'])
            window.notebook.set_current_page(window.notebook.page_num(survivor))
            assert panes['b'].terminal.get_realized() and panes['b'].terminal.get_mapped()
            panes['b'].send('floating-input');pump(.2)
            assert b'floating-input' in read_bytes(paths['b'])
            # Real keyboard events in the floating VTE still reach exactly selected peers.
            window.set_tiled_view(True)
            window.multiexec.set_selected(panes['a'], True);window.multiexec.set_enabled(True)
            floating.window.present();panes['b'].terminal.grab_focus();pump(.2)
            assert floating.window.get_focus() is panes['b'].terminal
            assert window.current_workspace() is survivor
            from gi.repository import GdkX11
            pointer.x11.XSetInputFocus.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
            pointer.x11.XSetInputFocus(pointer.display,GdkX11.X11Window.get_xid(floating.window.get_window()),1,0)
            pointer.x11.XKeysymToKeycode.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
            pointer.x11.XKeysymToKeycode.restype=ctypes.c_uint
            pointer.xtest.XTestFakeKeyEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
            code=pointer.x11.XKeysymToKeycode(pointer.display,ord('p'))
            pointer.xtest.XTestFakeKeyEvent(pointer.display,code,1,0);pointer.xtest.XTestFakeKeyEvent(pointer.display,code,0,0);pointer.x11.XFlush(pointer.display)
            wait_for(lambda: all(read_bytes(paths[name]).endswith(b'p') for name in ['a','b']), 'native floating MultiExec keyboard')
            assert not read_bytes(paths['c']).endswith(b'p')
            window.multiexec.set_enabled(False);window.multiexec.set_selected(panes['a'],False)
            window.set_tiled_view(False)
            floating.window.close();pump(.5)
            assert floating_owner not in window.floating_windows
            assert panes['b'].get_toplevel() is window
            assert (panes['b'].pid,panes['b'].terminal,panes['b'].connection_generation)==original['b']
            assert 'CAPTURE_READY_b' in panes['b'].terminal.get_text(lambda *_:True,None)[0].replace('\n','').replace('\r','')
            assert window.multiexec.selected_panes()==[panes['b']]
            print('PASS floating window / live PTY and history / native close returns to tab / recipients preserved',flush=True)
            # Native right click on an inactive tab must bind actions to that tab.
            window.notebook.set_current_page(window.notebook.page_num(survivor));pump(.2)
            window.tab_strip.reveal(target.tab_header);pump(.1)
            pointer.move(*pointer.center(target.tab_header));pointer.button(3,True);pointer.button(3,False)
            menu = window._tab_context_menu
            assert [item.get_label() for item in menu.get_children()]==contract['menu_plain']
            menu.popdown();pump(.1)
            for name in ['b','c']:
                window.merge_tabs(window._workspace_for_pane(panes[name]),target)
            window.notebook.set_current_page(window.notebook.page_num(survivor));pump(.2)
            menu=window.tab_context_menu(target)
            assert [item.get_label() for item in menu.get_children()]==contract['menu_merged']
            menu.get_children()[-1].activate();pump(.2)
            assert window.notebook.get_n_pages()==4 and window.active_pane() is panes['survivor']
            for name in ['b','c']:
                window.merge_tabs(window._workspace_for_pane(panes[name]),target)
            window.notebook.set_current_page(window.notebook.page_num(survivor));pump(.2)
            with mock.patch('astraterm.app.confirm', return_value=False) as confirm:
                window.tab_context_menu(target).get_children()[4].activate()
                assert confirm.call_count==1 and window.notebook.get_n_pages()==2
                window.tab_context_menu(target).get_children()[3].activate()
                assert confirm.call_count==2 and window.notebook.get_n_pages()==2
            with mock.patch('astraterm.app.confirm', return_value=True) as confirm:
                window.tab_context_menu(target).get_children()[4].activate()
                assert confirm.call_count==1 and window.notebook.get_n_pages()==1
                assert panes['survivor'].closed and not panes['a'].closed
                assert not window.tab_action('close-all',survivor)
                window.tab_context_menu(target).get_children()[2].activate()
                assert confirm.call_count==2 and window.notebook.get_n_pages()==0
            extra = window.open_session(dict(panes['a'].profile))
            wait_for(lambda: extra.active.pid is not None, 'new tab for close-all')
            with mock.patch('astraterm.app.confirm', return_value=True) as confirm:
                window.tab_context_menu(extra).get_children()[3].activate()
                assert confirm.call_count==1 and window.notebook.get_n_pages()==0
            wait_for(lambda: all(not os.path.exists('/proc/'+str(row[0])) for row in original.values()), 'management children released')
            print('PASS native pane/tab detach, context actions on inactive tab, bulk cancellation and PTY lifecycle',flush=True)
        finally:
            window.shutdown();window.destroy();pump(.1)


def merged_floating(pointer, contract):
    with tempfile.TemporaryDirectory(prefix='astraterm-merged-floating-') as directory:
        window = MainWindow(SessionStore(os.path.join(directory, 'sessions.json')), start_local=False)
        try:
            window.toggle_sidebar()
            window.toggle_files()
            window.resize(*contract['sizes'][0])
            window.present()
            workspaces = []
            paths = []
            for name in contract['panes']:
                path = os.path.join(directory, name)
                profile = default_session('local')
                profile.update(name=name, _argv=[sys.executable, '-u', '-c', CAPTURE_CHILD, path, name, 'no'])
                workspaces.append(window.open_session(profile))
                paths.append(path)
            target = workspaces[0]
            panes = [workspace.active for workspace in workspaces]
            wait_for(lambda: all(ready(pane, path) for pane, path in zip(panes, paths)), 'four local PTYs ready')
            original = [(pane.pid, pane.terminal, pane.connection_generation) for pane in panes]
            window.multiexec.set_selected(panes[1], True)
            for step, source in enumerate(workspaces[1:], 1):
                window.notebook.set_current_page(window.notebook.page_num(source))
                pump(.12)
                pointer.drag(source.tab_header, target.tab_header)
                wait_for(lambda: window.notebook.get_n_pages() == len(panes) - step, 'native drop onto first tab')
            assert target.panes == panes

            def assert_layout(container, toplevel):
                width, height = container.get_allocated_width(), container.get_allocated_height()
                root = container.get_children()[0]
                assert (root.get_allocated_width(), root.get_allocated_height()) == (width, height)
                widths = [pane.get_allocated_width() for pane in panes]
                assert max(widths) - min(widths) <= contract['width_tolerance'], widths
                end = 0
                for pane, pane_width in zip(panes, widths):
                    x, y = pane.translate_coordinates(container, 0, 0)
                    assert end <= x <= end + 8 and y == 0, (end, x, y)
                    assert pane.get_allocated_height() == height
                    assert pane.terminal.get_mapped() and pane.heading.get_mapped()
                    assert pane.terminal.get_window().get_toplevel() == toplevel.get_window()
                    end = x + pane_width
                assert end == width

            assert_layout(target, window)
            pointer.move(*pointer.center(target.float_button))
            pointer.button(1, True)
            pointer.button(1, False)
            wait_for(lambda: target in window.floating_windows, 'float button opens all four terminals')
            floating = window.floating_windows[target]
            for size in contract['sizes']:
                floating.window.resize(*size)
                pump(.25)
                assert_layout(floating.body, floating.window)
            for pane, path in zip(panes, paths):
                pointer.move(*pointer.center(pane.terminal))
                pointer.button(1, True)
                pointer.button(1, False)
                assert floating.window.get_focus() is pane.terminal
                pane.send('merged-floating-input')
                wait_for(lambda: b'merged-floating-input' in read_bytes(path), 'floating PTY still accepts input')
            floating.window.close()
            pump(.5)
            assert target not in window.floating_windows
            assert_layout(target, window)
            assert original == [(pane.pid, pane.terminal, pane.connection_generation) for pane in panes]
            assert window.multiexec.selected_panes() == [panes[1]]
            for name, pane in zip(contract['panes'], panes):
                assert 'CAPTURE_READY_'+name in pane.terminal.get_text(lambda *_: True, None)[0].replace('\n', '').replace('\r', '')
            print('PASS four equal terminal panes / float, resize and dock / live PTYs and history', flush=True)
        finally:
            window.shutdown()
            window.destroy()
            pump(.1)


def main():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'shared/contracts/tabs.json'), encoding='utf-8') as stream:
        contracts = json.load(stream)
    pointer = Pointer()
    try:
        merged_floating(pointer, contracts['floating']['merged'])
        for case in contracts['merges']:
            with tempfile.TemporaryDirectory(prefix='astraterm-tabs-') as directory:
                window = MainWindow(SessionStore(os.path.join(directory, 'sessions.json')), start_local=False)
                try:
                    assert window.main_menu_button.get_parent().get_children()[0] is window.main_menu_button
                    window.resize(1100, 740)
                    window.present()
                    paths = {}; panes = {}; owners = {}
                    for side in ('target', 'source'):
                        owner = None
                        for name in case[side]:
                            path = os.path.join(directory, name)
                            profile = default_session('local')
                            profile.update(name=name, _argv=[sys.executable, '-u', '-c', CAPTURE_CHILD, path, name, 'no'])
                            pane = window.open_session(profile).active if owner is None else window._split_ready(profile, workspace=owner)
                            owner = window._workspace_for_pane(pane)
                            paths[name], panes[name] = path, pane
                        owners[side] = owner
                    survivor_path = os.path.join(directory, 'survivor')
                    survivor_profile = default_session('local')
                    survivor_profile.update(name='survivor', _argv=[sys.executable, '-u', '-c', CAPTURE_CHILD, survivor_path, 'survivor', 'no'])
                    survivor = window.open_session(survivor_profile)
                    wait_for(lambda: ready(survivor.active, survivor_path), 'unrelated tab ready')
                    wait_for(lambda: all(ready(pane, paths[name]) for name, pane in panes.items()), 'tab PTYs ready')
                    window.notebook.set_current_page(window.notebook.page_num(owners['source']))
                    pump(.2)
                    original = {name: (pane.pid, pane.terminal, pane.connection_generation) for name,pane in panes.items()}
                    if case is contracts['merges'][0]:
                        selection = contracts['rangeSelection']
                        window._set_appearance(window.theme, True)
                        terminal = owners['source'].active.terminal
                        terminal.feed(("\r\x1b[2J\x1b[H" + selection['text']).encode('ascii'))
                        pump(.15)
                        origin = _widget_root_origin(terminal)
                        padding = terminal.get_style_context().get_padding(terminal.get_state_flags())
                        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
                        for col in selection['columns']:
                            pointer.move(origin[0] + padding.left + col * terminal.get_char_width() + 1,
                                         origin[1] + padding.top + terminal.get_char_height() // 2)
                            pointer.button(2, True)
                            pointer.button(2, False)
                        assert clipboard.wait_for_text() == selection['expected'], repr(clipboard.wait_for_text())
                        system_clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                        assert system_clipboard.wait_for_text() == selection['expected']
                        system_clipboard.set_text('ASTRA_RIGHT_CLICK_PASTE', -1)
                        pointer.button(3, True)
                        pointer.button(3, False)
                        wait_for(lambda: b'ASTRA_RIGHT_CLICK_PASTE' in read_bytes(paths[case['source'][0]]),
                                 'right click pastes the system clipboard')
                        window._set_appearance(window.theme, False)
                        terminal.feed(("CAPTURE_READY_" + case['source'][0]).encode('ascii'))
                        pointer.drag(owners['source'].tab_header, window.cancel_drop)
                        assert not window.cancel_drop.get_visible()
                        assert window.notebook.get_n_pages() == 3
                        assert not window._dragged_workspace
                        window.notify('Тестовое уведомление')
                        window.notifications.get_children()[0].get_children()[-1].clicked()
                        assert not window.notifications.get_children()
                    chosen = panes[case['source'][0]]
                    window.multiexec.set_selected(chosen, True)
                    with mock.patch('astraterm.app.confirm', side_effect=AssertionError('Unexpected warning')):
                        pointer.drag(owners['source'].tab_header, owners['target'].tab_header)
                        wait_for(lambda: window.notebook.get_n_pages() == 2, 'native tab drop merged two tabs')
                        target = owners['target']
                        assert target.panes == [panes[name] for name in case['expected']]
                        assert target.active is panes[case['source'][-1]]
                        assert window.multiexec.selected_panes() == [chosen]
                        for name,pane in panes.items():
                            assert (pane.pid,pane.terminal,pane.connection_generation) == original[name]
                            assert 'CAPTURE_READY_'+name in pane.terminal.get_text(lambda *_:True,None)[0].replace('\n','').replace('\r','')
                            assert not pane.closed
                        for rejected in contracts['rejected']:
                            source,destination = ((target,target) if rejected=='same-tab' else
                                                  (owners['source'],target) if rejected=='closed-source' else
                                                  (target,owners['source']))
                            assert not window.merge_tabs(source,destination)
                        window.notebook.set_current_page(window.notebook.page_num(survivor))
                        pump(.2)
                        if len(case['source']) == 1:
                            x, y = _widget_root_origin(target.tab_header)
                            pointer.move(x+2, y+target.tab_header.get_allocated_height()//2)
                        else:
                            pointer.move(*pointer.center(target.tab_header.get_child().get_children()[-1]))
                        pointer.button(2, True)
                        pointer.button(2, False)
                        wait_for(lambda: window.notebook.get_n_pages()==1, 'middle close entire split tab')
                        assert window.current_workspace() is survivor and not survivor.active.closed
                        assert all(pane.closed and pane._drain_fd is None for pane in panes.values())
                        wait_for(lambda: all(not os.path.exists('/proc/'+str(record[0])) for record in original.values()), 'closed tab children released')
                        assert not window.multiexec.selected_panes()
                    # A regular close still asks; refusing leaves the terminal alive.
                    with mock.patch('astraterm.app.confirm', return_value=False) as confirm:
                        window.close_tab(workspace=survivor)
                        assert confirm.called and window.notebook.get_n_pages()==1
                    print('PASS native tab drag / PTY and history / middle close without warning: '+case['name'], flush=True)
                finally:
                    window.shutdown();window.destroy();pump(.1)
        management(pointer, contracts['separate'])
    finally:
        pointer.close()
    print('TABS_SMOKE_OK',flush=True)


if __name__ == '__main__':
    if '--pointer-display-child' in sys.argv:
        main()
    else:
        # XTest events must never hit another application on the user's desktop.
        # Always give this check its own X server, even when the outer smoke
        # suite uses a shared DISPLAY.
        if not shutil.which('Xephyr'):
            raise RuntimeError('Native tab gesture checks require Xephyr (xserver-xephyr)')
        with tempfile.TemporaryDirectory(prefix='astraterm-tab-display-') as directory:
            display = next(number for number in range(90, 200) if not os.path.exists('/tmp/.X11-unix/X'+str(number)))
            with open(os.path.join(directory, 'xephyr.log'), 'wb') as log:
                server = subprocess.Popen(['Xephyr', ':'+str(display), '-screen', '1360x900', '-nolisten', 'tcp', '-ac'], stdout=log, stderr=log)
                try:
                    for attempt in range(100):
                        if server.poll() is not None:
                            raise RuntimeError('Could not start the private tab test display')
                        if os.path.exists('/tmp/.X11-unix/X'+str(display)):
                            break
                        time.sleep(.05)
                    else:
                        raise RuntimeError('Private tab test display did not start')
                    code = subprocess.call([sys.executable, __file__, '--pointer-display-child'],
                                           env=dict(os.environ, DISPLAY=':'+str(display)))
                finally:
                    if server.poll() is None:
                        server.terminate()
                        try: server.wait(timeout=5)
                        except subprocess.TimeoutExpired: server.kill(); server.wait()
                sys.exit(code)

