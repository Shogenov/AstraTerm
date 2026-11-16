#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Opt-in localhost end-to-end test; run on the Astra VM, never from discovery.

DISPLAY=:0 ASTRA_TEST_PASSWORD=... /usr/bin/python3.5 tests/smoke_network.py
Credentials are read from the environment and never written or printed.
Only a unique .astraterm-test-* directory and temporary local files are modified.
"""
from __future__ import unicode_literals

import argparse
import base64
import getpass
import glob
import hashlib
import os
import posixpath
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from astraterm.core import default_session, session_argv, tunnel_argv
from astraterm.files import FileBrowser, _connect_sftp_chain, _close_clients
from astraterm.terminal import TerminalPane


def trusted_keys():
    result = []
    for path in glob.glob("/etc/ssh/ssh_host_*_key.pub"):
        with open(path, "r") as stream:
            fields = stream.read().split()
        if len(fields) >= 2:
            result.append((fields[0], fields[1], base64.b64decode(fields[1])))
    if not result:
        raise RuntimeError("No readable localhost SSH public host keys")
    return result


class LocalHostPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, allowed):
        self.allowed = allowed

    def missing_host_key(self, client, hostname, key):
        if key.asbytes() not in self.allowed:
            raise paramiko.SSHException("Unexpected localhost host key")


def pump():
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


def drain_events(duration=0.5):
    """Give VTE/GLib time to reap hung-up children before Python exits."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        pump()
        time.sleep(0.01)


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for test condition")


def terminal_text(pane):
    return pane.terminal.get_text(lambda *unused: True, None)[0]


def answer_password(pane, secret, state):
    text = terminal_text(pane)
    if not state[0] and re.search(r"(?im)(?:password|пароль)[^:\r\n]*:\s*$", text):
        # No password is sent before a genuine terminal password prompt.
        pane.send(secret + "\n")
        state[0] = True
    return text


def strict_ssh_arguments(argv, known_hosts):
    argv = list(argv)
    for index in range(len(argv) - 1):
        if argv[index] == "-o" and argv[index + 1].startswith("StrictHostKeyChecking="):
            argv[index + 1] = "StrictHostKeyChecking=yes"
    return argv[:1] + ["-o", "UserKnownHostsFile=" + known_hosts,
                       "-o", "GlobalKnownHostsFile=/dev/null",
                       "-o", "PreferredAuthentications=password",
                       "-o", "PubkeyAuthentication=no",
                       "-o", "NumberOfPasswordPrompts=1"] + argv[1:]


def test_sftp(profile, secret, keys, local_dir, with_gui):
    allowed = [item[2] for item in keys]
    factory = lambda endpoint: LocalHostPolicy(allowed)
    client, sftp, home, gateways = _connect_sftp_chain(profile, secret, [], factory)
    directory = posixpath.join(home, ".astraterm-test-" + uuid.uuid4().hex)
    created = []
    made_directory = False
    browser = window = None
    try:
        sftp.mkdir(directory, mode=0o700)
        made_directory = True
        payload = ("Астра · SFTP · проверка UTF-8\n".encode("utf-8") + b"\x00" + bytes(range(256))) * 64
        source = os.path.join(local_dir, "upload.bin")
        downloaded = os.path.join(local_dir, "download.bin")
        with open(source, "wb") as stream:
            stream.write(payload)
        uploaded = posixpath.join(directory, "binary.bin")
        created.append(uploaded)
        sftp.put(source, uploaded)
        sftp.get(uploaded, downloaded)
        with open(downloaded, "rb") as stream:
            assert hashlib.sha256(stream.read()).digest() == hashlib.sha256(payload).digest()

        temporary = posixpath.join(directory, "replacement.tmp")
        created.append(temporary)
        with sftp.open(temporary, "wb") as stream:
            stream.write(b"replacement complete\n")
        sftp.chmod(uploaded, 0o600)
        FileBrowser._remote_replace(object(), sftp, temporary, uploaded, sftp.stat(uploaded))
        with sftp.open(uploaded, "rb") as stream:
            assert stream.read() == b"replacement complete\n"
        assert stat.S_IMODE(sftp.stat(uploaded).st_mode) == 0o600

        text_path = posixpath.join(directory, "editor.txt")
        created.append(text_path)
        with sftp.open(text_path, "wb") as stream:
            stream.write("Исходный текст\n".encode("utf-8"))
        sftp.chmod(text_path, 0o600)

        via_jump = dict(profile)
        via_jump["jump_host"] = ",".join([profile["username"] + "@127.0.0.1:22"] * 2)
        jump_client, jump_sftp, jump_home, jump_clients = _connect_sftp_chain(
            via_jump, secret, [secret, secret], factory)
        try:
            assert len(jump_clients) == 2
            assert jump_home == home
            with jump_sftp.open(text_path, "rb") as stream:
                assert stream.read().decode("utf-8") == "Исходный текст\n"
        finally:
            _close_clients(jump_clients + [jump_client])
        print("PASS SFTP direct, two jump hosts, binary/UTF-8 hash, safe replace + mode", flush=True)

        if with_gui:
            gui_client, gui_sftp, gui_home, gui_jumps = _connect_sftp_chain(profile, secret, [], factory)
            browser = FileBrowser(profile)
            browser._client = gui_client
            browser._sftp = gui_sftp
            browser._gateway_clients = gui_jumps
            browser._mode = "remote"
            browser._path = directory
            browser._remote_path = directory
            browser._remote_home = gui_home
            window = Gtk.Window(title="AstraTerm network smoke · SFTP")
            window.set_default_size(600, 500)
            window.add(browser)
            window.show_all()
            wait_until(lambda: not browser._busy and len(browser.model) >= 2)
            browser._edit(text_path)
            wait_until(lambda: not browser._busy and len(browser._editors) == 1)
            editor = browser._editors[0]
            buffer = editor._astraterm_buffer
            buffer.set_text("Изменено в GTK-редакторе\n")
            editor.response(Gtk.ResponseType.APPLY)
            wait_until(lambda: not browser._busy and not buffer.get_modified())
            with sftp.open(text_path, "rb") as stream:
                assert stream.read().decode("utf-8") == "Изменено в GTK-редакторе\n"
            assert browser.request_close()
            editor.response(Gtk.ResponseType.CLOSE)
            print("PASS GTK SFTP list, editor load/save, clean close guard", flush=True)
    finally:
        if browser:
            browser.shutdown()
        if window:
            window.destroy()
        for path in reversed(created):
            try:
                sftp.remove(path)
            except IOError as exc:
                if getattr(exc, "errno", None) != 2:
                    raise
        if made_directory:
            sftp.rmdir(directory)
        _close_clients(gateways + [client])


def terminal_window(profile):
    window = Gtk.Window(title="AstraTerm network smoke · SSH")
    window.set_default_size(800, 420)
    pane = TerminalPane(profile)
    window.add(pane)
    window.show_all()
    return window, pane


def test_ssh(profile, secret, known_hosts):
    token = "ASTRATERM_SSH_" + uuid.uuid4().hex
    ssh_profile = dict(profile)
    ssh_profile["remote_command"] = ""
    ssh_profile["_argv"] = strict_ssh_arguments(session_argv(ssh_profile), known_hosts)
    window, pane = terminal_window(ssh_profile)
    state = [False]
    try:
        print("CHECK VTE SSH password authentication", flush=True)
        wait_until(lambda: re.search(r"(?m)(?:^|\n)[^\r\n]*[$#]\s*$",
                   answer_password(pane, secret, state)) and state[0], timeout=25)
        assert state[0], "No authenticated password prompt observed"
        # The echoed command cannot contain the complete token; only executed
        # printf output joins the two independent arguments into that token.
        middle = len(token) // 2
        pane.send("printf '%s%s\\n' '" + token[:middle] + "' '" + token[middle:] + "'\n")
        wait_until(lambda: token in terminal_text(pane), timeout=15)
        print("PASS VTE SSH authentication and interactive remote shell token", flush=True)
    except Exception as exc:
        content = terminal_text(pane).replace(secret, "[REDACTED]")
        raise RuntimeError("SSH smoke failed: %s; password_sent=%r pid=%r exited=%r exit_code=%r\n%s" %
                           (str(exc).replace(secret, "[REDACTED]"), state[0], pane.pid,
                            pane.exited, pane.exit_code, content))
    finally:
        pane.close()
        window.destroy()
        drain_events()


def test_fast_ssh(profile, secret, known_hosts, directory):
    """Regression: a short remote command must retain its final PTY output."""
    print("CHECK fast SSH remote-command output, three iterations", flush=True)
    for iteration in range(3):
        token = "ASTRATERM_FAST_SSH_" + uuid.uuid4().hex
        ssh_profile = dict(profile)
        ssh_profile["remote_command"] = "printf '%s\\n' '" + token + "'"
        ssh_profile["_argv"] = strict_ssh_arguments(session_argv(ssh_profile), known_hosts)
        window, pane = terminal_window(ssh_profile)
        state = [False]
        output_path = os.path.join(directory, "fast-ssh-%d.txt" % iteration)
        full_text = ""

        def exited():
            answer_password(pane, secret, state)
            return pane.exited

        try:
            wait_until(exited, timeout=25)
            # Terminal.feed() schedules parsing on GTK idle. child-exited alone
            # does not imply drained bytes have entered the scrollback yet.
            drain_events(0.3)
            pane.save_output(output_path)
            with open(output_path, "r", encoding="utf-8", errors="replace") as stream:
                full_text = stream.read()
            assert state[0], "No authenticated password prompt observed"
            assert pane.exit_code == 0, "Remote command did not exit successfully"
            assert token in full_text, "Fast remote-command output missing from full scrollback"
            print("PASS fast SSH output %d/3" % (iteration + 1), flush=True)
        except Exception as exc:
            content = (full_text or terminal_text(pane)).replace(secret, "[REDACTED]")
            raise RuntimeError("Fast SSH smoke failed: %s; iteration=%d password_sent=%r pid=%r exited=%r exit_code=%r\n%s" %
                               (str(exc).replace(secret, "[REDACTED]"), iteration + 1, state[0],
                                pane.pid, pane.exited, pane.exit_code, content))
        finally:
            pane.close()
            window.destroy()
            drain_events()


def test_local_tunnel(profile, secret, known_hosts):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.3)
    stop = threading.Event()
    destination_port = server.getsockname()[1]
    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    listen_port = reservation.getsockname()[1]
    reservation.close()

    def echo():
        while not stop.is_set():
            try:
                connection, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                connection.settimeout(1)
                data = connection.recv(4096)
                if data:
                    connection.sendall(data)
            finally:
                connection.close()

    thread = threading.Thread(target=echo)
    thread.daemon = True
    thread.start()
    tunnel = {"type": "local", "host": "127.0.0.1", "port": 22,
              "username": profile["username"], "listen_port": listen_port,
              "bind_host": "127.0.0.1", "destination_host": "127.0.0.1",
              "destination_port": destination_port}
    tunnel_profile = {"protocol": "tunnel", "name": "Smoke local forward",
                      "encoding": "UTF-8", "_argv": strict_ssh_arguments(tunnel_argv(tunnel), known_hosts)}
    window, pane = terminal_window(tunnel_profile)
    state = [False]
    payload = b"astraterm-forward-" + uuid.uuid4().hex.encode("ascii")

    def forwarded():
        answer_password(pane, secret, state)
        try:
            connection = socket.create_connection(("127.0.0.1", listen_port), timeout=0.15)
        except OSError:
            return False
        try:
            connection.settimeout(1)
            connection.sendall(payload)
            received = b""
            while len(received) < len(payload):
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received += chunk
            return received == payload
        except (OSError, socket.timeout):
            return False
        finally:
            connection.close()

    try:
        print("CHECK SSH local tunnel TCP echo", flush=True)
        wait_until(forwarded, timeout=25)
        assert state[0]
        print("PASS SSH local tunnel with actual TCP echo round trip", flush=True)
    except Exception as exc:
        content = terminal_text(pane).replace(secret, "[REDACTED]")
        raise RuntimeError("Tunnel smoke failed: %s; password_sent=%r pid=%r exited=%r exit_code=%r\n%s" %
                           (str(exc).replace(secret, "[REDACTED]"), state[0], pane.pid,
                            pane.exited, pane.exit_code, content))
    finally:
        pane.close()
        window.destroy()
        stop.set()
        server.close()
        thread.join(2)
        drain_events()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sftp-only", action="store_true", help="omit all display-dependent checks")
    args = parser.parse_args()
    secret = os.environ.get("ASTRA_TEST_PASSWORD")
    if not secret:
        parser.error("ASTRA_TEST_PASSWORD is required; no credentials are stored in this script")
    username = os.environ.get("ASTRA_TEST_USER") or getpass.getuser()
    if not args.sftp_only and not Gtk.init_check()[0]:
        parser.error("GTK display is required (set DISPLAY or use --sftp-only)")
    profile = default_session("ssh")
    profile.update(host="127.0.0.1", username=username, port=22, name="Network smoke", compression=False)
    keys = trusted_keys()
    with tempfile.TemporaryDirectory(prefix="astraterm-network-") as directory:
        known = os.path.join(directory, "known_hosts")
        with open(known, "w") as stream:
            for kind, encoded, raw in keys:
                stream.write("127.0.0.1,localhost %s %s\n" % (kind, encoded))
        os.chmod(known, 0o600)
        test_sftp(profile, secret, keys, directory, not args.sftp_only)
        if not args.sftp_only:
            test_ssh(profile, secret, known)
            test_fast_ssh(profile, secret, known, directory)
            test_local_tunnel(profile, secret, known)
    print("NETWORK SMOKE PASS", flush=True)


if __name__ == "__main__":
    main()
