'use strict';

const net = require('node:net');
const { randomUUID } = require('node:crypto');

const MAX_RESOURCES = 100;
const CONNECT_TIMEOUT = 15000;
const MAX_PENDING_BYTES = 1024 * 1024;

function host(value, label, fallback) {
  if (value === undefined && fallback !== undefined) return fallback;
  if (typeof value !== 'string') throw new Error(`${label}: укажите адрес`);
  value = value.trim();
  if (value.startsWith('[') && value.endsWith(']')) value = value.slice(1, -1);
  if (!value || value.length > 253 || (!net.isIP(value) && !/^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$/.test(value))) {
    throw new Error(`${label}: неверный адрес`);
  }
  return value;
}

function port(value, label, allowZero = false) {
  if (typeof value === 'string' && /^\d+$/.test(value)) value = Number(value);
  if (!Number.isInteger(value) || value < (allowZero ? 0 : 1) || value > 65535) {
    throw new Error(`${label}: порт должен быть от ${allowZero ? 0 : 1} до 65535`);
  }
  return value;
}

function validateTunnel(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Неверные настройки туннеля');
  if (typeof value.terminalId !== 'string' || !value.terminalId || value.terminalId.length > 128) {
    throw new Error('Выберите активный SSH-терминал');
  }
  if (!['local', 'remote', 'dynamic'].includes(value.type)) throw new Error('Неверный тип туннеля');
  const id = value.id === undefined ? `tunnel-${randomUUID()}` : value.id;
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_.-]{1,128}$/.test(id)) throw new Error('Неверный ID туннеля');
  const name = value.name === undefined ? '' : value.name;
  if (typeof name !== 'string' || name.length > 160 || /[\x00-\x1f\x7f]/.test(name)) throw new Error('Неверное имя туннеля');
  const result = {
    id, name, terminalId: value.terminalId, type: value.type,
    bind_host: host(value.bind_host, 'Адрес прослушивания', '127.0.0.1'),
    listen_port: port(value.listen_port, 'Локальный порт', true),
  };
  if (value.type !== 'dynamic') {
    result.destination_host = host(value.destination_host, 'Адрес назначения');
    result.destination_port = port(value.destination_port, 'Порт назначения');
  }
  return result;
}

function destroy(resource) {
  try { resource.destroy(); } catch (_) { /* Already closed. */ }
}

function addressMatches(bind, actual) {
  if (bind === '0.0.0.0' || bind === '::') return true;
  if (bind.toLowerCase() === String(actual).toLowerCase()) return true;
  const loopback = new Set(['localhost', '127.0.0.1', '::1']);
  return (bind.toLowerCase() === 'localhost' || String(actual).toLowerCase() === 'localhost')
    && loopback.has(bind.toLowerCase()) && loopback.has(String(actual).toLowerCase());
}

class TunnelManager {
  constructor({ terminals, emit = () => {} } = {}) {
    this.terminals = terminals;
    this.emit = emit;
    this.entries = new Map();
    this.clients = new Map();
    this.resources = new Map();
    this.disposed = false;
  }

  _terminal(id) {
    if (this.terminals && typeof this.terminals.get === 'function') return this.terminals.get(id);
    for (const key of ['sessions', 'terminals']) {
      const collection = this.terminals && this.terminals[key];
      if (collection && typeof collection.get === 'function') return collection.get(id);
    }
    return null;
  }

  _descriptor(entry, status = entry.status) {
    return { ...entry.config, status };
  }

  _publish(entry, status) {
    this.emit('tunnel', this._descriptor(entry, status));
    this.emit('tunnels-changed', this.list());
  }

  list() {
    return [...this.entries.values()].map(entry => this._descriptor(entry));
  }

  _attachClient(entry) {
    let state = this.clients.get(entry.client);
    if (!state) {
      state = {
        entries: new Set(),
        onTCP: (info, accept, reject) => this._remoteConnection(entry.client, info, accept, reject),
        onClose: () => {
          const current = this.clients.get(entry.client);
          if (current) for (const item of [...current.entries]) void this.stop(item.config.id);
        },
      };
      this.clients.set(entry.client, state);
      entry.client.on('tcp connection', state.onTCP);
      entry.client.on('close', state.onClose);
    }
    state.entries.add(entry);
  }

  _detachClient(entry) {
    const state = this.clients.get(entry.client);
    if (!state) return;
    state.entries.delete(entry);
    if (!state.entries.size) {
      entry.client.removeListener('tcp connection', state.onTCP);
      entry.client.removeListener('close', state.onClose);
      this.clients.delete(entry.client);
    }
  }

  _adopt(entry, resource) {
    resource.on('error', () => {});
    if (entry.closed || resource.destroyed) { destroy(resource); return false; }
    let set = this.resources.get(entry.config.terminalId);
    if (!set) { set = new Set(); this.resources.set(entry.config.terminalId, set); }
    if (set.size >= MAX_RESOURCES) { destroy(resource); return false; }
    set.add(resource);
    entry.resources.add(resource);
    resource.once('close', () => {
      set.delete(resource);
      entry.resources.delete(resource);
      if (!set.size && this.resources.get(entry.config.terminalId) === set) this.resources.delete(entry.config.terminalId);
    });
    return true;
  }

  _timer(entry, callback, milliseconds = CONNECT_TIMEOUT) {
    const timer = setTimeout(() => { entry.timers.delete(timer); callback(); }, milliseconds);
    timer.unref?.();
    entry.timers.add(timer);
    return timer;
  }

  _clearTimer(entry, timer) {
    clearTimeout(timer);
    entry.timers.delete(timer);
  }

  _bridge(left, right, initial = null) {
    const closeLeft = () => destroy(left);
    const closeRight = () => destroy(right);
    left.once('error', closeRight);
    right.once('error', closeLeft);
    left.once('close', closeRight);
    right.once('close', closeLeft);
    if (initial && initial.length) right.write(initial);
    left.pipe(right);
    right.pipe(left);
    left.resume();
    right.resume();
  }

  _forward(entry, socket, destination, ready, failed) {
    socket.pause();
    let finished = false;
    const timer = this._timer(entry, () => {
      if (finished) return;
      finished = true;
      failed(Object.assign(new Error('Время подключения истекло'), { code: 'ETIMEDOUT' }));
    });
    const abandon = () => { finished = true; this._clearTimer(entry, timer); };
    socket.once('close', abandon);
    const complete = (error, channel) => {
      if (finished || entry.closed || socket.destroyed) { if (channel) destroy(channel); return; }
      finished = true;
      this._clearTimer(entry, timer);
      socket.removeListener('close', abandon);
      if (error) { failed(error); return; }
      if (!channel || !this._adopt(entry, channel)) { failed(new Error('Достигнут лимит соединений')); return; }
      ready(channel);
    };
    try {
      entry.client.forwardOut(socket.remoteAddress || '127.0.0.1', socket.remotePort || 0,
        destination.host, destination.port, complete);
    } catch (error) { complete(error); }
  }

  async start(value) {
    if (this.disposed) throw new Error('Менеджер туннелей закрыт');
    const config = validateTunnel(value);
    if (this.entries.has(config.id)) throw new Error('Туннель с таким ID уже запущен');
    const terminal = this._terminal(config.terminalId);
    const client = terminal && terminal.sshClient;
    if (!client || typeof client.forwardOut !== 'function') throw new Error('Для туннеля нужен активный SSH-терминал');
    if (config.type === 'remote' && typeof client.forwardIn !== 'function') throw new Error('Удалённый туннель недоступен');
    const entry = { config, client, resources: new Set(), timers: new Set(), status: 'starting', closed: false, server: null };
    this.entries.set(config.id, entry);
    this._attachClient(entry);
    try {
      if (config.type === 'remote') {
        await new Promise((resolve, reject) => {
          entry.cancelStart = () => reject(new Error('Запуск туннеля отменён'));
          const timer = this._timer(entry, () => reject(new Error('Время запуска туннеля истекло')));
          const callback = (error, assignedPort) => {
            this._clearTimer(entry, timer);
            if (entry.closed) {
              if (!error) try { client.unforwardIn(config.bind_host, assignedPort || config.listen_port, () => {}); } catch (_) {}
              return;
            }
            if (error) return reject(new Error('SSH-сервер отклонил удалённый туннель'));
            if (assignedPort) {
              try { config.listen_port = port(assignedPort, 'Удалённый порт'); }
              catch (_) { reject(new Error('SSH-сервер вернул неверный порт туннеля')); return; }
            }
            resolve();
          };
          try { client.forwardIn(config.bind_host, config.listen_port, callback); } catch (error) { reject(error); }
        });
      } else {
        entry.server = net.createServer(socket => {
          if (!this._adopt(entry, socket)) return;
          if (config.type === 'dynamic') this._socks(entry, socket);
          else this._forward(entry, socket, { host: config.destination_host, port: config.destination_port },
            channel => this._bridge(socket, channel), () => destroy(socket));
        });
        await new Promise((resolve, reject) => {
          entry.cancelStart = () => reject(new Error('Запуск туннеля отменён'));
          const onError = () => reject(new Error('Не удалось открыть порт туннеля'));
          entry.server.once('error', onError);
          entry.server.listen({ host: config.bind_host, port: config.listen_port, exclusive: true }, () => {
            entry.server.removeListener('error', onError);
            if (entry.closed) return;
            config.listen_port = entry.server.address().port;
            resolve();
          });
        });
        entry.server.on('error', () => { void this.stop(config.id); });
      }
      if (entry.closed) throw new Error('Запуск туннеля отменён');
      entry.cancelStart = null;
      entry.status = 'running';
      this._publish(entry);
      return this._descriptor(entry);
    } catch (error) {
      await this.stop(config.id);
      throw error;
    }
  }

  _remoteConnection(client, info, accept, reject) {
    const state = this.clients.get(client);
    const entry = state && [...state.entries].find(item => !item.closed && item.status === 'running'
      && item.config.type === 'remote' && item.config.listen_port === info.destPort
      && addressMatches(item.config.bind_host, info.destIP));
    if (!entry) { reject(); return; }
    const set = this.resources.get(entry.config.terminalId);
    if (set && set.size >= MAX_RESOURCES - 1) { reject(); return; }
    let channel;
    try { channel = accept(); } catch (_) { return; }
    if (!this._adopt(entry, channel)) return;
    channel.pause();
    const socket = net.createConnection({ host: entry.config.destination_host, port: entry.config.destination_port });
    if (!this._adopt(entry, socket)) { destroy(channel); return; }
    const timer = this._timer(entry, () => { destroy(channel); destroy(socket); });
    const failed = () => { this._clearTimer(entry, timer); destroy(channel); destroy(socket); };
    socket.once('error', failed);
    channel.once('error', failed);
    channel.once('close', () => { this._clearTimer(entry, timer); destroy(socket); });
    socket.once('connect', () => {
      this._clearTimer(entry, timer);
      socket.removeListener('error', failed);
      channel.removeListener('error', failed);
      if (entry.closed || channel.destroyed) { destroy(socket); return; }
      this._bridge(channel, socket);
    });
  }

  _socks(entry, socket) {
    let state = 'greeting';
    let pending = Buffer.alloc(0);
    let completed = false;
    const timer = this._timer(entry, () => destroy(socket));
    const finish = () => {
      this._clearTimer(entry, timer);
      socket.removeListener('data', data);
      completed = true;
    };
    socket.once('close', finish);
    const reject = code => {
      finish();
      socket.end(Buffer.from([5, code, 0, 1, 0, 0, 0, 0, 0, 0]));
      const flush = this._timer(entry, () => destroy(socket), 1000);
      socket.once('close', () => this._clearTimer(entry, flush));
    };
    const data = chunk => {
      if (completed) return;
      if (pending.length + chunk.length > MAX_PENDING_BYTES) { destroy(socket); return; }
      pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
      if (state === 'greeting') {
        if (pending.length < 2) return;
        if (pending[0] !== 5 || pending[1] === 0) { destroy(socket); return; }
        const size = 2 + pending[1];
        if (pending.length < size) return;
        if (!pending.subarray(2, size).includes(0)) {
          finish();
          socket.end(Buffer.from([5, 255]));
          const flush = this._timer(entry, () => destroy(socket), 1000);
          socket.once('close', () => this._clearTimer(entry, flush));
          return;
        }
        pending = pending.subarray(size);
        socket.write(Buffer.from([5, 0]));
        state = 'request';
      }
      if (state !== 'request' || pending.length < 4) return;
      if (pending[0] !== 5 || pending[2] !== 0) { reject(1); return; }
      if (pending[1] !== 1) { reject(7); return; }
      let destination;
      let size;
      switch (pending[3]) {
        case 1:
          size = 10;
          if (pending.length < size) return;
          destination = [...pending.subarray(4, 8)].join('.');
          break;
        case 3:
          if (pending.length < 5) return;
          if (!pending[4]) { reject(8); return; }
          size = 7 + pending[4];
          if (pending.length < size) return;
          destination = pending.subarray(5, size - 2).toString('ascii');
          if (!/^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$/.test(destination)) { reject(8); return; }
          break;
        case 4:
          size = 22;
          if (pending.length < size) return;
          destination = Array.from({ length: 8 }, (_, index) => pending.readUInt16BE(4 + index * 2).toString(16)).join(':');
          break;
        default: reject(8); return;
      }
      const destinationPort = pending.readUInt16BE(size - 2);
      if (!destinationPort) { reject(1); return; }
      const initial = pending.subarray(size);
      pending = Buffer.alloc(0);
      socket.pause();
      finish();
      this._forward(entry, socket, { host: destination, port: destinationPort }, channel => {
        socket.write(Buffer.from([5, 0, 0, 1, 0, 0, 0, 0, 0, 0]));
        this._bridge(socket, channel, initial);
      }, error => reject(error.code === 'ECONNREFUSED' ? 5 : error.code === 'ENOTFOUND' ? 4 : 1));
    };
    socket.on('data', data);
    socket.resume();
  }

  async stop(id) {
    const entry = this.entries.get(id);
    if (!entry) return null;
    entry.closed = true;
    entry.status = 'stopped';
    this.entries.delete(id);
    if (entry.cancelStart) { entry.cancelStart(); entry.cancelStart = null; }
    for (const timer of entry.timers) clearTimeout(timer);
    entry.timers.clear();
    for (const resource of [...entry.resources]) destroy(resource);
    if (entry.server) {
      try { entry.server.close(); } catch (_) { /* Not listening yet. */ }
    }
    this._detachClient(entry);
    if (entry.config.type === 'remote' && entry.config.listen_port && typeof entry.client.unforwardIn === 'function') {
      await new Promise(resolve => {
        const timer = setTimeout(resolve, 2000);
        try { entry.client.unforwardIn(entry.config.bind_host, entry.config.listen_port, () => { clearTimeout(timer); resolve(); }); }
        catch (_) { clearTimeout(timer); resolve(); }
      });
    }
    this._publish(entry);
    return this._descriptor(entry);
  }

  async sessionClosed(terminalId) {
    await Promise.all([...this.entries.values()].filter(entry => entry.config.terminalId === terminalId)
      .map(entry => this.stop(entry.config.id)));
  }

  async dispose() {
    this.disposed = true;
    await Promise.all([...this.entries.keys()].map(id => this.stop(id)));
  }
}

module.exports = { TunnelManager, validateTunnel };
