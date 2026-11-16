"""Native GTK host status and unified typed credential management; isolated HOME."""
from __future__ import print_function,unicode_literals
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,GLib,Gdk
from astraterm.app import MainWindow
from astraterm.core import SessionStore,default_session
from astraterm.dialogs import SessionDialog
from astraterm.credential_dialogs import CredentialManager
from smoke_multiexec import pump,wait_for

def main():
    with tempfile.TemporaryDirectory(prefix='astraterm-status-gui-') as directory:
        with mock.patch.dict(os.environ,{'HOME':directory}):
            window=MainWindow(SessionStore(Path(directory)/'sessions.json'),start_local=True,auto_sftp=False)
            try:
                window.resize(1024,690);window.present()
                wait_for(lambda:window.host_stats.labels['hostname'].get_text() not in ('','—'),'Local hostname')
                wait_for(lambda:'%' in window.host_stats.labels['cpu'].get_text(),'Sampled local CPU',timeout=12)
                assert window.host_stats.get_visible() and ' / ' in window.host_stats.labels['memory'].get_text()
                assert '/' in window.host_stats.labels['disks'].get_text()
                screenshot=os.environ.get('ASTRA_STATUS_SCREENSHOT')
                if screenshot:
                    drawable=window.get_window();size=window.get_size();Gdk.pixbuf_get_from_window(drawable,0,0,*size).savev(screenshot,'png',[],[])
                vault=window.credential_vault;vault.unlock('TEST_ONLY_status_master',create=True)
                vault.merge([{'id':'ssh-test','name':'SSH fixture','username':'tester','password':'TEST_ONLY_ssh','protocol':'ssh'},
                             {'id':'rdp-test','name':'RDP fixture','username':'tester','password':'TEST_ONLY_rdp','protocol':'rdp'}])
                manager=CredentialManager(window,window.credentials);pump(.1)
                assert len(manager.model)==2 and set(row[3] for row in manager.model)=={'SSH · пароль','RDP · пароль'}
                assert all(row[5] for row in manager.model)
                # Drive the real masked credential form; use synthetic secrets only.
                def fill():
                    for dialog in Gtk.Window.list_toplevels():
                        if isinstance(dialog,Gtk.Dialog) and dialog.get_title()=='Новый профиль':
                            grid=dialog.get_content_area().get_children()[0]
                            grid.get_child_at(1,0).set_active_id('rdp')
                            grid.get_child_at(1,2).set_text('New RDP fixture')
                            grid.get_child_at(1,3).set_text('tester')
                            secret=grid.get_child_at(1,4);assert not secret.get_visibility();secret.set_text('TEST_ONLY_created')
                            dialog.response(Gtk.ResponseType.OK);return False
                    return True
                GLib.timeout_add(40,fill);manager.add();pump(.2)
                assert len(vault.metadata())==3
                assert any(p['name']=='New RDP fixture' and p['protocol']=='rdp' for p in vault.metadata())
                assert 'TEST_ONLY_created' not in Path(vault.path).read_text()
                profile=default_session('rdp');profile.update(host='192.0.2.1',credential_profile='rdp-test')
                dialog=SessionDialog(window,profile,credential_profiles=vault.metadata())
                assert dialog.get_session()['credential_profile']=='rdp-test'
                assert 'ssh-test' not in [row[1] for row in dialog.fields['credential_profile'].get_model()]
                dialog.fields['protocol'].set_active_id('ssh')
                assert not dialog.get_session()['credential_profile'];dialog.destroy();manager.destroy()
                assert window.credentials.manager is None
                print('STATUS_SMOKE_OK local counters / sampled CPU / mounts / typed vault form / protocol selector / no plaintext',flush=True)
            finally:window.shutdown();window.destroy();pump(.2)

if __name__=='__main__':main()
