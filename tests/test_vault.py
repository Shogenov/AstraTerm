from __future__ import unicode_literals
import json
import os
from pathlib import Path
import tempfile
import unittest
from astraterm.vault import CredentialVault

ROOT=Path(__file__).resolve().parent.parent


class VaultTest(unittest.TestCase):
    def setUp(self):
        with (ROOT/'shared/contracts/vault.json').open(encoding='utf-8') as stream:self.vector=json.load(stream)

    def test_reads_both_native_platform_vectors(self):
        for key in ('document','windows_document'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'vault.json';path.write_text(json.dumps(self.vector[key]),encoding='utf-8')
                vault=CredentialVault(path);self.assertFalse(vault.unlocked)
                with self.assertRaises(ValueError):vault.unlock('TEST_ONLY_wrong_master')
                self.assertFalse(vault.unlocked)
                vault.unlock(self.vector['master']);self.assertEqual(vault.get('fixture'),self.vector['credential'])
                self.assertNotIn('password',json.dumps(vault.metadata()));vault.lock()
                with self.assertRaises(ValueError):vault.get('fixture')

    def test_atomic_encryption_nonces_restart_and_stale_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vault.json';vault=CredentialVault(path)
            vault.unlock(self.vector['master'],create=True);vault.merge([self.vector['credential']])
            first=json.loads(path.read_text());self.assertNotIn(self.vector['credential']['password'],path.read_text())
            if os.name!='nt':self.assertEqual(os.stat(str(path)).st_mode&0o777,0o600)
            other=CredentialVault(path);other.unlock(self.vector['master'])
            vault.merge([self.vector['credential']]);second=json.loads(path.read_text())
            self.assertNotEqual(first['nonce'],second['nonce'])
            before=path.read_bytes()
            with self.assertRaises(ValueError):other.merge([])
            self.assertEqual(path.read_bytes(),before)
            vault.lock();vault.unlock(self.vector['master']);self.assertEqual(vault.get('fixture'),self.vector['credential'])

    def test_tampering_and_absent_profile_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vault.json';document=dict(self.vector['document']);document['tag']='AAAAAAAAAAAAAAAAAAAAAA=='
            path.write_text(json.dumps(document),encoding='utf-8');vault=CredentialVault(path)
            with self.assertRaises(ValueError):vault.unlock(self.vector['master'])
            self.assertFalse(vault.unlocked)
            path.unlink();vault.unlock(self.vector['master'],create=True)
            with self.assertRaises(ValueError):vault.get('missing')

    def test_shared_credential_types_legacy_links_and_delete(self):
        with (ROOT/'shared/contracts/credential-types.json').open(encoding='utf-8') as stream:typed=json.load(stream)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vault.json';vault=CredentialVault(path)
            vault.unlock(self.vector['master'],create=True);vault.merge(typed['records']);vault.lock();vault.unlock(self.vector['master'])
            for identifier,protocol in typed['allowed']:self.assertEqual(vault.get(identifier,protocol)['id'],identifier)
            for identifier,protocol in typed['rejected']:
                with self.assertRaises(ValueError):vault.get(identifier,protocol)
            self.assertNotIn('password',json.dumps(vault.metadata()))
            before=path.read_bytes()
            with self.assertRaises(ValueError):vault.merge([dict(typed['records'][0],protocol='telnet')])
            self.assertEqual(path.read_bytes(),before)
            vault.remove('ssh-only');vault.lock();vault.unlock(self.vector['master'])
            self.assertEqual({r['id'] for r in vault.metadata()},{'legacy','rdp-only'})

    def test_master_change_and_locked_reset(self):
        masters = self.vector['master_management']
        original = self.vector['master']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'vault.json'
            path.write_text(json.dumps(self.vector['document']), encoding='utf-8')
            vault = CredentialVault(path)
            vault.unlock(original)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                vault.change_master(masters['wrong'], masters['changed'])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(vault.get('fixture'), self.vector['credential'])
            other = CredentialVault(path)
            other.unlock(original)
            vault.lock()
            vault.change_master(original, masters['changed'])
            self.assertEqual(vault.get('fixture'), self.vector['credential'])
            self.assertNotEqual(json.loads(before.decode())['salt'], json.loads(path.read_text())['salt'])
            other.reload_if_changed()
            self.assertFalse(other.unlocked)
            with self.assertRaises(ValueError):
                other.unlock(original)
            other.unlock(masters['changed'])
            self.assertEqual(other.get('fixture'), self.vector['credential'])
            vault.lock()
            vault.reset_master(masters['reset'])
            self.assertEqual(vault.metadata(), [])
            self.assertTrue(vault.unlocked)
            with self.assertRaises(ValueError):
                other.unlock(masters['changed'])
            vault.lock()
            vault.unlock(masters['reset'])
            self.assertEqual(vault.metadata(), [])

    def test_reset_can_replace_unreadable_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'vault.json'
            path.write_bytes(b'invalid vault envelope')
            vault = CredentialVault(path)
            master = self.vector['master_management']['reset']
            vault.reset_master(master)
            vault.lock()
            vault.unlock(master)
            self.assertEqual(vault.metadata(), [])


if __name__=='__main__':unittest.main()
