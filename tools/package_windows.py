#!/usr/bin/env python3
"""Create an allowlisted Windows source ZIP, excluding dependencies and state."""
from __future__ import print_function, unicode_literals

import argparse
import json
import os
from pathlib import Path
import stat
import zipfile


ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = ('.cjs', '.js', '.html', '.css', '.svg', '.ps1')
ASSET_SUFFIXES = ('.ico', '.png', '.svg', '-LICENSE.txt', '-COPYING.txt')


def source_files(root=ROOT):
    """Only explicit code/assets/doc paths enter the Windows source release."""
    selected = set()
    for relative, suffixes in (('windows/src', SOURCE_SUFFIXES),
                               ('windows/native', ('.cs',)),
                               ('windows/tools', ('.cjs', '.ps1')),
                               ('windows/build', ('.nsh',)),
                               ('windows/assets', ASSET_SUFFIXES),
                               ('windows/tests', ('.test.cjs',)),
                               ('shared', ('.json',)), ('docs', ('.md',))):
        directory = root / relative
        if directory.is_dir():
            for filename in directory.rglob('*'):
                if (filename.is_file() and not filename.is_symlink() and
                        any(filename.name.endswith(suffix) for suffix in suffixes) and
                        not any(part in ('node_modules', 'dist', '__pycache__', '.git')
                                for part in filename.relative_to(root).parts)):
                    selected.add(filename)
    for relative in ('windows/package.json', 'windows/package-lock.json', 'windows/README.md',
                     'windows/tests/electron-env.cjs', 'windows/tests/vm-integration.cjs',
                     'README.md', 'agent.md', 'AGENTS.md', 'LICENSE',
                     'docs/FEATURES.md', 'docs/ROADMAP.md', 'tools/package_windows.py',
                     'tools/sync_shared.py', 'tools/build.py', 'build-windows.ps1', 'build-linux.ps1', 'build-all.ps1',
                     'shared/sftp-chmod.py'):
        filename = root / relative
        if filename.is_file() and not filename.is_symlink():
            selected.add(filename)
    # Public website sources keep build + release deployment reproducible.
    for filename in (root / 'website').rglob('*'):
        if (filename.is_file() and not filename.is_symlink() and
                filename.suffix in ('.py', '.php', '.html', '.css', '.js', '.cjs', '.png', '.svg', '.md', '.txt', '.woff2', '.ttf') and
                not any(part in ('dist', '__pycache__', 'node_modules') for part in filename.relative_to(root).parts)):
            selected.add(filename)
    for relative in ('tools/release.py', 'tools/website_build.py', 'tools/build-requirements-windows.txt',
                     'tools/site-admin-password.ps1', 'tools/site_admin_reset.py', 'tests/site_statistics.php'):
        filename = root / relative
        if filename.is_file():
            selected.add(filename)
    return sorted(selected, key=lambda filename: filename.relative_to(root).as_posix())


def build_archive(output=None, root=ROOT):
    with (root / 'shared' / 'product.json').open(encoding='utf-8') as stream:
        version = json.load(stream)['version']
    if not isinstance(version, str) or not version or any(character not in '0123456789.-abcdefghijklmnopqrstuvwxyz' for character in version):
        raise ValueError('Invalid Windows package version')
    prefix = 'astraterm-{}-windows-source'.format(version)
    destination = Path(output) if output else root / 'dist' / 'windows' / (prefix + '.zip')
    destination = Path(os.path.abspath(str(destination)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(destination), 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in source_files(root):
            name = prefix + '/' + filename.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, filename.read_bytes())
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Destination .zip (default: dist/windows/)')
    args = parser.parse_args()
    print(str(build_archive(args.output)))


if __name__ == '__main__':
    main()
