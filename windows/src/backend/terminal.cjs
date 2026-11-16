'use strict';

const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');
const iconv = require('iconv-lite');
const { Client } = require('ssh2');
const { initializationCommand } = require('./powershell-init.cjs');

function dimensions(value = {}) {
  return { cols: Math.max(2, Math.min(500, Math.round(Number(value.cols) || 100))), rows: Math.max(2, Math.min(300, Math.round(Number(value.rows) || 30))) };
}

function parseJumpHosts(value, defaults) {
  if (!value) return [];
  if (typeof value !== 'string' || value.length > 2048) throw new Error('Некорректная цепочка SSH-шлюзов.');
  const entries = value.split(',').map(part => part.trim());
  if (entries.length > 5 || entries.some(part => !part)) throw new Error('Допускается не более пяти SSH-шлюзов.');
  return entries.map(entry => {
    const match = /^(?:([^\s@,:/\\]+)@)?(\[[0-9a-fA-F:.]+\]|[^\s@,:/\\]+)(?::([0-9]{1,5}))?$/.exec(entry);
    if (!match) throw new Error('Шлюз задаётся как пользователь@сервер:порт.');
    const port = Number(match[3] || 22);
    if (port < 1 || port > 65535) throw new Error('Некорректный порт SSH-шлюза.');
    return { ...defaults, credential_profile: '', credential_label: '', host: match[2].replace(/^\[|\]$/g, ''), username: match[1] || defaults.username, port, jump_host: '', remote_command: '' };
  });
}

function safeError(error) {
  const message = error && error.message ? String(error.message) : 'Соединение завершилось с ошибкой.';
  return message.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '').slice(0, 500);
}

function localDirectory(value) {
  if (!value) return os.homedir();
  if (typeof value !== 'string' || value.includes('\0')) throw new Error('Некорректный начальный каталог терминала.');
  const requested = value === '~' ? os.homedir() : /^~[\\/]/.test(value) ? path.join(os.homedir(), value.slice(2)) : path.resolve(value);
  try { if (fs.statSync(requested).isDirectory()) return requested; } catch {}
  throw new Error('Начальный каталог терминала не существует или недоступен.');
}

class TerminalManager {
  constructor({ emit = () => {}, prompt, identityManager, trustStore, credentialProvider, pty } = {}) {
    this.emit = emit;
    this.prompt = prompt;
    this.identityManager = identityManager;
    this.trustStore = trustStore;
    this.credentialProvider = credentialProvider;
    this.pty = pty;
    this.terminals = new Map();
    this.cleanupWaits = new Set();
    this.closed = false;
  }

  get(id) { return this.terminals.get(id); }
  list() { return [...this.terminals.values()].map(record => this._snapshot(record)); }
  _snapshot(record) { return { id: record.id, profile: { ...record.profile }, title: record.title, status: record.status, generation: record.generation }; }
  _current(record, generation) { return !this.closed && this.terminals.get(record.id) === record && record.generation === generation && !record.closing; }
  _status(record, status, extra = {}) { record.status = status; this.emit('terminal:status', { id: record.id, status, generation: record.generation, ...extra }); }

  async create(profile, size = {}) {
    if (this.closed) throw new Error('Приложение закрывается.');
    const protocol = String(profile.protocol || profile.type || 'local').toLowerCase();
    if (!['local', 'ssh'].includes(protocol)) throw new Error('Во встроенном терминале Windows доступны Local и SSH.');
    const record = { id: crypto.randomUUID(), profile: { ...profile, protocol }, title: profile.name || (protocol === 'ssh' ? `${profile.username || ''}@${profile.host}` : 'PowerShell'), status: 'connecting', generation: 1, dimensions: dimensions(size), clients: [], closing: false, sshClient: null, channel: null, process: null };
    this.terminals.set(record.id, record);
    const generation = record.generation;
    this.emit('terminal:created', this._snapshot(record));
    this._status(record, 'connecting');
    this.emit('terminal:title', { id: record.id, title: record.title });
    try {
      await this._start(record);
      return this._snapshot(record);
    } catch (error) {
      if (this._current(record, generation)) { this._status(record, 'error', { message: safeError(error) }); this._cleanup(record); }
      throw new Error(safeError(error));
    }
  }

  async _start(record) {
    if (record.profile.protocol === 'local') return this._startLocal(record);
    return this._startSSH(record);
  }

  _startLocal(record) {
    const generation = record.generation;
    const pty = this.pty || require('node-pty');
    const root = process.env.SystemRoot || 'C:\\Windows';
    const pwsh = process.env.ProgramFiles ? path.join(process.env.ProgramFiles, 'PowerShell', '7', 'pwsh.exe') : '';
    const shell = pwsh && fs.existsSync(pwsh) ? pwsh : path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    const env = { ...process.env, TERM: 'xterm-256color', COLORTERM: 'truecolor' };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = pty.spawn(shell, ['-NoLogo', '-NoExit', '-EncodedCommand', Buffer.from(initializationCommand, 'utf16le').toString('base64')], { name: 'xterm-256color', cwd: localDirectory(record.profile.startup_directory), cols: record.dimensions.cols, rows: record.dimensions.rows, env, useConpty: true, useConptyDll: true, conptyInheritCursor: false });
    record.process = child;
    record.subscriptions = [child.onData(data => { if (this._current(record, generation)) this.emit('terminal:data', { id: record.id, generation, data }); }), child.onExit(({ exitCode, signal }) => {
      if (!this._current(record, generation) || record.status !== 'running') return;
      this._status(record, 'exited');
      this._cleanup(record);
      this.emit('terminal:exit', { id: record.id, generation, exitCode, signal });
    })];
    this._status(record, 'running');
  }

  async _startSSH(record) {
    if (!this.identityManager || !this.trustStore) throw new Error('SSH-профили и проверка ключей сервера не настроены.');
    const generation = record.generation;
    const profile = record.profile;
    const encoding = profile.encoding || 'utf-8';
    if (!iconv.encodingExists(encoding)) throw new Error('Кодировка терминала не поддерживается.');
    const endpoints = parseJumpHosts(profile.jump_host, profile).concat({ ...profile, jump_host: '' });
    let previous;
    for (const endpoint of endpoints) {
      if (!this._current(record, generation)) throw new Error('Подключение отменено.');
      const passwordOnly = endpoint.credential_profile && !endpoint.key_file && (!endpoint.identity_profile || endpoint.identity_profile==='system');
      const identity = passwordOnly ? {} : await this.identityManager.ensure(endpoint);
      if (!this._current(record, generation)) throw new Error('Подключение отменено.');
      let sock;
      if (previous) {
        sock = await new Promise((resolve, reject) => previous.forwardOut('127.0.0.1', 0, endpoint.host, Number(endpoint.port || 22), (error, stream) => error ? reject(error) : resolve(stream)));
      }
      const client = await this._connectSSH(record, generation, endpoint, identity, sock);
      previous = client;
    }
    if (!this._current(record, generation)) throw new Error('Подключение отменено.');
    record.sshClient = previous;
    const channel = await new Promise((resolve, reject) => previous.shell({ term: 'xterm-256color', cols: record.dimensions.cols, rows: record.dimensions.rows, width: 0, height: 0 }, (error, stream) => error ? reject(error) : resolve(stream)));
    if (!this._current(record, generation)) { channel.close(); throw new Error('Подключение отменено.'); }
    record.channel = channel;
    record.decoder = iconv.getDecoder(encoding);
    const receive = data => {
      if (!this._current(record, generation)) return;
      const text = record.decoder.write(data);
      if (text) this.emit('terminal:data', { id: record.id, generation, data: text });
    };
    channel.on('data', receive);
    channel.stderr.on('data', receive);
    channel.on('close', (exitCode, signal) => {
      if (!this._current(record, generation) || !['running', 'connecting'].includes(record.status)) return;
      const tail = record.decoder.end();
      if (tail) this.emit('terminal:data', { id: record.id, generation, data: tail });
      this._status(record, 'exited');
      this._cleanup(record);
      this.emit('terminal:exit', { id: record.id, generation, exitCode: exitCode === undefined ? null : exitCode, signal });
    });
    channel.on('error', error => { if (this._current(record, generation)) this._status(record, 'error', { message: safeError(error) }); });
    this._status(record, 'running');
    if (profile.remote_command) this.write(record.id, profile.remote_command + '\r');
  }

  _connectSSH(record, generation, endpoint, identity, sock) {
    return new Promise((resolve, reject) => {
      const client = new Client();
      record.clients.push(client);
      let ready = false;
      let trustError;
      const tried = new Set();
      const authHandler = (methods, partial, callback) => {
        if (!this._current(record, generation)) return callback(false);
        const allowed = methods || ['none', 'publickey', 'password', 'keyboard-interactive'];
        if (!tried.has('none')) { tried.add('none'); return callback({ type: 'none', username: endpoint.username }); }
        if (allowed.includes('publickey') && !tried.has('key') && (identity.privateKey || identity.agent)) {
          tried.add('key');
          return callback(identity.privateKey ? { type: 'publickey', username: endpoint.username, key: identity.privateKey } : { type: 'agent', username: endpoint.username, agent: identity.agent });
        }
        // A selected identity is never silently replaced by unrelated agent keys.
        if (allowed.includes('password') && !tried.has('password') && (this.prompt || this.credentialProvider)) {
          tried.add('password');
          Promise.resolve().then(() => endpoint.credential_profile ? this.credentialProvider(endpoint) :
            this.prompt({ kind: 'password', title: 'Пароль SSH', host: endpoint.host, secret: true, message: `Пароль ${endpoint.username}@${endpoint.host}` })).then(password => {
            if (!this._current(record, generation) || password === null || password === undefined) return callback(false);
            callback({ type: 'password', username: endpoint.username, password: String(password) });
          }).catch(() => callback(false));
          return;
        }
        if (allowed.includes('keyboard-interactive') && !tried.has('keyboard') && this.prompt) {
          tried.add('keyboard');
          return callback({ type: 'keyboard-interactive', username: endpoint.username, prompt: async (name, instructions, language, prompts, finish) => {
            try {
              const answers = [];
              for (const item of prompts) {
                const savedPassword = endpoint.credential_profile && !item.echo && /password/i.test(item.prompt) && !/otp|token|code|passphrase|one.?time|verification|challenge|factor|\bpin\b/i.test(item.prompt) && !tried.has('saved-keyboard');
                if (savedPassword) tried.add('saved-keyboard');
                const answer = savedPassword ? await this.credentialProvider(endpoint) : await this.prompt({ kind: 'password', title: name || 'Аутентификация SSH', host: endpoint.host, secret: !item.echo, message: String(item.prompt || instructions || 'Ответ сервера').slice(0, 1000) });
                if (answer === null || answer === undefined || !this._current(record, generation)) return finish([]);
                answers.push(String(answer));
              }
              finish(answers);
            } catch { finish([]); }
          } });
        }
        callback(false);
      };
      client.once('ready', () => { ready = true; resolve(client); });
      client.on('error', error => {
        if(error.level==='client-authentication'){
          const named=endpoint.identity_profile&&endpoint.identity_profile!=='system';
          const source=named?'выбранный профиль SSH-ключа':endpoint.key_file?'файл '+path.basename(endpoint.key_file):identity.agent?'SSH-agent':'пароль';
          error=new Error(`Сервер отклонил аутентификацию ${endpoint.username}@${endpoint.host}. Использовались: ${source}. Проверьте пользователя и выбранный ключ либо пароль.`);
        }
        if (!ready) return reject(trustError || error);
        if (this._current(record, generation) && record.status === 'running') this._status(record, 'error', { message: safeError(error) });
      });
      client.on('close', () => {
        if (!ready) return reject(trustError || new Error('SSH-сервер закрыл соединение.'));
        if (!this._current(record, generation) || !['running', 'connecting'].includes(record.status)) return;
        this._status(record, 'exited');
        this._cleanup(record);
        this.emit('terminal:exit', { id: record.id, generation, exitCode: null });
      });
      try {
        client.connect({ host: endpoint.host, port: Number(endpoint.port || 22), username: endpoint.username, ...(sock ? { sock } : {}), readyTimeout: 120000, keepaliveInterval: 20000, keepaliveCountMax: 3, authHandler,
          algorithms: { compress: endpoint.compression ? ['zlib@openssh.com', 'zlib', 'none'] : ['none'] },
          hostVerifier: (key, callback) => {
            Promise.resolve(this.trustStore.verify({ host: endpoint.host, port: Number(endpoint.port || 22), key })).then(accepted => callback(this._current(record, generation) && accepted === true)).catch(error => { trustError = error; callback(false); });
          }
        });
      } catch (error) { reject(error); }
    });
  }

  write(id, text) {
    const record = this.terminals.get(id);
    if (!record || record.status !== 'running' || typeof text !== 'string' || text.length > 1024 * 1024) return false;
    if (record.process) record.process.write(text);
    else if (record.channel && !record.channel.destroyed) record.channel.write(iconv.encode(text, record.profile.encoding || 'utf-8'));
    else return false;
    return true;
  }

  writeBinary(id, text) {
    const record = this.terminals.get(id);
    if (!record || record.status !== 'running' || typeof text !== 'string' || text.length > 1024 * 1024 || /[^\x00-\xff]/.test(text)) return false;
    const data = Buffer.from(text, 'latin1');
    if (record.process) record.process.write(data);
    else if (record.channel && !record.channel.destroyed) record.channel.write(data);
    else return false;
    return true;
  }

  resize(id, size) {
    const record = this.terminals.get(id);
    if (!record) return false;
    record.dimensions = dimensions(size);
    try {
      if (record.process) record.process.resize(record.dimensions.cols, record.dimensions.rows);
      if (record.channel && !record.channel.destroyed) record.channel.setWindow(record.dimensions.rows, record.dimensions.cols, 0, 0);
    } catch { return false; }
    return true;
  }

  _cleanup(record) {
    if (record.subscriptions) for (const subscription of record.subscriptions) subscription.dispose();
    record.subscriptions = [];
    if (record.process) {
      const process = record.process;
      record.process = null;
      // ConPTY's output worker drains for one second after dispose. Electron must
      // not tear down its V8/worker environment while that owned thread is alive.
      const worker=process._agent?._conoutSocketWorker?._worker;
      if(worker && worker.threadId!==-1) {
        const stopped=new Promise(resolve=>{
          const timer=setTimeout(()=>{Promise.resolve(worker.terminate()).then(resolve,resolve);},3000);timer.unref();
          worker.once('exit',()=>{clearTimeout(timer);resolve();});
        });
        this.cleanupWaits.add(stopped);void stopped.then(()=>this.cleanupWaits.delete(stopped));
      }
      if (record.status !== 'exited') { try { process.kill(); } catch {} }
      // node-pty 1.1.0's ConPTY DLL branch only disposes this worker on a later
      // data event. A naturally exited shell may produce none, keeping the app
      // alive forever. Release the owned worker after the normal PTY close;
      // this uses no stale PID and preserves node-pty's final-data drain.
      try { process._agent?._conoutSocketWorker?.dispose(); } catch {}
    }
    const channel = record.channel;
    record.channel = null;
    if (channel) { try { channel.close(); } catch {} }
    const clients = record.clients.splice(0).reverse();
    record.sshClient = null;
    for (const client of clients) { try { client.end(); client.destroy(); } catch {} }
  }

  close(id) {
    const record = this.terminals.get(id);
    if (!record) return false;
    record.closing = true;
    this._cleanup(record);
    this.terminals.delete(id);
    this.emit('terminal:status', { id, status: 'closed', generation: record.generation });
    return true;
  }

  async reconnect(id, profile) {
    const record = this.terminals.get(id);
    if (!record) throw new Error('Терминал закрыт.');
    record.generation++;
    if(profile){record.profile={...profile};record.title=profile.name;this.emit('terminal:profile',{id,profile:{...profile},generation:record.generation});}
    const generation = record.generation;
    this._cleanup(record);
    record.closing = false;
    this.emit('terminal:selection-reset', { id, generation: record.generation });
    this._status(record, 'connecting', { clearSelection: true });
    try { await this._start(record); return this._snapshot(record); }
    catch (error) { if (this._current(record, generation)) { this._status(record, 'error', { message: safeError(error) }); this._cleanup(record); } throw new Error(safeError(error)); }
  }

  dispose() { this.closed = true; for (const id of this.terminals.keys()) this.close(id); return Promise.all([...this.cleanupWaits]); }
}

module.exports = { TerminalManager, dimensions, parseJumpHosts, safeError, localDirectory };
