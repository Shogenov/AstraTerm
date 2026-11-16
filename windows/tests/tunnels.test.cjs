'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');
const { EventEmitter, once } = require('node:events');
const { TunnelManager, validateTunnel } = require('../src/backend/tunnels.cjs');

async function listen(server, host = '127.0.0.1') {
  server.listen(0, host);
  await once(server, 'listening');
  return server.address().port;
}

function close(server) {
  return new Promise(resolve => { try { server.close(resolve); } catch (_) { resolve(); } });
}

class TestSSHClient extends EventEmitter {
  constructor() { super(); this.forwards = new Map(); this.out = []; this.authentications = 0; }
  forwardOut(source, sourcePort, host, port, callback) {
    this.out.push({ host, port });
    const socket = net.createConnection({ host, port });
    const failed = error => callback(error);
    socket.once('error', failed);
    socket.once('connect', () => { socket.removeListener('error', failed); callback(null, socket); });
  }
  forwardIn(host, port, callback) {
    const server = net.createServer(socket => this.emit('tcp connection', {
      destIP: host, destPort: server.address().port, srcIP: socket.remoteAddress, srcPort: socket.remotePort,
    }, () => socket, () => socket.destroy()));
    server.once('error', callback);
    server.listen(port, host, () => {
      this.forwards.set(server.address().port, server);
      callback(null, server.address().port);
    });
  }
  unforwardIn(host, port, callback) {
    const server = this.forwards.get(port);
    if (!server) return callback();
    this.forwards.delete(port);
    server.close(callback);
  }
}

async function fixture(t) {
  const echoSockets = new Set();
  const echo = net.createServer(socket => {
    echoSockets.add(socket);
    socket.once('close', () => echoSockets.delete(socket));
    socket.on('error', () => {});
    socket.pipe(socket);
  });
  const echoPort = await listen(echo);
  const client = new TestSSHClient();
  const terminals = new Map([['ssh-1', { sshClient: client }], ['local-1', {}]]);
  const events = [];
  const manager = new TunnelManager({ terminals, emit: (type, payload) => events.push({ type, payload }) });
  t.after(async () => {
    await manager.dispose();
    for (const socket of echoSockets) socket.destroy();
    await close(echo);
  });
  return { manager, client, echoPort, events };
}

async function connect(port) {
  const socket = net.createConnection({ host: '127.0.0.1', port });
  socket.on('error', () => {});
  await once(socket, 'connect');
  return socket;
}

function reader(socket) {
  let pending = Buffer.alloc(0);
  const waiting = [];
  let failure = null;
  function pump() {
    while (waiting.length && pending.length >= waiting[0].count) {
      const { count, resolve, timer } = waiting.shift();
      clearTimeout(timer);
      const result = pending.subarray(0, count);
      pending = pending.subarray(count);
      resolve(result);
    }
  }
  socket.on('data', data => { pending = Buffer.concat([pending, data]); pump(); });
  socket.on('close', () => {
    failure = new Error('Socket closed before expected data');
    while (waiting.length) { const item = waiting.shift(); clearTimeout(item.timer); item.reject(failure); }
  });
  return count => new Promise((resolve, reject) => {
    if (failure) return reject(failure);
    const item = { count, resolve, reject };
    item.timer = setTimeout(() => reject(new Error(`Timed out waiting for ${count} bytes`)), 5000);
    waiting.push(item);
    pump();
  });
}

function request(host, port, atyp = 3) {
  let address;
  if (atyp === 1) address = Buffer.from(host.split('.').map(Number));
  else if (atyp === 4) address = Buffer.from('00000000000000000000000000000001', 'hex');
  else { const name = Buffer.from(host); address = Buffer.concat([Buffer.from([name.length]), name]); }
  const last = Buffer.alloc(2);
  last.writeUInt16BE(port);
  return Buffer.concat([Buffer.from([5, 1, 0, atyp]), address, last]);
}

test('validation defaults to loopback and rejects malformed endpoints', () => {
  const base = { terminalId: 'ssh-1', type: 'local', listen_port: 0, destination_host: 'localhost', destination_port: 22 };
  assert.equal(validateTunnel(base).bind_host, '127.0.0.1');
  assert.equal(validateTunnel({ ...base, bind_host: '0.0.0.0' }).bind_host, '0.0.0.0');
  assert.equal(validateTunnel({ ...base, destination_host: '[::1]' }).destination_host, '::1');
  for (const overrides of [{ type: 'bad' }, { bind_host: '' }, { listen_port: -1 }, { listen_port: 65536 },
    { destination_port: 0 }, { destination_host: 'host; command' }, { terminalId: '' }, { id: '../escape' }]) {
    assert.throws(() => validateTunnel({ ...base, ...overrides }));
  }
});

test('local forward transports actual TCP bytes and stop closes listeners and streams', async t => {
  const { manager, client, echoPort, events } = await fixture(t);
  const tunnel = await manager.start({ terminalId: 'ssh-1', id: 'local', type: 'local', listen_port: 0,
    destination_host: '127.0.0.1', destination_port: echoPort });
  assert.equal(tunnel.status, 'running');
  assert.equal(tunnel.bind_host, '127.0.0.1');
  const socket = await connect(tunnel.listen_port);
  const read = reader(socket);
  const payload = Buffer.from('TCP forwarding: кириллица\0\xff');
  socket.write(payload);
  assert.deepEqual(await read(payload.length), payload);
  assert.equal(client.authentications, 0);
  assert.deepEqual(client.out, [{ host: '127.0.0.1', port: echoPort }]);
  const closed = once(socket, 'close');
  assert.equal((await manager.stop(tunnel.id)).status, 'stopped');
  await closed;
  assert.deepEqual(manager.list(), []);
  assert.equal(client.listenerCount('close'), 0);
  assert.ok(events.some(event => event.type === 'tunnel' && event.payload.status === 'running'));
});

test('SOCKS5 fragmented greeting/request preserves coalesced first payload exactly once', async t => {
  const { manager, echoPort } = await fixture(t);
  const tunnel = await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const socket = await connect(tunnel.listen_port);
  t.after(() => socket.destroy());
  const read = reader(socket);
  socket.write(Buffer.from([5]));
  await new Promise(resolve => setTimeout(resolve, 10));
  socket.write(Buffer.from([2, 2]));
  socket.write(Buffer.from([0]));
  assert.deepEqual(await read(2), Buffer.from([5, 0]));
  const req = request('127.0.0.1', echoPort);
  const payload = Buffer.from('first payload\0 and additional bytes');
  socket.write(req.subarray(0, 3));
  socket.write(Buffer.concat([req.subarray(3), payload]));
  assert.equal((await read(10))[1], 0);
  assert.deepEqual(await read(payload.length), payload);
  const second = Buffer.from('second');
  socket.write(second);
  assert.deepEqual(await read(second.length), second);
});

test('SOCKS5 coalesced greeting, IPv4 request, and binary payload retain framing', async t => {
  const { manager, echoPort } = await fixture(t);
  const tunnel = await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const socket = await connect(tunnel.listen_port);
  t.after(() => socket.destroy());
  const read = reader(socket);
  const payload = Buffer.alloc(65536);
  for (let i = 0; i < payload.length; ++i) payload[i] = i % 256;
  socket.write(Buffer.concat([Buffer.from([5, 1, 0]), request('127.0.0.1', echoPort, 1), payload]));
  assert.deepEqual(await read(2), Buffer.from([5, 0]));
  assert.equal((await read(10))[1], 0);
  assert.deepEqual(await read(payload.length), payload);
});

test('SOCKS5 IPv6 address is decoded and forwarded without changing initial data', async t => {
  const { manager, client, echoPort } = await fixture(t);
  const original = client.forwardOut.bind(client);
  let requested;
  client.forwardOut = (source, sourcePort, host, port, callback) => {
    requested = host;
    original(source, sourcePort, '127.0.0.1', port, callback);
  };
  const tunnel = await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const socket = await connect(tunnel.listen_port);
  t.after(() => socket.destroy());
  const read = reader(socket);
  socket.write(Buffer.concat([Buffer.from([5, 1, 0]), request('::1', echoPort, 4), Buffer.from('IPv6')]));
  await read(2);
  assert.equal((await read(10))[1], 0);
  assert.equal((await read(4)).toString(), 'IPv6');
  assert.equal(net.isIP(requested), 6);
  assert.equal(requested, '0:0:0:0:0:0:0:1');
});

test('SOCKS5 rejects authentication and unsupported commands', async t => {
  const { manager } = await fixture(t);
  const tunnel = await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const first = await connect(tunnel.listen_port);
  t.after(() => first.destroy());
  const firstRead = reader(first);
  first.write(Buffer.from([5, 1, 2]));
  assert.deepEqual(await firstRead(2), Buffer.from([5, 255]));
  const second = await connect(tunnel.listen_port);
  t.after(() => second.destroy());
  const secondRead = reader(second);
  second.write(Buffer.from([5, 1, 0, 5, 2, 0, 1]));
  await secondRead(2);
  assert.equal((await secondRead(10))[1], 7);
});

test('remote forward uses the existing SSH connection and local TCP destination', async t => {
  const { manager, client, echoPort } = await fixture(t);
  const tunnel = await manager.start({ terminalId: 'ssh-1', type: 'remote', listen_port: 0,
    destination_host: '127.0.0.1', destination_port: echoPort });
  const socket = await connect(tunnel.listen_port);
  t.after(() => socket.destroy());
  const read = reader(socket);
  socket.write('remote initial payload');
  assert.equal((await read(22)).toString(), 'remote initial payload');
  assert.equal(client.authentications, 0);
  assert.equal(client.forwards.size, 1);
  await manager.stop(tunnel.id);
  assert.equal(client.forwards.size, 0);
});

test('session closure stops all forwards and late channels are destroyed', async t => {
  const { manager, client, echoPort } = await fixture(t);
  let delayed;
  client.forwardOut = (...args) => { delayed = args.at(-1); };
  const first = await manager.start({ terminalId: 'ssh-1', type: 'local', listen_port: 0,
    destination_host: '127.0.0.1', destination_port: echoPort });
  await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const socket = await connect(first.listen_port);
  t.after(() => socket.destroy());
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(typeof delayed, 'function');
  client.emit('close');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(manager.list(), []);
  const late = new net.Socket();
  delayed(null, late);
  assert.equal(late.destroyed, true);
});

test('active SSH is required and duplicate IDs cannot replace a running listener', async t => {
  const { manager } = await fixture(t);
  const config = { terminalId: 'ssh-1', id: 'same', type: 'dynamic', listen_port: 0 };
  await assert.rejects(manager.start({ ...config, terminalId: 'local-1' }), /SSH/);
  const tunnel = await manager.start(config);
  await assert.rejects(manager.start(config), /ID/);
  assert.equal(manager.list()[0].listen_port, tunnel.listen_port);
  await manager.dispose();
  await assert.rejects(manager.start(config), /закрыт/);
});

test('one SSH session has a bounded shared resource budget across all listeners', async t => {
  const { manager } = await fixture(t);
  const one = await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const two = await manager.start({ terminalId: 'ssh-1', type: 'dynamic', listen_port: 0 });
  const sockets = await Promise.all(Array.from({ length: 100 }, (_, index) => connect(index % 2 ? one.listen_port : two.listen_port)));
  t.after(() => sockets.forEach(socket => socket.destroy()));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(manager.resources.get('ssh-1').size, 100);
  const extra = await connect(one.listen_port);
  t.after(() => extra.destroy());
  if (!extra.destroyed) await once(extra, 'close');
  assert.equal(manager.resources.get('ssh-1').size, 100);
  await manager.sessionClosed('ssh-1');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(manager.resources.has('ssh-1'), false);
  assert.deepEqual(manager.list(), []);
});

test('closing an SSH session during startup cancels the listener without an orphan', async t => {
  const { manager } = await fixture(t);
  const starting = manager.start({ terminalId: 'ssh-1', id: 'cancelled', type: 'dynamic', listen_port: 0 });
  const rejected = assert.rejects(starting, /отменён/);
  await manager.sessionClosed('ssh-1');
  await rejected;
  assert.deepEqual(manager.list(), []);
});
