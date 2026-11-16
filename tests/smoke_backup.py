"""Exercise GTK menu actions against a locked vault and a real OpenSSH key."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session
from astraterm.profile_location import active_profile


def main():
    from gi.repository import Gtk
    with tempfile.TemporaryDirectory(prefix='astraterm-backup-ui-') as directory:
        root = Path(directory)
        source, target = root / 'source', root / 'target'
        key = root / 'fixture key'
        passphrase = 'Synthetic fixture passphrase'
        subprocess.check_call(['ssh-keygen', '-q', '-t', 'ed25519', '-N', passphrase, '-f', str(key)])
        source.mkdir()
        window = MainWindow(SessionStore(str(source / 'sessions.json')), start_local=False, enable_identities=True)
        vault_password = 'Synthetic vault password'
        archive_password = 'Синтетический пароль копии'
        archive = str(root / 'copy.astraterm-backup')
        profile_id = 'backup-ui-key'
        record = {'id': 'backup-account', 'name': 'Backup fixture', 'username': 'fixture', 'password': 'fixture-secret'}
        try:
            window.identity_store.upsert({'id': profile_id, 'name': 'Backup key', 'key_file': str(key)})
            profile = dict(default_session(), host='backup.test', identity_profile=profile_id, credential_profile=record['id'])
            window.store.upsert(profile)
            window.credential_vault.unlock(vault_password, create=True)
            window.credential_vault.merge([record])
            window.credential_vault.lock()
            file_menu = window.main_menu.get_children()[0].get_submenu()
            actions = {item.get_label(): item for item in file_menu.get_children()}
            with mock.patch.multiple('astraterm.backup_dialogs',
                                     _choose_file=mock.Mock(return_value=archive),
                                     _password=mock.Mock(return_value=archive_password),
                                     error=mock.Mock(side_effect=AssertionError('Unexpected backup error'))):
                actions['Создать резервную копию…'].activate()
            assert Path(archive).is_file()
            assert b'fixture-secret' not in Path(archive).read_bytes()
        finally:
            window.shutdown()
            window.destroy()

        receiver = MainWindow(SessionStore(str(target / 'sessions.json')), start_local=False, enable_identities=True)
        try:
            file_menu = receiver.main_menu.get_children()[0].get_submenu()
            restore = next(item for item in file_menu.get_children()
                           if item.get_label() == 'Восстановить из резервной копии…')
            confirm = mock.Mock(return_value=True)
            with mock.patch.multiple('astraterm.backup_dialogs',
                                     _choose_file=mock.Mock(return_value=archive),
                                     _password=mock.Mock(return_value=archive_password),
                                     confirm=confirm,
                                     error=mock.Mock(side_effect=AssertionError('Unexpected restore error'))):
                with mock.patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.CLOSE):
                    restore.activate()
            assert confirm.called and not receiver.store.sessions
        finally:
            receiver.shutdown()
            receiver.destroy()

        # Simulate the next startup, with the source private key no longer present.
        key.unlink()
        restored_dir = Path(active_profile(str(target)))
        restored = MainWindow(SessionStore(str(restored_dir / 'sessions.json')), start_local=False,
                              enable_identities=True, backup_root=str(target))
        try:
            session = restored.store.sessions[0]
            assert session['identity_profile'] == profile_id and session['credential_profile'] == record['id']
            restored_file = restored.identity_store.get(profile_id)['key_file']
            public = subprocess.check_output(['ssh-keygen', '-y', '-P', passphrase, '-f', restored_file])
            assert public.split()[:2] == Path(str(key) + '.pub').read_bytes().split()[:2]
            restored.credential_vault.unlock(vault_password)
            assert restored.credential_vault.get(record['id']) == record
        finally:
            restored.shutdown()
            restored.destroy()
    print('BACKUP_GUI_PASS file menu / encrypted key and locked vault / restore across directories', flush=True)


if __name__ == '__main__':
    main()
