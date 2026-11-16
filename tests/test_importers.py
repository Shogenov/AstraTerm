from __future__ import unicode_literals
import json
import os
from pathlib import Path
import tempfile
import unittest
from astraterm.importers import parse_import, credential_id, stable_id
from astraterm.core import SessionStore, default_session

ROOT=Path(__file__).resolve().parent.parent


class ImportsTest(unittest.TestCase):
    def setUp(self):
        with (ROOT/'shared/contracts/imports.json').open(encoding='utf-8') as stream:self.contract=json.load(stream)

    def fixture(self, directory, files):
        paths=[]
        for name,text in files.items():
            path=Path(directory)/name;path.write_text(text,encoding='utf-8');paths.append(path)
        return paths

    def test_shared_import_contracts_and_secret_free_session_documents(self):
        for case in self.contract['cases']:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as directory:
                plan=parse_import(self.fixture(directory,case['files']));preview=plan.preview()
                for key in ('sessions','credentials','groups','missing'):
                    self.assertEqual(len(preview[key]) if isinstance(preview[key],list) else preview[key],case[key])
                for key,value in case.get('first',{}).items():
                    session=next(item for item in plan.sessions if item['name']==case['expected_session']) if case.get('expected_session') else plan.sessions[0]
                    self.assertEqual(session[key],value)
                for key in ('macros','tunnels'):
                    if key in case:self.assertEqual(len(plan.document()[key]),case[key])
                encoded=json.dumps(plan.document(),ensure_ascii=False)
                for item in plan.credentials.values():self.assertNotIn(item['password'],encoded)
                self.assertNotIn('"password":',json.dumps(preview))
                for item in plan.credentials.values():self.assertNotIn(item['password'],json.dumps(preview))

    def test_named_profile_order_aliases_and_exact_endpoint_port(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=self.fixture(directory,self.contract['cases'][0]['files'])
            plan=parse_import(paths);reverse=parse_import(list(reversed(paths)))
            self.assertEqual(plan.document(),reverse.document())
            self.assertEqual(plan.credentials,reverse.credentials)
            web,db,edge,rdp,missing=plan.sessions
            self.assertEqual(web['credential_profile'],db['credential_profile'])
            self.assertEqual(web['credential_profile'],credential_id('Shared'))
            self.assertEqual(plan.credentials[web['credential_profile']]['password'],'TEST_ONLY_alpha=equals ')
            self.assertEqual(plan.credentials[rdp['credential_profile']]['username'],'LAB\\operator')
            self.assertNotEqual(edge['credential_profile'],stable_id('mobaxterm','endpoint','ssh:22:ops@edge.example.invalid'))
            self.assertNotIn(missing['credential_profile'],plan.credentials)

    def test_reimport_keeps_ids_and_existing_group_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            plan=parse_import(self.fixture(directory,self.contract['cases'][0]['files']))
            store=SessionStore(Path(directory)/'sessions.json')
            store.upsert_group({'name':'Lab','ssh':{'username':'owner'}})
            store.import_document(plan.document(store));ids=[item['id'] for item in store.sessions]
            store.import_document(plan.document(store))
            self.assertEqual([item['id'] for item in store.sessions],ids)
            self.assertEqual(next(item for item in store.groups if item['name']=='Lab')['ssh']['username'],'owner')
            self.assertTrue(all(not item['inherit_group_ssh'] for item in store.sessions))

    def test_reject_conflicting_credentials_unsafe_yaml_and_malformed_csv(self):
        invalid=[('passwords.txt','Shared(ops) = TEST_ONLY_first\nShared(ops) = TEST_ONLY_other\n'),
                 ('asbru.yml','!!python/object/apply:os.system [echo UNSAFE]\n'),
                 ('asbru.yml','__PAC__ROOT__: {}\na: {name: first, name: second}\n'),
                 ('hosts.csv','Address,Username,Username\nnode.example.invalid,ops,other\n'),
                 ('hosts.csv','Address,Label\nnode.example.invalid,"unterminated\n'),
                 ('ssh_config','Host node\n HostName node.example.invalid\n ProxyCommand echo UNSAFE\n')]
        for name,text in invalid:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):parse_import(self.fixture(directory,{name:text}))

    def test_asbru_parent_cycles_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            text='__PAC__ROOT__: {}\ng: {_is_group: 1, name: Group, parent: g}\n'
            with self.assertRaises(ValueError):parse_import(self.fixture(directory,{'cycle.yml':text}))


if __name__=='__main__':unittest.main()
