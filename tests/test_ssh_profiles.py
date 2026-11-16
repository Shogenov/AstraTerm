"""Identity metadata and byte-safe agent/askpass boundaries without GTK."""
from __future__ import unicode_literals

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from astraterm.ssh_profiles import (ProfileStore, SSHAgent, SSHProfileError,
                                    _public_identity, _read_identities,
                                    _verify_terminal_config, discover_keys, generate_profile,
                                    public_key_blob, read_public_key, verify_terminal_identity)
from astraterm.core import default_session


def public_descriptor():
    kind = b"ssh-rsa"
    blob = struct.pack(">I", len(kind)) + kind + b"\x00\x00\x00\x03abc"
    return "ssh-rsa " + base64.b64encode(blob).decode("ascii") + " public comment"


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="astraterm-profile-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "profiles.json")

    def profile(self, name="first"):
        return {"id": name, "name": name, "key_file": os.path.join(self.directory.name, name)}

    @unittest.skipUnless(os.path.isfile("/usr/bin/ssh-keygen"), "Requires the Astra Linux OpenSSH client")
    def test_generated_key_pair_follows_shared_contract(self):
        root = Path(__file__).resolve().parents[1]
        contract = json.loads((root / "shared/contracts/sessions.json").read_text(encoding="utf-8"))["key_generation"]
        store = ProfileStore(self.path)
        directory = self.directory.name
        for item in contract["algorithms"]:
            secret = "synthetic-key-phrase" if item["protected"] else ""
            profile = generate_profile(store, "Synthetic key", item["filename"], item["algorithm"], secret, directory)
            private = Path(profile["key_file"])
            public = read_public_key(str(private))
            self.assertTrue(public.startswith(item["public_type"] + " "))
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            command = ["/usr/bin/ssh-keygen", "-y", "-f", str(private)]
            if item["protected"]:
                with self.assertRaises(subprocess.CalledProcessError):
                    subprocess.check_output(command, input=b"wrong\n", start_new_session=True,
                                            env={"PATH": "/usr/bin"}, stderr=subprocess.DEVNULL)
            verified = subprocess.check_output(command, input=(secret + "\n").encode("ascii"), start_new_session=True,
                                               env={"PATH": "/usr/bin"}, stderr=subprocess.DEVNULL).decode("ascii")
            self.assertEqual(verified.split()[:2], public.split()[:2])
            with self.assertRaises(SSHProfileError):
                generate_profile(store, "Duplicate", item["filename"], item["algorithm"], "", directory)
            self.assertEqual(store.get(profile["id"]), ProfileStore(self.path).get(profile["id"]))
        self.assertFalse(store.default_id)
        self.assertNotIn("synthetic-key-phrase", Path(self.path).read_text(encoding="utf-8"))
        for name in contract["invalid_filenames"]:
            with self.assertRaises(SSHProfileError):
                generate_profile(store, "Rejected", name, "ed25519", "", directory)
        existing = Path(directory) / "id_public_only.pub"
        existing.write_text("keep", encoding="ascii")
        with self.assertRaises(SSHProfileError):
            generate_profile(store, "Collision", "id_public_only", "ed25519", "", directory)
        self.assertEqual(existing.read_text(encoding="ascii"), "keep")
        self.assertFalse((Path(directory) / "id_public_only").exists())
        with mock.patch.object(store, "upsert", side_effect=SSHProfileError("failure")):
            with self.assertRaises(SSHProfileError):
                generate_profile(store, "Rollback", "id_rollback", "ed25519", "", directory)
        self.assertFalse(any(Path(directory).glob("id_rollback*")))

    def test_fresh_setup_and_skip_survive_restart_without_creating_profile(self):
        store = ProfileStore(self.path)
        self.assertFalse(store.configured)
        self.assertIsNone(store.get())
        store.skip_setup()
        reloaded = ProfileStore(self.path)
        self.assertTrue(reloaded.configured)
        self.assertEqual(reloaded.profiles, [])

    def test_upsert_round_trip_whitelists_secrets_and_first_profile_is_default(self):
        store = ProfileStore(self.path)
        value = dict(self.profile(), password="MUST_NOT_PERSIST", passphrase="MUST_NOT_PERSIST")
        record = store.upsert(value)
        self.assertEqual(store.default_id, "first")
        self.assertEqual(ProfileStore(self.path).get(), record)
        with open(self.path, "r", encoding="utf-8") as stream:
            content = stream.read()
        self.assertNotIn("MUST_NOT_PERSIST", content)
        self.assertNotIn("password", content)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_default_selection_and_explicit_agent_only_choice_are_preserved(self):
        store = ProfileStore(self.path)
        store.upsert(self.profile())
        store.upsert(self.profile("second"))
        self.assertEqual(store.default_id, "first")
        store.set_default("second")
        store.upsert(dict(self.profile(), name="renamed"))
        self.assertEqual(store.default_id, "second")
        store.set_default("")
        store.upsert(dict(self.profile("second"), name="updated"))
        store.upsert(self.profile("third"))
        self.assertEqual(store.default_id, "")
        self.assertEqual([item["id"] for item in store.profiles], ["first", "second", "third"])

    def test_delete_and_stale_write_roll_back_in_memory(self):
        first, stale = ProfileStore(self.path), ProfileStore(self.path)
        first.upsert(self.profile())
        with self.assertRaises(SSHProfileError):
            stale.upsert(self.profile("stale"))
        self.assertEqual(stale.profiles, [])
        first.upsert(self.profile("second"))
        self.assertTrue(first.delete("first"))
        self.assertEqual(first.default_id, "second")
        self.assertEqual(ProfileStore(self.path).default_id, "second")

    def test_missing_keys_can_be_loaded_as_metadata_but_control_paths_are_rejected(self):
        store = ProfileStore(self.path)
        self.assertFalse(os.path.exists(self.profile()["key_file"]))
        store.upsert(self.profile())
        self.assertIsNotNone(ProfileStore(self.path).get())
        with self.assertRaises(SSHProfileError):
            store.upsert(dict(self.profile(), key_file="bad\npath"))

    def test_invalid_schema_and_duplicate_ids_are_rejected(self):
        for document in ({"schema_version": 2},
                         {"profiles": [self.profile(), self.profile()]},
                         {"profiles": [], "default_id": "missing"},
                         {"profiles": [dict(self.profile(), id="system")]},
                         {"configured": "yes"}):
            with open(self.path, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            with self.assertRaises(SSHProfileError):
                ProfileStore(self.path)

    def test_discovery_lists_filenames_without_opening_private_material(self):
        for name in ("id_rsa", "custom-key", "id_rsa.pub", "config", "known_hosts", "authorized_keys", ".hidden", "old.bak"):
            with open(os.path.join(self.directory.name, name), "w") as stream:
                stream.write("MUST_NOT_BE_READ")
        os.mkdir(os.path.join(self.directory.name, "directory"))
        with mock.patch("builtins.open", side_effect=AssertionError("Discovery opened a key")):
            names = [os.path.basename(path) for path in discover_keys(self.directory.name)]
        self.assertEqual(names, ["custom-key", "id_rsa"])


class AgentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="astraterm-agent-test-")
        self.addCleanup(self.directory.cleanup)

    def test_public_fingerprint_is_sha256_of_agent_blob_and_wrong_type_is_rejected(self):
        identity = _public_identity(public_descriptor())
        blob = base64.b64decode(identity["blob"].encode("ascii"))
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
        self.assertEqual(identity["fingerprint"], expected)
        self.assertIsNone(_public_identity(public_descriptor().replace("ssh-rsa ", "ssh-ed25519 ", 1)))
        self.assertIsNone(_public_identity("ssh-rsa !not_base64!"))

    def test_public_lookup_never_reads_the_private_path(self):
        path = os.path.join(self.directory.name, "encrypted")
        with open(path, "w") as stream:
            stream.write("PRIVATE_MATERIAL")
        with open(path + ".pub", "w") as stream:
            stream.write(public_descriptor())
        real_open = open
        def only_public(filename, *args, **kwargs):
            self.assertNotEqual(filename, path)
            return real_open(filename, *args, **kwargs)
        with mock.patch("builtins.open", side_effect=only_public):
            self.assertEqual(public_key_blob(path), _public_identity(public_descriptor())["blob"])

    def test_selected_identity_matches_blob_or_saved_fingerprint_without_first_key_fallback(self):
        chosen = _public_identity(public_descriptor())
        other = dict(chosen, blob="OTHER", fingerprint="OTHER")
        agent = SSHAgent(self.directory.name)
        with mock.patch.object(agent, "identities", return_value=[other, chosen]), \
                mock.patch("astraterm.ssh_profiles.public_key_blob", return_value=""):
            self.assertEqual(agent.loaded_identity("missing", chosen["fingerprint"]), chosen)
            self.assertIsNone(agent.loaded_identity("missing"))
        with mock.patch.object(agent, "identities", return_value=[chosen]), \
                mock.patch("astraterm.ssh_profiles.public_key_blob", return_value="NEW_KEY_BLOB"):
            self.assertIsNone(agent.loaded_identity("changed-public-key", chosen["fingerprint"]))

    def test_public_identity_cache_contains_only_public_descriptor(self):
        chosen = _public_identity(public_descriptor())
        agent = SSHAgent(self.directory.name)
        with mock.patch.object(agent, "loaded_identity", return_value=chosen):
            path = agent.public_identity_file("not-read")
        with open(path, "r", encoding="ascii") as stream:
            value = stream.read()
        self.assertEqual(value, chosen["type"] + " " + chosen["blob"] + "\n")
        self.assertTrue(path.startswith(self.directory.name + os.sep))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_unlock_uses_private_broker_and_no_secret_in_process_argv_or_environment(self):
        path = os.path.join(self.directory.name, "key")
        helper = os.path.join(self.directory.name, "helper")
        for name in (path, helper):
            with open(name, "w") as stream:
                stream.write("not a real private key")
            os.chmod(name, 0o700)
        agent = SSHAgent(self.directory.name, helper_path=helper)
        identity = _public_identity(public_descriptor())
        broker = mock.Mock(path=os.path.join(self.directory.name, "private.sock"))
        secret = "PRIVATE_PASSPHRASE_Ж"
        with mock.patch.object(agent, "environment", return_value={"SSH_AUTH_SOCK": "agent.sock"}), \
                mock.patch("astraterm.ssh_profiles._AskpassBroker", return_value=broker) as broker_class, \
                mock.patch.object(agent, "_run", side_effect=[public_descriptor().encode("ascii"), b""]) as run, \
                mock.patch.object(agent, "loaded_identity", return_value=identity):
            self.assertEqual(agent.unlock(path, secret), identity)
        broker_class.assert_called_once_with(self.directory.name, secret)
        broker.close.assert_called_once_with()
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertNotIn(secret, repr(call))
            argv, environment = call[0]
            self.assertNotIn("-P", argv)
            self.assertEqual(environment["ASTRATERM_ASKPASS_SOCKET"], broker.path)
            self.assertEqual(environment["SSH_AUTH_SOCK"], "agent.sock")

    def test_cli_failure_does_not_expose_stderr_or_chain_sensitive_exception(self):
        process = mock.Mock(returncode=1)
        process.communicate.return_value = (b"", b"PRIVATE_PASSPHRASE_OR_KEY_DETAILS")
        with mock.patch("astraterm.ssh_profiles.subprocess.Popen", return_value=process):
            with self.assertRaises(SSHProfileError) as result:
                SSHAgent._run(["ssh-add", "selected-key"], {})
        self.assertNotIn("PRIVATE", str(result.exception))
        self.assertIsNone(result.exception.__context__)

    def test_agent_protocol_rejects_oversized_packet_before_reading_body(self):
        connection = mock.Mock()
        connection.recv.return_value = struct.pack(">I", 2 * 1024 * 1024)
        info = mock.Mock(st_mode=stat.S_IFSOCK, st_uid=os.getuid() if hasattr(os, "getuid") else 0)
        with mock.patch("astraterm.ssh_profiles.os.stat", return_value=info), \
                mock.patch("astraterm.ssh_profiles.socket.AF_UNIX", 1, create=True), \
                mock.patch("astraterm.ssh_profiles.socket.socket", return_value=connection):
            self.assertIsNone(_read_identities("socket"))
        connection.recv.assert_called_once_with(4)
        connection.close.assert_called_once_with()


class TerminalIdentityPolicyTests(unittest.TestCase):
    def test_exact_selected_identity_preserves_ordinary_configuration(self):
        path = os.path.abspath("selected-public-key.pub")
        output = "hostname original.example\nport 2222\nuser resolved-user\nidentityfile " + path + "\nproxyjump none\ncertificatefile none\n"
        self.assertEqual(_verify_terminal_config(output.encode("utf-8"), path),
                         {"host": "original.example", "port": 2222, "username": "resolved-user"})

    def test_other_identity_and_certificate_are_rejected_even_if_file_is_missing(self):
        path = os.path.abspath("selected-public-key.pub")
        for extra in ("identityfile missing-other-key\n", "certificatefile other-cert.pub\n"):
            with self.assertRaises(SSHProfileError) as result:
                _verify_terminal_config(("identityfile " + path + "\n" + extra).encode("utf-8"), path)
            self.assertNotIn("missing-other-key", str(result.exception))
            self.assertNotIn("other-cert.pub", str(result.exception))

    def test_proxy_routes_fail_closed_instead_of_discarding_a_gateway(self):
        path = os.path.abspath("selected-public-key.pub")
        for route in ("proxyjump user@mandatory-gateway", "proxycommand ssh -W %h:%p gateway"):
            with self.assertRaises(SSHProfileError) as result:
                _verify_terminal_config(("identityfile " + path + "\n" + route + "\n").encode("utf-8"), path)
            self.assertIn("ProxyJump/ProxyCommand", str(result.exception))
            self.assertNotIn("mandatory-gateway", str(result.exception))

    def test_ssh_g_has_no_remote_command_and_keeps_normal_configuration_enabled(self):
        profile = default_session("ssh")
        path = os.path.abspath("selected-public-key.pub")
        profile.update(host="host.example", key_file=path, identity_profile="chosen", remote_command="MUST_NOT_EXECUTE")
        with mock.patch.object(SSHAgent, "_run", return_value=("hostname real.example\nport 22\nuser remote-user\nidentityfile " + path + "\n").encode("utf-8")) as run:
            self.assertEqual(verify_terminal_identity(profile, path),
                             {"host": "real.example", "port": 22, "username": "remote-user"})
        argv = run.call_args[0][0]
        self.assertIn("-G", argv)
        self.assertNotIn("-F", argv)
        self.assertNotIn("MUST_NOT_EXECUTE", argv)
        self.assertEqual(argv[-2:], ["--", "host.example"])

    def test_host_key_alias_is_rejected_when_sftp_cannot_mirror_verification_policy(self):
        path = os.path.abspath("selected-public-key.pub")
        with self.assertRaises(SSHProfileError) as result:
            _verify_terminal_config(("identityfile " + path + "\nhostkeyalias custom-alias\n").encode("utf-8"), path)
        self.assertIn("HostKeyAlias", str(result.exception))
        self.assertNotIn("custom-alias", str(result.exception))


if __name__ == "__main__":
    unittest.main()
