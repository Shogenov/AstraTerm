"""Publication ordering and immutable release retry, using a fake FTP filesystem."""
import ftplib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from tools import release


class MemoryFTP(object):
    def __init__(self, files):
        self.files = files
        self.fail_commit = False
    def voidcmd(self, command):
        pass
    def size(self, name):
        if name not in self.files:
            raise ftplib.error_perm('550 absent')
        return len(self.files[name])
    def storbinary(self, command, stream, **kwargs):
        self.files[command[5:]] = stream.read()
    def retrbinary(self, command, consume):
        consume(self.files[command[5:]])
    def rename(self, source, target):
        if self.fail_commit and '.upload-' in source:
            raise OSError('simulated commit failure')
        self.files[target] = self.files.pop(source)


class ReleaseDeployTests(unittest.TestCase):
    def test_failed_commit_restores_original_and_keeps_immutable_retries_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'page.html'
            filename.write_bytes(b'new')
            client = object.__new__(release.FTPS)
            client.root, client.backup = '/www/site', '/private-backup/test'
            client.mkdirs = lambda unused: None
            client.ftp = MemoryFTP({'/www/site/index.html':b'old'})
            client.ftp.fail_commit = True
            with self.assertRaises(OSError):
                client.put(filename, 'index.html')
            self.assertEqual(client.ftp.files['/www/site/index.html'], b'old')
            client.ftp.fail_commit = False
            client.put(filename, 'releases/1.0.0/app.exe', replace=False)
            client.put(filename, 'releases/1.0.0/app.exe', replace=False)
            filename.write_bytes(b'different')
            with self.assertRaises(ValueError):
                client.put(filename, 'releases/1.0.0/app.exe', replace=False)

    def test_manifest_is_published_only_after_binaries_and_homepage_last(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/'index.html').write_text('home')
            (folder/'assets').mkdir()
            (folder/'assets/site.js').write_text('static')
            (folder/'admin').mkdir()
            (folder/'admin/index.php').write_text('entry point')
            (folder/'admin/statistics.php').write_text('dependency')
            binary = folder/'app.exe'
            binary.write_bytes(b'verified')
            events = []
            client = mock.Mock()
            client.put.side_effect = lambda local, remote, **unused: events.append(remote)
            release.publish_site(client, folder, [(binary,'releases/1.0.0/app.exe')], {}, b'{}')
            self.assertLess(events.index('releases/1.0.0/app.exe'), events.index('releases/latest.json'))
            self.assertLess(events.index('admin/statistics.php'), events.index('admin/index.php'))
            self.assertEqual(events[-1], 'index.html')
