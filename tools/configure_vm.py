"""Create optional example profiles and a desktop shortcut on a development VM.

Run as the desktop user after installing AstraTerm. No passwords are accepted.
"""
import getpass
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from astraterm.core import SessionStore, default_session


def main():
    store = SessionStore()
    local = default_session("local")
    local.update(name="Локальный терминал", group="Локальные")
    ssh = default_session("ssh")
    ssh.update(name="Astra Linux 1.6", group="Виртуальные машины",
               host=os.environ.get("ASTRA_VM_HOST", "127.0.0.1"),
               username=getpass.getuser(), x11=True)
    for profile in (local, ssh):
        if not any(item["name"] == profile["name"] for item in store.sessions):
            store.upsert(profile)
    desktop = subprocess.check_output(["xdg-user-dir", "DESKTOP"]).decode("utf-8").strip()
    if desktop and desktop != os.path.expanduser("~"):
        if not os.path.isdir(desktop):
            os.makedirs(desktop)
        destination = os.path.join(desktop, "astraterm.desktop")
        shutil.copyfile("/usr/share/applications/astraterm.desktop", destination)
        os.chmod(destination, 0o755)
        print("Desktop shortcut: " + destination)
    print("Profiles configured, no passwords saved.")


if __name__ == "__main__":
    main()
