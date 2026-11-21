import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from astraterm.file_permissions import parse_mode, permission_revision, sudo_chmod
from astraterm._shared import SFTP_CHMOD_SCRIPT


CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'shared/contracts/file-permissions.json').read_text())


class FilePermissionsTests(unittest.TestCase):
    def test_octal_mode(self):
        for value, mode in CONTRACT['modes'].items():
            self.assertEqual(parse_mode(value), mode)
        for value in CONTRACT['invalid']:
            with self.assertRaises(ValueError):
                parse_mode(value)

    def test_password_only_on_server_request(self):
        secret = Mock(return_value="synthetic password")
        revision = (0o100644, 1000, 1000, 1, 10)
        filename = "/test/quote ' and ;"
        with patch('astraterm.file_permissions._execute', side_effect=[
            (1, CONTRACT['sudo']['challenge'].encode()), (0, b''),
        ]) as command:
            self.assertTrue(sudo_chmod(object(), filename, 0o640, revision, secret, Mock()))
        first, retry = [call[0] for call in command.call_args_list]
        self.assertIsNone(first[2])
        self.assertNotIn(secret.return_value, retry[1])
        self.assertEqual(retry[2], secret.return_value)
        self.assertEqual(shlex.split(retry[1])[-7:], [filename, "0640"] + list(map(str, revision)))
        secret.assert_called_once_with()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'needs Linux /proc and openat')
    def test_descriptor_rejects_ancestor_symlink_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.join(directory, "real")
            os.mkdir(real)
            filename = os.path.join(real, "quote ';")
            with open(filename, 'w') as stream:
                stream.write('fixture')
            os.chmod(filename, 0o644)
            status = os.lstat(filename)
            revision = (status.st_mode, status.st_uid, status.st_gid, status.st_size, int(status.st_mtime))

            def change(target, revision):
                return subprocess.call([sys.executable, '-IS', '-c', SFTP_CHMOD_SCRIPT, target, '0600'] +
                                       list(map(str, revision)), stderr=subprocess.DEVNULL)

            link = os.path.join(directory, "link")
            os.symlink(real, link)
            self.assertNotEqual(change(os.path.join(link, os.path.basename(filename)), revision), 0)
            self.assertEqual(change(filename, revision[:-1] + (0,)), 55)
            self.assertEqual(stat.S_IMODE(os.lstat(filename).st_mode), 0o644)
            self.assertEqual(change(filename, revision), 0)
            self.assertEqual(stat.S_IMODE(os.lstat(filename).st_mode), 0o600)
            with self.assertRaises(IOError):
                permission_revision(Mock(st_mode=stat.S_IFLNK | 0o777))
