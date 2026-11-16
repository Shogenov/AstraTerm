import json
from pathlib import Path
import tempfile
import unittest

from astraterm.diagnostic_log import DiagnosticLog
from astraterm.file_sort import sort_rows


class DesktopSettingsTests(unittest.TestCase):
    def test_shared_sftp_column_sorting(self):
        filename = Path(__file__).resolve().parents[1] / 'shared/contracts/file-sort.json'
        contract = json.loads(filename.read_text(encoding='utf-8'))
        rows = [
            (None, entry['name'], None, None, entry['isDirectory'], None, None, None, None, None, None,
             tuple(None if key == 'size' and entry['isDirectory'] else entry.get(key)
                   for key in ('size', 'permissions', 'created', 'mtime')))
            for entry in contract['entries']
        ]
        for key, descending, expected in contract['cases']:
            self.assertEqual([row[1] for row in sort_rows(rows, key, descending)], expected)

    def test_diagnostic_logging_preference_and_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DiagnosticLog(directory)
            log.write('error', 'session.error')
            self.assertFalse(Path(log.filename).exists())
            log.configure(True, 'warning')
            reopened = DiagnosticLog(directory)
            reopened.write('debug', 'session.open')
            reopened.write('error', 'session.failed')
            reopened.write('warning', 'sensitive text')
            event = json.loads(Path(log.filename).read_text(encoding='utf-8'))
            self.assertEqual((event['level'], event['event']), ('error', 'session.failed'))
            reopened.configure(False, 'debug')
            reopened.write('error', 'session.other-error')
            self.assertEqual(len(Path(log.filename).read_text(encoding='utf-8').splitlines()), 1)
