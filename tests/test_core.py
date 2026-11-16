"""Regression tests for storage safety and process argument boundaries."""
from __future__ import unicode_literals

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from astraterm.core import (SessionStore, default_session, session_argv, tunnel_argv,
                            validate_session, validate_tunnel, parse_ssh_config,
                            normalize_group_name, validate_group)


class SessionArgumentsTest(unittest.TestCase):
    def session(self, protocol="ssh", **values):
        session = default_session(protocol)
        session.update({"host": "server.example", "username": "operator"})
        session.update(values)
        return session

    def test_ssh_preserves_argument_boundaries_and_standard_authentication(self):
        session = self.session(key_file="~/keys/a key", x11=True, jump_host="user@gateway:2222",
                               remote_command="printf '%s' '$(touch /tmp/never)'", password="secret")
        argv = session_argv(session)
        self.assertIn("StrictHostKeyChecking=ask", argv)
        self.assertIn("-X", argv)
        self.assertEqual(argv[argv.index("-J") + 1], "user@gateway:2222")
        self.assertEqual(argv[argv.index("-i") + 1], os.path.expanduser("~/keys/a key"))
        self.assertEqual(argv[-3:], ["--", "server.example", session["remote_command"]])
        self.assertNotIn("secret", " ".join(argv))

    def test_rejects_local_command_and_option_injection(self):
        for host in ("-oProxyCommand=evil", "good; touch /tmp/evil", "user@host", "host\n-o", "host name", "$(id)"):
            with self.assertRaises(ValueError):
                session_argv(self.session(host=host))
        for user in ("-oProxyCommand=evil", "user\n-o", "user name", "x;id"):
            with self.assertRaises(ValueError):
                session_argv(self.session(username=user))
        for gateway in ("-oProxyCommand=evil", "user@-target", "target:0", "target:99999", "user@host,", "user@host bad"):
            with self.assertRaises(ValueError):
                session_argv(self.session(jump_host=gateway))

    def test_identity_profile_roundtrip_and_explicit_key_selection(self):
        profile = self.session(identity_profile="work_key", key_file="~/.ssh/work.pub")
        argv = session_argv(profile)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("IdentityAgent=SSH_AUTH_SOCK", argv)
        self.assertIn("PubkeyAuthentication=yes", argv)
        self.assertEqual(validate_session(profile)["identity_profile"], "work_key")
        self.assertNotIn("IdentitiesOnly=yes", session_argv(self.session(identity_profile="system")))
        self.assertEqual(validate_session(self.session())["identity_profile"], "")
        with self.assertRaises(ValueError):
            validate_session(self.session(identity_profile="../../other-user"))

    def test_rejects_bad_ports_and_boolean_strings(self):
        for port in (0, -1, 65536, "22;id", True, 22.5, None):
            with self.assertRaises(ValueError):
                validate_session(self.session(port=port))
        with self.assertRaises(ValueError):
            validate_session(self.session(x11="false"))

    def test_local_and_serial_commands_are_real_terminal_processes(self):
        self.assertEqual(session_argv(default_session("local")), ["/bin/bash", "-l"])
        with mock.patch("astraterm.core.shutil.which", return_value="/usr/bin/picocom"):
            self.assertEqual(session_argv(default_session("serial")),
                             ["picocom", "--baud", "115200", "/dev/ttyUSB0"])
        with self.assertRaises(ValueError):
            session_argv(self.session("serial", serial_device="-e/bin/sh"))

    def test_ipv6_and_gui_clients_use_separate_safe_arguments(self):
        rdp = self.session("rdp", host="2001:db8::1", rdp_domain="DOMAIN")
        with mock.patch("astraterm.core.shutil.which", return_value="/usr/bin/xfreerdp"):
            argv = session_argv(rdp)
        self.assertIn("/v:[2001:db8::1]:3389", argv)
        self.assertNotIn("/cert:ignore", argv)
        self.assertEqual(session_argv(self.session("vnc", host="[::1]")), ["vncviewer", "[::1]::5900"])

    def test_tunnels_bind_loopback_and_require_forward_success(self):
        base = {"host": "gateway", "username": "operator", "listen_port": 8080,
                "destination_host": "::1", "destination_port": 80}
        argv = tunnel_argv(base)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertEqual(argv[argv.index("-L") + 1], "127.0.0.1:8080:[::1]:80")
        base.update({"type": "remote", "bind_host": "::1"})
        argv = tunnel_argv(base)
        self.assertEqual(argv[argv.index("-R") + 1], "[::1]:8080:[::1]:80")
        base["type"] = "dynamic"
        argv = tunnel_argv(base)
        self.assertEqual(argv[argv.index("-D") + 1], "[::1]:8080")
        self.assertEqual(argv[-2:], ["--", "gateway"])

    def test_tunnel_injection_is_rejected(self):
        for field, value in (("host", "-Fbad"), ("bind_host", "localhost:22:bad"),
                             ("destination_host", "target;id"), ("listen_port", 0),
                             ("type", "-oProxyCommand=bad")):
            data = {"host": "gateway", field: value}
            with self.assertRaises(ValueError):
                tunnel_argv(data)


class SessionStorageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="astraterm-test-")
        self.path = os.path.join(self.directory, "config", "sessions.json")
        self.store = SessionStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def session(self, name="Сервер"):
        session = default_session()
        session.update({"host": "server", "name": name})
        return session

    def write_import(self, data):
        path = os.path.join(self.directory, "import.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        return path

    def test_round_trip_unicode_and_secret_whitelist(self):
        session = self.session()
        session.update({"password": "NEVER-SAVE", "private_key": "NEVER-SAVE-EITHER"})
        saved = self.store.upsert(session)
        self.assertNotIn("password", saved)
        loaded = SessionStore(self.path)
        self.assertEqual(loaded.sessions[0]["name"], "Сервер")
        with open(self.path, "r", encoding="utf-8") as stream:
            text = stream.read()
        self.assertNotIn("NEVER-SAVE", text)
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(self.path)).st_mode & 0o777, 0o700)

    def test_upsert_and_delete_do_not_duplicate_records(self):
        session = self.store.upsert(self.session())
        session["name"] = "Другой сервер"
        self.store.upsert(session)
        self.assertEqual(len(self.store.sessions), 1)
        self.assertEqual(self.store.sessions[0]["name"], "Другой сервер")
        self.assertTrue(self.store.delete(session["id"]))
        self.assertEqual(SessionStore(self.path).sessions, [])
        self.assertFalse(self.store.delete(session["id"]))

    def test_invalid_import_leaves_memory_and_disk_unchanged(self):
        self.store.upsert(self.session("Original"))
        with open(self.path, "rb") as stream:
            before = stream.read()
        invalid = self.session("Invalid")
        invalid["host"] = "-oProxyCommand=bad"
        path = self.write_import({"sessions": [self.session("Valid"), invalid]})
        with self.assertRaises(ValueError):
            self.store.import_file(path)
        self.assertEqual(len(self.store.sessions), 1)
        with open(self.path, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_import_merges_ids_and_export_contains_all_types(self):
        session = self.store.upsert(self.session("Before"))
        imported = dict(session)
        imported["name"] = "After"
        path = self.write_import({"sessions": [imported, self.session("New")],
                                  "macros": [{"name": "Uptime", "text": "uptime"}],
                                  "tunnels": [{"host": "gateway", "listen_port": 1080, "type": "dynamic"}]})
        self.assertEqual(self.store.import_file(path), 2)
        self.assertEqual(len(self.store.sessions), 2)
        self.assertEqual(self.store.sessions[0]["name"], "After")
        exported = os.path.join(self.directory, "export.json")
        self.store.export_file(exported)
        other = SessionStore(exported)
        self.assertEqual(len(other.macros), 1)
        self.assertEqual(len(other.tunnels), 1)

    def test_future_schema_and_duplicate_ids_are_rejected(self):
        for document in ({"schema_version": 99},
                         {"sessions": [dict(self.session(), id="same"), dict(self.session(), id="same")]}):
            with self.assertRaises(ValueError):
                self.store.import_file(self.write_import(document))

    def test_failed_atomic_write_preserves_existing_config_and_memory(self):
        original = self.store.upsert(self.session("Original"))
        with open(self.path, "rb") as stream:
            before = stream.read()
        with mock.patch("astraterm.core.os.replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.store.upsert(self.session("New"))
        self.assertEqual([item["id"] for item in self.store.sessions], [original["id"]])
        with open(self.path, "rb") as stream:
            self.assertEqual(stream.read(), before)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), [".sessions.json.lock", "sessions.json"])

    def test_two_windows_cannot_silently_overwrite_another_windows_changes(self):
        self.store.upsert(self.session("Original"))
        second = SessionStore(self.path)
        self.store.upsert(self.session("First window"))
        with self.assertRaises(ValueError) as failure:
            second.upsert(self.session("Stale second window"))
        self.assertIn("другим окном", str(failure.exception))
        self.assertEqual([item["name"] for item in SessionStore(self.path).sessions],
                         ["Original", "First window"])
        self.assertEqual([item["name"] for item in second.sessions], ["Original"])
        second.load()
        second.upsert(self.session("Reloaded second window"))
        self.assertEqual([item["name"] for item in SessionStore(self.path).sessions],
                         ["Original", "First window", "Reloaded second window"])

    def test_two_initial_windows_use_absent_file_revision(self):
        second = SessionStore(self.path)
        self.store.upsert(self.session("Created first"))
        with self.assertRaises(ValueError):
            second.upsert(self.session("Created second"))
        self.assertEqual([item["name"] for item in SessionStore(self.path).sessions], ["Created first"])

    def test_export_to_own_database_cannot_bypass_stale_write_protection(self):
        self.store.upsert(self.session("Original"))
        second = SessionStore(self.path)
        self.store.upsert(self.session("Changed"))
        with self.assertRaises(ValueError):
            second.export_file(self.path)
        exported = os.path.join(self.directory, "independent-export.json")
        second.export_file(exported)
        self.assertEqual(len(SessionStore(exported).sessions), 1)
        self.assertEqual(len(SessionStore(self.path).sessions), 2)

    def test_malformed_json_is_not_overwritten(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as stream:
            stream.write("{broken")
        with self.assertRaises(ValueError):
            SessionStore(self.path)
        with open(self.path) as stream:
            self.assertEqual(stream.read(), "{broken")


class GroupStorageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="astraterm-groups-")
        self.path = os.path.join(self.directory, "sessions.json")
        self.store = SessionStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def session(self, group="", protocol="ssh", **settings):
        value = default_session(protocol)
        value.update(host="host.example", name="Host", group=group)
        value.update(settings)
        return value

    def group(self, name, **settings):
        return self.store.upsert_group({"name": name, "ssh": settings})

    def incoming(self, document):
        path = os.path.join(self.directory, "import.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        return path

    def contents(self):
        with open(self.path, "rb") as stream:
            return stream.read()

    def test_schema_one_migration_retains_values_without_automatic_inheritance(self):
        session = self.session("Operations/Production", username="personal", port=2207)
        del session["inherit_group_ssh"]
        document = {"schema_version": 1, "sessions": [session],
                    "macros": [{"name": "Example", "text": "echo test"}],
                    "tunnels": [{"host": "gateway", "name": "Forward"}]}
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        before = self.contents()
        migrated = SessionStore(self.path)
        self.assertFalse(migrated.sessions[0]["inherit_group_ssh"])
        self.assertEqual(migrated.sessions[0]["username"], "personal")
        self.assertEqual(migrated.sessions[0]["port"], 2207)
        self.assertEqual([item["name"] for item in migrated.groups],
                         ["Operations", "Operations/Production"])
        self.assertEqual(len(migrated.macros), 1)
        self.assertEqual(len(migrated.tunnels), 1)
        self.assertEqual(self.contents(), before, "Reading must not rewrite a legacy database")
        migrated.save()
        saved = json.loads(self.contents().decode("utf-8"))
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(len(saved["groups"]), 2)
        self.assertEqual(migrated.groups, SessionStore(self.path).groups)

    def test_empty_nested_folders_and_defaults_roundtrip_export_import(self):
        saved = self.group("Work/Empty", username="deploy", identity_profile="work")
        self.assertEqual(self.store.sessions, [])
        self.assertEqual([group["name"] for group in self.store.groups], ["Work", "Work/Empty"])
        reloaded = SessionStore(self.path)
        self.assertEqual(reloaded.groups[-1], saved)
        export = os.path.join(self.directory, "export.json")
        reloaded.export_file(export)
        other = SessionStore(os.path.join(self.directory, "other.json"))
        self.assertEqual(other.import_file(export), 0)
        self.assertEqual(other.groups, reloaded.groups)

    def test_nested_partial_settings_resolve_without_changing_unique_session_fields(self):
        self.group("Work", username="deploy", port=2200, compression=False, key_file="~/keys/work")
        self.group("Work/Prod", port=2202, x11=True, encoding="CP1251")
        own = self.session("Work/Prod", username="individual", remote_command="printf hello", name="Unique")
        effective = self.store.resolve_session(own)
        self.assertEqual(effective["username"], "deploy")
        self.assertEqual(effective["port"], 2202)
        self.assertFalse(effective["compression"])
        self.assertTrue(effective["x11"])
        self.assertEqual(effective["encoding"], "CP1251")
        self.assertEqual(effective["key_file"], "~/keys/work")
        for key in ("id", "host", "name", "group", "remote_command"):
            self.assertEqual(effective[key], own[key])
        self.assertEqual(own["username"], "individual", "Resolution must not flatten saved values")
        own["inherit_group_ssh"] = False
        self.assertEqual(self.store.resolve_session(own)["username"], "individual")
        rdp = self.session("Work/Prod", "rdp", username="windows")
        self.assertEqual(self.store.resolve_session(rdp)["port"], 3389)
        self.assertEqual(self.store.resolve_session(rdp)["username"], "windows")
        mosh = self.session("Work/Prod", "mosh")
        self.assertEqual(self.store.resolve_session(mosh)["port"], 2202)

    def test_explicit_empty_child_setting_clears_parent_value(self):
        self.group("A", username="deploy", key_file="~/keys/parent", jump_host="gateway")
        self.group("A/B", username="", key_file="", jump_host="")
        effective = self.store.resolve_session(self.session("A/B"))
        for key in ("username", "key_file", "jump_host"):
            self.assertEqual(effective[key], "")

    def test_apply_existing_is_recursive_and_keeps_unrelated_protocols_and_prefixes(self):
        parent = self.group("A", username="deploy")
        direct = self.store.upsert(self.session("A", inherit_group_ssh=False, username="own"))
        child = self.store.upsert(self.session("A/Child", inherit_group_ssh=False))
        similar = self.store.upsert(self.session("AB", inherit_group_ssh=False))
        rdp = self.store.upsert(self.session("A", "rdp", inherit_group_ssh=False))
        self.store.upsert_group(parent, apply_to_sessions=True)
        by_id = {session["id"]: session for session in self.store.sessions}
        self.assertTrue(by_id[direct["id"]]["inherit_group_ssh"])
        self.assertTrue(by_id[child["id"]]["inherit_group_ssh"])
        self.assertFalse(by_id[similar["id"]]["inherit_group_ssh"])
        self.assertFalse(by_id[rdp["id"]]["inherit_group_ssh"])
        self.assertEqual(by_id[direct["id"]]["username"], "own")
        self.assertEqual(self.store.resolve_session(by_id[direct["id"]])["username"], "deploy")

    def test_group_editor_can_rename_update_defaults_and_apply_with_one_write(self):
        group = self.group("Old", username="old")
        child = self.group("Old/Nested", compression=False)
        session = self.store.upsert(self.session("Old/Nested", inherit_group_ssh=False))
        changed = dict(group, name="New/Parent", ssh={"username": "new"})
        with mock.patch.object(self.store, "save", wraps=self.store.save) as saved:
            result = self.store.upsert_group(changed, apply_to_sessions=True)
        self.assertEqual(saved.call_count, 1)
        self.assertEqual(result["id"], group["id"])
        self.assertEqual(result["ssh"], {"username": "new"})
        by_id = {value["id"]: value for value in self.store.groups}
        self.assertEqual(by_id[child["id"]]["name"], "New/Parent/Nested")
        self.assertEqual(self.store.sessions[0]["id"], session["id"])
        self.assertEqual(self.store.sessions[0]["group"], "New/Parent/Nested")
        self.assertTrue(self.store.sessions[0]["inherit_group_ssh"])
        self.assertEqual(self.store.resolve_session(self.store.sessions[0])["username"], "new")

    def test_rename_collisions_and_self_descendants_leave_memory_and_disk_unchanged(self):
        group = self.group("A", username="a")
        self.group("A/Nested")
        self.group("B")
        self.store.upsert(self.session("A/Nested"))
        before, groups, sessions = self.contents(), list(self.store.groups), list(self.store.sessions)
        for destination in ("B", "A/Nested/New"):
            with self.assertRaises(ValueError):
                self.store.rename_group("A", destination)
            self.assertEqual(self.contents(), before)
            self.assertEqual(self.store.groups, groups)
            self.assertEqual(self.store.sessions, sessions)
        with self.assertRaises(ValueError):
            self.store.upsert_group({"name": "A", "ssh": {}})
        self.assertEqual(self.store.groups[0]["id"], group["id"])

    def test_reusing_a_renamed_folder_name_creates_a_new_identity(self):
        group = self.group("Original", username="keep")
        self.store.rename_group("Original", "Renamed")
        replacement = self.group("Original", username="new")
        self.assertNotEqual(group["id"], replacement["id"])
        by_name = {value["name"]: value for value in self.store.groups}
        self.assertEqual(by_name["Renamed"]["id"], group["id"])
        self.assertEqual(by_name["Renamed"]["ssh"]["username"], "keep")
        # Inferred legacy folders use deterministic IDs until persisted; a
        # renamed inferred folder must not collide with reuse of its old name.
        self.store.upsert(self.session("Implicit"))
        implicit = next(value for value in self.store.groups if value["name"] == "Implicit")
        self.store.rename_group("Implicit", "Former")
        self.store.upsert(self.session("Implicit"))
        current = {value["name"]: value for value in self.store.groups}
        self.assertEqual(current["Former"]["id"], implicit["id"])
        self.assertNotEqual(current["Implicit"]["id"], implicit["id"])
        self.store.upsert(self.session("ImportLegacy"))
        inferred = next(value for value in self.store.groups if value["name"] == "ImportLegacy")
        self.store.rename_group("ImportLegacy", "KeptLegacy")
        incoming = self.session("ImportLegacy")
        del incoming["inherit_group_ssh"]
        self.store.import_file(self.incoming({"sessions": [incoming]}))
        imported = {value["name"]: value for value in self.store.groups}
        self.assertEqual(imported["KeptLegacy"]["id"], inferred["id"])
        self.assertNotEqual(imported["ImportLegacy"]["id"], inferred["id"])

    def test_delete_folder_tree_preserves_connections_and_effective_credentials(self):
        self.group("Parent", username="parent")
        self.group("Parent/Remove", username="child", port=2207)
        self.group("Parent/Remove/Nested")
        self.group("Unrelated")
        original = self.store.upsert(self.session("Parent/Remove/Nested", name="Keep me"))
        self.assertTrue(self.store.delete_group("Parent/Remove"))
        self.assertEqual([group["name"] for group in self.store.groups], ["Parent", "Unrelated"])
        preserved = self.store.sessions[0]
        self.assertEqual(preserved["id"], original["id"])
        self.assertEqual(preserved["host"], original["host"])
        self.assertEqual(preserved["group"], "Parent")
        self.assertFalse(preserved["inherit_group_ssh"])
        self.assertEqual(self.store.resolve_session(preserved)["username"], "child")
        self.assertEqual(preserved["port"], 2207)
        self.assertFalse(self.store.delete_group("Missing"))
        self.store.delete_group("Parent")
        self.assertEqual(self.store.sessions[0]["group"], "")

    def test_failed_and_stale_group_save_roll_back_defaults_rename_and_sessions(self):
        group = self.group("A", username="before")
        self.store.upsert(self.session("A", inherit_group_ssh=False))
        before, groups, sessions = self.contents(), list(self.store.groups), list(self.store.sessions)
        with mock.patch("astraterm.core.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                self.store.upsert_group(dict(group, name="Renamed", ssh={"username": "after"}), True)
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.store.groups, groups)
        self.assertEqual(self.store.sessions, sessions)
        stale = SessionStore(self.path)
        self.group("Second")
        with self.assertRaises(ValueError):
            stale.upsert_group(dict(group, ssh={"username": "stale"}), True)
        self.assertEqual(stale.groups, groups)
        self.assertEqual(stale.sessions, sessions)

    def test_legacy_import_does_not_erase_existing_group_defaults(self):
        self.group("A", username="saved")
        incoming = self.session("A")
        del incoming["inherit_group_ssh"]
        self.store.import_file(self.incoming({"schema_version": 1, "sessions": [incoming]}))
        self.assertEqual(self.store.group_ssh_settings("A"), {"username": "saved"})
        self.assertFalse(self.store.sessions[0]["inherit_group_ssh"])
        second = self.session("Legacy/Child")
        del second["inherit_group_ssh"]
        self.store.import_file(self.incoming([second]))
        self.assertIn("Legacy/Child", [group["name"] for group in self.store.groups])

    def test_import_same_group_id_renames_tree_and_preserves_collision_safety(self):
        group = self.group("A", username="old")
        self.group("A/Child")
        self.store.upsert(self.session("A/Child"))
        incoming = {"schema_version": 2, "groups": [dict(group, name="B", ssh={"username": "new"})]}
        self.store.import_file(self.incoming(incoming))
        self.assertEqual(self.store.sessions[0]["group"], "B/Child")
        self.assertEqual(self.store.group_ssh_settings("B/Child")["username"], "new")
        collision = self.group("Collision")
        before, groups, sessions = self.contents(), list(self.store.groups), list(self.store.sessions)
        bad = {"schema_version": 2, "groups": [dict(group, name=collision["name"])],
               "sessions": [self.session("Fresh")]}
        with self.assertRaises(ValueError):
            self.store.import_file(self.incoming(bad))
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.store.groups, groups)
        self.assertEqual(self.store.sessions, sessions)

    def test_group_settings_whitelist_validates_and_never_inherits_commands_or_hosts(self):
        group = validate_group({"name": " A / B ", "password": "do not save",
                                "ssh": {"username": "user", "password": "never",
                                        "host": "elsewhere", "remote_command": "UNSAFE"}})
        self.assertEqual(group["name"], "A/B")
        self.assertEqual(group["ssh"], {"username": "user"})
        self.assertNotIn("password", group)
        for name in ("", "/A", "A/", "A//B", "A/../B", "A\nB"):
            with self.assertRaises(ValueError):
                normalize_group_name(name)
        for settings in ({"port": 0}, {"x11": "false"}, {"username": "-oProxyCommand=x"},
                         {"identity_profile": "../../other"}, {"encoding": "unknown-codec"}):
            with self.assertRaises(ValueError):
                validate_group({"name": "A", "ssh": settings})
        with self.assertRaises(ValueError):
            validate_session(self.session(inherit_group_ssh="yes"))


class SSHImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="astraterm-ssh-test-")
        self.path = os.path.join(self.directory, "config")

    def tearDown(self):
        shutil.rmtree(self.directory)

    def parse(self, text):
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write(text)
        return parse_ssh_config(self.path)

    def test_concrete_hosts_defaults_quotes_and_negated_patterns(self):
        sessions = self.parse('''
Host web database
  HostName = 192.0.2.10
  User deploy
  IdentityFile "~/keys/my key"
  Port=2222
Host * !database
  ForwardX11 yes
  Compression no
Host *
  ProxyJump jump@example.org:22
''')
        self.assertEqual([s["name"] for s in sessions], ["web", "database"])
        self.assertEqual(sessions[0]["key_file"], "~/keys/my key")
        self.assertEqual(sessions[0]["port"], 2222)
        self.assertTrue(sessions[0]["x11"])
        self.assertFalse(sessions[1]["x11"])
        self.assertEqual(sessions[1]["jump_host"], "jump@example.org:22")

    def test_match_and_include_are_rejected_without_execution_or_expansion(self):
        for directive in ('Include /tmp/does-not-exist', 'Match exec "touch /tmp/NEVER-EXECUTE"'):
            with self.assertRaises(ValueError) as failure:
                self.parse("Host primary\n HostName 192.0.2.20\n" + directive + "\n User wrong\n")
            self.assertIn("Include/Match", str(failure.exception))

    def test_alias_connection_policies_are_not_silently_lost(self):
        for directive in ("ProxyCommand ssh bastion -W %h:%p", "CertificateFile ~/.ssh/cert.pub",
                          "IdentityAgent /run/agent.sock", "HostKeyAlias trusted-key"):
            with self.assertRaises(ValueError) as failure:
                self.parse("Host internal\n HostName 10.0.0.2\n " + directive + "\n")
            self.assertIn("internal", str(failure.exception))
            self.assertIn(directive.split()[0].lower(), str(failure.exception).lower())

    def test_multiple_matching_identity_files_are_rejected(self):
        with self.assertRaises(ValueError) as failure:
            self.parse("Host box\n IdentityFile ~/.ssh/key1\nHost *\n IdentityFile ~/.ssh/key2\n")
        self.assertIn("несколько ключей", str(failure.exception))

    def test_first_obtained_value_matches_openssh_rules(self):
        sessions = self.parse("User default\nHost box\n User ignored\n Port 2222\n")
        self.assertEqual(sessions[0]["username"], "default")


if __name__ == "__main__":
    unittest.main()
