#!/usr/bin/env python3
"""Create a source archive without caches, VM credentials or local state."""
from __future__ import print_function, unicode_literals

import argparse
import gzip
import json
import os
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parent.parent
VERSION = json.loads((ROOT / 'shared/product.json').read_text(encoding='utf-8'))['version']


def source_files(root=ROOT):
    """An allowlist keeps development authentication helpers out of releases."""
    selected = set()
    for directory, patterns in (("astraterm", ("*.py", "*.css")), ("scripts", ("*.sh", "astraterm", "astraterm-askpass")),
                                 ("share", ("*.desktop", "*.svg")), ("docs", ("*.md",)),
                                 ("tests", ("*.py",)), ("shared", ("*.json",))):
        base = root / directory
        if base.is_dir():
            for pattern in patterns:
                for path in base.rglob(pattern):
                    if path.is_file() and "__pycache__" not in path.parts:
                        selected.add(path)
    for relative in ("README.md", "agent.md", "AGENTS.md", "LICENSE", "CHANGELOG.md", "build-windows.ps1", "build-linux.ps1", "build-all.ps1",
                     "tools/package.py", "tools/package_windows.py", "tools/build_deb.py", "tools/sync_shared.py", "tools/build.py"):
        path = root / relative
        if path.is_file():
            selected.add(path)
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
    return sorted(selected, key=lambda path: str(path.relative_to(root)))


def build_archive(output=None):
    output = Path(output) if output else ROOT / "dist" / ("astraterm-{}-source.tar.gz".format(VERSION))
    output = Path(os.path.abspath(str(output)))
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = "astraterm-{}".format(VERSION)

    def normalize(info):
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = 0
        info.mode = 0o755 if info.name.endswith(("scripts/install.sh", "scripts/astraterm", "scripts/astraterm-askpass")) else 0o644
        return info

    # Normalize gzip and tar timestamps. The VM clock may differ from the host,
    # and extracted release files must not generate "timestamp in the future".
    with open(str(output), "wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in source_files():
                    relative = path.relative_to(ROOT).as_posix()
                    archive.add(str(path), arcname=prefix + "/" + relative, recursive=False, filter=normalize)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Destination .tar.gz (default: dist/)")
    args = parser.parse_args()
    print(str(build_archive(args.output)))


if __name__ == "__main__":
    main()
