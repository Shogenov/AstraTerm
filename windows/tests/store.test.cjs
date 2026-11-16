'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { Store, validateSession, normalizeDocument } = require('../src/backend/store.cjs');
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-store-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, store: new Store(directory) };
}
test('Linux v1 import preserves individual SSH defaults and whitelists secrets', t => {
  const { store } = fixture(t);
  store.importDocument({ sessions: [{ id: 'old', host: 'example.invalid', username: 'own', group: 'Prod/App', password: 'not-saved', passphrase: 'not-saved', privateKey: 'not-saved' }],
    groups: [{ id: 'prod', name: 'Prod', ssh: { username: 'shared', password: 'not-saved' } }] });
  assert.equal(store.resolveSession('old').username, 'own');
  assert.equal(store.snapshot().sessions[0].inherit_group_ssh, false);
  assert.ok(store.snapshot().groups.some(group => group.name === 'Prod/App'));
  assert.ok(!JSON.stringify(store.exportDocument()).includes('not-saved'));
  assert.equal(store.exportDocument().schema_version, 2);
});
test('nested defaults apply only to inheriting SSH/Mosh; snapshots are independent', t => {
  const { store } = fixture(t);
  store.upsertGroup({ id: 'g1', name: 'Prod', ssh: { username: 'admin', port: 2222 } });
  store.upsertGroup({ id: 'g2', name: 'Prod/DB', ssh: { port: 2200, compression: false, identity_profile: 'key1' } });
  store.upsertSession({ id: 'ssh', host: 'db.invalid', group: 'Prod/DB', remote_command: 'echo hello' });
  store.upsertSession({ id: 'rdp', protocol: 'rdp', host: 'rdp.invalid', group: 'Prod/DB', username: 'windows' });
  const ssh = store.resolveSession('ssh');
  assert.equal(ssh.username, 'admin'); assert.equal(ssh.port, 2200); assert.equal(ssh.compression, false);
  assert.equal(ssh.host, 'db.invalid'); assert.equal(ssh.remote_command, 'echo hello');
  assert.equal(store.resolveSession('rdp').username, 'windows');
  const snapshot = store.snapshot(); snapshot.groups[0].ssh.username = 'tampered';
  assert.equal(store.resolveSession('ssh').username, 'admin');
});
test('rename moves an entire subtree with stable IDs and rejects conflicts atomically', t => {
  const { store } = fixture(t);
  store.upsertGroup({ id: 'root', name: 'Prod', ssh: { username: 'root' } });
  store.upsertGroup({ id: 'child', name: 'Prod/DB', ssh: { port: 2222 } });
  store.upsertSession({ id: 's', host: 'db.invalid', group: 'Prod/DB' });
  store.upsertGroup({ id: 'other', name: 'Other', ssh: {} });
  const before = store.snapshot();
  assert.throws(() => store.upsertGroup({ id: 'root', name: 'Other', ssh: {} }), /уже есть/);
  assert.deepEqual(store.snapshot(), before);
  assert.throws(() => store.upsertGroup({ id: 'root', name: 'Prod/DB/New', ssh: {} }), /внутрь/);
  assert.deepEqual(store.snapshot(), before);
  store.upsertGroup({ id: 'root', name: 'Archive/Prod', ssh: { username: 'archived' } });
  assert.equal(store.snapshot().groups.find(group => group.id === 'child').name, 'Archive/Prod/DB');
  assert.equal(store.resolveSession('s').group, 'Archive/Prod/DB');
  assert.equal(store.resolveSession('s').username, 'archived');
  // A former inferred folder's identifier must not prevent reusing its old name.
  store.upsertSession({ id: 's2', host: 'new.invalid', group: 'Prod/DB' });
  assert.ok(store.snapshot().groups.some(group => group.name === 'Prod/DB'));
});
test('delete folder retains sessions and snapshots effective credentials', t => {
  const { store } = fixture(t);
  store.upsertGroup({ id: 'a', name: 'Prod', ssh: { username: 'parent' } });
  store.upsertGroup({ id: 'b', name: 'Prod/DB', ssh: { username: 'db', port: 2222 } });
  store.upsertSession({ id: 's', host: 'db.invalid', group: 'Prod/DB/Replica' });
  assert.equal(store.deleteGroup('Prod/DB'), true);
  const record = store.resolveSession('s');
  assert.equal(record.group, 'Prod'); assert.equal(record.username, 'db'); assert.equal(record.port, 2222);
  assert.equal(record.inherit_group_ssh, false);
  assert.equal(store.deleteGroup('missing'), false);
});
test('apply group settings requires explicit opt-in for existing legacy sessions', t => {
  const { store } = fixture(t);
  store.importDocument([{ id: 's', host: 'host.invalid', username: 'original', group: 'Prod' }]);
  const group = store.snapshot().groups[0];
  store.upsertGroup({ ...group, ssh: { username: 'common' } });
  assert.equal(store.resolveSession('s').username, 'original');
  store.upsertGroup({ ...group, ssh: { username: 'common' } }, true);
  assert.equal(store.resolveSession('s').username, 'common');
});
test('legacy import inferred folders never erase saved defaults', t => {
  const { store } = fixture(t);
  store.upsertGroup({ id: 'prod', name: 'Prod', ssh: { username: 'saved' } });
  store.importDocument([{ id: 's', host: 'host.invalid', group: 'Prod' }]);
  assert.equal(store.snapshot().groups.find(group => group.name === 'Prod').ssh.username, 'saved');
});
test('stale and locked writers leave both memory and saved data intact', t => {
  const { directory, store } = fixture(t), other = new Store(directory);
  store.upsertSession({ id: 'first', host: 'first.invalid' });
  assert.throws(() => other.upsertSession({ id: 'second', host: 'second.invalid' }), /другим окном/);
  assert.equal(other.snapshot().sessions.length, 0);
  assert.equal(new Store(directory).snapshot().sessions.length, 1);
  other.load();
  fs.writeFileSync(path.join(directory, '.sessions.json.lock'), '');
  assert.throws(() => other.upsertSession({ id: 'second', host: 'second.invalid' }), /заняты/);
  assert.equal(other.snapshot().sessions.length, 1);
});
test('validation rejects option/control injection and unknown encodings', () => {
  for (const bad of [{ host: '-oProxyCommand=evil' }, { host: 'valid\ninvalid' }, { host: 'host.invalid', port: true },
    { host: 'host.invalid', username: '-root' }, { host: 'host.invalid', jump_host: 'x@bad:0' },
    { host: 'host.invalid', encoding: 'imaginary-encoding' }]) assert.throws(() => validateSession(bad));
  assert.equal(validateSession({ host: '[2001:db8::1]', jump_host: 'user@[::1]:2222', encoding: 'CP1251' }).host, '2001:db8::1');
  assert.throws(() => normalizeDocument({ schema_version: 3 }));
  assert.throws(() => normalizeDocument({ groups: [{ name: 'a/../b' }] }));
  assert.equal(validateSession({ protocol: 'local' }).name, 'PowerShell');
});
test('explicit empty/null or non-string protocol is rejected instead of silently stored as SSH', t => {
  const { store } = fixture(t);
  for (const protocol of ['', null, undefined, false, 0, [], {}, 'unknown']) {
    assert.throws(() => store.upsertSession({ id: 'invalid', host: 'host.invalid', protocol }), /протокол/);
    assert.equal(store.snapshot().sessions.length, 0);
  }
  assert.equal(store.upsertSession({ id: 'missing', host: 'host.invalid' }).protocol, 'ssh');
});
test('legacy Linux protocols and less common encoding survive import/export/reload unchanged', t => {
  const { directory, store } = fixture(t);
  const protocols = ['mosh', 'vnc', 'telnet', 'serial', 'rdp', 'local'];
  const records = protocols.map(protocol => ({ id: protocol, name: `Linux ${protocol}`, protocol,
    host: ['local', 'serial'].includes(protocol) ? '' : 'host.invalid', group: 'Imported/Linux',
    encoding: 'CP866', serial_device: '/dev/ttyUSB7', remote_command: protocol === 'ssh' ? 'echo hello' : '' }));
  store.importDocument({ schema_version: 1, sessions: records });
  const exported = store.exportDocument();
  assert.deepEqual(exported.sessions.map(record => record.protocol), protocols);
  assert.ok(exported.sessions.every(record => record.encoding === 'CP866' && record.inherit_group_ssh === false));
  assert.equal(exported.sessions.find(record => record.protocol === 'serial').serial_device, '/dev/ttyUSB7');
  assert.deepEqual(new Store(directory).exportDocument(), exported);
  const secondDirectory = path.join(directory, 'second');
  const second = new Store(secondDirectory); second.importDocument(exported);
  assert.deepEqual(second.exportDocument(), exported);
});
test('macros/tunnels round-trip with metadata only and duplicate IDs fail', t => {
  const { store, directory } = fixture(t);
  store.upsertMacro({ id: 'm', text: 'printf hello\n', password: 'not-saved' });
  store.upsertTunnel({ id: 't', host: 'host.invalid', type: 'dynamic', password: 'not-saved' });
  assert.equal(new Store(directory).snapshot().macros[0].text, 'printf hello\n');
  assert.ok(!fs.readFileSync(store.path, 'utf8').includes('not-saved'));
  assert.throws(() => store.importDocument({ sessions: [{ id: 's', host: 'a' }, { id: 's', host: 'b' }] }), /Повторяющиеся/);
  assert.equal(store.deleteMacro('m'), true); assert.equal(store.deleteTunnel('t'), true);
});
