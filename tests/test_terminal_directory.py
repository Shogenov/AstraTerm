import json
from pathlib import Path
import unittest

from astraterm.terminal_directory import directory_report, resolve_terminal_directory


class TerminalDirectoryTests(unittest.TestCase):
    def test_shared_shell_reports(self):
        contract = Path(__file__).resolve().parents[1] / "shared/contracts/file-drops.json"
        for report in json.loads(contract.read_text(encoding="utf-8"))["directory_reports"]:
            with self.subTest(value=report["value"]):
                expected = tuple(report["expected"]) if report["expected"] is not None else None
                self.assertEqual(directory_report(report["kind"], report["value"]), expected)

    def test_home_and_reported_server_are_bound_to_same_sftp_session(self):
        report = directory_report("title", "alice@fixture: ~/work")
        self.assertEqual(resolve_terminal_directory(report, "192.0.2.12", "alice", "/home/alice",
                                                    lambda: "fixture.example"), "/home/alice/work")
        with self.assertRaises(IOError):
            resolve_terminal_directory(report, "192.0.2.12", "alice", "/home/alice", lambda: "other")
        with self.assertRaises(IOError):
            resolve_terminal_directory(report, "fixture", "other", "/home/other", lambda: "")
