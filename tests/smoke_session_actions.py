"""Native duplicate/reconnect scope and newest saved settings, with real owned PTYs."""
import json
import os,sys,tempfile
from pathlib import Path
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk','3.0')
gi.require_version('Gdk','3.0')
from gi.repository import Gdk, Gtk, GLib
from astraterm.app import MainWindow
from astraterm.core import SessionStore,default_session
from smoke_multiexec import key_event,pump,wait_for


def check_startup():
    contract = json.loads((Path(__file__).resolve().parents[1] / 'shared/contracts/tabs.json').read_text(encoding='utf-8'))
    fixture = contract['startup']
    with tempfile.TemporaryDirectory(prefix='astraterm-workspace-') as directory:
        store = SessionStore(os.path.join(directory, 'sessions.json'))
        profile = dict(default_session('local'), id=fixture['saved_id'], name=fixture['initial_name'])
        store.upsert(profile)
        window = MainWindow(store)
        try:
            window.present()
            assert bool(window.notebook.get_n_pages()) == fixture['local_default']
            assert window.remember_tabs == fixture['remember_default']
            row = window._session_rows[profile['id']]
            assert window.session_tree_store.get_value(row, 6) is None
            first_tab = window.open_session(profile)
            first = first_tab.active
            wait_for(lambda: bool(first.pid), 'saved local terminal runs')
            live_dot = window.session_tree_store.get_value(row, 6)
            assert live_dot and live_dot == first_tab.tab_indicator.get_pixbuf()
            first.terminal.event(key_event(first.terminal, Gdk.KEY_d, Gdk.ModifierType.CONTROL_MASK))
            wait_for(lambda: first.exited and not first.pid, 'Ctrl+D closes the shell')
            inactive_dot = window.session_tree_store.get_value(row, 6)
            assert inactive_dot is not None and inactive_dot != live_dot
            first.terminal.event(key_event(first.terminal, Gdk.keyval_from_name(contract['ended_session']['restart_key'])))
            wait_for(lambda: bool(first.pid), 'R reconnects the ended shell')
            assert window.session_tree_store.get_value(row, 6) == live_dot
            first.send('exit\n')
            wait_for(lambda: first.exited and not first.pid, 'reopened shell can exit')
            first.terminal.event(key_event(first.terminal, Gdk.keyval_from_name(contract['ended_session']['close_key'])))
            wait_for(lambda: window.notebook.get_n_pages() == 0, 'other key closes the disconnected tab')
            assert window.session_tree_store.get_value(row, 6) is None

            first_tab = window.open_session(profile)
            second = window._split_ready(dict(default_session('local'), name=fixture['second_pane']),
                                         Gtk.Orientation.VERTICAL, first_tab)
            last = window.open_session(dict(default_session('local'), name=fixture['last_tab']))
            pump(.25)
            window.notebook.set_current_page(window.notebook.page_num(first_tab))
            first_tab.active = second

            def set_remember():
                dialog = next(widget for widget in Gtk.Window.list_toplevels()
                              if isinstance(widget, Gtk.Dialog) and widget.get_title() == 'Настройки')
                remember = next(widget for widget in dialog.get_content_area().get_children()
                                if isinstance(widget, Gtk.CheckButton) and widget.get_label().startswith('Запоминать'))
                remember.set_active(True)
                dialog.response(Gtk.ResponseType.OK)
                return False

            GLib.idle_add(set_remember)
            window.settings()
            assert window.remember_tabs
            # Snapshot while the workspace is tiled, then load through normal startup.
            window.set_tiled_view(True)
            window._pane_focus(second)
        finally:
            window.shutdown()
            window.destroy()
            pump(.2)
        store.upsert(dict(profile, name=fixture['updated_name']))
        reopened = MainWindow(store)
        try:
            reopened.present()
            wait_for(lambda: reopened.notebook.get_n_pages() == 2 and len(reopened.all_panes()) == 3,
                     'saved tabs and split restored')
            wait_for(lambda: reopened.active_pane().profile['name'] == fixture['second_pane'], 'active tab and pane restored')
            first_tab, last = [reopened.notebook.get_nth_page(i) for i in range(2)]
            assert [pane.profile['name'] for pane in first_tab.panes] == [fixture['updated_name'], fixture['second_pane']]
            assert first_tab.get_children()[0].get_orientation() == Gtk.Orientation.VERTICAL
            assert last.active.profile['name'] == fixture['last_tab']
            close = next(button for button in last.tab_header.get_child().get_children()
                         if button.get_tooltip_text() == 'Закрыть вкладку')
            with mock.patch('astraterm.app.confirm', side_effect=AssertionError('tab close prompted')):
                close.clicked()
            assert reopened.notebook.get_n_pages() == 1
        finally:
            reopened.shutdown()
            reopened.destroy()
            pump(.2)
    print('STARTUP_TABS_SMOKE_OK empty start, exit keys, live indicators, saved split and direct close', flush=True)

def main():
    with tempfile.TemporaryDirectory(prefix='astraterm-session-actions-') as directory:
        store=SessionStore(os.path.join(directory,'sessions.json'));window=MainWindow(store,start_local=False)
        try:
            window.resize(1100,740);window.present()
            profile=default_session('local');profile.update(name='A',_argv=[sys.executable,'-u','-c','import time;print("READY_A",flush=True);time.sleep(300)'])
            target=window.open_session(profile);first=target.active
            profile=dict(profile,name='B',id='fixture-b');second=window._split_ready(profile,Gtk.Orientation.VERTICAL,target)
            survivor=window.open_session(dict(profile,name='Survivor',id='fixture-survivor')).active
            wait_for(lambda:all(p.pid for p in [first,second,survivor]),'Initial PTYs')
            original=[first.pid,second.pid,survivor.pid];terminals=[first.terminal,second.terminal];window.multiexec.set_selected(first,True)
            window.float_window(target);pump(.2)
            assert window.tab_action('duplicate',target)
            wait_for(lambda:window.notebook.get_n_pages()==3 and len(window.all_panes())==5,'Duplicate entire split tab')
            copies=[p for p in window.all_panes() if p not in [first,second,survivor]]
            wait_for(lambda:all(p.pid for p in copies),'New independent duplicate PTYs')
            assert all(p.pid not in original for p in copies)
            owner=window._workspace_for_pane(copies[0]);assert owner is window._workspace_for_pane(copies[1])
            assert owner.get_children()[0].get_orientation()==Gtk.Orientation.VERTICAL
            assert window.multiexec.selected_panes()==[first]
            assert [p.pid for p in [first,second,survivor]]==original
            assert target in window.floating_windows
            window.dock_window(target)
            window.close_tab(workspace=owner,ask=False);pump(.2)
            changed=default_session('local');changed.update(id=first.profile['id'],name='Changed saved session',startup_directory=directory)
            store.upsert(changed)
            window.notebook.set_current_page(window.notebook.page_num(window._workspace_for_pane(survivor)))
            with mock.patch('astraterm.app.confirm',return_value=True) as confirmation:assert window.tab_action('reconnect',target);assert confirmation.call_count==1
            wait_for(lambda:first.pid and second.pid and first.pid!=original[0] and second.pid!=original[1],'Reconnect clicked split tab')
            assert survivor.pid==original[2] and first.terminal is terminals[0] and second.terminal is terminals[1]
            assert first.profile['startup_directory']==directory and first.profile['name']=='Changed saved session'
            assert window.multiexec.selected_panes()==[]
            print('SESSION_ACTIONS_SMOKE_OK duplicate tree/new PTYs/unselected / reconnect clicked tab/latest saved settings/other tab retained',flush=True)
        finally:window.shutdown();window.destroy();pump(.2)

if __name__=='__main__':
    main()
    check_startup()
