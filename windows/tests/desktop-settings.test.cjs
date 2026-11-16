'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { entryURL, localResource, installLocalUI } = require('../src/local-ui.cjs');
const { DiagnosticLog } = require('../src/backend/diagnostic-log.cjs');
const { sortFileEntries } = require('../src/renderer/file-sort.js');
const { mobaConfigPaths } = require('../src/backend/mobaxterm.cjs');
const sorting = require('../../shared/contracts/file-sort.json');

test('desktop scheme loads packaged files and rejects other origins and paths', async () => {
  assert.equal(localResource(entryURL), 'src/renderer/index.html');
  for (const url of ['file:///index.html', 'https://app/src/renderer/index.html', 'astraterm://other/src/renderer/index.html',
    'astraterm://app/src/backend/vault.cjs', 'astraterm://app/src/renderer/%2e%2e/backend/vault.cjs',
    'astraterm://user@app/src/renderer/index.html', 'astraterm://app/src/renderer/index.html?extra=1']) {
    assert.equal(localResource(url), null);
  }
  let handle;
  installLocalUI({ handle: (_scheme, callback) => { handle = callback; } }, path.resolve(__dirname, '..'));
  const response = await handle(new Request(entryURL));
  assert.equal(response.status, 200);
  assert.match(await response.text(), /Content-Security-Policy/);
  assert.equal((await handle(new Request(entryURL, { method: 'POST' }))).status, 404);
});

test('SFTP columns sort typed metadata and keep directories first', () => {
  for (const [key, descending, expected] of sorting.cases) {
    assert.deepEqual(sortFileEntries(sorting.entries, key, descending).map(entry => entry.name), expected);
  }
});

test('logging is opt-in and filters by level', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astra-diagnostic-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const log = new DiagnosticLog(directory);
  log.write('error', 'session.error');
  assert.ok(!fs.existsSync(log.filename));
  log.settings = log.validate({ enabled: true, level: 'warning' });
  fs.writeFileSync(log.settingsPath, JSON.stringify(log.settings));
  const reopened = new DiagnosticLog(directory);
  reopened.write('debug', 'session.open');
  reopened.write('error', 'session.failed');
  reopened.write('warning', 'sensitive text');
  const event = JSON.parse(fs.readFileSync(log.filename, 'utf8'));
  assert.equal(event.event, 'session.failed');
  assert.equal(event.level, 'error');
});

test('MobaXterm discovery searches the redirected Documents folder and AppData', () => {
  assert.deepEqual(mobaConfigPaths({ documents: '/docs', appData: '/app' }),
    ['/docs', '/app'].map(root => path.join(root, 'MobaXterm', 'MobaXterm.ini')));
});
