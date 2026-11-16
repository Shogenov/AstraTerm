'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const shared = require('../src/shared.cjs');
const { Store, validateSession } = require('../src/backend/store.cjs');
const { InputRouter } = require('../src/renderer/input-router.js');
const root = path.resolve(__dirname, '..', '..');
const fixture = name => JSON.parse(fs.readFileSync(path.join(root, 'shared', 'contracts', name), 'utf8'));

test('duplicate saved session in the same folder', () => {
  const vector = fixture('sessions.json').duplicate;
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-duplicate-'));
  try {
    const store = new Store(directory);
    const source = store.upsertSession(vector.source);
    store.upsertSession({ ...source, id: 'other', name: vector.other_group_name, group: 'Other' });
    let current = source;
    for (const name of vector.expected) {
      current = store.duplicateSession(current.id);
      assert.notEqual(current.id, source.id);
      assert.deepEqual(current, { ...source, id: current.id, name });
    }
    assert.deepEqual(new Store(directory).snapshot().sessions.filter(item => item.group === source.group).map(item => item.name),
      [source.name, ...vector.expected]);
    assert.throws(() => store.duplicateSession('missing'));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('shared RDP metadata keeps passwords outside session documents', () => {
  for (const item of fixture('rdp.json').sessions) {
    const result=validateSession(item.input);
    for(const [key,value] of Object.entries(item.expected))assert.deepEqual(result[key],value);
    for(const key of item.absent)assert(!Object.hasOwn(result,key));
  }
});

test('shared release and defaults match generated Windows metadata', () => {
  const product = JSON.parse(fs.readFileSync(path.join(root, 'shared', 'product.json'), 'utf8'));
  assert.equal(shared.VERSION, product.version);
  assert.equal(require('../package.json').version, product.version);
  assert.deepEqual(shared.DEFAULT_PORTS, product.default_ports);
  assert.deepEqual(shared.GROUP_SSH_FIELDS, product.group_ssh_fields);
  assert.equal(shared.SESSION_SCHEMA_VERSION, product.session_schema_version);
});
test('shared session validation and secret whitelist', () => {
  for (const item of fixture('sessions.json').valid) {
    const result = validateSession(item.input);
    for (const [key, value] of Object.entries(item.expected)) assert.deepEqual(result[key], value, item.name);
    for (const key of item.absent || []) assert.ok(!Object.hasOwn(result, key), item.name);
  }
  for (const item of fixture('sessions.json').invalid) assert.throws(() => validateSession(item.input), undefined, item.name);
});
test('shared documents migrate and resolve group settings identically', () => {
  for (const item of fixture('sessions.json').documents) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-contract-'));
    try {
      fs.writeFileSync(path.join(directory, 'sessions.json'), JSON.stringify(item.document));
      const resolved = new Store(directory).resolveSession(item.session);
      for (const [key, value] of Object.entries(item.expected)) assert.deepEqual(resolved[key], value, item.name);
    } finally {
      assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep + 'astraterm-contract-'));
      fs.rmSync(directory, { recursive: true, force: true });
    }
  }
});
test('shared MultiExec cases match source delivery and reconnect/exit policy', () => {
  for (const item of fixture('multiexec.json').cases) {
    const writes = [];
    const router = new InputRouter((id, data) => writes.push({ id, data }));
    for (const id of ['a', 'b', 'c']) { router.register(id, 1); router.update(id, 1, true); }
    for (const id of item.selected || []) router.select(id, true);
    if (item.reconnect) router.update(item.reconnect, 2, true);
    if (item.exit) router.update(item.exit, 1, false);
    if (item.select_all) router.selectAll();
    if (item.clear) router.clear();
    router.enabled = item.enabled || false;
    router.input(item.source, item.data || 'x', item.human !== false);
    assert.deepEqual(writes.map(write => write.id).sort(), [...item.expected].sort(), item.name);
    assert.ok(writes.every(write => write.data === (item.data || 'x')), item.name);
  }
});
