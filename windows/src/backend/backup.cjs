'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { promisify, TextDecoder } = require('node:util');
const { BACKUP_PARAMETERS: P, VAULT_PARAMETERS: V } = require('../shared.cjs');
const { normalizeDocument } = require('./store.cjs');
const { safeFilename, protectKeyDirectory, validateIdentity } = require('./identities.cjs');

const DOCUMENTS = ['sessions.json', 'ssh-profiles.json', 'credentials.vault.json',
  'sidebar.json', 'logging.json', 'known-hosts.json'];
const MARKER = 'active-profile.json';
const PROFILES = 'restored-profiles';
const MAX_DOCUMENT = 8 * 1024 * 1024;
const MAX_PAYLOAD = P.maximum_payload_bytes;
const MAX_ARCHIVE = Math.floor(MAX_PAYLOAD * 4 / 3) + 1024;
const MAX_KEY = P.maximum_key_bytes;
const HEADER = { format: 'AstraTerm backup', version: P.version, iterations: P.iterations };
const AAD = Buffer.from('AstraTerm profile backup v1');
const KEY_REF = 'astraterm-backup-key:';
const KEY_PARTS = ['private', 'public', 'certificate'];
const KEY_SUFFIXES = ['', '.pub', '-cert.pub'];
const KEY_HEADER = /^(?:-----BEGIN (?:OPENSSH |RSA |DSA |EC |ENCRYPTED )?PRIVATE KEY-----|PuTTY-User-Key-File-[23]:)/;
const INVALID = 'Формат резервной копии повреждён или не поддерживается.';
const decoder = new TextDecoder('utf-8', { fatal: true });
const pbkdf2 = promisify(crypto.pbkdf2);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const jsonBytes = value => Buffer.from(JSON.stringify(value));

function read(filename, limit, optional = false) {
  let fd;
  try { fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NONBLOCK ?? 0)); }
  catch (error) { if (optional && error.code === 'ENOENT') return null; throw error; }
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size > limit) throw Error('Недопустимый тип или размер файла резервной копии.');
    const buffer = Buffer.allocUnsafe(info.size + 1);
    let offset = 0;
    while (offset < buffer.length) {
      const count = fs.readSync(fd, buffer, offset, buffer.length - offset, null);
      if (!count) break;
      offset += count;
    }
    if (offset !== info.size) throw Error('Файл изменился во время чтения.');
    return buffer.subarray(0, offset);
  } finally { fs.closeSync(fd); }
}

function writeNew(filename, bytes) {
  const fd = fs.openSync(filename, 'wx', 0o600);
  try {
    try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
  } catch (error) {
    fs.rmSync(filename, { force: true });
    throw error;
  }
}

function decode(value, limit, exact) {
  if (typeof value !== 'string' || value.length > Math.ceil(limit / 3) * 4) throw Error(INVALID);
  const data = Buffer.from(value, 'base64');
  if (data.length > limit || (exact !== undefined && data.length !== exact) ||
      data.toString('base64') !== value) throw Error(INVALID);
  return data;
}

function checkPassword(password, fresh = false) {
  if (typeof password !== 'string' || !password || password.includes('\0') ||
      Array.from(password).length > 4096 || !password.isWellFormed()) throw Error('Введите пароль резервной копии.');
  if (fresh && Array.from(password).length < 10) throw Error('Пароль резервной копии должен содержать не менее 10 символов.');
}

function activeProfile(root) {
  root = path.resolve(root);
  const raw = read(path.join(root, MARKER), 1024, true);
  if (raw === null) return root;
  try {
    const { directory: id } = JSON.parse(decoder.decode(raw));
    if (typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)) throw Error();
    const parent = path.join(root, PROFILES), directory = path.join(parent, id);
    if (!fs.lstatSync(parent).isDirectory() || !fs.lstatSync(directory).isDirectory()) throw Error();
    return directory;
  } catch { throw Error('Не удалось открыть восстановленный профиль AstraTerm.'); }
}

function holders(documents) {
  const database = documents['sessions.json'];
  return [...database.sessions, ...database.tunnels, ...database.groups.map(group => group.ssh),
    ...documents['ssh-profiles.json'].profiles];
}

function normalizeDocuments(documents) {
  if (!object(documents) || Object.entries(documents).some(([name, data]) => !DOCUMENTS.includes(name) || !object(data))) {
    throw Error(INVALID);
  }
  const checked = { ...documents, 'sessions.json': normalizeDocument(documents['sessions.json'] ?? {}) };
  const identities = documents['ssh-profiles.json'] ?? {};
  const { profiles = [], default_id = '', configured = false } = identities;
  if ((identities.schema ?? 1) !== 1 || (identities.schema_version ?? 1) !== 1 ||
      !Array.isArray(profiles) || profiles.length > 1000 || typeof configured !== 'boolean' ||
      typeof default_id !== 'string') throw Error(INVALID);
  const records = profiles.map(record => validateIdentity(record));
  const ids = new Set(records.map(item => item.id));
  if (ids.size !== records.length || (default_id && !ids.has(default_id))) throw Error(INVALID);
  checked['ssh-profiles.json'] = { schema: 1, schema_version: 1, profiles: records, default_id, configured };
  const vault = checked['credentials.vault.json'];
  if (vault) {
    for (const name of ['version', 'cipher', 'kdf', 'iterations']) {
      if (vault[name] !== V[name]) throw Error('Формат хранилища учётных данных не поддерживается.');
    }
    for (const name of ['salt', 'nonce', 'tag']) {
      const length = V[name + '_bytes'];
      decode(vault[name], length, length);
    }
    if (!decode(vault.ciphertext, MAX_DOCUMENT).length) throw Error(INVALID);
    checked['credentials.vault.json'] = Object.fromEntries(
      ['version', 'cipher', 'kdf', 'iterations', 'salt', 'nonce', 'tag', 'ciphertext'].map(name => [name, vault[name]]));
  }
  const sidebar = checked['sidebar.json'];
  if (sidebar) {
    if (!Array.isArray(sidebar.collapsed)) throw Error(INVALID);
    const groups = new Set(checked['sessions.json'].groups.map(group => group.id));
    checked['sidebar.json'] = { collapsed: sidebar.collapsed.filter(id => groups.has(id)) };
  }
  const log = checked['logging.json'];
  if (log) {
    if (typeof log.enabled !== 'boolean' || !['error', 'warning', 'info', 'debug'].includes(log.level)) throw Error(INVALID);
    checked['logging.json'] = { enabled: log.enabled, level: log.level };
  }
  const trust = checked['known-hosts.json'];
  if (trust && (trust.schema !== 1 || !object(trust.hosts))) throw Error(INVALID);
  return checked;
}

function keyId(parts) {
  return crypto.createHash('sha256').update(KEY_PARTS.map(name => parts[name]).join('\n'), 'ascii').digest('hex');
}

function captureProfile(directory, documentsDirectory) {
  const stored = {};
  for (const name of DOCUMENTS) {
    const raw = read(path.join(directory, name), MAX_DOCUMENT, true);
    if (raw !== null) stored[name] = JSON.parse(decoder.decode(raw));
  }
  const documents = normalizeDocuments(stored);
  const keys = {}, byPath = new Map(), missing = [];
  let size = jsonBytes(documents).length;
  for (const record of holders(documents)) {
    const filename = record.key_file;
    if (!filename) continue;
    if (byPath.has(filename)) { record.key_file = byPath.get(filename); continue; }
    let parts;
    try {
      const local = fs.realpathSync(safeFilename(filename, documentsDirectory));
      const data = read(local, MAX_KEY);
      if (!KEY_HEADER.test(data.subarray(0, 128).toString('ascii'))) throw Error();
      parts = { private: data.toString('base64'), public: read(local + '.pub', MAX_KEY, true)?.toString('base64') ?? '',
        certificate: read(local + '-cert.pub', MAX_KEY, true)?.toString('base64') ?? '' };
    } catch {
      missing.push(filename);
      byPath.set(filename, filename);
      continue;
    }
    const id = keyId(parts);
    if (!Object.hasOwn(keys, id)) {
      size += KEY_PARTS.reduce((total, name) => total + parts[name].length, 0);
      if (Object.keys(keys).length >= P.maximum_keys || size > MAX_PAYLOAD) {
        throw Error('Резервная копия превышает допустимый размер или число SSH-ключей.');
      }
      keys[id] = parts;
    }
    record.key_file = KEY_REF + id;
    byPath.set(filename, record.key_file);
  }
  return { documents, keys, missing };
}

function validatePayload(payload) {
  try {
    const documents = normalizeDocuments(payload.documents);
    const { keys, missing } = payload;
    if (!object(keys) || Object.keys(keys).length > P.maximum_keys || !Array.isArray(missing) ||
        missing.some(item => typeof item !== 'string' || !item || item.length > 4096 || /[\x00-\x1f]/.test(item))) {
      throw Error();
    }
    for (const [id, parts] of Object.entries(keys)) {
      if (!object(parts) || Object.keys(parts).length !== KEY_PARTS.length ||
          !KEY_HEADER.test(decode(parts.private, MAX_KEY).subarray(0, 128).toString('ascii'))) throw Error();
      for (const name of KEY_PARTS.slice(1)) decode(parts[name], MAX_KEY);
      if (keyId(parts) !== id) throw Error();
    }
    const refs = new Set(holders(documents).map(record => record.key_file)
      .filter(value => value?.startsWith(KEY_REF)).map(value => value.slice(KEY_REF.length)));
    if (refs.size !== Object.keys(keys).length || [...refs].some(id => !Object.hasOwn(keys, id))) throw Error();
    return { documents, keys, missing };
  } catch { throw Error(INVALID); }
}

async function writeBackup(filename, payload, password, root) {
  const directory = fs.realpathSync(root);
  filename = path.join(fs.realpathSync(path.dirname(path.resolve(filename))), path.basename(filename));
  const relative = path.relative(directory, filename);
  if (!relative || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep))) {
    throw Error('Выберите файл вне каталога настроек AstraTerm.');
  }
  checkPassword(password, true);
  const plain = jsonBytes(payload);
  try {
    if (plain.length > MAX_PAYLOAD) throw Error('Резервная копия превышает допустимый размер.');
    const salt = crypto.randomBytes(32), nonce = crypto.randomBytes(12);
    const key = await pbkdf2(password, salt, P.iterations, 32, 'sha256');
    try {
      const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
      cipher.setAAD(AAD);
      const ciphertext = Buffer.concat([cipher.update(plain), cipher.final()]);
      const envelope = { ...HEADER, salt: salt.toString('base64'), nonce: nonce.toString('base64'),
        tag: cipher.getAuthTag().toString('base64'), ciphertext: ciphertext.toString('base64') };
      try { writeNew(filename, jsonBytes(envelope)); }
      catch (error) {
        if (error.code === 'EEXIST') throw Error('Файл уже существует. Выберите новое имя для резервной копии.');
        throw error;
      }
    } finally { key.fill(0); }
  } finally { plain.fill(0); }
}

async function readBackup(filename, password) {
  checkPassword(password);
  const raw = read(filename, MAX_ARCHIVE);
  let salt, nonce, tag, ciphertext;
  try {
    const envelope = JSON.parse(decoder.decode(raw));
    if (Object.keys(HEADER).some(name => envelope[name] !== HEADER[name])) throw Error();
    salt = decode(envelope.salt, 32, 32);
    nonce = decode(envelope.nonce, 12, 12);
    tag = decode(envelope.tag, 16, 16);
    ciphertext = decode(envelope.ciphertext, MAX_PAYLOAD);
  } catch { throw Error(INVALID); }
  const key = await pbkdf2(password, salt, P.iterations, 32, 'sha256');
  let plain, decrypted;
  try {
    const decipher = crypto.createDecipheriv('aes-256-gcm', key, nonce);
    decipher.setAAD(AAD);
    decipher.setAuthTag(tag);
    decrypted = decipher.update(ciphertext);
    plain = Buffer.concat([decrypted, decipher.final()]);
  } catch { throw Error('Неверный пароль резервной копии или файл повреждён.'); }
  finally { key.fill(0); decrypted?.fill(0); }
  try { return validatePayload(JSON.parse(decoder.decode(plain))); }
  catch { throw Error(INVALID); }
  finally { plain.fill(0); }
}

async function restoreProfile(root, previous, payload) {
  const { documents, keys } = validatePayload(payload);
  root = path.resolve(root);
  const parent = path.join(root, PROFILES);
  fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
  if (!fs.lstatSync(parent).isDirectory()) throw Error('Недопустимый каталог восстановления AstraTerm.');
  const id = crypto.randomBytes(16).toString('hex');
  const directory = path.join(parent, id);
  fs.mkdirSync(directory, { mode: 0o700 });
  const marker = path.join(root, MARKER), temporary = marker + '.' + id + '.tmp';
  try {
    await protectKeyDirectory(directory);
    for (const [identifier, parts] of Object.entries(keys)) {
      const folder = path.join(directory, 'keys', identifier);
      fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
      KEY_PARTS.forEach((name, index) => {
        if (!parts[name]) return;
        const bytes = decode(parts[name], MAX_KEY);
        try { writeNew(path.join(folder, 'identity' + KEY_SUFFIXES[index]), bytes); }
        finally { bytes.fill(0); }
      });
    }
    for (const record of holders(documents)) {
      if (record.key_file?.startsWith(KEY_REF)) {
        record.key_file = path.join(directory, 'keys', record.key_file.slice(KEY_REF.length), 'identity');
      }
    }
    for (const [name, document] of Object.entries(documents)) {
      const bytes = jsonBytes(document);
      if (bytes.length > MAX_DOCUMENT) throw Error('Восстановленные настройки превышают допустимый размер.');
      writeNew(path.join(directory, name), bytes);
    }
    // Electron's main process is the only writer. Keep the check and switch synchronous.
    if (activeProfile(root) !== previous) throw Error('Активный профиль изменился. Перезапустите AstraTerm и повторите восстановление.');
    writeNew(temporary, jsonBytes({ directory: id }));
    fs.renameSync(temporary, marker);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    fs.rmSync(directory, { recursive: true, force: true });
    throw error;
  }
  return directory;
}

module.exports = { activeProfile, captureProfile, writeBackup, readBackup, restoreProfile };
