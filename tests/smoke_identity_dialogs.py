#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Real GTK identity workflows with an isolated metadata store and fake agent.

Run in the user's desktop: DISPLAY=:0 /usr/bin/python3.5 tests/smoke_identity_dialogs.py
No real SSH agent, private key, network, or saved user profile is touched.
"""
from __future__ import print_function, unicode_literals

import os
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from astraterm import identity_dialogs as ui
from astraterm.ssh_profiles import ProfileStore


TEST_PASSPHRASE = "temporary-ui-fixture-passphrase"
FINGERPRINT = "SHA256:" + "A" * 43


def pump(seconds=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.003)


def wait_for(predicate, label, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump()
        if predicate():
            return
    raise AssertionError("Timed out: " + label)


def button_named(widget, label):
    if isinstance(widget, Gtk.Button) and widget.get_label() == label:
        return widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            result = button_named(child, label)
            if result is not None:
                return result
    return None


class FakeAgent(object):
    def __init__(self):
        self.loaded = {}
        self.unlock_calls = []
        self.ensure_threads = []
        self.block = None
        self.started = threading.Event()

    def ensure(self):
        self.ensure_threads.append(threading.get_ident())
        return "/unused-test-agent.sock"

    def loaded_identity(self, key_file, fingerprint=""):
        self.started.set()
        if self.block is not None:
            self.block.wait(5)
        return self.loaded.get(key_file)

    def unlock(self, key_file, passphrase=""):
        correct = passphrase == TEST_PASSPHRASE
        self.unlock_calls.append((threading.get_ident(), bool(passphrase), correct))
        if not correct:
            raise ValueError("Fixture key remains locked")
        identity = {"fingerprint": FINGERPRINT, "blob": "fixture-public-blob",
                    "type": "ssh-rsa", "comment": "test"}
        self.loaded[key_file] = identity
        return identity


def main():
    with tempfile.TemporaryDirectory(prefix="astraterm-identity-ui-") as directory:
        key_file = os.path.join(directory, "fixture-key")
        with open(key_file, "w") as stream:
            stream.write("Not a private key. GUI-only test fixture.\n")
        store = ProfileStore(os.path.join(directory, "profiles.json"))
        agent = FakeAgent()
        original_discovery = ui.discover_keys
        ui.discover_keys = lambda: [key_file]
        parent = Gtk.Window(title="AstraTerm identity UI smoke")
        parent.set_default_size(500, 220)
        parent.show_all()
        parent.present()
        results = []
        try:
            request = ui.ensure_identity(parent, store, agent,
                                         callback=lambda profile, error: results.append((profile, error)))
            same = ui.ensure_identity(parent, store, agent,
                                      callback=lambda profile, error: results.append((profile, error)))
            assert same is request, "Concurrent identity requests did not coalesce"
            wait_for(lambda: isinstance(request.dialog, ui.IdentityProfileDialog), "first-launch profile dialog")
            editor = request.dialog
            editor.name_entry.set_text("GUI fixture profile")
            editor.key_entry.set_text(key_file)
            editor.response(Gtk.ResponseType.OK)
            wait_for(lambda: request.dialog is not None and
                     request.dialog.get_title() == "Разблокировать SSH-ключ", "passphrase prompt")
            assert store.configured and store.default_id, "First identity did not become the default"
            assert results == [], "Locked key was returned as ready"

            first_prompt = request.dialog
            old_entry = request.passphrase_entry
            old_entry.set_text("incorrect-fixture-value")
            first_prompt.response(Gtk.ResponseType.OK)
            assert old_entry.get_text() == "", "Passphrase remained in the destroyed entry"
            wait_for(lambda: request.dialog is not None and request.dialog is not first_prompt, "retry prompt")
            assert results == [], "Incorrect passphrase completed the request"

            old_entry = request.passphrase_entry
            old_entry.set_text(TEST_PASSPHRASE)
            request.dialog.response(Gtk.ResponseType.OK)
            assert old_entry.get_text() == "", "Successful passphrase remained in the entry"
            wait_for(lambda: len(results) == 2, "coalesced success callbacks")
            assert all(profile and error is None for profile, error in results)
            assert store.get()["fingerprint"] == FINGERPRINT
            assert ProfileStore(store.path).get()["fingerprint"] == FINGERPRINT
            with open(store.path, "r", encoding="utf-8") as stream:
                assert TEST_PASSPHRASE not in stream.read(), "Metadata contains a passphrase"
            assert all(thread != threading.get_ident() for thread in agent.ensure_threads)
            assert all(call[0] != threading.get_ident() for call in agent.unlock_calls)
            print("PASS first-launch key selection / retry / shared unlock / metadata / worker threads", flush=True)

            before = len(agent.unlock_calls)
            second = ui.ensure_identity(parent, store, agent,
                                        callback=lambda profile, error: results.append((profile, error)))
            wait_for(lambda: len(results) == 3, "already loaded identity")
            assert second.dialog is None and results[-1][1] is None
            assert len(agent.unlock_calls) == before, "Loaded identity requested the passphrase again"
            print("PASS loaded identity reused without another passphrase prompt", flush=True)

            agent.loaded.clear()
            noninteractive = []
            ui.ensure_identity(parent, store, agent, interactive=False,
                               callback=lambda profile, error: noninteractive.append((profile, error)))
            wait_for(lambda: bool(noninteractive), "noninteractive locked result")
            assert isinstance(noninteractive[0][1], ui.IdentityLocked)
            canceled = []
            request = ui.ensure_identity(parent, store, agent,
                                         callback=lambda profile, error: canceled.append((profile, error)))
            wait_for(lambda: request.dialog is not None, "cancelable unlock")
            request.dialog.response(Gtk.ResponseType.CANCEL)
            wait_for(lambda: bool(canceled), "cancel callback")
            assert isinstance(canceled[0][1], ui.IdentityCancelled)
            print("PASS noninteractive locked result / explicit cancellation", flush=True)

            changes = []
            manager = ui.manage_profiles(parent, store, agent, lambda: changes.append(True))
            assert len(manager.model) == 1
            button = button_named(manager, "Использовать системный ssh-agent без профиля по умолчанию")
            assert button is not None
            button.clicked()
            pump()
            assert not store.default_id and store.configured and changes
            button_named(manager, "Добавить").clicked()
            editors = [window for window in Gtk.Window.list_toplevels()
                       if isinstance(window, ui.IdentityProfileDialog) and window.get_transient_for() is manager]
            assert len(editors) == 1, "Profile editor did not open"
            manager.response(Gtk.ResponseType.CLOSE)
            assert not editors[0]._alive, "Closing the manager left an editable orphan dialog"
            skipped = []
            ui.ensure_identity(parent, store, agent,
                               callback=lambda profile, error: skipped.append((profile, error)))
            wait_for(lambda: bool(skipped), "system-agent preference")
            assert skipped == [(None, None)], "System-agent choice did not skip profile prompting"
            assert os.path.isfile(key_file), "Profile management removed a key file"
            print("PASS profile manager / system-agent preference / key file retained", flush=True)

            store.set_default(store.profiles[0]["id"])
            agent.started.clear()
            agent.block = threading.Event()
            closing = Gtk.Window()
            closing.show_all()
            destroyed = []
            request = ui.ensure_identity(closing, store, agent,
                                         callback=lambda profile, error: destroyed.append((profile, error)))
            wait_for(agent.started.is_set, "background check before parent closes")
            closing.destroy()
            assert destroyed and isinstance(destroyed[0][1], ui.IdentityCancelled)
            agent.block.set()
            pump(0.2)
            assert request.finished and request.dialog is None
            print("PASS closing parent invalidates asynchronous unlock callbacks", flush=True)
            print("IDENTITY_UI_SMOKE_OK", flush=True)
        finally:
            if agent.block is not None:
                agent.block.set()
            for request in list(ui._pending.values()):
                request.cancel()
            for window in Gtk.Window.list_toplevels():
                if window is not parent:
                    window.destroy()
            parent.destroy()
            ui.discover_keys = original_discovery
            pump()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
