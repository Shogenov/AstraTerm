"""The same contract vectors are exercised by the JavaScript implementation."""
from __future__ import unicode_literals

import json
from pathlib import Path
import tempfile
import unittest

from astraterm import __version__
from astraterm.core import SessionStore, validate_session
from astraterm.multiexec import MultiExecController
from astraterm import _shared

ROOT = Path(__file__).resolve().parent.parent


def fixture(name):
    with (ROOT / 'shared/contracts' / name).open(encoding='utf-8') as stream:
        return json.load(stream)


class SharedContractTests(unittest.TestCase):
    def test_duplicate_saved_session_in_same_folder(self):
        case = fixture('sessions.json')['duplicate']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.json'
            store = SessionStore(path)
            source = store.upsert(case['source'])
            store.upsert(dict(source, id='other', name=case['other_group_name'], group='Other'))
            current = source
            for name in case['expected']:
                current = store.duplicate(current['id'])
                self.assertNotEqual(current['id'], source['id'])
                self.assertEqual(current, dict(source, id=current['id'], name=name))
            self.assertEqual([item['name'] for item in SessionStore(path).sessions if item['group'] == source['group']],
                             [source['name']] + case['expected'])
            with self.assertRaises(ValueError):
                store.duplicate('missing')

    def test_rdp_metadata_and_secret_boundary(self):
        for case in fixture('rdp.json')['sessions']:
            profile = validate_session(case['input'])
            for key, value in case['expected'].items(): self.assertEqual(profile[key], value)
            for key in case['absent']: self.assertNotIn(key, profile)

    def test_generated_release_and_defaults_match_canonical_product(self):
        with (ROOT / 'shared/product.json').open(encoding='utf-8') as stream:
            product = json.load(stream)
        self.assertEqual(__version__, product['version'])
        self.assertEqual(_shared.DEFAULT_PORTS, product['default_ports'])
        self.assertEqual(list(_shared.GROUP_SSH_FIELDS), product['group_ssh_fields'])
        self.assertEqual(_shared.SESSION_SCHEMA_VERSION, product['session_schema_version'])

    def test_shared_session_validation_and_secret_whitelist(self):
        for case in fixture('sessions.json')['valid']:
            with self.subTest(case=case['name']):
                result = validate_session(case['input'])
                for key, value in case['expected'].items():
                    self.assertEqual(result[key], value)
                for key in case.get('absent', []):
                    self.assertNotIn(key, result)
        for case in fixture('sessions.json')['invalid']:
            with self.subTest(case=case['name']), self.assertRaises((ValueError, TypeError)):
                validate_session(case['input'])

    def test_shared_documents_and_group_resolution(self):
        for case in fixture('sessions.json')['documents']:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as directory:
                filename = Path(directory) / 'sessions.json'
                filename.write_text(json.dumps(case['document']), encoding='utf-8')
                store = SessionStore(str(filename))
                session = next(item for item in store.sessions if item['id'] == case['session'])
                resolved = store.resolve_session(session)
                for key, value in case['expected'].items():
                    self.assertEqual(resolved[key], value)

    def test_shared_multiexec_routing_and_lifecycle(self):
        class Pane(object):
            def __init__(self, name):
                self.name, self.pid, self.closed = name, 1, False
                self.profile = {'protocol': 'ssh', 'encoding': 'UTF-8'}
                self.connection_generation, self.received = 1, []
            def send_bytes(self, data):
                self.received.append(data)
        for case in fixture('multiexec.json')['cases']:
            with self.subTest(case=case['name']):
                controller = MultiExecController()
                panes = {name: Pane(name) for name in ('a', 'b', 'c')}
                for pane in panes.values():
                    controller.register(pane)
                for name in case.get('selected', []):
                    controller.set_selected(panes[name], True)
                if case.get('reconnect'):
                    pane = panes[case['reconnect']]
                    pane.connection_generation += 1
                    controller.connection_changed(pane)
                if case.get('exit'):
                    pane = panes[case['exit']]
                    pane.closed = True
                    controller.connection_changed(pane)
                if case.get('select_all'):
                    controller.select_all()
                if case.get('clear'):
                    controller.clear_selection()
                controller.set_enabled(case.get('enabled', False))
                source, data = panes[case['source']], case.get('data', 'x').encode('utf-8')
                # VTE itself already writes to the source; the Linux router sends only to peers.
                if controller.eligible(source):
                    source.send_bytes(data)
                if case.get('human', True):
                    controller.route(source, data)
                self.assertEqual(sorted(name for name, pane in panes.items() if pane.received), sorted(case['expected']))
                self.assertTrue(all(pane.received == [data] for pane in panes.values() if pane.received))


if __name__ == '__main__':
    unittest.main()
