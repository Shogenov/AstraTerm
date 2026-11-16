#!/usr/bin/env python3
"""Build the architecture-independent Astra Linux .deb using dpkg-deb."""
from __future__ import print_function, unicode_literals

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
VERSION = json.loads((ROOT / 'shared/product.json').read_text(encoding='utf-8'))['version']


def copy_file(source, destination, executable=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source), str(destination))
    os.chmod(str(destination), 0o755 if executable else 0o644)


def builder_command():
    executable = shutil.which("dpkg-deb")
    if not executable:
        raise RuntimeError("Нужен dpkg-deb. Запустите сборку на Astra Linux / Debian.")
    help_text = subprocess.check_output([executable, "--help"]).decode("utf-8", "replace")
    if "--root-owner-group" in help_text:
        return [executable, "--root-owner-group", "--build"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return [executable, "--build"]
    fakeroot = shutil.which("fakeroot")
    if fakeroot:
        return [fakeroot, executable, "--build"]
    raise RuntimeError("Этот dpkg-deb требует fakeroot или сборку через sudo, чтобы файлы пакета принадлежали root.")


def build_deb(output=None):
    command = builder_command()
    output = Path(os.path.abspath(str(output))) if output else ROOT / "dist" / ("astraterm_{}_all.deb".format(VERSION))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="astraterm-deb-") as temporary:
        stage = Path(temporary) / "astraterm"
        stage.mkdir(mode=0o755)
        metadata = stage / "DEBIAN"
        metadata.mkdir(mode=0o755)
        control = """Package: astraterm
Version: {version}
Section: net
Priority: optional
Architecture: all
Maintainer: AstraTerm <noreply@localhost>
Depends: python3.5 | python3 (>= 3.5), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-vte-2.91, python3-paramiko, python3-cryptography, python3-yaml, openssh-client, xauth, libxcb1
Recommends: telnet, screen, freerdp2-x11 | freerdp-x11, tigervnc-viewer | xtightvncviewer
Description: Native terminal and connection workspace for Astra Linux
 AstraTerm provides VTE terminal tabs and cross-host splits, live MultiExec
 with selected recipients, saved SSH sessions, an SFTP file browser,
 macros and SSH port forwarding. RDP uses an embedded FreeRDP X11 surface;
 VNC connections use an external native client.
""".format(version=VERSION)
        (metadata / "control").write_text(control, encoding="utf-8")
        os.chmod(str(metadata / "control"), 0o644)

        data_root = stage / "usr/share/astraterm"
        for pattern in ("*.py", "*.css"):
            for source in sorted((ROOT / "astraterm").glob(pattern)):
                copy_file(source, data_root / "astraterm" / source.name)
        copy_file(ROOT / "scripts/astraterm", stage / "usr/bin/astraterm", executable=True)
        copy_file(ROOT / "scripts/astraterm-askpass", data_root / "scripts/astraterm-askpass", executable=True)
        copy_file(ROOT / "share/astraterm.svg", data_root / "share/astraterm.svg")
        copy_file(ROOT / "share/astraterm.svg", stage / "usr/share/icons/hicolor/scalable/apps/astraterm.svg")
        copy_file(ROOT / "share/astraterm.desktop", stage / "usr/share/applications/astraterm.desktop")
        copy_file(ROOT / "docs/THIRD_PARTY_NOTICES.md", stage / "usr/share/doc/astraterm/copyright")
        if (ROOT / "README.md").exists():
            copy_file(ROOT / "README.md", stage / "usr/share/doc/astraterm/README.md")
        if (ROOT / "docs").is_dir():
            for source in (ROOT / "docs").glob("*.md"):
                copy_file(source, stage / "usr/share/doc/astraterm/docs" / source.name)
        postinst = metadata / "postinst"
        postinst.write_text("""#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
exit 0
""", encoding="utf-8")
        os.chmod(str(postinst), 0o755)
        # A restrictive caller umask must not make /usr/share unreadable.
        for directory, subdirectories, unused_files in os.walk(str(stage)):
            os.chmod(directory, 0o755)
        # TemporaryDirectory itself is mode 0700; stage is traversable in .deb.
        subprocess.check_call(command + [str(stage), str(output)])
    os.chmod(str(output), 0o644)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Destination .deb (default: dist/)")
    args = parser.parse_args()
    try:
        print(str(build_deb(args.output)))
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print("Сборка не выполнена: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
