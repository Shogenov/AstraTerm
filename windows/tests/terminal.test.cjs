'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const net = require('node:net');
const { Server, utils } = require('ssh2');
const { TerminalManager, parseJumpHosts, localDirectory } = require('../src/backend/terminal.cjs');
const { IdentityManager } = require('../src/backend/identities.cjs');
const { HostTrustStore } = require('../src/backend/host-trust.cjs');

test('shutdown waits for an owned ConPTY worker even when its tab was already closed',async()=>{
  const {EventEmitter}=require('node:events');
  const worker=new EventEmitter();worker.threadId=17;worker.terminate=async()=>{worker.threadId=-1;worker.emit('exit',0);};
  let kills=0,disposals=0;
  const child={onData:()=>({dispose(){}}),onExit:()=>({dispose(){}}),kill:()=>kills++,
    _agent:{_conoutSocketWorker:{_worker:worker,dispose:()=>disposals++}}};
  const manager=new TerminalManager({pty:{spawn:()=>child}});
  const item=await manager.create({protocol:'local'});manager.close(item.id);
  assert.equal(kills,1);assert.equal(disposals,1);assert.equal(manager.list().length,0);
  let done=false;const stopped=manager.dispose().then(()=>{done=true;});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(done,false);
  worker.threadId=-1;worker.emit('exit',0);await stopped;assert.equal(done,true);
});

async function until(predicate, timeout = 10000) {
  const start = Date.now();
  while (!predicate()) { if (Date.now() - start > timeout) throw new Error('Timed out waiting for terminal event'); await new Promise(resolve => setTimeout(resolve, 20)); }
}

function keyPair(encrypted = false) {
  return crypto.generateKeyPairSync('rsa', { modulusLength: 2048, publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs1', format: 'pem', ...(encrypted ? { cipher: 'aes-256-cbc', passphrase: 'fixture key passphrase' } : {}) } });
}

async function sshFixture(t, { sharedUserKey, ed25519Host = false } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-ssh-'));
  const hostKey = ed25519Host ? { privateKey: utils.generateKeyPairSync('ed25519').private } : keyPair();
  const userKey = sharedUserKey || keyPair(true);
  const user = utils.parseKey(userKey.privateKey, 'fixture key passphrase');
  const filename = path.join(directory, 'id_fixture');
  fs.writeFileSync(filename, userKey.privateKey);
  const state = { ptys: [], windows: [], handshakes: [], authenticated: 0, forwarded: 0, shells: [], clients: new Set(), received: [] };
  const server = new Server({ hostKeys: [hostKey.privateKey] }, client => {
    state.clients.add(client);
    client.on('error', () => {});
    client.on('close', () => state.clients.delete(client));
    client.on('handshake', negotiated => state.handshakes.push(negotiated));
    client.on('authentication', context => {
      if (context.username !== 'fixture-user' || context.method !== 'publickey' || !context.key.data.equals(user.getPublicSSH())) return context.reject(['publickey']);
      if (context.signature && user.verify(context.blob, context.signature, context.hashAlgo) !== true) return context.reject(['publickey']);
      context.accept();
    });
    client.on('ready', () => {
      state.authenticated++;
      client.on('tcpip', (accept, reject, info) => {
        const destination = net.connect(info.destPort, info.destIP);
        destination.on('error', () => { try { reject(); } catch {} });
        destination.once('connect', () => { state.forwarded++; const channel = accept(); channel.pipe(destination).pipe(channel); channel.on('close', () => destination.destroy()); });
      });
      client.on('session', accept => {
        const session = accept();
        session.on('pty', (acceptPty, reject, info) => { state.ptys.push(info); acceptPty(); });
        session.on('window-change', (acceptWindow, reject, info) => { state.windows.push(info); if (acceptWindow) acceptWindow(); });
        session.on('shell', acceptShell => {
          const shell = acceptShell();
          state.shells.push(shell);
          shell.write('SYNTHETIC READY\r\n');
          shell.on('data', data => { state.received.push(Buffer.from(data)); shell.write(data); });
        });
      });
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    for (const client of state.clients) client.end();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  return { directory, filename, userKey, port: server.address().port, state };
}

test('SSH uses Ed25519 server, real PTY, encrypted RSA once, strict trust, resize and reconnect generations', async t => {
  const fixture = await sshFixture(t, { ed25519Host: true });
  let unlocks = 0;
  let confirms = 0;
  const prompt = async request => {
    if (request.kind === 'passphrase') { unlocks++; return 'fixture key passphrase'; }
    if (request.kind === 'host-key') { confirms++; return true; }
    throw new Error('Unexpected password request');
  };
  const identityManager = new IdentityManager({ directory: fixture.directory, agentPath: path.join(fixture.directory, 'missing-agent'), prompt });
  const profile = identityManager.upsert({ name: 'Fixture', key_file: fixture.filename });
  const trustStore = new HostTrustStore({ directory: fixture.directory, knownHostsPath: path.join(fixture.directory, 'known_hosts'), prompt });
  const events = [];
  const manager = new TerminalManager({ identityManager, trustStore, prompt, emit: (type, payload) => events.push({ type, ...payload }) });
  t.after(() => { manager.dispose(); identityManager.dispose(); trustStore.dispose(); });
  const session = { protocol: 'ssh', host: '127.0.0.1', port: fixture.port, username: 'fixture-user', name: 'Synthetic SSH', identity_profile: profile.id };
  const first = await manager.create(session, { cols: 91, rows: 27 });
  assert.equal(first.status, 'running');
  assert.equal(events[0].type, 'terminal:created');
  assert.equal(manager.get(first.id).sshClient.constructor.name, 'Client');
  assert.deepEqual([fixture.state.ptys[0].cols, fixture.state.ptys[0].rows], [91, 27]);
  assert.equal(fixture.state.ptys[0].term, 'xterm-256color');
  assert.equal(fixture.state.handshakes[0].cs.compress, 'none');
  manager.write(first.id, 'Привет\r');
  await until(() => events.some(event => event.type === 'terminal:data' && event.data.includes('Привет')));
  manager.writeBinary(first.id, '\x1b[M\xff\x20\x21');
  await until(() => fixture.state.received.some(data => data.equals(Buffer.from([27, 91, 77, 255, 32, 33]))));
  manager.resize(first.id, { cols: 112, rows: 39 });
  await until(() => fixture.state.windows.length > 0);
  assert.deepEqual([fixture.state.windows.at(-1).cols, fixture.state.windows.at(-1).rows], [112, 39]);
  const second = await manager.create({ ...session, compression: true });
  assert.equal(fixture.state.handshakes.at(-1).cs.compress, 'zlib@openssh.com');
  assert.equal(unlocks, 1);
  assert.equal(confirms, 1);
  const reconnected = await manager.reconnect(first.id);
  assert.equal(reconnected.generation, 2);
  assert.equal(events.some(event => event.type === 'terminal:selection-reset' && event.id === first.id), true);
  assert.equal(unlocks, 1);
  assert.equal(confirms, 1);
  assert.equal(manager.close(second.id), true);
  assert.equal(manager.get(second.id), undefined);
  fixture.state.shells.at(-1).exit(0);
  fixture.state.shells.at(-1).end();
  await until(() => manager.get(first.id).status === 'exited');
  assert.equal(manager.write(first.id, 'ignored'), false);
});

test('rejected host key never reaches authentication', async t => {
  const fixture = await sshFixture(t);
  const identities = new IdentityManager({ directory: fixture.directory, agentPath: path.join(fixture.directory, 'missing-agent'), prompt: async () => 'fixture key passphrase' });
  identities.upsert({ name: 'Fixture', key_file: fixture.filename });
  const trust = new HostTrustStore({ directory: fixture.directory, knownHostsPath: path.join(fixture.directory, 'known_hosts'), prompt: async () => false });
  const manager = new TerminalManager({ identityManager: identities, trustStore: trust });
  t.after(() => { manager.dispose(); identities.dispose(); });
  await assert.rejects(manager.create({ protocol: 'ssh', host: '127.0.0.1', port: fixture.port, username: 'fixture-user' }), /Host denied|verification|Handshake/i);
  assert.equal(fixture.state.authenticated, 0);
});

test('local PowerShell runs on genuine Windows ConPTY with interactive output and lifetime cleanup', { skip: process.platform !== 'win32' }, async t => {
  const output = [];
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-cwd-'));
  const manager = new TerminalManager({ emit: (type, payload) => { if (type === 'terminal:data') output.push(payload.data); } });
  t.after(() => { manager.dispose(); fs.rmSync(directory, { recursive: true, force: true }); });
  const local = await manager.create({ protocol: 'local', name: 'ConPTY test', startup_directory: directory }, { cols: 100, rows: 30 });
  await until(() => output.join('').includes('PS '), 20000);
  manager.write(local.id, "[Console]::WriteLine('ASTRA_CONPTY_' + (6 * 7))\r");
  await until(() => output.join('').includes('ASTRA_CONPTY_42'), 20000);
  // The runner's TEMP can use an 8.3 alias while PowerShell displays the long path.
  const expectedDirectory = fs.realpathSync.native(directory).replace(/'/g, "''");
  manager.write(local.id, `[Console]::WriteLine('ASTRA_CWD_' + ((Get-Item -LiteralPath .).FullName -ieq '${expectedDirectory}'))\r`);
  await until(() => /ASTRA_CWD_(True|False)/.test(output.join('')), 20000);
  assert.match(output.join(''), /ASTRA_CWD_True/, 'PowerShell started in the wrong directory');
  manager.write(local.id, "[Console]::WriteLine('ASTRA_UNDO_' + (6 * 7))");
  await new Promise(resolve => setTimeout(resolve, 250));
  manager.write(local.id, 'Q');
  await new Promise(resolve => setTimeout(resolve, 250));
  manager.write(local.id, '\x1a'); // Ctrl+Z must undo Q, also with Russian layout.
  await new Promise(resolve => setTimeout(resolve, 250));
  manager.write(local.id, '\r');
  await until(() => output.join('').includes('ASTRA_UNDO_42'), 20000);
  manager.write(local.id, 'Start-Sleep -Seconds 20\r');
  await new Promise(resolve => setTimeout(resolve, 300));
  manager.write(local.id, '\x03'); // The same byte must interrupt an executing pipeline.
  await new Promise(resolve => setTimeout(resolve, 200));
  manager.write(local.id, "[Console]::WriteLine('ASTRA_INTERRUPT_' + (6 * 7))\r");
  await until(() => output.join('').includes('ASTRA_INTERRUPT_42'), 5000);
  manager.write(local.id, 'ASTRA_PENDING_LINE');
  await new Promise(resolve => setTimeout(resolve, 200));
  manager.write(local.id, '\x03'); // Ctrl+C must cancel the line, not insert Cyrillic c.
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.equal(manager.resize(local.id, { cols: 120, rows: 35 }), true);
  manager.write(local.id, 'exit\r');
  await until(() => manager.get(local.id).status === 'exited', 20000);
  assert.equal(manager.write(local.id, 'ignored'), false);
});

test('local startup directory expands home and rejects missing paths or ordinary files', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-directory-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'file.txt');
  fs.writeFileSync(filename, 'fixture');
  assert.equal(localDirectory('~'), os.homedir());
  assert.equal(localDirectory('~/.'), os.homedir());
  assert.equal(localDirectory('~\\.'), os.homedir());
  assert.equal(localDirectory(directory), directory);
  assert.throws(() => localDirectory(path.join(directory, 'missing')), /не существует/);
  assert.throws(() => localDirectory(filename), /не существует/);
});

test('jump parser preserves mandatory chains and refuses malformed shell fragments', () => {
  const chain = parseJumpHosts('admin@jump.internal:2222,user@[::1]:2200', { username: 'default', identity_profile: 'same' });
  assert.deepEqual(chain.map(item => [item.username, item.host, item.port]), [['admin', 'jump.internal', 2222], ['user', '::1', 2200]]);
  assert.throws(() => parseJumpHosts('gateway,', {}), /шлюзов/);
  assert.throws(() => parseJumpHosts('gateway; command', {}), /пользователь/);
});

test('mandatory SSH gateway authenticates and verifies each hop, then carries the PTY connection', async t => {
  const gateway = await sshFixture(t);
  const target = await sshFixture(t, { sharedUserKey: gateway.userKey });
  const confirmations = [];
  const prompt = async request => {
    if (request.kind === 'host-key') { confirmations.push(request.host); return true; }
    if (request.kind === 'passphrase') return 'fixture key passphrase';
    throw new Error('Unexpected password request');
  };
  const identities = new IdentityManager({ directory: gateway.directory, agentPath: path.join(gateway.directory, 'missing-agent'), prompt });
  identities.upsert({ name: 'Fixture', key_file: gateway.filename });
  const trust = new HostTrustStore({ directory: gateway.directory, knownHostsPath: path.join(gateway.directory, 'known_hosts'), prompt });
  const events = [];
  const manager = new TerminalManager({ identityManager: identities, trustStore: trust, prompt, emit: (type, payload) => events.push({ type, ...payload }) });
  t.after(() => { manager.dispose(); identities.dispose(); });
  const terminal = await manager.create({ protocol: 'ssh', host: '127.0.0.1', port: target.port, username: 'fixture-user', jump_host: `fixture-user@127.0.0.1:${gateway.port}` });
  manager.write(terminal.id, 'THROUGH_GATEWAY\r');
  await until(() => events.some(event => event.data && event.data.includes('THROUGH_GATEWAY')));
  assert.equal(gateway.state.authenticated, 1);
  assert.equal(gateway.state.forwarded, 1);
  assert.equal(target.state.authenticated, 1);
  assert.equal(confirmations.length, 2);
  assert.notEqual(confirmations[0], confirmations[1]);
});
