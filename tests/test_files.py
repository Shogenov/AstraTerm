# -*- coding: utf-8 -*-
"""Pure safety tests for SFTP publishing, without starting GTK or using a server."""
from __future__ import unicode_literals

import importlib.util
import os
import json
from pathlib import Path
import stat
import sys
import threading
import tempfile
import types
import unittest
from unittest.mock import patch


def load_helpers():
    # Isolate only the native UI imports. The actual files.py implementation is
    # executed, so these tests also run on developer machines without GTK.
    repository = types.ModuleType("gi.repository")
    repository.Gtk = types.SimpleNamespace(Box=object)
    repository.Gdk = types.SimpleNamespace()
    repository.Gio = types.SimpleNamespace()
    repository.GLib = types.SimpleNamespace(idle_add=lambda *unused: None)
    repository.Pango = types.SimpleNamespace()
    gi = types.ModuleType("gi")
    gi.require_version = lambda *unused: None
    gi.repository = repository
    source = str(Path(__file__).resolve().parents[1] / "astraterm" / "files.py")
    spec = importlib.util.spec_from_file_location("astraterm._file_helpers_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository, "paramiko": None}):
        spec.loader.exec_module(module)
    return module


files = load_helpers()


def regular_info(uid=1000, gid=1000, mode=0o644):
    return types.SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid)


class FakeSFTP(object):
    def __init__(self):
        self.files = {"target": b"original content", "temporary": b"complete new content"}
        self.operations = []
        self.fail_publish = False
        self.fail_restore = False
        self.fail_backup_cleanup = False
        self.fail_chown = False

    def stat(self, path):
        if path not in self.files:
            raise IOError("No such file")
        return regular_info()

    def chmod(self, path, mode):
        self.operations.append(("chmod", path, mode))

    def chown(self, path, uid, gid):
        self.operations.append(("chown", path, uid, gid))
        if self.fail_chown:
            raise PermissionError("Operation not permitted")

    def rename(self, source, destination):
        self.operations.append(("rename", source, destination))
        if self.fail_publish and source == "temporary":
            raise IOError("Interrupted while publishing")
        if self.fail_restore and ".astraterm-backup-" in source:
            raise IOError("Interrupted while restoring")
        if destination in self.files:
            raise IOError("Destination already exists")
        self.files[destination] = self.files.pop(source)

    def remove(self, path):
        self.operations.append(("remove", path))
        if self.fail_backup_cleanup and ".astraterm-backup-" in path:
            raise IOError("Cannot remove backup")
        del self.files[path]


class AtomicSFTP(FakeSFTP):
    extension_error = None

    def posix_rename(self, source, destination):
        self.operations.append(("posix_rename", source, destination))
        if self.extension_error:
            raise IOError(self.extension_error)
        self.files[destination] = self.files.pop(source)


class FilenameSafetyTests(unittest.TestCase):
    def test_valid_single_components(self):
        for name in ("readme.txt", ".ssh", "file with spaces", "файл.txt"):
            self.assertTrue(files._safe_name(name), name)

    def test_reject_traversal_and_control_characters(self):
        for name in ("", ".", "..", "../secret", "/etc/passwd", "a/b", "a\\b",
                     "a\x00", "a\n", "a\r", None, 1):
            self.assertFalse(files._safe_name(name), repr(name))


class JumpHostTests(unittest.TestCase):
    def test_empty_jump_chain(self):
        self.assertEqual(files._parse_jump_hosts("", "local-user"), [])

    def test_default_user_and_port(self):
        result = files._parse_jump_hosts("gateway.example", "local-user")
        self.assertEqual([(item["host"], item["port"], item["username"]) for item in result],
                         [("gateway.example", 22, "local-user")])

    def test_chain_has_independent_users_ports_and_ipv6(self):
        result = files._parse_jump_hosts("first@gw1:2201,second@[2001:db8::2]:2202,[::1]", "local-user")
        self.assertEqual([(item["host"], item["port"], item["username"]) for item in result],
                         [("gw1", 2201, "first"), ("2001:db8::2", 2202, "second"),
                          ("::1", 22, "local-user")])
        self.assertEqual(files._endpoint_label(result[1]), "second@[2001:db8::2]:2202")

    def test_reject_invalid_proxyjump_before_connecting(self):
        for chain in ("-oProxyCommand=x", "gw,", "gw,,other", "gw:0", "gw:65536",
                      "user@@gw", "gw;touch", "gw\nother", "2001:db8::2", 0, False):
            with self.assertRaises(ValueError, msg=repr(chain)):
                files._parse_jump_hosts(chain, "local-user")


class FakeChannel(object):
    def __init__(self, destination=None):
        self.destination = destination
        self.closed = False
        self.timeout = None

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        self.timeout = timeout


class FakeTransport(object):
    def __init__(self):
        self.channels = []
        self.keepalive = None

    def open_channel(self, kind, destination, origin, timeout=None):
        if kind != "direct-tcpip":
            raise AssertionError("Unexpected channel kind")
        channel = FakeChannel(destination)
        self.channels.append(channel)
        return channel

    def set_keepalive(self, value):
        self.keepalive = value


class FakeSFTPConnection(object):
    def __init__(self):
        self.channel = FakeChannel()

    def get_channel(self):
        return self.channel

    def normalize(self, path):
        return "/home/remote-user"


class SSHFactory(object):
    def __init__(self, fail_index=None):
        self.clients = []
        self.close_order = []
        self.fail_index = fail_index

    def __call__(self):
        factory = self

        class Client(object):
            def __init__(self):
                self.index = len(factory.clients)
                self.transport = FakeTransport()
                self.arguments = None
                self.policy = None
                self.closed = False
                self.connected = False
                factory.clients.append(self)

            def load_system_host_keys(self):
                pass

            def load_host_keys(self, path):
                pass

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, **kwargs):
                self.arguments = kwargs
                if self.index == factory.fail_index:
                    raise RuntimeError("Simulated authentication failure")
                self.connected = True

            def get_transport(self):
                return self.transport

            def open_sftp(self):
                return FakeSFTPConnection()

            def close(self):
                self.closed = True
                factory.close_order.append(self.index)

        return Client()


class JumpConnectionTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"protocol": "ssh", "host": "target.internal", "port": 2222,
                        "username": "target-user", "jump_host": "one@gateway:2201,two@[::1]:2202"}

    def connect(self, factory, cancelled=None, jump_secrets=None):
        with patch.object(files, "paramiko", types.SimpleNamespace(SSHClient=factory)):
            return files._connect_sftp_chain(
                self.profile, "target-secret", ["first-secret", "second-secret"] if jump_secrets is None else jump_secrets,
                policy_factory=lambda endpoint: dict(endpoint), cancelled=cancelled)

    def test_nested_channels_use_correct_destinations_and_per_host_auth(self):
        factory = SSHFactory()
        target, sftp, home, gateways = self.connect(factory)
        self.assertIs(target, factory.clients[2])
        self.assertEqual(gateways, factory.clients[:2])
        self.assertEqual(home, "/home/remote-user")
        self.assertEqual(sftp.channel.timeout, 20)
        first, second, final = factory.clients
        self.assertIsNone(first.arguments["sock"])
        self.assertEqual(first.arguments["password"], "first-secret")
        self.assertEqual(first.arguments["username"], "one")
        self.assertEqual(first.arguments["hostname"], "gateway")
        self.assertIs(second.arguments["sock"], first.transport.channels[0])
        self.assertEqual(second.arguments["sock"].destination, ("::1", 2202))
        self.assertEqual(second.arguments["password"], "second-secret")
        self.assertIs(final.arguments["sock"], second.transport.channels[0])
        self.assertEqual(final.arguments["sock"].destination, ("target.internal", 2222))
        self.assertEqual(final.arguments["password"], "target-secret")
        self.assertEqual([client.policy["role"] for client in factory.clients],
                         ["SSH-шлюз 1", "SSH-шлюз 2", "Сервер SFTP"])
        self.assertTrue(all(client.transport.keepalive == 30 for client in factory.clients))

    def test_authentication_failure_closes_whole_chain_and_channels(self):
        factory = SSHFactory(fail_index=2)
        with self.assertRaises(RuntimeError):
            self.connect(factory)
        self.assertEqual(factory.close_order, [2, 1, 0])
        self.assertTrue(all(client.closed for client in factory.clients))
        self.assertTrue(all(channel.closed for client in factory.clients for channel in client.transport.channels))

    def test_cancellation_after_gateway_auth_closes_gateway(self):
        factory = SSHFactory()
        with self.assertRaisesRegex(RuntimeError, "отменено"):
            self.connect(factory, cancelled=lambda: bool(factory.clients and factory.clients[0].connected))
        self.assertEqual(len(factory.clients), 1)
        self.assertTrue(factory.clients[0].closed)

    def test_wrong_secret_count_fails_before_network_work(self):
        factory = SSHFactory()
        with self.assertRaises(ValueError):
            self.connect(factory, jump_secrets=[])
        self.assertEqual(factory.clients, [])


class NativeHostKeyFallbackTests(unittest.TestCase):
    class SSHError(Exception):
        pass

    class AuthenticationFailure(SSHError):
        pass

    class PublicKeyOnly(AuthenticationFailure):
        allowed_types = ["publickey"]

    def setUp(self):
        self.profile = {"protocol": "ssh", "host": "target.internal", "port": 2222,
                        "username": "alice", "_ssh_agent_sock": "/private/agent.sock"}
        self.failure = self.SSHError("Incompatible ssh peer (no acceptable host key)")

    def factory(self, error):
        factory = SSHFactory()
        original = factory.__call__

        def create():
            client = original()
            def fail(**arguments):
                client.arguments = arguments
                raise error
            client.connect = fail
            return client
        return factory, create

    def test_only_host_algorithm_negotiation_qualifies_for_native_retry(self):
        with patch.object(files, "paramiko", types.SimpleNamespace(SSHException=self.SSHError)):
            self.assertTrue(files._native_host_key_required(self.failure))
            for error in (RuntimeError(str(self.failure)), self.SSHError("Authentication failed"),
                          self.SSHError("Host key for server does not match"),
                          self.SSHError("Connection refused")):
                self.assertFalse(files._native_host_key_required(error))

    def test_direct_retry_closes_old_client_and_preserves_identity_and_cancel_hooks(self):
        factory, create = self.factory(self.failure)
        selected = types.SimpleNamespace(get_base64=lambda: "chosen-key")
        closed = []
        agent = types.SimpleNamespace(get_keys=lambda: (selected,), close=lambda: closed.append(True))
        profile = dict(self.profile, _agent_key_blob="chosen-key", _identity_required=True,
                       identity_profile="chosen-profile", key_file="/private/public.pub")
        register = lambda unused: None
        cancelled = lambda: False
        expected = (object(), object(), "/home/alice", [])
        def native(*arguments, **options):
            self.assertTrue(factory.clients[0].closed)
            forwarded, secret, policy, known = arguments
            self.assertEqual(forwarded["_agent_key_blob"], "chosen-key")
            self.assertEqual(forwarded["_ssh_agent_sock"], "/private/agent.sock")
            self.assertTrue(forwarded["_identity_required"])
            self.assertEqual(secret, "in-memory-password")
            self.assertEqual(policy["host"], "target.internal")
            self.assertIs(options["register_client"], register)
            self.assertIs(options["cancelled"], cancelled)
            return expected
        module = types.SimpleNamespace(SSHClient=create, Agent=lambda: agent, SSHException=self.SSHError)
        with patch.object(files, "paramiko", module), patch.object(files, "_connect_openssh_sftp", native):
            result = files._connect_sftp_chain(profile, "in-memory-password", [], lambda endpoint: endpoint,
                                               register_client=register, cancelled=cancelled)
        self.assertIs(result, expected)
        self.assertEqual(closed, [True])

    def test_unclassified_and_host_trust_errors_are_never_retried(self):
        for text in ("Authentication failed", "Host key mismatch"):
            error = self.SSHError(text)
            factory, create = self.factory(error)
            module = types.SimpleNamespace(SSHClient=create, SSHException=self.SSHError)
            with patch.object(files, "paramiko", module), \
                    patch.object(files, "_connect_openssh_sftp") as native:
                with self.assertRaises(self.SSHError):
                    files._connect_sftp_chain(self.profile, "", [], lambda endpoint: endpoint)
                native.assert_not_called()
            self.assertTrue(factory.clients[0].closed)

    def test_public_key_retry_uses_the_same_identity_without_the_vault_password(self):
        failure = self.PublicKeyOnly("Bad authentication type")
        factory, create = self.factory(failure)
        profile = dict(self.profile, key_file="/private/key", credential_profile="account")
        module = types.SimpleNamespace(SSHClient=create, SSHException=self.SSHError,
                                       AuthenticationException=self.AuthenticationFailure,
                                       BadAuthenticationType=self.PublicKeyOnly)
        with patch.object(files, "paramiko", module), patch.object(files, "_connect_openssh_sftp") as native:
            files._connect_sftp_chain(profile, "vault-password", [], lambda endpoint: endpoint)
            arguments, options = native.call_args
            self.assertTrue(options["public_key_only"])
            self.assertIsNone(arguments[1])
            self.assertEqual(arguments[0]["key_file"], profile["key_file"])
            self.assertEqual(arguments[0]["_ssh_agent_sock"], self.profile["_ssh_agent_sock"])
            self.assertTrue(factory.clients[0].closed)

    def test_password_only_profile_does_not_switch_authentication_methods(self):
        failure = self.PublicKeyOnly("Bad authentication type")
        factory, create = self.factory(failure)
        module = types.SimpleNamespace(SSHException=self.SSHError, AuthenticationException=self.AuthenticationFailure,
                                       BadAuthenticationType=self.PublicKeyOnly)
        with patch.object(files, "paramiko", module), patch.object(files, "_PasswordSSHClient", create), \
                patch.object(files, "_connect_openssh_sftp") as native:
            with self.assertRaises(self.PublicKeyOnly):
                files._connect_sftp_chain(dict(self.profile, credential_profile="account"), "vault-password", [],
                                          lambda endpoint: endpoint)
            native.assert_not_called()
            self.assertTrue(factory.clients[0].closed)

    def test_gateway_failure_cannot_be_retried_as_a_direct_connection(self):
        for failure in (self.failure, self.PublicKeyOnly("Bad authentication type")):
            factory, create = self.factory(failure)
            module = types.SimpleNamespace(SSHClient=create, SSHException=self.SSHError,
                                           AuthenticationException=self.AuthenticationFailure,
                                           BadAuthenticationType=self.PublicKeyOnly)
            with patch.object(files, "paramiko", module), patch.object(files, "_connect_openssh_sftp") as native:
                with self.assertRaises(self.SSHError):
                    files._connect_sftp_chain(dict(self.profile, jump_host="jump.internal"), "", [""],
                                              lambda endpoint: endpoint)
                native.assert_not_called()
            self.assertTrue(factory.clients[0].closed)

    def test_cancellation_after_failed_negotiation_prevents_native_launch(self):
        factory, create = self.factory(self.failure)
        module = types.SimpleNamespace(SSHClient=create, SSHException=self.SSHError)
        cancelled = lambda: bool(factory.clients and factory.clients[0].closed)
        with patch.object(files, "paramiko", module), patch.object(files, "_connect_openssh_sftp") as native:
            with self.assertRaisesRegex(RuntimeError, "отменено"):
                files._connect_sftp_chain(self.profile, "", [], lambda endpoint: endpoint, cancelled=cancelled)
            native.assert_not_called()


class CloseGuardTests(unittest.TestCase):
    def browser(self, busy=False, dirty=(), answer=False):
        errors, questions = [], []
        editors = [types.SimpleNamespace(
            get_title=lambda title=title: title,
            _astraterm_buffer=types.SimpleNamespace(get_modified=lambda: True)) for title in dirty]
        browser = types.SimpleNamespace(_busy=busy, _editors=editors,
                                        _error=lambda detail: errors.append(detail))
        def confirm(title, detail):
            questions.append((title, detail))
            return answer
        browser._confirm = confirm
        return browser, errors, questions

    def test_busy_operation_blocks_main_window_close(self):
        browser, errors, questions = self.browser(busy=True)
        self.assertFalse(files.FileBrowser.request_close(browser))
        self.assertEqual(len(errors), 1)
        self.assertEqual(questions, [])

    def test_dirty_editors_require_explicit_discard(self):
        browser, errors, questions = self.browser(dirty=("first.txt", "second.txt"), answer=False)
        self.assertFalse(files.FileBrowser.request_close(browser))
        self.assertIn("first.txt", questions[0][1])
        self.assertIn("second.txt", questions[0][1])
        browser, errors, questions = self.browser(dirty=("first.txt",), answer=True)
        self.assertTrue(files.FileBrowser.request_close(browser))

    def test_clean_idle_browser_closes_without_prompt(self):
        browser, errors, questions = self.browser()
        self.assertTrue(files.FileBrowser.request_close(browser))
        self.assertEqual(errors + questions, [])


class SelectedIdentityTests(unittest.TestCase):
    def test_selected_loaded_key_is_used_without_other_keys_or_private_key_decryption(self):
        factory = SSHFactory()
        wrong = types.SimpleNamespace(get_base64=lambda: "wrong-key")
        chosen = types.SimpleNamespace(get_base64=lambda: "chosen-key")
        closed = []
        agent = types.SimpleNamespace(get_keys=lambda: (wrong, chosen), close=lambda: closed.append(True))
        profile = {"protocol": "ssh", "host": "target.internal", "key_file": "/private/encrypted-key",
                   "jump_host": "one@gateway:2201", "_agent_key_blob": "chosen-key"}
        with patch.object(files, "paramiko", types.SimpleNamespace(SSHClient=factory, Agent=lambda: agent)):
            files._connect_sftp_chain(profile, "", [""], lambda endpoint: endpoint)
        self.assertEqual(len(factory.clients), 2)
        for client in factory.clients:
            self.assertIs(client.arguments["pkey"], chosen)
            self.assertIsNone(client.arguments["key_filename"])
            self.assertFalse(client.arguments["allow_agent"])
            self.assertFalse(client.arguments["look_for_keys"])
        self.assertEqual(closed, [True])

    def test_missing_selected_agent_key_never_attempts_an_unrelated_identity(self):
        factory = SSHFactory()
        closed = []
        agent = types.SimpleNamespace(get_keys=lambda: (), close=lambda: closed.append(True))
        with patch.object(files, "paramiko", types.SimpleNamespace(SSHClient=factory, Agent=lambda: agent)):
            with self.assertRaises(files.SSHIdentityUnavailable):
                files._connect_sftp_chain({"protocol": "ssh", "host": "target.internal",
                                          "_agent_key_blob": "unloaded-key"}, "", [], lambda endpoint: endpoint)
        self.assertEqual(factory.clients, [])
        self.assertEqual(closed, [True])

    def test_required_profile_cannot_fall_back_when_unlock_was_cancelled(self):
        factory = SSHFactory()
        with patch.object(files, "paramiko", types.SimpleNamespace(SSHClient=factory)):
            with self.assertRaises(files.SSHIdentityUnavailable):
                files._connect_sftp_chain({"protocol": "ssh", "host": "target.internal",
                                          "_identity_required": True}, "", [], lambda endpoint: endpoint)
        self.assertEqual(factory.clients, [])


class FileFollowTests(unittest.TestCase):
    def browser(self):
        browser = files.FileBrowser.__new__(files.FileBrowser)
        browser.profile = {"protocol": "ssh", "host": "first.internal", "username": "alice"}
        browser._auth_generation = "agent-generation"
        browser.auto_follow = True
        browser._closed, browser._busy = False, False
        browser._generation, browser._operation_kind = 0, None
        browser._follow_source = None
        browser._pending_profile, browser._pending_auth_generation = None, None
        browser._pending_upload = None
        browser._follow_failures = set()
        browser._remote_cache = files.OrderedDict()
        browser._remote_directories = files.OrderedDict()
        browser._client, browser._sftp, browser._gateway_clients = None, None, []
        browser._remote_home, browser._remote_path = None, None
        browser._connecting_clients = []
        browser._connection_lock = threading.Lock()
        browser._mode, browser._path = "local", "/home/local"
        browser._local_path = "/home/local"
        browser._editors = []
        browser._drag_root = None
        browser._drag_timers = set()
        browser.diagnostic_log = None
        browser.rows, browser.statuses, browser.errors, browser.refreshes = [], [], [], []
        browser.model = types.SimpleNamespace(clear=lambda: browser.rows.clear())
        browser.path_values = []
        browser.path_entry = types.SimpleNamespace(set_text=lambda text: browser.path_values.append(text))
        browser._status = lambda text: browser.statuses.append(text)
        browser._error = lambda text: browser.errors.append(text)
        browser._update_controls = lambda: None
        browser.refresh = lambda: browser.refreshes.append((browser._mode, browser._path))
        return browser

    def live_connection(self):
        closed = []
        state = {"active": True}
        transport = types.SimpleNamespace(is_active=lambda: state["active"], is_authenticated=lambda: True)
        client = types.SimpleNamespace(get_transport=lambda: transport,
                                       close=lambda: closed.append(True))
        return client, FakeSFTPConnection(), closed, state

    def test_dropped_files_keep_the_captured_remote_directory(self):
        browser = self.browser()
        browser._mode = "remote"
        browser._remote_path = "/remote/original"
        published, sources = [], []
        browser._sftp = types.SimpleNamespace(
            put=lambda source, target, callback: sources.append(source), remove=lambda target: None)
        browser._remote_stat = lambda unused, target: None
        browser._remote_replace = lambda unused, temp, target, original: published.append(target)
        browser._progress = lambda *unused: None
        tasks = []
        browser._submit = lambda label, work, *args, **kwargs: tasks.append(work)
        with tempfile.TemporaryDirectory() as directory:
            contract = json.loads((Path(__file__).resolve().parents[1] / "shared/contracts/file-drops.json").read_text(encoding="utf-8"))
            paths = [os.path.join(directory, name) for name in contract["files"]]
            for source in paths:
                Path(source).write_bytes(b"fixture")
            browser.upload_paths(paths)
            browser._remote_path = "/remote/changed"
            tasks[0]()
            self.assertEqual(sources, paths)
            self.assertEqual(published, ["/remote/original/" + name for name in contract["files"]])

    def test_terminal_drop_is_cancelled_if_its_connection_changes(self):
        browser = self.browser()
        browser._mode, browser._remote_path = "remote", "/remote/original"
        published, alive = [], [True]
        browser._sftp = types.SimpleNamespace(
            normalize=lambda directory: directory,
            stat=lambda directory: types.SimpleNamespace(st_mode=stat.S_IFDIR),
            put=lambda *args, **kwargs: alive.__setitem__(0, False), remove=lambda target: None)
        browser._remote_stat = lambda unused, target: None
        browser._remote_replace = lambda *args: published.append(True)
        browser._progress = lambda *unused: None
        tasks = []
        browser._submit = lambda label, work, *args, **kwargs: tasks.append(work)
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "fixture.txt")
            Path(source).write_bytes(b"fixture")
            browser.upload_paths([source], terminal_check=lambda: alive[0],
                                 terminal_directory=("", "", "/shell/cwd"))
            with self.assertRaises(IOError):
                tasks[0]()
        self.assertEqual(published, [])

    def test_unknown_terminal_directory_cannot_use_the_panel_path(self):
        browser = self.browser()
        browser._mode, browser._remote_path = "remote", "/remote/panel"
        browser.upload_paths(["/local/file"], terminal_check=lambda: True)
        self.assertEqual(browser.errors, [files.UNKNOWN_DIRECTORY])
        self.assertIsNone(browser._pending_upload)

    def test_last_directory_survives_a_cancelled_listing_transport(self):
        browser = self.browser()
        client, sftp, unused, state = self.live_connection()
        browser._client, browser._sftp = client, sftp
        browser._mode, browser._path = "remote", "/home/alice/work"
        browser._remote_path, browser._remote_home = browser._path, "/home/alice"
        key = files._sftp_profile_key(browser.profile, browser._auth_generation)
        browser._release_connection(cache=False)
        self.assertEqual(browser._remote_directories[key], "/home/alice/work")

    def test_switching_hosts_restores_transport_and_directory_without_reconnecting(self):
        browser = self.browser()
        client, sftp, closed, unused = self.live_connection()
        browser._client, browser._sftp = client, sftp
        browser._mode, browser._path = "remote", "/home/alice/work"
        browser._remote_path, browser._remote_home = browser._path, "/home/alice"
        browser.rows.append("old-host-row")
        pending, removed = [], []
        with patch.object(files, "GLib", types.SimpleNamespace(
                timeout_add=lambda delay, callback: pending.append(callback) or len(pending),
                source_remove=lambda source: removed.append(source))):
            browser.set_profile({"protocol": "ssh", "host": "second.internal"}, "agent-generation")
            self.assertEqual(browser.rows, [])
            self.assertEqual(browser.path_values[-1], "")
            self.assertIsNone(browser._sftp)
            browser.set_profile({"protocol": "ssh", "host": "first.internal", "username": "alice"},
                                "agent-generation")
        self.assertIs(browser._client, client)
        self.assertIs(browser._sftp, sftp)
        self.assertEqual(browser._path, "/home/alice/work")
        self.assertEqual(browser.refreshes[-1], ("remote", "/home/alice/work"))
        self.assertEqual(closed, [])
        self.assertEqual(removed, [1])

    def test_cache_is_bounded_and_shutdown_closes_cached_and_active_transports(self):
        browser = self.browser()
        connections = []
        for index in range(files.MAX_REMOTE_CONNECTIONS + 2):
            browser.profile = {"protocol": "ssh", "host": "host-%d" % index}
            client, sftp, closed, unused = self.live_connection()
            connections.append(closed)
            browser._client, browser._sftp = client, sftp
            browser._remote_home, browser._remote_path = "/home/alice", "/home/alice/work"
            browser._release_connection(cache=True)
        self.assertLessEqual(len(browser._remote_cache), files.MAX_REMOTE_CONNECTIONS - 1)
        self.assertTrue(connections[0])
        browser.shutdown()
        self.assertTrue(all(closed for closed in connections))

    def test_busy_write_defers_switch_and_hides_previous_host_rows_immediately(self):
        browser = self.browser()
        client, sftp, closed, unused = self.live_connection()
        browser._client, browser._sftp = client, sftp
        browser._busy, browser._operation_kind = True, "write"
        browser.rows.append("old-host-row")
        desired = {"protocol": "ssh", "host": "second.internal"}
        browser.set_profile(desired, "new-identity")
        self.assertEqual(browser.profile["host"], "first.internal")
        self.assertEqual(browser._pending_profile, desired)
        self.assertEqual(browser._pending_auth_generation, "new-identity")
        self.assertEqual(browser.rows, [])
        self.assertEqual(browser.path_values[-1], "")
        self.assertIs(browser._client, client)
        self.assertEqual(closed, [])

    def test_failed_automatic_target_does_not_prompt_again_on_focus_changes(self):
        browser = self.browser()
        target = {"protocol": "ssh", "host": "second.internal"}
        browser._follow_failures.add(files._sftp_profile_key(target, "agent-generation"))
        with patch.object(files, "GLib", types.SimpleNamespace(
                timeout_add=lambda *unused: self.fail("Failed target retried automatically"),
                source_remove=lambda *unused: None)):
            browser.set_profile(target, "agent-generation")
        self.assertEqual(browser._mode, "waiting")
        self.assertIn("повторной", browser.statuses[-1])

    def test_endpoint_cache_separates_identity_and_unlock_generation(self):
        profile = {"protocol": "ssh", "host": "target.internal", "_agent_key_blob": "first"}
        original = files._sftp_profile_key(profile, "one")
        self.assertNotEqual(original, files._sftp_profile_key(profile, "two"))
        self.assertNotEqual(original, files._sftp_profile_key(dict(profile, _agent_key_blob="second"), "one"))
        self.assertNotEqual(original, files._sftp_profile_key(dict(profile, username="other"), "one"))

    def test_connect_first_tries_agent_then_prompts_only_after_authentication_failure(self):
        browser = self.browser()
        factory = SSHFactory()
        attempts, prompts = [], []

        class AuthenticationFailure(Exception):
            pass

        def make_client():
            client = factory()
            original = client.connect
            def authenticate(**arguments):
                attempts.append(arguments)
                original(**arguments)
                if client.index == 0:
                    raise AuthenticationFailure("Authentication failed")
            client.connect = authenticate
            return client

        def submit(label, work, complete=None, failed=None, **unused):
            try:
                result = work()
            except Exception as error:
                failed(error)
            else:
                complete(result)
        browser._submit = submit
        browser._request_credentials = lambda profile, jumps: (prompts.append(len(attempts)) or ("password", []))
        fake_paramiko = types.SimpleNamespace(SSHClient=make_client, MissingHostKeyPolicy=object,
                                             AuthenticationException=AuthenticationFailure,
                                             SSHException=RuntimeError)
        with patch.object(files, "paramiko", fake_paramiko), \
                patch.object(files, "_connect_openssh_sftp", side_effect=AuthenticationFailure("Authentication failed")) as native:
            browser.connect_remote()
            self.assertTrue(native.call_args[1]["public_key_only"])
        self.assertEqual(prompts, [1])
        self.assertIsNone(attempts[0]["password"])
        self.assertEqual(attempts[1]["password"], "password")
        self.assertEqual(browser._mode, "remote")
        self.assertEqual(browser.errors, [])


class RemotePublishTests(unittest.TestCase):
    def publish(self, remote, original=None):
        files.FileBrowser._remote_replace(object(), remote, "temporary", "target", original)

    def test_new_file_never_replaces_an_unconfirmed_existing_target(self):
        remote = FakeSFTP()
        with self.assertRaises(IOError):
            self.publish(remote)
        self.assertEqual(remote.files["target"], b"original content")
        self.assertEqual(remote.files["temporary"], b"complete new content")

    def test_new_file_is_published_after_upload(self):
        remote = FakeSFTP()
        del remote.files["target"]
        self.publish(remote)
        self.assertEqual(remote.files, {"target": b"complete new content"})

    def test_existing_file_preserves_mode_and_removes_backup(self):
        remote = FakeSFTP()
        self.publish(remote, regular_info(mode=0o640))
        self.assertEqual(remote.files, {"target": b"complete new content"})
        self.assertIn(("chmod", "temporary", 0o640), remote.operations)

    def test_existing_file_preserves_owner_before_publish(self):
        remote = FakeSFTP()
        self.publish(remote, regular_info(uid=0, gid=4))
        self.assertEqual(remote.operations[0], ("chown", "temporary", 0, 4))
        self.assertEqual(remote.files["target"], b"complete new content")

    def test_owner_preservation_failure_leaves_original_untouched(self):
        remote = FakeSFTP()
        remote.fail_chown = True
        with self.assertRaises(PermissionError):
            self.publish(remote, regular_info(uid=0))
        self.assertEqual(remote.files["target"], b"original content")
        self.assertFalse(any(op[0] == "rename" for op in remote.operations))

    def test_interrupted_publish_restores_original(self):
        remote = FakeSFTP()
        remote.fail_publish = True
        with self.assertRaises(IOError):
            self.publish(remote, regular_info())
        self.assertEqual(remote.files, {"target": b"original content", "temporary": b"complete new content"})

    def test_failed_rollback_reports_recoverable_backup_path(self):
        remote = FakeSFTP()
        remote.fail_publish = True
        remote.fail_restore = True
        with self.assertRaisesRegex(IOError, "astraterm-backup-"):
            self.publish(remote, regular_info())
        backups = [name for name in remote.files if ".astraterm-backup-" in name]
        self.assertEqual(len(backups), 1)
        self.assertEqual(remote.files[backups[0]], b"original content")

    def test_symlinks_and_special_files_cannot_be_replaced(self):
        for file_type in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO):
            remote = FakeSFTP()
            original = types.SimpleNamespace(st_mode=file_type | 0o644)
            with self.assertRaises(IOError):
                self.publish(remote, original)
            self.assertEqual(remote.files["target"], b"original content")
            self.assertEqual(remote.operations, [])

    def test_atomic_extension_avoids_backup_rename_gap(self):
        remote = AtomicSFTP()
        self.publish(remote, regular_info())
        self.assertEqual(remote.files, {"target": b"complete new content"})
        self.assertTrue(any(op[0] == "posix_rename" for op in remote.operations))
        self.assertFalse(any(op[0] == "rename" for op in remote.operations))

    def test_unsupported_extension_uses_safe_backup_fallback(self):
        remote = AtomicSFTP()
        remote.extension_error = "Operation unsupported"
        self.publish(remote, regular_info())
        self.assertEqual(remote.files, {"target": b"complete new content"})

    def test_other_atomic_rename_failure_does_not_move_original(self):
        remote = AtomicSFTP()
        remote.extension_error = "Permission denied"
        with self.assertRaises(IOError):
            self.publish(remote, regular_info())
        self.assertEqual(remote.files["target"], b"original content")
        self.assertFalse(any(op[0] == "rename" for op in remote.operations))

    def test_backup_cleanup_failure_reports_success_and_retained_backup(self):
        remote = FakeSFTP()
        remote.fail_backup_cleanup = True
        with self.assertRaisesRegex(IOError, "Файл сохранён"):
            self.publish(remote, regular_info())
        self.assertEqual(remote.files["target"], b"complete new content")
        backups = [name for name in remote.files if ".astraterm-backup-" in name]
        self.assertEqual(remote.files[backups[0]], b"original content")


if __name__ == "__main__":
    unittest.main()
