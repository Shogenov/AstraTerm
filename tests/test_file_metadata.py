from __future__ import unicode_literals

import json
import unittest
from pathlib import Path

from astraterm.file_metadata import parse_birth_times, permissions, remote_birth_times


class FileMetadataTest(unittest.TestCase):
    def test_permissions_and_optional_birth_times(self):
        contract = Path(__file__).resolve().parents[1] / 'shared/contracts/file-metadata.json'
        data = json.loads(contract.read_text(encoding='utf-8'))
        for mode, expected in data['permissions']:
            self.assertEqual(permissions(mode), expected)
        self.assertEqual(parse_birth_times(data['birth_records'].encode('utf-8')), data['birth_times'])
        self.assertEqual(remote_birth_times(None, '/fixture'), {})
