"""Native imported-password SSH/SFTP with synthetic accounts and isolated HOME."""
from __future__ import unicode_literals, print_function
import json
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paramiko
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,Gdk,GLib
from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session, session_argv
from astraterm.importers import parse_import
from astraterm.dialogs import SessionDialog
from astraterm.vault import CredentialVault
from smoke_multiexec import pump, wait_for
from smoke_tabs import Pointer
from smoke_groups import KEYBOARD, rename_with_f2

DRAG_BYTES = b'SFTP copy to the local file manager\x00\xff'


class Server(paramiko.ServerInterface):
    def __init__(self,fixture):self.fixture=fixture;self.shell=threading.Event();self.password_accepted=False
    def get_allowed_auths(self,username):
        return 'keyboard-interactive' if self.fixture.factor and self.password_accepted else 'password'
    def check_auth_password(self,username,password):
        if username==self.fixture.username and password==self.fixture.password:
            if self.fixture.factor:
                self.password_accepted=True
                return paramiko.AUTH_PARTIALLY_SUCCESSFUL
            self.fixture.authenticated+=1;return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED
    def check_auth_interactive(self,username,submethods):
        if self.password_accepted and username==self.fixture.username:
            factor=self.fixture.factor
            return paramiko.InteractiveQuery('SSH', factor['factor_instruction'], (factor['factor_prompt'], False))
        return paramiko.AUTH_FAILED
    def check_auth_interactive_response(self,responses):
        if self.password_accepted and responses == [self.fixture.factor['factor_response']]:
            self.fixture.authenticated+=1
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED
    def check_channel_request(self,kind,channel):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_pty_request(self,*args):return True
    def check_channel_shell_request(self,channel):self.shell.set();return True


class SFTP(paramiko.SFTPServerInterface):
    def __init__(self, server):
        super(SFTP, self).__init__(server)
        self.entries = {"/fixture.txt": (0o100640, len(DRAG_BYTES)), "/z-small.txt": (0o100600, 1)}

    def canonicalize(self,path):return '/'
    def list_folder(self,path):
        return [self.stat(path) for path in self.entries]
    def stat(self,path):
        item=paramiko.SFTPAttributes()
        if path == '/':
            item.st_mode=0o40700;item.st_size=0
            return item
        if path in self.entries:
            item.filename = path[1:]
            item.st_mode, item.st_size = self.entries[path]
            item.st_mtime = item.st_atime = 1700000000
            return item
        return paramiko.SFTP_NO_SUCH_FILE
    lstat = stat

    def mkdir(self, path, attr):
        self.entries[path] = (0o40700, 0)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        if oldpath not in self.entries or newpath in self.entries:
            return paramiko.SFTP_FAILURE
        if oldpath.startswith('/.astraterm-upload-'):
            time.sleep(.7)
        self.entries[newpath] = self.entries.pop(oldpath)
        return paramiko.SFTP_OK

    def remove(self, path):
        return paramiko.SFTP_OK if self.entries.pop(path, None) is not None else paramiko.SFTP_NO_SUCH_FILE

    def chattr(self, path, attr):
        if path not in self.entries or attr.st_mode is None:
            return paramiko.SFTP_FAILURE
        old, size = self.entries[path]
        self.entries[path] = ((old & ~0o7777) | (attr.st_mode & 0o7777), size)
        return paramiko.SFTP_OK

    def open(self, path, flags, attr):
        if path.startswith('/.astraterm-upload-') and flags & os.O_WRONLY:
            handle = paramiko.SFTPHandle(flags)
            handle.writefile = io.BytesIO()

            def close():
                self.entries[path] = (0o100600, len(handle.writefile.getvalue()))
                paramiko.SFTPHandle.close(handle)

            handle.close = close
            return handle
        if path != '/fixture.txt' or flags & (os.O_WRONLY | os.O_RDWR):
            return paramiko.SFTP_PERMISSION_DENIED
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = io.BytesIO(DRAG_BYTES)
        return handle


def check_drag_export(browser, destination):
    tree = browser.tree
    assert all(column.get_visible() for column in browser.metadata_columns)
    assert browser.model[0][7] == 'rw-r-----' and browser.model[0][8] == '—' and browser.model[0][3] != '—'
    browser._sort_columns['size'].clicked()
    assert [row[1] for row in browser.model] == ['z-small.txt', 'fixture.txt']
    browser._sort_columns['size'].clicked()
    assert [row[1] for row in browser.model] == ['fixture.txt', 'z-small.txt']
    target = Gtk.Window(type=Gtk.WindowType.POPUP)
    target.move(20, 50)
    target.set_default_size(140, 85)
    label = Gtk.Label(label='Local fixture')
    target.add(label)
    label.drag_dest_set(Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new('text/uri-list', 0, 0)], Gdk.DragAction.COPY)

    def receive(_label, context, _x, _y, selection, _info, timestamp):
        local, _host = GLib.filename_from_uri(selection.get_uris()[0])
        shutil.copyfile(local, str(destination))
        Gtk.drag_finish(context, True, False, timestamp)
    label.connect('drag-data-received', receive)
    target.show_all()
    pump(.2)
    pointer = Pointer()
    try:
        rect = tree.get_cell_area(Gtk.TreePath.new_from_string('0'), tree.get_column(0))
        origin = tree.get_bin_window().get_origin()
        start = (origin[-2] + rect.x + 36, origin[-1] + rect.y + rect.height // 2)
        pointer.move(*start)
        pointer.button(1, True)
        pointer.move(start[0] - 16, start[1])
        wait_for(lambda: bool(browser._drag_file), 'native GTK drag starts after SFTP preparation')
        pointer.move(*Pointer.center(label))
        pointer.button(1, False)
        wait_for(destination.exists, 'local GTK target receives the exported file')
        assert destination.read_bytes() == DRAG_BYTES
    finally:
        pointer.button(1, False)
        pointer.close()
        target.destroy()
    print('PASS SFTP permissions, dates and native GTK drag to local file', flush=True)


class Fixture(object):
    def __init__(self,factor=None):
        self.factor=factor
        self.username=factor['username'] if factor else 'ops'
        self.password=factor['password'] if factor else 'TEST_ONLY_imported_account=пароль'
        self.authenticated=0;self.clients=[];self.closed=False
        self.key=paramiko.RSAKey.generate(2048)
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen(8);self.socket.settimeout(.2)
        self.port=self.socket.getsockname()[1]
        thread=threading.Thread(target=self.accept);thread.daemon=True;thread.start()
    def accept(self):
        while not self.closed:
            try:stream,unused=self.socket.accept()
            except socket.timeout:continue
            except OSError:return
            thread=threading.Thread(target=self.serve,args=(stream,));thread.daemon=True;thread.start()
    def serve(self,stream):
        transport=paramiko.Transport(stream);self.clients.append(transport)
        try:
            transport.add_server_key(self.key);transport.set_subsystem_handler('sftp',paramiko.SFTPServer,SFTP)
            server=Server(self);transport.start_server(server=server)
            channel=transport.accept(10)
            if channel is None:return
            while transport.is_active() and not self.closed:
                if server.shell.is_set():
                    channel.send(b'IMPORT_SSH_READY\r\n')
                    channel.settimeout(.2)
                    while transport.is_active() and not self.closed:
                        try:data=channel.recv(4096)
                        except socket.timeout:continue
                        if not data:break
                        channel.send(data)
                    break
                time.sleep(.03)
        except (OSError,EOFError,paramiko.SSHException):pass
        finally:transport.close()
    def close(self):
        self.closed=True;self.socket.close()
        for client in self.clients:client.close()


def check_interactive_auth(root):
    contract_path = Path(__file__).resolve().parents[1] / 'shared/contracts/ssh-interactive.json'
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    fixture = Fixture(factor=contract)
    window = MainWindow(SessionStore(root/'mfa/sessions.json'), start_local=False, auto_sftp=False)
    try:
        known = root/'mfa-known-hosts'
        known.write_text('[127.0.0.1]:{} {} {}\n'.format(fixture.port, fixture.key.get_name(), fixture.key.get_base64()),
                         encoding='utf-8')
        vault = window.credential_vault
        vault.unlock('TEST_ONLY_master_password', create=True)
        vault.merge([{'id': 'mfa-fixture', 'name': 'SSH interactive fixture', 'username': '',
                      'password': fixture.password, 'protocol': 'ssh'}])
        profile = default_session('ssh')
        profile.update(host='127.0.0.1', port=fixture.port, credential_profile='mfa-fixture')
        window.present()
        answered = []

        def answer_factor():
            for dialog in Gtk.Window.list_toplevels():
                if not isinstance(dialog, Gtk.Dialog) or dialog.get_title() != 'Аутентификация SSH · 127.0.0.1':
                    continue
                controls = dialog.get_content_area().get_children()
                entry = next(item for item in controls if isinstance(item, Gtk.Entry))
                label = next(item for item in controls if isinstance(item, Gtk.Label))
                answered.append((label.get_text().strip(), entry.get_visibility()))
                entry.set_text(contract['factor_response'])
                dialog.response(Gtk.ResponseType.OK)
                return False
            return True

        def isolated_argv(profile):
            argv = session_argv(profile)
            return argv[:1] + ['-F', '/dev/null', '-o', 'UserKnownHostsFile=' + str(known), '-o',
                               'GlobalKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=yes'] + argv[1:]

        timer = GLib.timeout_add(50, answer_factor)
        try:
            with mock.patch('astraterm.app.name_dialog', return_value=contract['username']) as username_dialog, \
                    mock.patch('astraterm.terminal.session_argv', side_effect=isolated_argv):
                window.open_session(profile)
                wait_for(lambda: fixture.authenticated == 1, 'OpenSSH partial password and interactive second factor', timeout=15)
            assert username_dialog.call_count == 1
            assert answered == [(contract['factor_prompt'], False)]
        finally:
            if not answered:
                GLib.source_remove(timer)
        print('PASS native OpenSSH prompts for a missing username and for the second factor after a vault password', flush=True)
    finally:
        window.shutdown()
        window.destroy()
        fixture.close()


def main():
    with tempfile.TemporaryDirectory(prefix='astraterm-import-gui-') as directory:
        previous_home=os.environ.get('HOME');os.environ['HOME']=directory
        fixture=Fixture();window=None
        try:
            root=Path(directory);(root/'.ssh').mkdir(mode=0o700)
            known=root/'.ssh/known_hosts'
            known.write_text('[127.0.0.1]:{} {} {}\n'.format(fixture.port,fixture.key.get_name(),fixture.key.get_base64()),encoding='utf-8')
            fields=['']*37
            for i,v in {0:'0',1:'127.0.0.1',2:str(fixture.port),3:'[Shared]',5:'0',6:'-1',19:'0'}.items():fields[i]=v
            sessions=root/'sessions.mxtsessions';sessions.write_text('[Bookmarks]\nSubRep=Lab\nImgNum=41\nImported=#109#'+'%'.join(fields)+'#font#0# #-1\n',encoding='utf-8')
            passwords=root/'passwords.txt';passwords.write_text('Shared(ops) = '+fixture.password+'\n',encoding='utf-8')
            plan=parse_import([sessions,passwords]);store=SessionStore(root/'app/sessions.json');store.import_document(plan.document())
            window=MainWindow(store,start_local=False,auto_sftp=True);window.present()
            vault=window.credential_vault;vault.unlock('TEST_ONLY_master_password',create=True);vault.merge(list(plan.credentials.values()))
            assert fixture.password not in (root/'app/sessions.json').read_text()
            assert fixture.password not in Path(vault.path).read_text()
            # The real UI selector preserves the imported reference while locked.
            dialog=SessionDialog(window,store.sessions[0],session_store=store,credential_profiles=vault.metadata())
            assert dialog.get_session()['credential_profile']==store.sessions[0]['credential_profile'];dialog.destroy()
            profile=dict(store.sessions[0]);profile['_argv']=['ssh','-F','/dev/null','-o','UserKnownHostsFile='+str(known),'-o','GlobalKnownHostsFile=/dev/null','-o','StrictHostKeyChecking=ask','-p',str(fixture.port),'-l','ops','-tt','--','127.0.0.1']
            window.open_session(profile)
            wait_for(lambda:window.active_pane() is not None,'imported terminal opened')
            pane=window.active_pane()
            wait_for(lambda:'IMPORT_SSH_READY' in pane.terminal.get_text(lambda *_:True,None)[0],'OpenSSH 7.4 receives vault password via private askpass',timeout=25)
            wait_for(lambda:window.file_browser._sftp is not None,'SFTP automatically uses vault password',timeout=25)
            wait_for(lambda:len(window.file_browser.model)>0,'SFTP directory rendered')
            assert pane.profile['username']=='ops' and fixture.authenticated==2
            assert fixture.password not in str(pane.profile)
            check_drag_export(window.file_browser, root/'local-copy.txt')
            browser = window.file_browser
            contract_path = Path(__file__).resolve().parents[1] / 'shared/contracts/file-drops.json'
            progress = json.loads(contract_path.read_text(encoding='utf-8'))['upload_progress']
            source = root / 'test-upload.bin'
            source.write_bytes(b'x' * progress['bytes'])
            browser.upload_paths([str(source)])
            wait_for(lambda: browser._upload_notice.get_mapped() and
                     browser._upload_bar.get_fraction() == progress['before_commit'] / 100,
                     'SFTP notice shows progress before commit')
            assert '64.0 КБ / 64.0 КБ' in browser._upload_metrics.get_text()
            assert browser._upload_metrics.get_text().endswith('/с')
            wait_for(lambda: browser._upload_bar.get_fraction() == progress['completed'] / 100,
                     'SFTP notice shows 100% after commit')
            wait_for(lambda: any(item[1] == source.name for item in browser.model), 'uploaded file listed')
            print('PASS SFTP upload notice shows size, rate and commit status', flush=True)
            shortcuts = KEYBOARD["rename"]
            browser._sftp.rename("/z-small.txt", "/" + shortcuts["file"][0])
            browser._sftp.mkdir("/" + shortcuts["directory"][0])
            browser.refresh()
            for kind in ("file", "directory"):
                names = shortcuts[kind]
                wait_for(lambda: any(row[1] == names[0] for row in browser.model), "SFTP rename source listed")
                row = next(row for row in browser.model if row[1] == names[0])
                browser.tree.get_selection().select_path(row.path)
                rename_with_f2(browser.tree, "Переименовать", names)
                wait_for(lambda: any(row[1] == names[1] for row in browser.model), "SFTP F2 rename applied")
            print("PASS F2 renames SFTP file and directory", flush=True)
            row = next(tuple(item) for item in browser.model if item[1] == 'fixture.txt')
            separate_row = []

            def change_mode():
                dialog = next((item for item in Gtk.Window.list_toplevels()
                               if isinstance(item, Gtk.Dialog) and item.get_title() == 'Права · fixture.txt'), None)
                if dialog is None:
                    return True
                try:
                    controls = dialog.get_content_area().get_children()
                    grid = next(item for item in controls if isinstance(item, Gtk.Grid))
                    sudo = next(item for item in controls if isinstance(item, Gtk.CheckButton))
                    grid_bounds = grid.get_allocation()
                    separate_row.append(sudo.get_allocation().y >= grid_bounds.y + grid_bounds.height)
                    field = next(item for item in grid.get_children() if isinstance(item, Gtk.Entry))
                    field.set_text('644')
                finally:
                    dialog.response(Gtk.ResponseType.OK)
                return False

            timer = GLib.timeout_add(80, change_mode)
            try:
                browser._chmod(row)
                wait_for(lambda: any(item[1] == 'fixture.txt' and item[7] == 'rw-r--r--' for item in browser.model),
                         'SFTP chmod and permissions refresh')
                assert separate_row == [True]
            finally:
                if not separate_row:
                    GLib.source_remove(timer)
            print("PASS SFTP permissions editor applies mode and refreshes metadata", flush=True)
            recording=root/'import-recording.log'
            recorded_profile=dict(profile,_recording_path=str(recording))
            window.open_session(recorded_profile)
            wait_for(lambda:fixture.authenticated==3,'recorded SSH also receives private askpass password',timeout=20)
            recorded=window.active_pane()
            wait_for(lambda:'IMPORT_SSH_READY' in recorded.terminal.get_text(lambda *_:True,None)[0],'recorded SSH output')
            window.close_tab(workspace=window.current_workspace(),ask=False)
            pump(.2)
            assert fixture.password not in recording.read_text()
            window.notebook.set_current_page(window.notebook.page_num(window._workspace_for_pane(pane)))
            pump(.1)
            vault.lock();master_prompts=[];old_pid=pane.pid
            def answer_restart():
                for dialog in Gtk.Window.list_toplevels():
                    if not isinstance(dialog,Gtk.Dialog) or not dialog.get_visible():continue
                    if isinstance(dialog,Gtk.MessageDialog):dialog.response(Gtk.ResponseType.OK)
                    elif dialog.get_title()=='Разблокировать хранилище':
                        entry=next(item for item in dialog.get_content_area().get_children() if isinstance(item,Gtk.Entry))
                        entry.set_text('TEST_ONLY_master_password');master_prompts.append(True);dialog.response(Gtk.ResponseType.OK)
                return fixture.authenticated<4
            GLib.timeout_add(30,answer_restart)
            pane.restart()
            wait_for(lambda:fixture.authenticated==4 and pane.pid is not None and pane.pid!=old_pid,'locked vault gates native context-menu reconnect',timeout=20)
            assert len(master_prompts)==1 and vault.unlocked
            original=pane.pid
            # Stop the owned SSH child and verify that wrapper lifecycle does not orphan it.
            children=subprocess.check_output(['ps','-o','pid=','--ppid',str(original)]).decode('ascii').split()
            window.close_tab(workspace=window.current_workspace(),ask=False)
            wait_for(lambda:all(not os.path.exists('/proc/'+pid) for pid in children),'SSH child released with VTE wrapper',timeout=10)
            vault.lock()
            assert not vault.unlocked
            vault.unlock('TEST_ONLY_master_password')
            assert vault.get(store.sessions[0]['credential_profile'])['password']==fixture.password
            manager = window.credentials.manage()
            vault_file = Path(vault.path)
            sessions_before = (root/'app/sessions.json').read_bytes()
            changed_master = 'TEST_ONLY_changed_master'
            reset_master = 'TEST_ONLY_reset_master'

            def master_dialog(index, values, response):
                manager.master_buttons[index].clicked()
                dialog = next(item for item in Gtk.Window.list_toplevels()
                              if isinstance(item, Gtk.Dialog) and item.get_transient_for() is manager)
                grid = next(item for item in dialog.get_content_area().get_children() if isinstance(item, Gtk.Grid))
                entries = {entry.get_name(): entry for entry in grid.get_children() if isinstance(entry, Gtk.Entry)}
                expected = {'vault-master', 'vault-repeat'} | ({'vault-current'} if index == 0 else set())
                assert set(entries) == expected and all(not entry.get_visibility() for entry in entries.values())
                for name, value in values.items():
                    entries['vault-' + name].set_text(value)
                dialog.response(response)
                wait_for(lambda: dialog.get_window() is None, 'master password dialog closes after commit')

            master_dialog(0, {'current': 'TEST_ONLY_master_password', 'master': changed_master, 'repeat': changed_master},
                          Gtk.ResponseType.OK)
            reopened = CredentialVault(vault_file)
            reopened.unlock(changed_master)
            assert reopened.get(store.sessions[0]['credential_profile'])['password'] == fixture.password
            reopened.lock()
            manager.lock()
            before_cancel = vault_file.read_bytes()
            master_dialog(1, {}, Gtk.ResponseType.CANCEL)
            assert vault_file.read_bytes() == before_cancel
            master_dialog(1, {'master': reset_master, 'repeat': reset_master}, Gtk.ResponseType.OK)
            reopened.unlock(reset_master)
            assert reopened.metadata() == [] and (root/'app/sessions.json').read_bytes() == sessions_before
            reopened.lock()
            manager.destroy()
            print('PASS vault manager change / locked reset / cancel / saved sessions', flush=True)
            # Drive the real preview and masked master-password dialog in another
            # temporary database; no imported file is copied into app settings.
            second=MainWindow(SessionStore(root/'second/sessions.json'),start_local=False)
            second.present();answered=[]
            def answer_dialogs():
                for dialog in Gtk.Window.list_toplevels():
                    if not isinstance(dialog,Gtk.Dialog) or not dialog.get_visible():continue
                    if dialog.get_title()=='Предпросмотр импорта':
                        answered.append('preview');dialog.response(Gtk.ResponseType.OK)
                    elif dialog.get_title()=='Создать хранилище':
                        entries=[item for item in dialog.get_content_area().get_children() if isinstance(item,Gtk.Entry)]
                        assert len(entries)==2 and all(not item.get_visibility() for item in entries)
                        for item in entries:item.set_text('TEST_ONLY_other_master')
                        answered.append('master');dialog.response(Gtk.ResponseType.OK)
                return len(answered)<2
            GLib.timeout_add(30,answer_dialogs)
            try:
                second.credentials.import_files([sessions,passwords])
                wait_for(lambda:len(second.store.sessions)==1,'native import preview and master dialog commit',timeout=15)
                assert answered==['preview','master']
                assert second.credential_vault.unlocked
            finally:second.shutdown();second.destroy();pump(.1)
            print('PASS imported profile selector / encrypted at rest / real OpenSSH askpass / automatic SFTP / child cleanup / restart unlock',flush=True)
            check_interactive_auth(root)
            print('IMPORTS_SMOKE_OK',flush=True)
        finally:
            if window is not None:window.shutdown();window.destroy();pump(.2)
            fixture.close()
            if previous_home is None:os.environ.pop('HOME',None)
            else:os.environ['HOME']=previous_home


if __name__=='__main__':main()
