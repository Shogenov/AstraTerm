'use strict';

// Optional, read-only acceptance against an explicitly configured development VM.
// Credentials are obtained only from the caller's environment, never argv/files.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { TerminalManager } = require('../src/backend/terminal.cjs');
const { IdentityManager } = require('../src/backend/identities.cjs');
const { FilesService } = require('../src/backend/files.cjs');

async function main() {
  const host = process.env.ASTRA_VM_HOST;
  const username = process.env.ASTRA_VM_USER;
  const password = process.env.ASTRA_VM_PASSWORD;
  const pinned = process.env.ASTRA_VM_FINGERPRINT;
  if (!host || !username || !password || !pinned) throw new Error('Set ASTRA_VM_HOST, ASTRA_VM_USER, ASTRA_VM_PASSWORD and ASTRA_VM_FINGERPRINT');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-win-vm-'));
  let passwords = 0;
  let output = '';
  const prompt = async request => {
    assert.equal(request.kind, 'password'); passwords++;
    return password;
  };
  const identities = new IdentityManager({ directory, prompt, sshDirectory: path.join(directory, 'ssh'), agentPath: `\\.\pipe\astraterm-isolated-${crypto.randomUUID()}` });
  const trustStore = { async verify({ key }) {
    const md5 = crypto.createHash('md5').update(key).digest('hex');
    const sha = 'SHA256:' + crypto.createHash('sha256').update(key).digest('base64').replace(/=+$/, '');
    assert.ok(pinned === sha || pinned.replace(/:/g, '') === md5, 'Pinned VM host key does not match');
    return true;
  } };
  const terminals = new TerminalManager({ identityManager: identities, trustStore, prompt, emit: (type, payload) => { if (type === 'terminal:data') output += payload.data; } });
  const files = new FilesService({ terminals });
  try {
    const terminal = await terminals.create({ protocol: 'ssh', host, username, port: Number(process.env.ASTRA_VM_PORT || 22), identity_profile: 'system', encoding: 'UTF-8', compression: true });
    terminals.write(terminal.id, "printf 'ASTRA_WINDOWS_VM_%s\\n' 42\r");
    const start = Date.now();
    while (!output.includes('ASTRA_WINDOWS_VM_42')) {
      if (Date.now() - start > 15000) throw new Error('Remote PTY output missing');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    const binding = { mode: 'sftp', terminalId: terminal.id };
    const listing = await files.list(binding);
    assert.ok(Array.isArray(listing.entries));
    const version = await files.readText({ ...binding, path: '/etc/astra_version' });
    assert.match(version.text, /1\.6/);
    assert.equal(passwords, 1, 'SFTP must reuse the authenticated terminal');
    process.stdout.write(JSON.stringify({ result: 'WINDOWS_ASTRA_VM_PASS', version: version.text.trim(), checks: ['pinned host key', 'remote PTY', 'shared SFTP', 'single authentication', 'read-only version file'] }) + '\n');
  } finally {
    files.dispose(); terminals.dispose(); identities.dispose();
    const resolved = path.resolve(directory);
    assert.ok(resolved.startsWith(path.resolve(os.tmpdir()) + path.sep + 'astraterm-win-vm-'));
    fs.rmSync(resolved, { recursive: true, force: true });
  }
}
main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
