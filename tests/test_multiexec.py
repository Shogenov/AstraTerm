"""Routing invariants independent of GTK/VTE; GUI commits are tested on Astra."""
from __future__ import unicode_literals

import unittest

from astraterm.multiexec import MultiExecController


class Pane(object):
    def __init__(self, pid, protocol="local"):
        self.pid = pid
        self.closed = False
        self.exited = False
        self.profile = {"protocol": protocol}
        self.connection_generation = 0
        self.received = []
        self.before_send = None

    def send_bytes(self, data):
        if self.before_send:
            self.before_send(data)
        self.received.append(data)

    def send(self, text):
        self.received.append(text)


class LegacyPane(Pane):
    send_bytes = None


class MultiExecTest(unittest.TestCase):
    def setUp(self):
        self.errors = []
        self.changes = []
        self.router = MultiExecController(on_change=lambda router: self.changes.append(router.enabled),
                                          on_error=lambda pane, exc: self.errors.append((pane, exc)))
        self.first, self.second, self.third = Pane(101), Pane(102), Pane(103)
        for pane in (self.first, self.second, self.third):
            self.router.register(pane)

    def enable_pair(self):
        self.router.set_selected(self.first, True)
        self.router.set_selected(self.second, True)
        self.router.set_enabled(True)

    def test_initially_disabled_and_every_new_registration_excluded(self):
        self.assertFalse(self.router.enabled)
        self.assertEqual(self.router.selected_panes(), [])
        self.router.select_all()
        self.assertEqual(self.router.route(self.first, b"disabled"), 0)
        self.router.set_enabled(True)
        fourth = Pane(104)
        self.router.register(fourth)
        self.assertFalse(self.router.is_selected(fourth))
        self.assertEqual(self.router.route(self.first, b"input"), 2)
        self.assertEqual(fourth.received, [])

    def test_selected_pair_forwards_once_and_original_is_not_fed_again(self):
        self.enable_pair()
        self.assertEqual(self.router.route(self.first, b"one\r"), 1)
        self.assertEqual(self.first.received, [])
        self.assertEqual(self.second.received, [b"one\r"])
        self.assertEqual(self.third.received, [])
        self.assertEqual(self.router.route(self.second, b"two\r"), 1)
        self.assertEqual(self.first.received, [b"two\r"])
        self.assertEqual(self.second.received, [b"one\r"])

    def test_unselected_source_remains_local_even_when_others_selected(self):
        self.enable_pair()
        self.assertEqual(self.router.route(self.third, b"local-only"), 0)
        self.assertEqual(self.first.received + self.second.received, [])

    def test_control_unicode_bracketed_paste_and_nul_bytes_are_unchanged(self):
        self.enable_pair()
        payload = (b"\x00\x03\x04\x08\x7f\t\r\x1b[A\x1b[H\x1b[F\x1b[200~" +
                   "Привет\n世界".encode("utf-8") + b"\xff\x1b[201~")
        self.assertEqual(self.router.route(self.first, payload), 1)
        self.assertEqual(self.second.received, [payload])
        self.assertIs(self.second.received[0], payload)
        self.router.route(self.first, bytearray(b"\x00\xff"))
        self.router.route(self.first, memoryview(b"\r\t"))
        self.assertEqual(self.second.received[1:], [b"\x00\xff", b"\r\t"])

    def test_duplicate_registration_cannot_duplicate_delivery(self):
        self.enable_pair()
        self.assertFalse(self.router.register(self.second))
        self.router.select_all()
        self.assertEqual(self.router.route(self.first, b"once"), 2)
        self.assertEqual(self.second.received, [b"once"])
        self.assertEqual(self.third.received, [b"once"])

    def test_reentrant_commit_during_feed_cannot_loop_or_duplicate(self):
        self.router.select_all()
        self.router.set_enabled(True)
        nested = []
        self.second.before_send = lambda data: nested.append(self.router.route(self.second, data))
        self.third.before_send = lambda data: nested.append(self.router.route(self.third, data))
        self.assertEqual(self.router.route(self.first, b"safe"), 2)
        self.assertEqual(nested, [0, 0])
        self.assertEqual(self.first.received, [])
        self.assertEqual(self.second.received, [b"safe"])
        self.assertEqual(self.third.received, [b"safe"])

    def test_rdp_vnc_tunnels_dead_and_unregistered_panes_excluded(self):
        for index, protocol in enumerate(("rdp", "vnc", "tunnel", "unknown")):
            pane = Pane(200 + index, protocol)
            self.router.register(pane)
            self.assertFalse(self.router.set_selected(pane, True))
        self.third.pid = None
        self.router.select_all()
        self.assertEqual(self.router.selected_panes(), [self.first, self.second])
        self.assertFalse(self.router.set_selected(Pane(999), True))
        for pid in (None, 0, -1, True):
            pane = Pane(pid)
            self.assertFalse(self.router.eligible(pane))
        malformed = Pane(888)
        malformed.profile["protocol"] = []
        self.assertFalse(self.router.eligible(malformed))

    def test_each_supported_terminal_protocol_is_eligible(self):
        for protocol in ("local", "ssh", "telnet", "mosh", "serial"):
            self.assertTrue(self.router.eligible(Pane(123, protocol)))

    def test_closed_or_exited_recipient_is_pruned_permanently(self):
        self.enable_pair()
        self.second.closed = True
        self.assertEqual(self.router.route(self.first, b"closed"), 0)
        self.assertFalse(self.router.is_selected(self.second))
        self.second.closed = False
        self.assertEqual(self.router.route(self.first, b"not-restored"), 0)
        self.router.set_selected(self.second, True)
        self.second.exited = True
        self.assertEqual(self.router.selected_panes(), [self.first])

    def test_changed_pid_or_generation_requires_explicit_reselection(self):
        self.enable_pair()
        self.second.pid = 502
        self.assertEqual(self.router.route(self.first, b"old-pid"), 0)
        self.assertFalse(self.router.is_selected(self.second))
        self.router.set_selected(self.second, True)
        self.second.connection_generation += 1
        self.assertEqual(self.router.route(self.first, b"same-pid-new-child"), 0)
        self.assertFalse(self.router.is_selected(self.second))

    def test_explicit_lifecycle_notification_handles_reused_pid(self):
        self.enable_pair()
        self.assertTrue(self.router.connection_changed(self.second))
        self.assertFalse(self.router.is_selected(self.second))
        self.assertEqual(self.router.route(self.first, b"must-not-reconnect"), 0)
        self.router.set_selected(self.second, True)
        self.assertEqual(self.router.route(self.first, b"explicitly-selected"), 1)

    def test_dead_source_cannot_broadcast_to_live_recipients(self):
        self.enable_pair()
        self.first.pid = None
        self.assertEqual(self.router.route(self.first, b"stale-source"), 0)
        self.assertEqual(self.second.received, [])

    def test_retired_history_pane_never_remains_an_input_source_or_target(self):
        self.enable_pair()
        self.second.input_retired = True
        self.assertFalse(self.router.eligible(self.second))
        self.assertEqual(self.router.route(self.first, b"not-for-history"), 0)
        self.assertFalse(self.router.is_selected(self.second))
        self.assertEqual(self.router.route(self.second, b"not-from-history"), 0)
        self.assertEqual(self.first.received + self.second.received, [])

    def test_unregister_and_reregister_never_restores_selection(self):
        self.enable_pair()
        self.assertTrue(self.router.unregister(self.second))
        self.assertFalse(self.router.unregister(self.second))
        self.router.register(self.second)
        self.assertFalse(self.router.is_selected(self.second))
        self.assertEqual(self.router.route(self.first, b"excluded"), 0)

    def test_pause_and_resume_preserve_only_existing_explicit_members(self):
        self.enable_pair()
        self.router.set_enabled(False)
        self.assertEqual(self.router.route(self.first, b"paused"), 0)
        self.assertEqual(self.router.selected_panes(), [self.first, self.second])
        self.router.set_enabled(True)
        self.assertEqual(self.router.route(self.first, b"resumed"), 1)
        self.router.clear_selection()
        self.assertEqual(self.router.route(self.first, b"cleared"), 0)

    def test_recipient_failure_and_error_callback_failure_do_not_stop_others(self):
        self.router.select_all()
        self.router.set_enabled(True)
        def fail(data):
            raise OSError("unavailable PTY")
        self.second.before_send = fail
        self.assertEqual(self.router.route(self.first, b"continue"), 1)
        self.assertEqual(self.second.received, [])
        self.assertEqual(self.third.received, [b"continue"])
        self.assertIs(self.errors[0][0], self.second)
        def fail_callback(pane, exception):
            raise RuntimeError("broken UI callback")
        self.router.on_error = fail_callback
        self.assertEqual(self.router.route(self.first, b"again"), 1)
        self.assertEqual(self.third.received, [b"continue", b"again"])

    def test_disabling_during_route_stops_remaining_recipients_immediately(self):
        self.router.select_all()
        self.router.set_enabled(True)
        self.second.before_send = lambda data: self.router.set_enabled(False)
        self.assertEqual(self.router.route(self.first, b"pause"), 1)
        self.assertEqual(self.second.received, [b"pause"])
        self.assertEqual(self.third.received, [])

    def test_new_selection_during_route_waits_for_the_next_batch(self):
        self.enable_pair()
        self.second.before_send = lambda data: self.router.set_selected(self.third, True)
        self.assertEqual(self.router.route(self.first, b"first-batch"), 1)
        self.assertEqual(self.third.received, [])
        self.assertEqual(self.router.route(self.first, b"next-batch"), 2)
        self.assertEqual(self.third.received, [b"next-batch"])

    def test_reconnected_recipient_during_route_does_not_receive_old_batch(self):
        self.router.select_all()
        self.router.set_enabled(True)
        def restart_third(data):
            self.router.connection_changed(self.third)
            self.third.pid += 100
            self.router.set_selected(self.third, True)
        self.second.before_send = restart_third
        self.assertEqual(self.router.route(self.first, b"old-batch"), 1)
        self.assertEqual(self.third.received, [])

    def test_legacy_send_decodes_unicode_strictly_without_lossy_fallback(self):
        legacy = LegacyPane(333)
        self.router.register(legacy)
        self.router.set_selected(self.first, True)
        self.router.set_selected(legacy, True)
        self.router.set_enabled(True)
        self.assertEqual(self.router.route(self.first, "Привет\x03".encode("utf-8")), 1)
        self.assertEqual(legacy.received, ["Привет\x03"])
        self.assertEqual(self.router.route(self.first, b"\xff"), 0)
        self.assertEqual(legacy.received, ["Привет\x03"])
        self.assertIsInstance(self.errors[0][1], ValueError)

    def test_equal_codec_aliases_preserve_bytes_without_a_decode_round_trip(self):
        self.enable_pair()
        self.first.profile["encoding"] = "utf8"
        self.second.profile["encoding"] = "UTF-8"
        payload = b"\x00\xff\x1b[A"
        self.assertEqual(self.router.route(self.first, payload), 1)
        self.assertIs(self.second.received[0], payload)

    def test_utf8_and_cp1251_cyrillic_transcode_with_controls_and_nul(self):
        self.enable_pair()
        self.first.profile["encoding"] = "UTF-8"
        self.second.profile["encoding"] = "windows-1251"
        text = "\x1b[200~Привет\x00\r\n\x03\t\x1b[201~"
        self.assertEqual(self.router.route(self.first, text.encode("utf-8")), 1)
        self.assertEqual(self.second.received, [text.encode("cp1251")])
        self.assertEqual(self.router.route(self.second, text.encode("cp1251")), 1)
        self.assertEqual(self.first.received, [text.encode("utf-8")])

    def test_unencodable_target_is_skipped_while_other_recipients_continue(self):
        self.router.select_all()
        self.router.set_enabled(True)
        self.second.profile["encoding"] = "cp1251"
        payload = "PRIVATE_世界\x03".encode("utf-8")
        self.assertEqual(self.router.route(self.first, payload), 1)
        self.assertEqual(self.second.received, [])
        self.assertEqual(self.third.received, [payload])
        self.assertIs(self.errors[0][0], self.second)
        self.assertNotIn("PRIVATE", str(self.errors[0][1]))
        self.assertNotIn("世界", str(self.errors[0][1]))
        self.assertFalse(hasattr(self.errors[0][1], "object"))
        self.assertIsNone(self.errors[0][1].__context__)

    def test_invalid_codec_does_not_break_compatible_recipients(self):
        self.router.select_all()
        self.router.set_enabled(True)
        self.second.profile["encoding"] = "not-a-codec"
        self.assertEqual(self.router.route(self.first, b"safe\r"), 1)
        self.assertEqual(self.second.received, [])
        self.assertEqual(self.third.received, [b"safe\r"])
        self.assertEqual(len(self.errors), 1)

    def test_direct_programmatic_send_is_not_broadcast(self):
        self.enable_pair()
        self.first.send("macro in active pane")
        self.assertEqual(self.first.received, ["macro in active pane"])
        self.assertEqual(self.second.received, [])

    def test_empty_input_and_invalid_payload_do_not_feed_terminals(self):
        self.enable_pair()
        self.assertEqual(self.router.route(self.first, b""), 0)
        with self.assertRaises(TypeError):
            self.router.route(self.first, "not encoded")
        self.assertEqual(self.second.received, [])

    def test_registration_and_selection_use_identity_not_overloaded_equality(self):
        class EqualPane(Pane):
            __hash__ = None
            def __eq__(self, other):
                return True
        one, two = EqualPane(601), EqualPane(602)
        self.router.register(one)
        self.router.register(two)
        self.router.set_selected(one, True)
        self.router.set_selected(two, True)
        self.router.set_enabled(True)
        self.assertEqual(self.router.route(one, b"identity"), 1)
        self.assertEqual(one.received, [])
        self.assertEqual(two.received, [b"identity"])


if __name__ == "__main__":
    unittest.main()
