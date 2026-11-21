'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');
const { parseMode, sudoChmod } = require('../src/backend/file-permissions.cjs');
const contract = require('../../shared/contracts/file-permissions.json');

test('file permissions accept only three or four octal digits', () => {
  for (const [mode, expected] of Object.entries(contract.modes)) assert.equal(parseMode(mode), expected);
  for (const mode of contract.invalid) assert.throws(() => parseMode(mode), /восьмеричные/);
});

test('sudo uses one password on stdin only after a challenge and revalidates the binding', async () => {
  const calls = [];
  const client = { exec(command, callback) {
    const stream = new EventEmitter();
    stream.stderr = new EventEmitter();
    stream.end = password => {
      calls.push({ command, password });
      if (calls.length === 1) stream.stderr.emit('data', Buffer.from(contract.sudo.challenge));
      stream.emit('close', calls.length === 1 ? 1 : 0);
    };
    callback(null, stream);
  } };
  let checks = 0;
  const password = 'synthetic password';
  const changed = await sudoChmod(client, "/test/quote ';", 0o600, [0o100644, 1000, 1000, 1, 10],
    async () => password, () => { checks++; });
  assert.equal(changed, true);
  assert.equal(checks, 2);
  assert.equal(calls[0].password, undefined);
  assert.equal(calls[1].password, password + '\n');
  assert.ok(calls.every(call => !call.command.includes(password) && call.command.includes('/usr/bin/python3')));
});
