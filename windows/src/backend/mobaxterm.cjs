'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { ImportPlan, credentialId, decodeImportText, mobaSessions, stableId } = require('./importers.cjs');
const { defaultSession } = require('./store.cjs');
const { validateCredentials } = require('./vault.cjs');

const endpointPattern = /^(?:(ssh|rdp|vnc|telnet)([0-9]*):)?(.+)@(.+)$/i;

function mobaConfigPaths({ appData, documents }) {
  return [documents, appData].map(root => path.join(root, 'MobaXterm', 'MobaXterm.ini'));
}

function configSections(text) {
  const sections = new Map();
  let entries;
  for (const line of text.split(/\r?\n/)) {
    const header = /^\[([^\]]+)\]$/.exec(line.trim());
    if (header) {
      const name = ['Misc', 'Sesspass', 'Passwords', 'Credentials'].find(section => section.toLowerCase() === header[1].trim().toLowerCase());
      entries = undefined;
      if (!name) continue;
      if (sections.has(name)) throw Error('В файле MobaXterm повторяется раздел.');
      entries = new Map();
      sections.set(name, entries);
    } else if (entries && line.trim() && !/^\s*[#;]/.test(line)) {
      const separator = line.indexOf('=');
      if (separator < 1) throw Error('Повреждена настройка MobaXterm.');
      const key = line.slice(0, separator).trim();
      if (entries.has(key)) throw Error('В файле MobaXterm повторяется настройка.');
      entries.set(key, line.slice(separator + 1).trim());
    }
  }
  return sections;
}

function verifiedKey(sections, master) {
  const entropy = [...(sections.get('Misc') || [])].find(([key]) => key.toLowerCase() === 'sessionp')?.[1];
  const records = [...(sections.get('Sesspass')?.values() || [])].filter(value => value.length >= 80);
  if (!entropy || !records.length) throw Error('В этом MobaXterm.ini нет данных для проверки паролей. Выберите конфигурацию текущего пользователя Windows или импортируйте текстовый экспорт паролей через «Импорт подключений».');
  if (typeof master !== 'string' || !master || master.length > 4096) throw Error('Введите мастер-пароль MobaXterm.');
  const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const script = path.join(__dirname, 'mobaxterm-dpapi.ps1').replace('app.asar' + path.sep, 'app.asar.unpacked' + path.sep);
  return new Promise((resolve, reject) => {
    const child = execFile(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script],
      { windowsHide: true, timeout: 12000, maxBuffer: 4096 }, (error, stdout) => {
        if (error || !/^[A-Za-z0-9+/]{86}==$/.test(stdout)) {
          reject(Error('Не удалось подтвердить мастер-пароль MobaXterm. Проверьте пароль и выберите MobaXterm.ini из той же учётной записи Windows, где были сохранены пароли.'));
          return;
        }
        const digest = Buffer.from(stdout, 'base64');
        const key = Buffer.from(digest.subarray(0, 32));
        digest.fill(0);
        resolve(key);
      });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({ entropy, records, master }), 'utf8');
  });
}

function decryptCfb8(data, key, iv) {
  // Electron's BoringSSL exposes AES-ECB but not CFB8. Each CFB8 step
  // XORs one byte with AES of the previous 16 ciphertext bytes (or IV).
  const blockSize = 16;
  const feedback = Buffer.concat([iv, data]);
  const input = Buffer.alloc(Math.min(4096, data.length) * blockSize);
  const clear = Buffer.alloc(data.length);
  const cipher = crypto.createCipheriv('aes-256-ecb', key, null);
  cipher.setAutoPadding(false);
  for (let start = 0; start < data.length;) {
    const count = Math.min(input.length / blockSize, data.length - start);
    for (let index = 0; index < count; index++) {
      feedback.copy(input, index * blockSize, start + index, start + index + blockSize);
    }
    const encrypted = cipher.update(input.subarray(0, count * blockSize));
    for (let index = 0; index < count; index++) {
      clear[start + index] = data[start + index] ^ encrypted[index * blockSize];
    }
    encrypted.fill(0);
    start += count;
  }
  cipher.final();
  return clear;
}

function decodeMobaPassword(value, key) {
  if (!value) return '';
  let iv, encoded;
  if (value.startsWith('_@')) {
    if (value.length < 20 || !/^[A-Za-z0-9_@+/=]+$/.test(value.slice(2))) throw Error('Повреждена запись MobaXterm.');
    // MobaXterm 25.x also writes its full prefix for a zero-length password.
    if (value.length === 20) return '';
    iv = Buffer.from(value.slice(2, 18), 'ascii');
    encoded = value.slice(20).replaceAll('@', '+').replaceAll('_', '/');
  } else {
    const cipher = crypto.createCipheriv('aes-256-ecb', key, null);
    cipher.setAutoPadding(false);
    iv = Buffer.concat([cipher.update(Buffer.alloc(16)), cipher.final()]);
    encoded = value;
  }
  const data = Buffer.from(encoded, 'base64');
  if (!data.length || data.toString('base64').replace(/=+$/, '') !== encoded.replace(/=+$/, '')) {
    throw Error('Повреждена запись MobaXterm.');
  }
  const clear = decryptCfb8(data, key, iv);
  try {
    let end = clear.length;
    while (end > 0 && clear[end - 1] === 0) end--;
    return new TextDecoder('utf-8', { fatal: true }).decode(clear.subarray(0, end));
  } catch {
    throw Error('Не удалось прочитать одну из записей MobaXterm.');
  } finally { clear.fill(0); }
}

function readConfig(filename) {
  const descriptor = fs.openSync(filename, 'r');
  try {
    const stat = fs.fstatSync(descriptor);
    const limit = 8 * 1024 * 1024;
    if (!stat.isFile() || stat.size > limit) throw Error('Файл MobaXterm должен быть не больше 8 МиБ.');
    const bytes = Buffer.alloc(stat.size + 1);
    const count = fs.readSync(descriptor, bytes);
    if (count !== stat.size) throw Error('MobaXterm изменил файл. Повторите импорт.');
    return decodeImportText(bytes.subarray(0, count));
  } finally { fs.closeSync(descriptor); }
}

function parseMobaCredentials(text, sections, key) {
  const plan = new ImportPlan();
  const endpoints = new Map();
  let unsupported = 0, empty = 0;
  function addPassword(record, encoded) {
    const password = decodeMobaPassword(encoded, key);
    if (!password) {
      empty++;
      return false;
    }
    return add({ ...record, password });
  }
  function add(record) {
    try { validateCredentials([record]); }
    catch { unsupported++; return false; }
    plan.secret(record);
    return true;
  }
  mobaSessions(text, plan);
  for (const [name, value] of sections.get('Credentials') || []) {
    const separator = value.indexOf(':');
    if (separator < 0) throw Error('Неизвестный формат именованного профиля MobaXterm.');
    addPassword({ id: credentialId(name), name, username: value.slice(0, separator) }, value.slice(separator + 1));
  }
  for (const [label, value] of sections.get('Passwords') || []) {
    if (!value) {
      empty++;
      continue;
    }
    const match = endpointPattern.exec(label);
    if (!match) throw Error('Неизвестный адресный профиль MobaXterm.');
    const [, transport, inputPort, username, address] = match;
    const protocol = (transport || 'ssh').toLowerCase();
    const port = Number(inputPort || defaultSession(protocol).port);
    if (port < 1 || port > 65535) throw Error('Некорректный порт профиля MobaXterm.');
    const host = address.toLowerCase();
    const id = stableId('mobaxterm', 'endpoint', `${protocol}:${port}:${username}@${host}`);
    const record = { id, name: `${protocol}${port}:${username}@${host}`, username };
    if (protocol === 'ssh' || protocol === 'rdp') record.protocol = protocol;
    if (addPassword(record, value)) endpoints.set(id, JSON.stringify([protocol, port, username, host]));
  }
  if (!plan.credentials.size && unsupported) throw Error('Сохранённые профили MobaXterm не подходят формату AstraTerm. Проверьте имена пользователей и профилей в MobaXterm.');
  if (!plan.credentials.size) throw Error('В файле MobaXterm нет сохранённых паролей.');
  return { plan, endpoints, unsupported, empty };
}

async function readMobaCredentials(filename, master) {
  const text = readConfig(filename);
  const sections = configSections(text);
  const key = await verifiedKey(sections, master);
  try { return parseMobaCredentials(text, sections, key); }
  finally { key.fill(0); }
}

function planMobaLinks(store, source, vault) {
  const { plan, endpoints } = source;
  const bookmarks = new Map(plan.sessions.map(row => [row.id, row]));
  const saved = store.snapshot().sessions.map(row => ({ row, effective: store.resolveSession(row) }));
  const existing = new Set(vault.metadata().map(row => row.id));
  const available = new Map();
  const pending = new Map();
  let conflicts = 0, stale = 0, matched = 0;
  const location = row => [row.protocol, row.port, row.host.toLowerCase()];
  const endpoint = row => JSON.stringify([row.protocol, row.port, row.username, row.host.toLowerCase()]);
  function matches(id, record, row, effective) {
    if (!['both', effective.protocol].includes(record.protocol || 'both')) return false;
    if (endpoints.has(id)) return endpoints.get(id) === endpoint(effective) && !effective.rdp_domain;
    const bookmark = bookmarks.get(row.id);
    return bookmark?.credential_profile === id && JSON.stringify(location(bookmark)) === JSON.stringify(location(effective)) &&
      (bookmark.rdp_domain || '') === (effective.rdp_domain || '') && (!effective.username || effective.username === record.username);
  }
  for (const [id, record] of plan.credentials) {
    const previous = existing.has(id) ? vault.get(id) : null;
    if (previous && (previous.username !== record.username ||
      (previous.protocol && record.protocol && previous.protocol !== record.protocol) ||
      (previous.password !== record.password && previous.name !== record.name))) {
      conflicts++;
      continue;
    }
    available.set(id, { ...record, protocol: record.protocol || previous?.protocol || 'both' });
    if (!previous || previous.password !== record.password) pending.set(id, previous ? { ...previous, password: record.password } : record);
  }
  for (const { row, effective } of saved) {
    const id = row.credential_profile;
    if (available.has(id) && !matches(id, available.get(id), row, effective)) {
      available.delete(id);
      stale++;
    }
  }
  const sessions = [];
  const previewRows = [];
  const directEndpoints = new Map([...endpoints].filter(([id]) => available.has(id)).map(([id, target]) => [target, id]));
  for (const { row, effective } of saved) {
    let id = row.credential_profile;
    if (id && existing.has(id) && !available.has(id)) continue;
    if (!id || !available.has(id)) {
      const named = bookmarks.get(row.id)?.credential_profile;
      id = named && available.has(named) && matches(named, available.get(named), row, effective) ?
        named : directEndpoints.get(endpoint(effective));
    }
    const record = available.get(id);
    if (!record || !matches(id, record, row, effective)) continue;
    matched++;
    previewRows.push({ name: row.name, group: row.group, protocol: row.protocol, host: row.host, username: record.username, credential: record.name });
    if (row.credential_profile !== id) sessions.push({ ...row, credential_profile: id, credential_label: record.name });
  }
  const records = [...pending].filter(([id]) => available.has(id)).map(([, record]) => record);
  return { sessions, records, previewRows, matched, conflicts, stale };
}

module.exports = { mobaConfigPaths, configSections, decodeMobaPassword, parseMobaCredentials, readMobaCredentials, planMobaLinks };
