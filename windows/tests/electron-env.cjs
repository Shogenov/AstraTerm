'use strict';

// Run with ELECTRON_RUN_AS_NODE=1 using the installed Electron executable.
// This checks the shipped N-API addon and a real Windows pseudoconsole, without
// opening the application, reading profiles, or changing a user SSH agent.
const assert = require('node:assert/strict');
const pty = require('node-pty');

assert.ok(process.versions.electron, 'Run this smoke test with Electron in Node mode');
assert.equal(process.platform, 'win32');

const marker = 'ASTRATERM_ELECTRON_CONPTY_OK';
const child = pty.spawn(process.env.ComSpec || 'C:\\Windows\\System32\\cmd.exe', ['/d', '/q'], {
  name: 'xterm-256color', cols: 80, rows: 24, cwd: process.env.TEMP,
  env: { ...process.env }, useConpty: true,
});
let output = '';
let timedOut = false;
const timeout = setTimeout(() => { timedOut = true; child.kill(); }, 15000);
const dataSubscription = child.onData(data => { output += data; });
const exitSubscription = child.onExit(({ exitCode }) => {
  clearTimeout(timeout);
  // node-pty 1.1.0 leaves its output-draining worker alive after a natural
  // process exit. Release that worker without signalling an exited process ID.
  const outputWorker = child._agent && child._agent._conoutSocketWorker;
  if (outputWorker && typeof outputWorker.dispose === 'function') outputWorker.dispose();
  dataSubscription.dispose();
  exitSubscription.dispose();
  try {
    assert.equal(timedOut, false, 'Pseudoconsole did not terminate');
    assert.equal(exitCode, 0);
    assert.ok(output.includes(marker), 'Real command output was not received');
    process.stdout.write(JSON.stringify({ result: 'ELECTRON_ENV_SMOKE_OK', electron: process.versions.electron,
      node: process.versions.node, terminal: 'Windows ConPTY', addon: 'node-pty 1.1.0', exitCode }) + '\n');
  } catch (error) {
    process.stderr.write('ELECTRON_ENV_SMOKE_FAILED: ' + error.message + '\n');
    process.exitCode = 1;
  }
});
child.resize(104, 32);
child.write(`echo ${marker}\rexit /b 0\r`);
