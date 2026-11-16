import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from astraterm.backup import capture_profile, read_backup, restore_profile, write_backup
from astraterm.core import SessionStore
from astraterm.profile_location import active_profile
from astraterm.ssh_profiles import ProfileStore
from astraterm.vault import CredentialVault


CONTRACT = json.loads((Path(__file__).resolve().parent.parent / 'shared/contracts/backup.json').read_text(encoding='utf-8'))
KEY = b'-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-backup-fixture\n-----END OPENSSH PRIVATE KEY-----\n'
PUBLIC = b'ssh-ed25519 synthetic-fixture\n'
CERTIFICATE = b'ssh-ed25519-cert-v01@openssh.com synthetic-fixture\n'


def fixture(source):
    source.mkdir()
    key = source / 'testing key'
    missing = source / 'missing key'
    key.write_bytes(KEY)
    (source / (key.name + '.pub')).write_bytes(PUBLIC)
    (source / (key.name + '-cert.pub')).write_bytes(CERTIFICATE)
    for name, field in (('sessions.json', 'sessions'), ('ssh-profiles.json', 'identities'),
                        ('sidebar.json', 'sidebar'), ('logging.json', 'logging')):
        document = json.dumps(CONTRACT[field], ensure_ascii=False)
        document = document.replace('$KEY', json.dumps(str(key))[1:-1])
        document = document.replace('$MISSING', json.dumps(str(missing))[1:-1])
        (source / name).write_text(document, encoding='utf-8')
    vault = CredentialVault(str(source / 'credentials.vault.json'))
    vault.unlock(CONTRACT['master_password'], create=True)
    vault.merge([CONTRACT['credential']])
    vault.lock()
    return missing


class BackupTest(unittest.TestCase):
    def test_restores_passwords_and_keys_with_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, archive = root / 'source', root / 'target', root / 'transfer.astraterm-backup'
            missing = fixture(source)
            target.mkdir()
            old = target / 'sessions.json'
            old.write_text('{"sessions":[]}', encoding='utf-8')
            payload = capture_profile(str(source))
            self.assertEqual(payload['missing'], [str(missing)])
            self.assertEqual(len(payload['keys']), 1)
            write_backup(str(archive), payload, CONTRACT['password'], str(source))
            self.assertNotIn(b'synthetic-backup-fixture', archive.read_bytes())
            self.assertNotIn(b'backup.test', archive.read_bytes())
            with self.assertRaisesRegex(ValueError, 'Неверный пароль'):
                read_backup(str(archive), 'incorrect password')
            self.assertEqual(active_profile(str(target)), str(target))
            restored = Path(restore_profile(str(target), str(target), read_backup(str(archive), CONTRACT['password'])))
            self.assertEqual(active_profile(str(target)), str(restored))
            self.assertEqual(old.read_text(encoding='utf-8'), '{"sessions":[]}')
            store = SessionStore(str(restored / 'sessions.json'))
            profile = ProfileStore(str(restored / 'ssh-profiles.json')).get()
            key = Path(profile['key_file'])
            self.assertEqual(key.read_bytes(), KEY)
            self.assertEqual(Path(str(key) + '.pub').read_bytes(), PUBLIC)
            self.assertEqual(Path(str(key) + '-cert.pub').read_bytes(), CERTIFICATE)
            self.assertEqual(store.resolve_session(store.sessions[0])['key_file'], str(key))
            self.assertEqual(store.tunnels[0]['key_file'], str(key))
            self.assertEqual(store.macros[0]['text'], 'date')
            self.assertEqual(json.loads((restored / 'sidebar.json').read_text()), CONTRACT['sidebar'])
            self.assertEqual(json.loads((restored / 'logging.json').read_text()), CONTRACT['logging'])
            vault = CredentialVault(str(restored / 'credentials.vault.json'))
            vault.unlock(CONTRACT['master_password'])
            self.assertEqual(vault.get(store.sessions[0]['credential_profile'], 'ssh'), CONTRACT['credential'])
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o700)
            with self.assertRaisesRegex(ValueError, 'Активный профиль изменился'):
                restore_profile(str(target), str(target), payload)
            self.assertEqual(list(restored.parent.iterdir()), [restored])
            self.assertEqual(active_profile(str(target)), str(restored))

    def test_rejects_tampering_and_will_not_overwrite_an_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, archive = root / 'source', root / 'copy.astraterm-backup'
            source.mkdir()
            payload = capture_profile(str(source))
            write_backup(str(archive), payload, CONTRACT['password'], str(source))
            before = archive.read_bytes()
            with self.assertRaisesRegex(ValueError, 'Файл уже существует'):
                write_backup(str(archive), payload, CONTRACT['password'], str(source))
            self.assertEqual(archive.read_bytes(), before)
            data = json.loads(before.decode('utf-8'))
            data['tag'] = ('A' if data['tag'][0] != 'A' else 'B') + data['tag'][1:]
            archive.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'повреждён'):
                read_backup(str(archive), CONTRACT['password'])
