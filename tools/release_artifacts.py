#!/usr/bin/env python3
"""Collect the verified Windows and Linux assets for a GitHub release."""
import argparse
import json
from pathlib import Path
import shutil

from build import artifact
from sync_shared import product


def collect(release_dir, output):
    version = product()['version']
    windows_name = 'AstraTerm-{}-Windows-x64'.format(version)
    expected = {
        'windows': [windows_name + suffix for suffix in ('-Setup.exe', '.exe', '.zip')]
                   + ['astraterm-{}-windows-source.zip'.format(version)],
        'linux': ['astraterm_{}_all.deb'.format(version), 'astraterm-{}-source.tar.gz'.format(version)],
    }
    assets = []
    for platform, names in sorted(expected.items()):
        directory = release_dir / platform
        report = json.loads((directory / 'build.json').read_text(encoding='utf-8'))
        if (report.get('version'), report.get('status'), report.get('tests'), report.get('gui')) != (
                version, 'built', 'passed', 'passed'):
            raise ValueError('Release needs a tested native build for ' + platform)
        recorded = {entry['name']: entry for entry in report['artifacts']}
        for name in names:
            source = directory / name
            entry = artifact(source)
            if recorded.get(name) != entry:
                raise ValueError('Asset differs from its build report: ' + name)
            assets.append((source, entry['sha256']))
    output.mkdir(parents=True, exist_ok=False)
    for source, unused in assets:
        shutil.copyfile(str(source), str(output / source.name))
    (output / 'SHA256SUMS.txt').write_text(''.join('{}  {}\n'.format(digest, source.name)
                                                for source, digest in assets), encoding='ascii')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release_dir', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    collect(args.release_dir, args.output)
