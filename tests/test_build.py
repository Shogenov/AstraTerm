"""Release orchestration safety checks; no GUI, network or production credentials."""
from __future__ import unicode_literals

import io
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
import sys
from unittest import mock

from tools import build, sync_shared, package, package_windows


class BuildTests(unittest.TestCase):
    def version_fixture(self, directory):
        root = Path(directory) / 'repo'
        for relative in ('tools/build.py', 'tools/sync_shared.py', 'tools/package.py',
                         'tools/package_windows.py', 'tools/release.py', 'shared/product.json', 'shared/sftp-chmod.py'):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(build.ROOT / relative), str(target))
        # Linux source archives only include the Linux implementation.
        (root / 'astraterm').mkdir()
        (root / 'windows/src').mkdir(parents=True)
        version = sync_shared.product(root)['version']
        build.write_json(root / 'windows/package.json', {'version': version})
        build.write_json(root / 'windows/package-lock.json', {'version': version, 'packages': {'': {'version': version}}})
        sync_shared.synchronize(root=root)
        return root

    def test_version_plan_preserves_metadata_and_uses_requested_output_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.version_fixture(directory)
            before = {name: (root / name).read_bytes() for name in
                      ('shared/product.json', 'astraterm/_shared.py', 'windows/src/shared.cjs', 'windows/package.json')}
            output = io.StringIO()
            with mock.patch.object(build, 'ROOT', root), mock.patch.object(build.sys, 'stdout', output):
                self.assertEqual(build.main(['all', '--source-only', '--version', '1.2.3', '--plan']), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan['version'], '1.2.3')
            self.assertEqual(Path(plan['output']), root / 'dist/releases/1.2.3')
            for name, original in before.items():
                self.assertEqual((root / name).read_bytes(), original)
            self.assertFalse((root / 'dist').exists())

    def test_version_reaches_both_platform_archives_and_metadata_through_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.version_fixture(directory)
            subprocess.check_output([sys.executable, str(root / 'tools/build.py'), 'all',
                                     '--source-only', '--version', '1.2.3'], cwd=str(root))
            output = root / 'dist/releases/1.2.3'
            summary = json.loads((output / 'build-summary.json').read_text(encoding='utf-8'))
            self.assertEqual([item['version'] for item in summary['platforms']], ['1.2.3', '1.2.3'])
            self.assertEqual(summary['status'], 'built')
            self.assertEqual(sync_shared.product(root)['version'], '1.2.3')
            self.assertEqual(sync_shared.synchronize(check=True, root=root), [])
            self.assertTrue((output / 'windows/astraterm-1.2.3-windows-source.zip').is_file())
            self.assertTrue((output / 'linux/astraterm-1.2.3-source.tar.gz').is_file())
            self.assertTrue((output / 'astraterm-1.2.3-all-source.zip').is_file())

    def test_invalid_version_and_rejected_deployment_leave_metadata_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.version_fixture(directory)
            original = (root / 'shared/product.json').read_bytes()
            cases = [ ['all', '--source-only', '--version', version]
                      for version in ('1.2', '1.2.3\n', '../1.2.3', '') ]
            cases.append(['all', '--source-only', '--version', '1.2.3', '--deploy'])
            with mock.patch.object(build, 'ROOT', root):
                for args in cases:
                    with self.subTest(args=args), self.assertRaises(ValueError):
                        build.main(args)
                    self.assertEqual((root / 'shared/product.json').read_bytes(), original)

    def test_requested_version_is_applied_before_build_and_deployment(self):
        from tools import release
        with tempfile.TemporaryDirectory() as directory:
            root = self.version_fixture(directory)
            report = {'platform': 'windows', 'version': '1.2.3', 'status': 'built',
                      'tests': 'passed', 'artifacts': []}

            def built(args, target, version):
                self.assertEqual(version, '1.2.3')
                self.assertEqual(sync_shared.synchronize(check=True, root=root), [])
                target.mkdir(parents=True, exist_ok=True)
                return report

            with mock.patch.object(build, 'ROOT', root), mock.patch.object(build.sys, 'platform', 'win32'), \
                    mock.patch.object(build, 'build_windows', side_effect=built), \
                    mock.patch.object(release, 'main') as deploy, mock.patch.object(build.sys, 'stdout', io.StringIO()):
                build.main(['windows', '--version', '1.2.3', '--deploy', '--deploy-host', 'ftp.example.invalid',
                            '--deploy-user', 'fixture', '--deploy-root', '/www/example.invalid',
                            '--signing-key', 'synthetic-signing.pem', '--release-notes', 'Fixture release'])
            command = deploy.call_args[0][0]
            self.assertEqual(command[command.index('--release-dir') + 1], str(root / 'dist/releases/1.2.3'))
            self.assertEqual(command[command.index('--signing-key') + 1], 'synthetic-signing.pem')
            self.assertEqual(command[command.index('--notes') + 1], 'Fixture release')
            summary = json.loads((root / 'dist/releases/1.2.3/build-summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['deployment'], 'published')

    def test_deploy_cannot_publish_source_only_or_untested_binaries(self):
        for flag in ('--source-only', '--skip-tests'):
            with self.assertRaises(ValueError):
                build.main(['windows', '--deploy', flag])

    def test_zero_exit_gui_callback_traceback_still_fails_the_build(self):
        with mock.patch.object(build.sys, 'stdout', io.StringIO()):
            with self.assertRaises(RuntimeError):
                build.run([sys.executable, '-c', 'print("Traceback (most recent call last):")'], reject_tracebacks=True)

    def test_unknown_builder_or_control_characters_never_reach_ssh(self):
        for host in ('-oProxyCommand=bad', 'host;command', 'host\n', 'user@host command', '$(command)', ''):
            with self.subTest(host=host), self.assertRaises(ValueError):
                build.ssh_arguments(build.parse_args(['linux', '--linux-host=' + host]))

    def test_known_hosts_with_spaces_remains_one_strict_openssh_option(self):
        with tempfile.TemporaryDirectory(prefix='folder with spaces ') as directory:
            known_hosts = Path(directory) / 'known_hosts'
            known_hosts.write_text('', encoding='ascii')
            args = build.parse_args(['linux', '--linux-host', 'builder@node.example', '--known-hosts', str(known_hosts)])
            with mock.patch.object(build, 'needed', side_effect=lambda name: name):
                command = build.ssh_arguments(args)
        self.assertIn('BatchMode=yes', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertTrue(any(item.startswith('UserKnownHostsFile="') and 'folder with spaces' in item for item in command))

    def test_failed_publish_preserves_previous_release(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'new', Path(directory) / 'release'
            source.write_bytes(b'new release')
            target.write_bytes(b'previous release')
            with mock.patch.object(build.shutil, 'copyfile', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    build.publish(source, target)
            self.assertEqual(target.read_bytes(), b'previous release')
            self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), ['new', 'release'])
            build.publish(source, target)
            self.assertEqual(target.read_bytes(), b'new release')

    def test_second_platform_failure_cannot_produce_combined_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            (root / 'windows').mkdir(parents=True)
            (root / 'astraterm').mkdir()
            output = Path(directory) / 'output'
            windows = {'platform': 'windows', 'version': sync_shared.product()['version'], 'status': 'built', 'artifacts': []}
            args = ['all', '--linux-host', 'builder.example', '--output', str(output)]
            with mock.patch.object(build, 'ROOT', root), mock.patch.object(build.sys, 'platform', 'win32'), \
                    mock.patch.object(build, 'ssh_arguments', return_value=['ssh']), \
                    mock.patch.object(build.sync_shared, 'synchronize'), \
                    mock.patch.object(build, 'build_windows', return_value=windows), \
                    mock.patch.object(build, 'build_linux_remote', side_effect=RuntimeError('builder unavailable')):
                with self.assertRaises(RuntimeError):
                    build.main(args)
            report = json.loads((output / 'build-summary.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'failed')
            self.assertEqual([item['platform'] for item in report['platforms']], ['windows'])

    def test_generated_metadata_is_current_and_packagers_share_version(self):
        self.assertEqual(sync_shared.synchronize(check=True), [])
        self.assertEqual(package.VERSION, sync_shared.product()['version'])
        self.assertIn(build.ROOT / 'shared/product.json', package.source_files())
        self.assertIn(build.ROOT / 'shared/product.json', package_windows.source_files())

    def test_source_allowlists_include_shared_contracts_and_builders_without_local_state(self):
        for selected in (package.source_files(), package_windows.source_files()):
            names = [item.relative_to(build.ROOT).as_posix() for item in selected]
            for expected in ('shared/contracts/sessions.json', 'shared/contracts/multiexec.json', 'tools/build.py', 'tools/sync_shared.py', 'build-all.ps1'):
                self.assertIn(expected, names)
            self.assertFalse(any('node_modules' in name or name.startswith('dist/') or name.endswith('/sessions.json') and name != 'shared/contracts/sessions.json' or name == 'tools/vm.py' for name in names))


if __name__ == '__main__':
    unittest.main()
