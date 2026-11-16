'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { Worker, isMainThread, parentPort, workerData } = require('node:worker_threads');
const { utils, OpenSSHAgent, BaseAgent } = require('ssh2');
const { fingerprint } = require('./host-trust.cjs');
const { isMobaKeyPath, resolveMobaDocumentsKey, MOBA_KEY_PATH_WARNING } = require('./moba-key-path.cjs');

if (!isMainThread && workerData?.astraKeygen) {
  try {
    const { algorithm, filename, passphrase } = workerData;
    const options = { comment: filename, ...(algorithm === 'rsa' ? { bits: 3072 } : {}) };
    if (passphrase) Object.assign(options, { passphrase, cipher: 'aes256-ctr', rounds: 32 });
    const keys = utils.generateKeyPairSync(algorithm, options);
    parentPort.postMessage(keys);
  } catch {
    parentPort.postMessage(null);
  }
} else if (!isMainThread && workerData && workerData.astraIdentityWorker) {
  let raw;
  try {
    raw = fs.readFileSync(workerData.filename);
    const key = utils.parseKey(raw, workerData.passphrase || undefined);
    if (key instanceof Error) {
      const encrypted = /encrypted|passphrase|decrypt|bad password/i.test(key.message);
      parentPort.postMessage({ ok: false, encrypted });
    } else {
      const selected = Array.isArray(key) ? key[0] : key;
      parentPort.postMessage({ ok: true, type: selected.type, pem: selected.getPrivatePEM(), public: selected.getPublicSSH(), fingerprint: fingerprint(selected.getPublicSSH()) });
    }
  } catch { parentPort.postMessage({ ok: false, encrypted: false }); }
  finally { if (raw) raw.fill(0); }
} else {

function safeFilename(value, documents) {
  if (typeof value !== 'string' || !value || value.includes('\0')) throw new Error('Выберите файл приватного SSH-ключа.');
  if (isMobaKeyPath(value)) {
    value = resolveMobaDocumentsKey(value, documents);
    if (!value) throw new Error(MOBA_KEY_PATH_WARNING);
  }
  const filename = value.startsWith('~/') || value.startsWith('~\\') ? path.join(os.homedir(), value.slice(2)) : path.resolve(value);
  let stat;
  try { stat = fs.statSync(filename); }
  catch (error) {
    if (['ENOENT', 'ENOTDIR'].includes(error.code)) throw new Error(`Не найден файл SSH-ключа ${filename}. Укажите его путь в настройках подключения или профиля.`);
    throw error;
  }
  if (!stat.isFile() || stat.size > 2 * 1024 * 1024 || !stat.size) throw new Error('Файл SSH-ключа имеет недопустимый размер.');
  return filename;
}

function parseInWorker(filename, passphrase, workers) {
  return new Promise((resolve, reject) => {
    const worker = new Worker(__filename, { workerData: { astraIdentityWorker: true, filename, passphrase } });
    workers.add(worker);
    let complete = false;
    worker.once('message', message => { complete = true; resolve(message); });
    worker.once('error', () => { complete = true; reject(new Error('Не удалось прочитать SSH-ключ.')); });
    worker.once('exit', () => { workers.delete(worker); if (!complete) reject(new Error('Разблокировка SSH-ключа отменена.')); });
  });
}

const execute = promisify(execFile);
function validateIdentity(record) {
  if (!record || typeof record !== 'object') throw new Error('Некорректный SSH-профиль.');
  const id = String(record.id || crypto.randomUUID());
  if (id === 'system' || !/^[a-zA-Z0-9_.-]{1,100}$/.test(id)) throw new Error('Некорректный идентификатор SSH-профиля.');
  const name = String(record.name || '').trim();
  const key_file = String(record.key_file || '').trim();
  if (!name || name.length > 200 || !key_file || key_file.includes('\0')) throw new Error('Укажите название профиля и файл ключа.');
  return { id, name, key_file, fingerprint: typeof record.fingerprint === 'string' ? record.fingerprint : '' };
}

async function protectKeyDirectory(directory) {
  if (process.platform !== 'win32') return;
  const system = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32');
  const options = { windowsHide: true, timeout: 10000 };
  const { stdout } = await execute(path.join(system, 'whoami.exe'), ['/user', '/fo', 'csv', '/nh'], options);
  const sid = stdout.match(/"(S-1-(?:[0-9]+-)*[0-9]+)"/i)?.[1];
  if (!sid) throw new Error('Не удалось определить владельца SSH-ключа.');
  await execute(path.join(system, 'icacls.exe'), [directory, '/inheritance:r', '/grant:r', '*' + sid + ':(OI)(CI)F'], options);
}

class ExactAgent extends BaseAgent {
  constructor(agent, publicKey) { super(); this.agent = agent; this.publicKey = publicKey; }
  getIdentities(callback) {
    this.agent.getIdentities((error, keys) => {
      if (error) return callback(error);
      callback(null, keys.filter(key => key.getPublicSSH().equals(this.publicKey)));
    });
  }
  sign(key, data, options, callback) {
    const parsed = Buffer.isBuffer(key) || typeof key === 'string' ? utils.parseKey(key) : key;
    if (parsed instanceof Error || !parsed.getPublicSSH().equals(this.publicKey)) return callback(new Error('SSH-agent предложил другой ключ.'));
    this.agent.sign(key, data, options, callback);
  }
}

class Ed25519KeyAgent extends BaseAgent {
  constructor(pem, publicKey) {
    super();
    this.pem = pem;
    this.publicKey = publicKey;
  }
  getIdentities(callback) {
    callback(null, [utils.parseKey('ssh-ed25519 ' + this.publicKey.toString('base64'))]);
  }
  sign(key, data, options, callback) {
    const parsed = utils.parseKey(key);
    if (parsed instanceof Error || !parsed.getPublicSSH().equals(this.publicKey))
      return callback(new Error('Выбран другой SSH-ключ.'));
    let signature;
    try { signature = crypto.sign(null, data, this.pem); }
    catch { return callback(new Error('Не удалось подписать запрос ключом Ed25519.')); }
    callback(null, signature);
  }
}

function cachedIdentity(material, identityId) {
  const result = { identityId, unlockScope: 'application' };
  // SSH2 does not reparse its own Ed25519 PKCS#8 export as a private key.
  if (material.type === 'ssh-ed25519') result.agent = new Ed25519KeyAgent(material.pem, material.publicKey);
  else result.privateKey = material.pem;
  return result;
}

class IdentityManager {
  constructor({ directory, prompt, sshDirectory, agentPath, documents } = {}) {
    if (!directory) throw new Error('Не задан каталог SSH-профилей.');
    this.filename = path.join(directory, 'ssh-profiles.json');
    this.sshDirectory = sshDirectory || path.join(os.homedir(), '.ssh');
    this.documents = documents;
    this.prompt = prompt;
    this.agentPath = agentPath || (process.platform === 'win32' ? '\\\\.\\pipe\\openssh-ssh-agent' : process.env.SSH_AUTH_SOCK);
    this.agent = this.agentPath ? new OpenSSHAgent(this.agentPath) : null;
    this.cache = new Map();
    this.pending = new Map();
    this.workers = new Set();
    this.unlocked = new Map();
    this.agentAvailable = false;
    this.revision = 0;
    this.closed = false;
    this.data = { schema: 1, profiles: [], default_id: '', configured: false };
    try {
      const parsed = JSON.parse(fs.readFileSync(this.filename, 'utf8'));
      if (parsed.schema !== 1 || !Array.isArray(parsed.profiles)) throw new Error('format');
      this.data = { schema: 1, profiles: parsed.profiles.map(validateIdentity), default_id: String(parsed.default_id || ''), configured: Boolean(parsed.configured) };
    } catch (error) { if (error.code !== 'ENOENT') throw new Error('Не удалось прочитать профили SSH-ключей.'); }
  }

  _save() {
    fs.mkdirSync(path.dirname(this.filename), { recursive: true, mode: 0o700 });
    const temporary = this.filename + '.' + crypto.randomUUID() + '.tmp';
    try {
      fs.writeFileSync(temporary, JSON.stringify(this.data, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
      fs.renameSync(temporary, this.filename);
    } catch (error) { try { fs.unlinkSync(temporary); } catch {} throw new Error('Не удалось сохранить SSH-профили.'); }
  }

  list() {
    let keys = [];
    try {
      keys = fs.readdirSync(this.sshDirectory, { withFileTypes: true }).filter(entry => entry.isFile() && !/\.pub$|^known_hosts|^authorized_keys|^config$|\.old$|\.json$/i.test(entry.name)).slice(0, 256).map(entry => path.join(this.sshDirectory, entry.name));
    } catch (error) { if (error.code !== 'ENOENT') keys = []; }
    return { ...this.data, profiles: this.data.profiles.map(profile => ({ ...profile })), keys, unlocked: this.data.profiles.filter(profile => this.unlocked.has(profile.id)).map(profile => profile.id), unlocked_scopes: Object.fromEntries(this.unlocked), agent: this.agentAvailable, unlock_scope: 'application' };
  }

  upsert(record, { makeDefault = false, defaultIfUnset = true } = {}) {
    const valid = validateIdentity(record);
    const filename = safeFilename(valid.key_file, this.documents);
    const previous = this.data;
    const profiles = this.data.profiles.filter(profile => profile.id !== valid.id).concat(valid);
    this.data = { ...this.data, profiles, configured: true, default_id: makeDefault || (defaultIfUnset && !this.data.default_id) ? valid.id : this.data.default_id };
    try { this._save(); } catch (error) { this.data = previous; throw error; }
    this.revision++;
    const cached = this.cache.get(valid.id);
    if (cached && cached.filename !== filename) this._clear(valid.id);
    return { ...valid };
  }

  async generate({ name, filename, algorithm, passphrase }) {
    if (typeof filename !== 'string' || !/^id_[A-Za-z0-9_-]{1,75}$/.test(filename) ||
        !['ed25519', 'rsa'].includes(algorithm) || /[\r\n]/.test(filename))
      throw new Error('Укажите тип ключа и имя файла вида id_astraterm.');
    if (typeof passphrase !== 'string' || Buffer.byteLength(passphrase) > 1024 || /[\0\r\n]/.test(passphrase))
      throw new Error('Недопустимая парольная фраза SSH-ключа.');
    if (typeof name !== 'string' || /[\x00-\x1f\x7f]/.test(name))
      throw new Error('Некорректное название SSH-профиля.');
    const destination = path.join(this.sshDirectory, filename);
    const profile = validateIdentity({ name, key_file: destination });
    const linked = [];
    let temporary, saved;
    try {
      if (this.closed) throw new Error('Приложение закрывается.');
      await fs.promises.mkdir(this.sshDirectory, { recursive: true, mode: 0o700 });
      for (const target of [destination, destination + '.pub']) {
        if (await fs.promises.lstat(target).catch(error => {
          if (error.code !== 'ENOENT') throw error;
        })) throw Object.assign(new Error('existing key'), { code: 'EEXIST' });
      }
      temporary = await fs.promises.mkdtemp(path.join(this.sshDirectory, '.astraterm-key-'));
      await protectKeyDirectory(temporary);
      const keys = await new Promise((resolve, reject) => {
        const worker = new Worker(__filename, { workerData: { astraKeygen: true, algorithm, filename, passphrase } });
        this.workers.add(worker);
        worker.once('message', resolve);
        worker.once('error', reject);
        worker.once('exit', () => {
          this.workers.delete(worker);
          reject(new Error('Генерация SSH-ключа завершена.'));
        });
      });
      if (!keys || this.closed) throw new Error('Генерация SSH-ключа прервана.');
      const parsed = utils.parseKey(keys.public);
      if (parsed instanceof Error) throw parsed;
      const privateFile = path.join(temporary, 'key');
      await fs.promises.writeFile(privateFile, keys.private, { flag: 'wx', mode: 0o600 });
      await fs.promises.writeFile(privateFile + '.pub', keys.public + '\n', { flag: 'wx', mode: 0o644 });
      for (const suffix of ['', '.pub']) {
        await fs.promises.link(privateFile + suffix, destination + suffix);
        linked.push(destination + suffix);
      }
      if (this.closed) throw new Error('Приложение закрывается.');
      saved = this.upsert({ ...profile, fingerprint: fingerprint(parsed.getPublicSSH()) }, { defaultIfUnset: false });
      return saved;
    } catch (error) {
      if (error.code === 'EEXIST') throw new Error('Такой ключ или файл .pub уже существует. Выберите другое имя файла.');
      throw new Error('Не удалось создать SSH-ключ в ~/.ssh.');
    } finally {
      if (!saved) for (const target of linked.reverse()) await fs.promises.unlink(target).catch(() => {});
      if (temporary) await fs.promises.rm(temporary, { recursive: true, force: true }).catch(() => {});
    }
  }

  publicKey(identifier) {
    const profile = this.data.profiles.find(record => record.id === identifier);
    if (!profile) throw new Error('SSH-профиль не найден.');
    const publicFile = safeFilename(profile.key_file + '.pub', this.documents);
    const text = fs.readFileSync(publicFile, 'utf8').trim();
    if (!/^(?:ssh-|ecdsa-|sk-)\S+ [A-Za-z0-9+/=]+(?: [^\r\n]*)?$/.test(text) || utils.parseKey(text) instanceof Error)
      throw new Error('Некорректный файл публичного SSH-ключа.');
    return text;
  }

  remove(id) {
    const previous = this.data;
    this.data = { ...this.data, profiles: this.data.profiles.filter(profile => profile.id !== id), default_id: this.data.default_id === id ? '' : this.data.default_id };
    try { this._save(); } catch (error) { this.data = previous; throw error; }
    this.revision++;
    this._clear(id);
    return this.list();
  }

  setDefault(id) {
    if (id && id !== 'system' && !this.data.profiles.some(profile => profile.id === id)) throw new Error('SSH-профиль не найден.');
    const previous = this.data;
    this.data = { ...this.data, default_id: id || '', configured: true };
    try { this._save(); } catch (error) { this.data = previous; throw error; }
    this.revision++;
    return this.list();
  }

  async _identities() {
    if (!this.agent) return [];
    return new Promise(resolve => {
      let finished = false;
      const timer = setTimeout(() => { if (!finished) { finished = true; resolve([]); } }, 1800);
      this.agent.getIdentities((error, keys) => { if (finished) return; finished = true; clearTimeout(timer); this.agentAvailable = !error; resolve(error ? [] : keys); });
    });
  }

  async _agentFor(publicKey) {
    if ((await this._identities()).some(key => key.getPublicSSH().equals(publicKey))) return new ExactAgent(this.agent, publicKey);
    return null;
  }

  async ensure(profile = {}) {
    if (this.closed) throw new Error('Приложение закрывается.');
    const explicit = profile.identity_profile || '';
    const requested = explicit && explicit !== 'system' ? explicit : profile.key_file ? '' : explicit === 'system' ? 'system' : this.data.default_id || 'system';
    if (requested === 'system') {
      const keys = await this._identities();
      return keys.length ? { agent: this.agent, identityId: 'system', unlockScope: 'windows-agent' } : { identityId: 'system', unlockScope: 'password' };
    }
    const record = requested ? this.data.profiles.find(candidate => candidate.id === requested) : null;
    if (requested && !record) throw new Error('Выбранный SSH-профиль недоступен. Выберите существующий профиль в настройках подключения.');
    const filename = safeFilename(record ? record.key_file : profile.key_file, this.documents);
    const identityId = record ? record.id : 'file:' + filename;
    const stat = fs.statSync(filename);
    const signature = `${stat.size}:${stat.mtimeMs}`;
    if(!this.cache.has(identityId)){
      const alias=[...this.cache.values()].find(item=>item.filename===filename&&item.signature===signature);
      if(alias){this.cache.set(identityId,{...alias,pem:Buffer.from(alias.pem),publicKey:Buffer.from(alias.publicKey)});this.unlocked.set(identityId,'application');}
    }
    const cached = this.cache.get(identityId);
    if (cached && cached.filename === filename && cached.signature === signature) return cachedIdentity(cached, identityId);
    this._clear(identityId);
    if (this.pending.has(identityId)) return this.pending.get(identityId);
    const operation = this._unlock(filename, identityId, record, signature, this.revision);
    this.pending.set(identityId, operation);
    try { return await operation; } finally { this.pending.delete(identityId); }
  }

  _checkUnlock(filename, signature, revision, material) {
    const stat = fs.statSync(filename);
    if (this.closed || revision !== this.revision || signature !== `${stat.size}:${stat.mtimeMs}`) {
      if (material && material.fill) material.fill(0);
      throw new Error('SSH-профиль изменён во время разблокировки. Повторите подключение.');
    }
  }

  async _unlock(filename, identityId, record, signature, revision) {
    // A companion public key lets an already-loaded Windows agent sign without a prompt.
    try {
      const parsed = utils.parseKey(fs.readFileSync(filename + '.pub'));
      if (!(parsed instanceof Error)) {
        const agent = await this._agentFor(parsed.getPublicSSH());
        if (agent) { this._checkUnlock(filename, signature, revision); this.unlocked.set(identityId, 'windows-agent'); return { agent, identityId, unlockScope: 'windows-agent' }; }
      }
    } catch {}
    let result = await parseInWorker(filename, undefined, this.workers);
    if (!result.ok && result.encrypted) {
      if (!this.prompt) throw new Error('Для SSH-ключа требуется passphrase.');
      let passphrase = await this.prompt({ kind: 'passphrase', title: 'Разблокировать SSH-ключ', profileId: identityId, secret: true, message: `Введите passphrase ключа ${record ? record.name : path.basename(filename)}. Если ключ отсутствует в Windows ssh-agent, разблокировка действует до закрытия AstraTerm.` });
      if (passphrase === null || passphrase === undefined) throw new Error('Разблокировка SSH-ключа отменена.');
      result = await parseInWorker(filename, String(passphrase), this.workers);
      passphrase = undefined;
    }
    if (this.closed) { if (result.pem && result.pem.fill) result.pem.fill(0); throw new Error('Разблокировка SSH-ключа отменена.'); }
    if (!result.ok) throw new Error(result.encrypted ? 'Не удалось разблокировать SSH-ключ: проверьте passphrase.' : 'Не удалось прочитать SSH-ключ: формат не поддерживается или файл повреждён.');
    const pem = Buffer.from(result.pem);
    const publicKey = Buffer.from(result.public);
    const agent = await this._agentFor(publicKey);
    this._checkUnlock(filename, signature, revision, pem);
    if (agent) { pem.fill(0); this.unlocked.set(identityId, 'windows-agent'); return { agent, identityId, unlockScope: 'windows-agent' }; }
    const material = { pem, publicKey, filename, signature, type: result.type };
    this.cache.set(identityId, material);
    this.unlocked.set(identityId, 'application');
    return { ...cachedIdentity(material, identityId), fingerprint: result.fingerprint };
  }

  _clear(id) { const cached = this.cache.get(id); if (cached) { cached.pem.fill(0); this.cache.delete(id); } this.unlocked.delete(id); }
  dispose() { this.closed = true; for (const id of this.cache.keys()) this._clear(id); this.unlocked.clear(); for (const worker of this.workers) worker.terminate(); this.workers.clear(); }
}

module.exports = { IdentityManager, ExactAgent, safeFilename, protectKeyDirectory, validateIdentity };
}
