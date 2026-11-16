"""Shared signed-feed vectors and real streamed-file verification; no user keys."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from astraterm import updates

ROOT = Path(__file__).resolve().parent.parent


class UpdateTests(unittest.TestCase):
    def test_shared_signed_contract(self):
        contract = json.loads((ROOT / 'shared/contracts/updates.json').read_text(encoding='utf-8'))
        for case in contract['cases']:
            for platform in ('linux', 'windows'):
                with self.subTest(case=case['name'], platform=platform):
                    raw = json.dumps(case['envelope']).encode('utf-8')
                    if case['valid']:
                        item = updates.read_manifest(raw, platform, case['current'], contract['public_key'])
                        self.assertEqual(item['available'], case['available'])
                    else:
                        with self.assertRaises(ValueError):
                            updates.read_manifest(raw, platform, case['current'], contract['public_key'])

    def test_streamed_download_rejects_corruption_truncation_and_oversize(self):
        good = b'synthetic update'
        item = {'url':'https://astraterminal.ru/releases/0.10.0/astraterm_0.10.0_all.deb', 'bytes':len(good), 'sha256':hashlib.sha256(good).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            for bad in (b'corrupted update', good[:-1], good + b'overflow'):
                with self.assertRaises(ValueError):
                    updates.download(item, directory, lambda unused: io.BytesIO(bad))
                self.assertEqual(os.listdir(directory), [])
            filename = updates.download(item, directory, lambda unused: io.BytesIO(good))
            self.assertEqual(Path(filename).read_bytes(), good)

    def test_manifest_and_url_bounds(self):
        with self.assertRaises(ValueError):
            updates.read_manifest(b'x' * 131073)
        for value in ('https://astraterminal.ru/releases/../latest.json', 'https://astraterminal.ru/releases/1.0.0/%2e.exe', 'https://astraterminal.ru/releases/1.0.0/a.exe?q=1'):
            with self.assertRaises(ValueError):
                updates.release_url(value)
