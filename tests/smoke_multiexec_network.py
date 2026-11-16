#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Opt-in real SSH MultiExec checks on the Astra VM's graphical desktop.

ASTRA_TEST_USER defaults to the local login. ASTRA_TEST_PASSWORD is read only
from the environment, or from one stdin line when the variable is missing.
ASTRA_TEST_HOST defaults to 127.0.0.1 and must identify this same VM: its public
host keys are pinned from /etc/ssh. Two addresses do not represent independent
remote servers. No credentials, user configuration, or remote files are saved.
"""
from __future__ import print_function, unicode_literals

import getpass
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

from astraterm.app import MainWindow
from astraterm.core import SessionStore, default_session, session_argv, validate_host
from smoke_multiexec import focus, press, pump, wait_for
from smoke_network import trusted_keys, strict_ssh_arguments, answer_password, terminal_text


def type_command(window, pane, command):
    """Enter a command through real GDK events, never through pane.send()."""
    focus(window, pane)
    for character in command:
        if ord(character) < 32 or ord(character) > 126:
            raise ValueError("This network smoke types printable ASCII only")
        press(pane.terminal, Gdk.unicode_to_keyval(ord(character)))
    press(pane.terminal, Gdk.KEY_Return)


def token_command(token):
    # Separate arguments keep the entire token out of the echoed command line.
    # Only an executed printf joins them, so echoed input cannot pass the test.
    middle = len(token) // 2
    return "printf '%s%s\\n' '" + token[:middle] + "' '" + token[middle:] + "'"


def new_token():
    return "ME_" + uuid.uuid4().hex[:20]


def open_ssh(window, host, username, secret, known_hosts, label):
    profile = default_session("ssh")
    profile.update(host=host, username=username, name=label, compression=False, x11=False)
    profile["_argv"] = strict_ssh_arguments(session_argv(profile), known_hosts)
    workspace = window.open_session(profile)
    if workspace is None:
        raise AssertionError("SSH workspace was not created")
    pane = workspace.active
    state = [False]

    def shell_ready():
        text = answer_password(pane, secret, state)
        if pane.exited:
            raise AssertionError("SSH exited before interactive shell was ready")
        return state[0] and text.rstrip().endswith(("$", "#"))

    # Programmatic password entry is limited to a verified password prompt,
    # while live MultiExec is still disabled and no recipients are selected.
    assert not window.multiexec.enabled
    try:
        wait_for(shell_ready, "authenticated SSH shell " + label, timeout=25)
    except Exception as exc:
        raise RuntimeError("%s: %s; password_sent=%r exited=%r exit_code=%r\n%s" %
                           (label, str(exc).replace(secret, "[REDACTED]"), state[0], pane.exited,
                            pane.exit_code, terminal_text(pane).replace(secret, "[REDACTED]"))) from None
    return pane


def main():
    secret = os.environ.get("ASTRA_TEST_PASSWORD")
    if secret is None:
        secret = sys.stdin.readline().rstrip("\r\n")
    if not secret:
        raise SystemExit("ASTRA_TEST_PASSWORD or one password line on stdin is required")
    username = os.environ.get("ASTRA_TEST_USER") or getpass.getuser()
    second_host = validate_host(os.environ.get("ASTRA_TEST_HOST") or "127.0.0.1")
    if not Gtk.init_check()[0]:
        raise SystemExit("A graphical GTK session is required; set DISPLAY")
    keys = trusted_keys()
    with tempfile.TemporaryDirectory(prefix="astraterm-multiexec-network-") as directory:
        known_hosts = os.path.join(directory, "known_hosts")
        names = ["127.0.0.1"]
        if second_host not in names:
            names.append(second_host)
        with open(known_hosts, "w") as stream:
            for kind, encoded, unused in keys:
                stream.write("%s %s %s\n" % (",".join(names), kind, encoded))
        os.chmod(known_hosts, 0o600)
        window = MainWindow(SessionStore(os.path.join(directory, "sessions.json")), start_local=False)
        window.present()
        panes = []

        def snapshot(pane):
            index = panes.index(pane)
            path = os.path.join(directory, "scrollback-%d.txt" % index)
            pane.save_output(path)
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                return stream.read()

        def assert_once(targets, token, label, timeout=12):
            wait_for(lambda: all(token in snapshot(pane) for pane in targets), label, timeout=timeout)
            pump(0.2)
            for pane in targets:
                assert snapshot(pane).count(token) == 1, "Duplicated execution in selected SSH pane"

        def assert_absent(targets, token):
            pump(0.2)
            for pane in targets:
                assert token not in snapshot(pane), "Input reached an excluded/paused recipient"

        try:
            first = open_ssh(window, "127.0.0.1", username, secret, known_hosts, "SSH A · localhost")
            panes.append(first)
            second = open_ssh(window, second_host, username, secret, known_hosts, "SSH B · test address")
            panes.append(second)
            local_workspace = window.new_local()
            assert local_workspace is not None
            excluded = local_workspace.active
            panes.append(excluded)
            wait_for(lambda: excluded.pid is not None and
                     terminal_text(excluded).rstrip().endswith(("$", "#")), "excluded local shell")
            assert window.all_panes() == panes
            original_pids = [pane.pid for pane in panes]
            original_parents = [pane.get_parent() for pane in panes]
            print("PASS two independently authenticated SSH sessions before MultiExec", flush=True)

            window.set_tiled_view(True)
            pump(0.3)
            assert window.tiled.active and window.tiled.panes == panes
            assert [pane.pid for pane in panes] == original_pids
            assert all(pane.terminal.get_mapped() for pane in panes)
            assert window.multiexec.set_selected(first, True)
            assert window.multiexec.set_selected(second, True)
            assert not window.multiexec.is_selected(excluded)
            window.set_multiexec(True)
            pump(0.2)
            assert window.multiexec.enabled

            shared = new_token()
            type_command(window, first, token_command(shared))
            assert_once((first, second), shared, "real SSH selected-pair command")
            assert_absent((excluded,), shared)
            print("PASS real GDK input executes once in both selected SSH panes; local excluded", flush=True)

            window.multiexec.set_selected(second, False)
            only_first = new_token()
            type_command(window, first, token_command(only_first))
            assert_once((first,), only_first, "unselected SSH pane excluded")
            assert_absent((second, excluded), only_first)
            print("PASS removing one SSH recipient stops its live input immediately", flush=True)

            assert window.multiexec.set_selected(second, True)
            window.set_multiexec(False)
            pump(0.1)
            paused = new_token()
            type_command(window, first, token_command(paused))
            assert_once((first,), paused, "paused MultiExec source remains local")
            assert_absent((second, excluded), paused)
            print("PASS paused MultiExec keeps terminal input local", flush=True)

            window.set_multiexec(True)
            pump(0.1)
            type_command(window, first, "sleep 20")
            pump(0.4)
            focus(window, first)
            press(first.terminal, Gdk.KEY_c, Gdk.ModifierType.CONTROL_MASK)
            pump(0.2)
            interrupted = new_token()
            type_command(window, first, token_command(interrupted))
            # Without Ctrl+C propagation the second shell remains asleep for20s.
            assert_once((first, second), interrupted, "Ctrl+C interrupts both SSH foreground commands", timeout=9)
            assert_absent((excluded,), interrupted)
            print("PASS Ctrl+C interrupts foreground sleep in both SSH sessions", flush=True)

            window.set_multiexec(False)
            window.set_tiled_view(False)
            pump(0.3)
            assert not window.tiled.active and not window.multiexec.enabled
            assert [pane.pid for pane in panes] == original_pids
            assert [pane.get_parent() for pane in panes] == original_parents
            assert_once((first, second), shared, "SSH scrollback preserved after tile restoration")
            print("PASS tile/restore preserves SSH processes, parents and terminal history", flush=True)
            print("NETWORK MULTIEXEC SMOKE PASS", flush=True)
        except Exception as exc:
            print("FAIL network MultiExec: " + str(exc).replace(secret, "[REDACTED]"), flush=True)
            for index, pane in enumerate(panes):
                try:
                    content = snapshot(pane).replace(secret, "[REDACTED]")[-3500:]
                except Exception:
                    content = terminal_text(pane).replace(secret, "[REDACTED]")[-3500:]
                print("PANE %d pid=%r exited=%r selected=%r\n%s" %
                      (index, pane.pid, pane.exited, window.multiexec.is_selected(pane), content), flush=True)
            raise RuntimeError("Network MultiExec smoke failed; see redacted diagnostics") from None
        finally:
            window.shutdown()
            window.destroy()
            pump(0.8)


if __name__ == "__main__":
    main()
