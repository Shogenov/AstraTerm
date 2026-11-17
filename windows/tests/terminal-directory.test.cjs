'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { directoryReport, resolveTerminalDirectory } = require('../src/backend/terminal-directory.cjs');
const contract = require('../../shared/contracts/file-drops.json');

test('working directory reports use the shared shell formats', () => {
  for (const {kind, value, expected} of contract.directory_reports) {
    const report = directoryReport(kind, value);
    assert.deepEqual(report ? [report.host, report.user, report.path] : null, expected, value);
  }
});

test('relative home and hostname checks are tied to the SFTP connection', async () => {
  const report = directoryReport('title', 'alice@fixture: ~/work');
  const session = { host: '192.0.2.12', user: 'alice', readHome: async () => '/home/alice',
    readHostname: async () => 'fixture.example' };
  assert.equal(await resolveTerminalDirectory(report, session), '/home/alice/work');
  await assert.rejects(resolveTerminalDirectory(report, { ...session, readHostname: async () => 'other' }), /другому SSH-хосту/);
  await assert.rejects(resolveTerminalDirectory(report, { ...session, user: 'other' }), /другого пользователя/);
});
