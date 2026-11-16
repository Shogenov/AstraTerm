'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { InputRouter } = require('../src/renderer/input-router.js');

function workspace() {
  const writes = [];
  const router = new InputRouter((id, data) => writes.push({ id, data }));
  for (const id of ['alpha', 'bravo', 'charlie']) {
    router.register(id);
    router.update(id, 1, true);
  }
  return { router, writes };
}
function recipients(writes) { return writes.map(entry => entry.id).sort(); }

test('MultiExec starts disabled and human input remains local even with selected panes', () => {
  const { router, writes } = workspace();
  router.selectAll();
  assert.equal(router.enabled, false);
  router.input('alpha', 'whoami\r', true);
  assert.deepEqual(writes, [{ id: 'alpha', data: 'whoami\r' }]);
});

test('human input from a checked source reaches exactly the checked panes once', () => {
  const { router, writes } = workspace();
  router.enabled = true;
  router.select('alpha', true); router.select('bravo', true); router.select('alpha', true);
  router.input('alpha', 'hostname\r', true);
  assert.deepEqual(recipients(writes), ['alpha', 'bravo']);
  assert.equal(writes.filter(entry => entry.id === 'alpha').length, 1, 'source must not receive both native and broadcast sends');
  assert.ok(writes.every(entry => entry.data === 'hostname\r'));
});

test('a human source without its checkbox stays local while the other recipients remain selected', () => {
  const { router, writes } = workspace();
  router.enabled = true;
  router.select('alpha', true); router.select('bravo', true);
  router.input('charlie', 'private command\r', true);
  assert.deepEqual(writes, [{ id: 'charlie', data: 'private command\r' }]);
  assert.deepEqual([...router.selected].sort(), ['alpha', 'bravo']);
});

test('programmatic macros and terminal replies never become broadcast sources', () => {
  const { router, writes } = workspace();
  router.enabled = true; router.selectAll();
  router.input('alpha', 'saved macro\r');
  router.input('alpha', '\x1b[12;34R', false); // Cursor-position response.
  router.input('bravo', '\x1b[?1;2c', false); // Device-attributes response.
  assert.deepEqual(writes, [
    { id: 'alpha', data: 'saved macro\r' },
    { id: 'alpha', data: '\x1b[12;34R' },
    { id: 'bravo', data: '\x1b[?1;2c' },
  ]);
});

test('select all includes running panes only and later connections require a new explicit choice', () => {
  const { router, writes } = workspace();
  router.update('bravo', 1, false);
  router.register('connecting');
  router.enabled = true; router.selectAll();
  assert.deepEqual([...router.selected].sort(), ['alpha', 'charlie']);
  router.register('new-host', 1); router.update('new-host', 1, true);
  router.update('connecting', 1, true);
  router.input('alpha', 'selected only', true);
  assert.deepEqual(recipients(writes), ['alpha', 'charlie']);
  assert.equal(router.selected.has('new-host'), false);
  assert.equal(router.selected.has('connecting'), false);
  router.selectAll();
  assert.deepEqual([...router.selected].sort(), ['alpha', 'charlie', 'connecting', 'new-host']);
});

test('reconnect generations invalidate the old checkbox until the user selects the new connection', () => {
  const { router, writes } = workspace();
  router.enabled = true; router.selectAll();
  router.update('bravo', 2, false);
  router.select('bravo', true);
  assert.equal(router.selected.has('bravo'), false, 'a connecting generation cannot be selected');
  router.update('bravo', 2, true);
  router.input('alpha', 'first', true);
  assert.deepEqual(recipients(writes), ['alpha', 'charlie']);
  writes.length = 0;
  router.select('bravo', true); router.input('alpha', 'second', true);
  assert.deepEqual(recipients(writes), ['alpha', 'bravo', 'charlie']);
  router.update('bravo', 3, true);
  assert.equal(router.selected.has('bravo'), false, 'direct running-to-running replacement is also a new identity');
});

test('exited and removed panes receive no input and cannot inherit their previous selection', () => {
  const { router, writes } = workspace();
  router.enabled = true; router.selectAll();
  router.update('bravo', 1, false); router.remove('charlie');
  router.input('bravo', 'must not reach a dead PTY', true);
  router.input('charlie', 'must not reach a removed PTY', false);
  router.input('alpha', 'live only', true);
  assert.deepEqual(writes, [{ id: 'alpha', data: 'live only' }]);
  router.register('charlie', 2); router.update('charlie', 2, true);
  assert.equal(router.selected.has('charlie'), false);
});

test('pause preserves chosen recipients; clear removes all recipients without disabling local input', () => {
  const { router, writes } = workspace();
  router.enabled = true; router.select('alpha', true); router.select('bravo', true);
  router.enabled = false; router.input('alpha', 'paused', true);
  assert.deepEqual(recipients(writes), ['alpha']);
  assert.deepEqual([...router.selected].sort(), ['alpha', 'bravo']);
  writes.length = 0; router.enabled = true; router.input('alpha', 'resumed', true);
  assert.deepEqual(recipients(writes), ['alpha', 'bravo']);
  writes.length = 0; router.clear(); router.input('alpha', 'local', true);
  assert.deepEqual(writes, [{ id: 'alpha', data: 'local' }]);
});

test('NUL, control/navigation keys, Cyrillic and Unicode payloads are delivered unchanged', () => {
  const { router, writes } = workspace();
  router.enabled = true; router.select('alpha', true); router.select('charlie', true);
  const payloads = ['\0', '\x03', '\x04', '\x7f', '\t', '\r', '\x1b[A', '\x1b[3~', 'Привет, мир! 🛰️', '\x1b[200~строка 1\nстрока 2\x1b[201~'];
  for (const payload of payloads) router.input('alpha', payload, true);
  assert.equal(writes.length, payloads.length * 2);
  for (const id of ['alpha', 'charlie']) {
    assert.deepEqual(writes.filter(entry => entry.id === id).map(entry => entry.data), payloads);
  }
  assert.equal(writes.some(entry => entry.id === 'bravo'), false);
});
