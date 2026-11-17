#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Opt-in encrypted-identity / SSH / active-host SFTP integration on Astra.

Run in the desktop session as its ordinary user:
DISPLAY=:0 /usr/bin/python3.5 tests/smoke_auth_profiles.py

An isolated loopback sshd, encrypted synthetic key, agent, known_hosts and all
application settings live in one temporary directory below the current home.
No real private keys, authorized_keys or known_hosts are read or changed.
"""
from __future__ import print_function, unicode_literals

import os
import json
import pwd
import shutil
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Gio
import paramiko
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from astraterm import app as app_module
from astraterm import files as files_module
from astraterm import terminal as terminal_module
from astraterm.core import SessionStore, default_session
from astraterm.ssh_profiles import ProfileStore, SSHAgent, read_public_key
from astraterm.identity_dialogs import GenerateKeyDialog
from smoke_multiexec import pump, wait_for, focus
from smoke_tabs import Pointer


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def make_key(path, passphrase=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    protection = (serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
                  if passphrase else serialization.NoEncryption())
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             protection)
    write_private(path, data)
    if passphrase:
        assert b"Proc-Type: 4,ENCRYPTED" in data and b"DEK-Info:" in data, "Fixture key is not encrypted"
        try:
            serialization.load_pem_private_key(data, password=b"definitely-wrong", backend=default_backend())
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError("Encrypted fixture accepted a wrong passphrase")
        loaded = serialization.load_pem_private_key(
            data, password=passphrase.encode("utf-8"), backend=default_backend())
        assert loaded.public_key().public_numbers() == key.public_key().public_numbers()
    public = paramiko.RSAKey(key=key)
    write_private(path + ".pub", (public.get_name() + " " + public.get_base64() + "\n").encode("ascii"))
    return public


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_sshd(directory, user_key, host_key, username):
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not os.path.isfile(sshd):
        raise RuntimeError("Isolated auth smoke needs the installed OpenSSH server")
    port = free_port()
    authorized = os.path.join(directory, "authorized_keys")
    write_private(authorized, (user_key.get_name() + " " + user_key.get_base64() + "\n").encode("ascii"))
    remote = os.path.join(directory, "remote")
    os.mkdir(remote, 0o700)
    for name in ("A", "B"):
        os.mkdir(os.path.join(remote, name), 0o700)
        write_private(os.path.join(remote, name, "only-%s.txt" % name), b"synthetic fixture\n")
    config = os.path.join(directory, "sshd_config")
    # The fixture directory is generated below a normal Linux home; reject a
    # surprising path rather than interpolating it into sshd configuration.
    if any(char in directory for char in ("\n", "\r", '"', "\\")):
        raise RuntimeError("Unsupported temporary path for isolated sshd")
    values = ["Port %d" % port, "ListenAddress 127.0.0.1", 'HostKey "%s"' % host_key,
              'PidFile "%s"' % os.path.join(directory, "sshd.pid"),
              'AuthorizedKeysFile "%s"' % authorized,
              "StrictModes yes", "PubkeyAuthentication yes", "PasswordAuthentication no",
              "ChallengeResponseAuthentication no", "UsePAM no", "UsePrivilegeSeparation no",
              "UseDNS no", "PrintMotd no", "LogLevel ERROR", "AllowUsers " + username,
              'Subsystem sftp internal-sftp -d "%s"' % remote]
    write_private(config, ("\n".join(values) + "\n").encode("utf-8"))
    checked = subprocess.Popen([sshd, "-t", "-f", config], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    unused, failure = checked.communicate(timeout=10)
    if checked.returncode:
        raise RuntimeError("Isolated sshd configuration rejected: " + failure.decode("utf-8", "replace")[-1500:])
    log_path = os.path.join(directory, "sshd.log")
    log = open(log_path, "wb")
    process = subprocess.Popen([sshd, "-D", "-e", "-f", config], stdout=log, stderr=log,
                               start_new_session=True, close_fds=True)
    log.close()
    def listening():
        if process.poll() is not None:
            with open(log_path, "r") as stream:
                raise RuntimeError("Non-root isolated sshd could not start: " + stream.read()[-1500:])
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.1)
            connection.close()
            return True
        except OSError:
            return False
    try:
        wait_for(listening, "isolated loopback sshd", timeout=8)
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise
    return process, port, remote, config, log_path


def stop_owned_process(process, expected_path):
    if process is None or process.poll() is not None:
        return
    with open("/proc/%d/cmdline" % process.pid, "rb") as stream:
        command = stream.read()
    assert expected_path.encode("utf-8") in command, "Refusing to stop a different sshd process"
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def stop_owned_agent(agent):
    if agent is None or agent.managed_pid is None:
        return
    pid = agent.managed_pid
    try:
        info = os.stat("/proc/%d" % pid)
        with open("/proc/%d/cmdline" % pid, "rb") as stream:
            command = stream.read()
        with open("/proc/%d/comm" % pid, "r") as stream:
            name = stream.read().strip()
    except FileNotFoundError:
        return
    assert info.st_uid == os.getuid() and name == "ssh-agent"
    owned_socket = agent.socket_path or os.path.join(agent.runtime_dir, "agent.sock")
    assert owned_socket.encode("utf-8") in command, "Refusing to stop a different ssh-agent"
    os.kill(pid, signal.SIGTERM)


def terminal_text(pane):
    return pane.terminal.get_text(lambda *unused: True, None)[0]


def main():
    if not Gtk.init_check()[0]:
        raise RuntimeError("Astra desktop session required; set DISPLAY")
    username = pwd.getpwuid(os.getuid()).pw_name
    secret = "fixture-" + uuid.uuid4().hex
    old_socket = os.environ.pop("SSH_AUTH_SOCK", None)
    sshd, agent, window = None, None, None
    delayed_release = threading.Event()
    with tempfile.TemporaryDirectory(prefix=".astraterm-auth-", dir=os.path.expanduser("~")) as directory:
        try:
            host_path = os.path.join(directory, "host.pem")
            profiles = ProfileStore(os.path.join(directory, "profiles.json"))
            parent = Gtk.Window()
            created = []
            generator = GenerateKeyDialog(parent, profiles, created.append, directory=directory)
            try:
                generator.profile_name.set_text("Encrypted generated Ed25519")
                generator.password.set_text(secret)
                generator.repeat.set_text(secret)
                generator.response(Gtk.ResponseType.OK)
                wait_for(lambda: bool(created) or not generator.busy, "SSH-key generation")
                assert created, generator.message.get_text()
                identity = created[0]
                private_path = identity["key_file"]
                line = read_public_key(private_path)
                generator.response(Gtk.ResponseType.APPLY)
                assert Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text() == line
            finally:
                generator.destroy()
                parent.destroy()
            kind, blob = line.split()[:2]
            public = types.SimpleNamespace(get_name=lambda kind=kind: kind, get_base64=lambda blob=blob: blob)
            if os.environ.get("ASTRA_TEST_ED25519_HOST") == "1":
                subprocess.check_call(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", host_path])
                with open(host_path + ".pub", "r") as stream:
                    kind, blob = stream.read().split()[:2]
                host_public = types.SimpleNamespace(get_name=lambda: kind, get_base64=lambda: blob)
            else:
                host_public = make_key(host_path)
            sshd, port, remote, sshd_config, sshd_log = start_sshd(directory, public, host_path, username)
            known = os.path.join(directory, "known_hosts")
            known_lines = ["[%s]:%d %s %s\n" % (host, port, host_public.get_name(), host_public.get_base64())
                           for host in ("127.0.0.1", "localhost")]
            write_private(known, "".join(known_lines).encode("ascii"))
            agent = SSHAgent(runtime_dir=os.path.join(directory, "agent"))
            store = SessionStore(os.path.join(directory, "sessions.json"))
            local = os.path.join(directory, "local")
            os.mkdir(local, 0o700)
            write_private(os.path.join(local, "local-only.txt"), b"local fixture\n")
            prompts, credential_prompts, errors = [], [], []
            original_argv = app_module.session_argv
            original_client = paramiko.SSHClient
            original_native_sftp = files_module._connect_openssh_sftp

            def isolated_native_sftp(profile, secret, policy, unused_known, **options):
                return original_native_sftp(profile, secret, policy, known, **options)

            def isolated_argv(profile):
                argv = original_argv(profile)
                if profile.get("protocol") == "ssh":
                    argv = ["StrictHostKeyChecking=yes" if value == "StrictHostKeyChecking=ask" else value for value in argv]
                    argv[1:1] = ["-F", "/dev/null", "-o", "UserKnownHostsFile=" + known,
                                 "-o", "GlobalKnownHostsFile=/dev/null", "-o", "BatchMode=yes"]
                return argv

            class FixtureSSHClient(original_client):
                def load_system_host_keys(self, filename=None):
                    self._system_host_keys.load(known)
                def load_host_keys(self, filename):
                    self._host_keys.load(known)

            def setup_window(existing_store=store, existing_profiles=profiles, existing_agent=agent):
                existing_store.upsert_group({"name": "Synthetic/Shared", "ssh": {
                    "port": port, "username": username, "identity_profile": identity["id"]}})
                result = app_module.MainWindow(existing_store, start_local=False, enable_identities=False,
                                               identity_store=existing_profiles, ssh_agent=existing_agent, auto_sftp=True)
                result.file_browser._local_path = local
                result.file_browser._path = local
                result.file_browser._request_credentials = lambda *unused: credential_prompts.append(True) or None
                result.file_browser._confirm_from_worker = lambda *unused: False
                result.present()
                return result

            with patch.object(app_module, "session_argv", isolated_argv), \
                    patch.object(terminal_module, "session_argv", isolated_argv), \
                    patch.object(paramiko, "SSHClient", FixtureSSHClient), \
                    patch.object(files_module, "_connect_openssh_sftp", isolated_native_sftp), \
                    patch.object(app_module, "error", lambda parent, text, detail="": errors.append(text + " " + str(detail))):
                window = setup_window()
                seen_dialogs = set()

                def checked(predicate):
                    if errors:
                        raise AssertionError("Unexpected GUI error: " + errors[-1])
                    if credential_prompts:
                        raise AssertionError("SFTP asked for credentials despite the unlocked profile")
                    for request in list(window._identity_jobs):
                        dialog = getattr(request, "dialog", None)
                        entry = getattr(request, "passphrase_entry", None)
                        if dialog is not None and entry is not None and id(dialog) not in seen_dialogs:
                            seen_dialogs.add(id(dialog))
                            prompts.append(True)
                            assert len(prompts) == 1, "Passphrase was requested repeatedly"
                            entry.set_text(secret)
                            dialog.response(Gtk.ResponseType.OK)
                    return predicate()

                def wait(predicate, label, timeout=25):
                    wait_for(lambda: checked(predicate), label, timeout=timeout)

                def open_ssh(host, label):
                    profile = default_session("ssh")
                    profile.update(host=host, port=1, username="fixture_wrong_user", name=label,
                                   group="Synthetic/Shared", inherit_group_ssh=True, identity_profile="",
                                   remote_command="/bin/bash --noprofile --norc -i")
                    previous = set(id(pane) for pane in window.all_panes())
                    window.open_session(profile)
                    wait(lambda: any(id(pane) not in previous for pane in window.all_panes()), "new managed SSH pane")
                    pane = next(pane for pane in window.all_panes() if id(pane) not in previous)
                    assert pane.profile["port"] == port and pane.profile["username"] == username
                    assert pane.profile["identity_profile"] == identity["id"], "Group SSH identity was not inherited"
                    wait(lambda: pane.pid is not None or pane.exited, "SSH child spawn")
                    assert not pane.exited, "SSH process exited before authentication"
                    token = "AUTH_SSH_" + uuid.uuid4().hex
                    half = len(token) // 2
                    pane.send("printf '%s%s\\n' '" + token[:half] + "' '" + token[half:] + "'\n")
                    wait(lambda: token in terminal_text(pane) or pane.exited, "authenticated managed SSH shell")
                    assert not pane.exited and token in terminal_text(pane), "SSH key authentication failed"
                    assert pane.profile.get("_agent_key_blob") == public.get_base64(), "Generated public key differs from the selected agent identity"
                    assert pane.profile["key_file"] != private_path and pane.profile["key_file"].endswith(".pub")
                    return pane

                def wait_sftp(host, path=None):
                    browser = window.file_browser
                    wait(lambda: browser.profile.get("host") == host and browser._mode == "remote"
                         and browser._sftp is not None and not browser._busy
                         and (path is None or browser._path == path), "active-host automatic SFTP")
                    return browser

                def activate(pane):
                    workspace = window._workspace_for_pane(pane)
                    window.notebook.set_current_page(window.notebook.page_num(workspace))
                    focus(window, pane)

                first = open_ssh("127.0.0.1", "Synthetic SSH A")
                browser = wait_sftp("127.0.0.1")
                wait(lambda:window.host_stats.labels['hostname'].get_text() not in ('','—'),"SSH host statistics")
                assert '/' in window.host_stats.labels['disks'].get_text()
                print("PASS read-only SSH host status via authenticated SFTP",flush=True)
                assert len(prompts) == 1 and not credential_prompts, "Expected one passphrase prompt and no password fallback"
                print("PASS group SSH port/user/identity inheritance / one GUI passphrase / SSH + automatic SFTP", flush=True)
                saved=dict(first.profile,inherit_group_ssh=False,port=1)
                window.store.upsert(saved);old_pid=first.pid;generation=first.connection_generation
                window.reconnect_pane(first,ask=False)
                wait(lambda:first.connection_generation>generation and first.exited,'latest saved refused port on reconnect')
                assert first.profile['port']==1
                saved['port']=port;window.store.upsert(saved);window.reconnect_pane(first,ask=False)
                wait(lambda:first.pid is not None and first.pid!=old_pid and not first.exited,'corrected saved SSH reconnect')
                browser=wait_sftp('127.0.0.1')
                assert len(prompts)==1 and not credential_prompts
                print('PASS reconnect uses corrected saved SSH parameters without another key unlock',flush=True)
                path_a, path_b = os.path.join(remote, "A"), os.path.join(remote, "B")
                browser._navigate(path_a)
                wait_sftp("127.0.0.1", path_a)
                client_a, sftp_a = browser._client, browser._sftp
                second = open_ssh("localhost", "Synthetic SSH B")
                browser = wait_sftp("localhost")
                browser._navigate(path_b)
                wait_sftp("localhost", path_b)
                client_b = browser._client
                assert client_b is not client_a and len(prompts) == 1
                screenshot = os.environ.get("ASTRA_AUTH_SCREENSHOT")
                if screenshot:
                    window.present()
                    pump(0.2)
                    native = window.get_window()
                    pixbuf = Gdk.pixbuf_get_from_window(native, 0, 0, native.get_width(), native.get_height())
                    assert pixbuf is not None, "Cannot capture the authentication preview window"
                    destination = os.path.abspath(os.path.expanduser(screenshot))
                    pixbuf.savev(destination, "png", [], [])
                    print("AUTH_SCREENSHOT=" + destination, flush=True)
                print("PASS second SSH endpoint reuses unlocked identity without credential dialogs", flush=True)

                activate(first)
                wait_sftp("127.0.0.1", path_a)
                assert browser._client is client_a
                activate(second)
                wait_sftp("localhost", path_b)
                assert browser._client is client_b
                print("PASS active-host switching restores cached transports and directories", flush=True)

                delayed_started = threading.Event()
                original_list = sftp_a.listdir_attr
                def delayed_list(path):
                    delayed_started.set()
                    if not delayed_release.wait(8):
                        raise RuntimeError("Delayed test read was not released")
                    return original_list(path)
                sftp_a.listdir_attr = delayed_list
                activate(first)
                wait(lambda: delayed_started.is_set(), "in-flight old-host directory read")
                activate(second)
                delayed_release.set()
                wait_sftp("localhost", path_b)
                pump(0.3)
                assert browser.profile["host"] == "localhost" and browser._client is client_b
                names = [row[1] for row in browser.model]
                assert "only-B.txt" in names and "only-A.txt" not in names
                window.move_pane_to_workspace(first, window._workspace_for_pane(second))
                activate(second)
                wait_sftp("localhost", path_b)
                with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                       "shared", "contracts", "file-drops.json"), encoding="utf-8") as stream:
                    drop_contract = json.load(stream)
                shell_directory = os.path.join(remote, drop_contract["directories"][2])
                os.mkdir(shell_directory)
                prompt = r"""printf '\033]0;%s@%s: %s\007' "$USER" "${HOSTNAME%%.*}" "$PWD" """
                first.send("PROMPT_COMMAND={}; cd -- {}\n".format(shlex.quote(prompt), shlex.quote(shell_directory)))
                wait(lambda: first.directory_report and first.directory_report[2] == shell_directory,
                     "Bash reports its directory after cd")
                drop_paths = [os.path.join(directory, name) for name in drop_contract["files"]]
                for path in drop_paths:
                    with open(path, "wb") as stream:
                        stream.write(os.path.basename(path).encode("utf-8"))

                popup = Gtk.Window(type=Gtk.WindowType.POPUP)
                source = Gtk.EventBox()
                source.add(Gtk.Label(label="Test files"))
                popup.add(source)
                popup.set_default_size(110, 50)
                popup.move(15, 35)
                source.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                                       [Gtk.TargetEntry.new("text/uri-list", 0, 0)], Gdk.DragAction.COPY)
                selected = [drop_paths[2]]
                source.connect("drag-data-get", lambda widget, context, data, info, timestamp:
                               data.set_uris([Gio.File.new_for_path(path).get_uri() for path in selected]))
                popup.show_all()
                pointer = Pointer()
                try:
                    pump(0.2)
                    pointer.drag(source, window.cancel_drop)
                    assert not browser._busy
                    assert not os.path.exists(os.path.join(path_b, "drop-C.txt"))
                    pointer.drag(source, browser.tree)
                    wait(lambda: os.path.isfile(os.path.join(path_b, "drop-C.txt")), "file drop on SFTP panel")
                    wait_sftp("localhost", path_b)
                    selected[:] = drop_paths[:2]
                    pointer.drag(source, first.terminal)
                    wait(lambda: all(os.path.isfile(os.path.join(shell_directory, os.path.basename(path)))
                                     for path in selected), "file drop on inactive SSH pane")
                    assert all(not os.path.exists(os.path.join(path_a, os.path.basename(path))) for path in selected)
                    assert all(not os.path.exists(os.path.join(path_b, os.path.basename(path))) for path in selected)
                    wait_sftp("127.0.0.1", path_a)
                    assert all(os.path.basename(path) not in terminal_text(first) for path in selected)
                    questions = []
                    original_confirm = browser._confirm_from_worker
                    browser._confirm_from_worker = lambda *args: questions.append(True) or False
                    try:
                        selected[:] = drop_paths[:1]
                        with open(selected[0], "wb") as stream:
                            stream.write(b"replacement must be cancelled")
                        pointer.drag(source, first.terminal)
                        wait(lambda: bool(questions) and not browser._busy, "cancel overwritten dropped file")
                        with open(os.path.join(shell_directory, os.path.basename(selected[0])), "rb") as stream:
                            assert stream.read() == os.path.basename(selected[0]).encode("utf-8")
                    finally:
                        browser._confirm_from_worker = original_confirm
                    print("PASS terminal and SFTP panel drops use independent directories", flush=True)
                finally:
                    pointer.close()
                    popup.destroy()
                print("PASS rapid host switching discards stale SFTP directory results", flush=True)

                window.new_local()
                wait(lambda: browser._mode == "local" and not browser._busy, "local terminal file view")
                assert browser.profile.get("protocol") != "ssh"
                assert [row[1] for row in browser.model] == ["local-only.txt"]
                with open(profiles.path, "rb") as stream:
                    metadata = stream.read()
                assert secret.encode("utf-8") not in metadata and b"PRIVATE KEY" not in metadata
                assert stat.S_IMODE(os.stat(profiles.path).st_mode) == 0o600
                assert len(prompts) == 1 and not credential_prompts
                print("PASS local focus clears remote listing / profile stores metadata only", flush=True)

                window.shutdown()
                window.destroy()
                pump(0.5)
                reused_agent = SSHAgent(runtime_dir=agent.runtime_dir)
                window = setup_window(SessionStore(os.path.join(directory, "sessions-reopened.json")),
                                      ProfileStore(profiles.path), reused_agent)
                open_ssh("127.0.0.1", "Reopened application")
                wait_sftp("127.0.0.1")
                assert len(prompts) == 1 and not credential_prompts
                assert reused_agent.socket_path == agent.socket_path and reused_agent.managed_pid is None
                print("PASS application restart reuses the shared unlocked agent without passphrase", flush=True)
                print("AUTH_PROFILES_SMOKE_OK", flush=True)
        except Exception as error:
            if window is not None:
                for pane in window.all_panes():
                    print("PANE pid=%r exited=%r\n%s" % (pane.pid, pane.exited,
                          terminal_text(pane).replace(secret, "[REDACTED]")[-1800:]), flush=True)
                print("SFTP status: " + window.file_browser.status_label.get_text().replace(secret, "[REDACTED]"), flush=True)
            if sshd is not None and os.path.isfile(sshd_log):
                with open(sshd_log, "r") as stream:
                    print("ISOLATED SSHD: " + stream.read()[-1800:].replace(secret, "[REDACTED]"), flush=True)
            raise RuntimeError(str(error).replace(secret, "[REDACTED]")) from None
        finally:
            delayed_release.set()
            if window is not None:
                window.shutdown()
                window.destroy()
                pump(0.5)
            stop_owned_process(sshd, sshd_config if sshd is not None else "")
            stop_owned_agent(agent)
            if old_socket is None:
                os.environ.pop("SSH_AUTH_SOCK", None)
            else:
                os.environ["SSH_AUTH_SOCK"] = old_socket


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
