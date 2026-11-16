"""Native duplicate/reconnect scope and newest saved settings, with real owned PTYs."""
import os,sys,tempfile
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk
from astraterm.app import MainWindow
from astraterm.core import SessionStore,default_session
from smoke_multiexec import pump,wait_for

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

if __name__=='__main__':main()
