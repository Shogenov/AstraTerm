'use strict';

// Schema 2 is shared with the Linux application. Secrets never enter this store.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const crypto = require('node:crypto');

const MAX_BYTES = 8 * 1024 * 1024;
const MAX_ITEMS = 10000;
const { DEFAULT_PORTS: PORTS, GROUP_SSH_FIELDS, SESSION_SCHEMA_VERSION } = require('../shared.cjs');
const clone = value => JSON.parse(JSON.stringify(value));
function object(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name}: ожидается объект`);
  return value;
}
function text(value, name, maximum = 4096, single = true) {
  if (value == null) value = '';
  if (typeof value !== 'string' || value.length > maximum || value.includes('\0') || (single && /[\x00-\x1f\x7f]/.test(value))) throw new Error(`Недопустимое поле «${name}»`);
  return single ? value.trim() : value;
}
function id(value) {
  value = value || crypto.randomUUID().replace(/-/g, '');
  if (typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(value)) throw new Error('Недопустимый идентификатор');
  return value;
}
function bool(value, name) {
  if (typeof value === 'boolean') return value;
  if (value === 0 || value === 1) return !!value;
  throw new Error(`Недопустимое логическое поле «${name}»`);
}
function port(value, zero = false) {
  if (typeof value === 'boolean' || !/^[0-9]+$/.test(String(value))) throw new Error('Порт должен быть целым числом');
  value = Number(value);
  if (!Number.isSafeInteger(value) || value < (zero ? 0 : 1) || value > 65535) throw new Error('Порт должен быть от 1 до 65535');
  return value;
}
function host(value, required = true, wildcard = false) {
  value = text(value, 'Сервер', 255);
  if (value.startsWith('[') && value.endsWith(']')) value = value.slice(1, -1);
  if (!value && !required) return '';
  if (wildcard && value === '*') return value;
  if (!value || value.startsWith('-') || !/^[A-Za-z0-9_.:%-]+$/.test(value)) throw new Error('Недопустимое имя сервера');
  if (value.includes(':') && net.isIP(value.split('%')[0]) !== 6) throw new Error('IPv6-адрес указывается без номера порта');
  return value;
}
function username(value) {
  value = text(value, 'Пользователь', 255);
  if (value && (value.startsWith('-') || !/^[A-Za-z0-9_.@\\-]+$/.test(value))) throw new Error('Недопустимое имя пользователя');
  return value;
}
function validateJumpHost(value) {
  value = text(value, 'SSH-шлюз', 2048);
  if (!value) return '';
  for (const entry of value.split(',')) {
    let target = entry;
    const at = target.lastIndexOf('@');
    if (at >= 0) {
      const user = username(target.slice(0, at));
      if (!user || user.includes('@')) throw new Error('Недопустимый пользователь SSH-шлюза');
      target = target.slice(at + 1);
    }
    if (target.startsWith('[')) {
      const match = /^\[([^\]]+)\](?::([0-9]+))?$/.exec(target);
      if (!match) throw new Error('Недопустимый IPv6 SSH-шлюз');
      host(match[1]);
      if (match[2]) port(match[2]);
    } else if (target.includes(':')) {
      const split = target.lastIndexOf(':');
      if (target.slice(0, split).includes(':')) throw new Error('IPv6 SSH-шлюза укажите в квадратных скобках');
      host(target.slice(0, split)); port(target.slice(split + 1));
    } else host(target);
  }
  return value;
}
function normalizeGroupName(value, allowEmpty = false) {
  value = text(value, 'Папка', 200);
  if (!value && allowEmpty) return '';
  const parts = value.split('/').map(part => part.trim());
  if (!value || parts.some(part => !part || part === '.' || part === '..')) throw new Error('Укажите названия папок через / без . и ..');
  return parts.join('/');
}
function validateEncoding(value) {
  value = text(value, 'Кодировка', 100) || 'UTF-8';
  // iconv-lite is also the terminal codec. Keep validation independent of Electron.
  let supported;
  try { supported = require('iconv-lite').encodingExists(value); }
  catch (_) { supported = /^(utf-?8|utf-?16(le|be)?|ascii|latin1|iso-?8859-?1|windows-?125[0-8]|cp125[0-8]|koi8-?r|koi8-?u|cp866)$/i.test(value); }
  if (!supported) throw new Error('Неизвестная кодировка');
  return value;
}
function defaultSession(protocol = 'ssh') {
  if (typeof protocol !== 'string' || !Object.hasOwn(PORTS, protocol)) throw new Error('Неизвестный протокол');
  return { id: id(), name: '', group: '', inherit_group_ssh: true, protocol,
    host: '', port: PORTS[protocol], username: '', key_file: '', identity_profile: '',
    credential_profile: '', credential_label: '', import_warning: '', x11: false,
    compression: true, jump_host: '', remote_command: '', encoding: 'UTF-8', startup_directory: '',
    serial_device: 'COM1', baud: 115200, rdp_domain: '', rdp_resolution: '1280x800' };
}
function validateSession(record) {
  object(record, 'Подключение');
  const protocol = Object.hasOwn(record, 'protocol') ? record.protocol : 'ssh';
  if (typeof protocol !== 'string' || !Object.hasOwn(PORTS, protocol)) throw new Error('Неизвестный протокол');
  const result = defaultSession(protocol);
  for (const key of Object.keys(result)) if (Object.hasOwn(record, key)) result[key] = record[key];
  result.id = id(result.id);
  result.name = text(result.name, 'Название', 200);
  result.group = normalizeGroupName(result.group, true);
  result.inherit_group_ssh = bool(result.inherit_group_ssh, 'Настройки группы');
  const remote = !['local', 'serial'].includes(result.protocol);
  result.host = host(result.host, remote);
  result.port = port(result.port, !remote);
  result.username = username(result.username);
  result.key_file = text(result.key_file, 'Файл ключа');
  result.identity_profile = text(result.identity_profile, 'Профиль ключа', 80);
  if (result.identity_profile) id(result.identity_profile);
  result.credential_profile = text(result.credential_profile, 'Учётные данные', 80);
  if (result.credential_profile) id(result.credential_profile);
  result.credential_label = text(result.credential_label, 'Название учётных данных', 200);
  result.import_warning = text(result.import_warning, 'Ограничения импорта', 2000);
  result.x11 = bool(result.x11, 'X11');
  result.compression = bool(result.compression, 'Сжатие');
  result.jump_host = validateJumpHost(result.jump_host);
  result.remote_command = text(result.remote_command, 'Удалённая команда', 65536, false);
  result.startup_directory = text(result.startup_directory, 'Начальный каталог');
  result.serial_device = text(result.serial_device, 'Последовательный порт');
  // Linux device metadata remains importable; Windows runtime reports unsupported protocols.
  if (!Number.isInteger(Number(result.baud)) || Number(result.baud) < 50 || Number(result.baud) > 4000000 || typeof result.baud === 'boolean') throw new Error('Недопустимая скорость порта');
  result.baud = Number(result.baud);
  result.encoding = validateEncoding(result.encoding);
  result.rdp_domain = text(result.rdp_domain, 'Домен RDP', 255);
  result.rdp_resolution = text(result.rdp_resolution, 'Разрешение RDP', 40);
  if (!/^[0-9]{3,5}x[0-9]{3,5}$/.test(result.rdp_resolution)) throw new Error('Разрешение указывается в формате 1280x800');
  result.name ||= result.protocol === 'local' ? 'PowerShell' : result.protocol === 'serial' ? result.serial_device : result.host;
  return result;
}
function validateGroup(record) {
  object(record, 'Папка');
  const name = normalizeGroupName(record.name);
  const settings = object(record.ssh || {}, 'SSH-настройки папки');
  const candidate = { protocol: 'ssh', host: 'group.invalid' };
  for (const field of GROUP_SSH_FIELDS) if (Object.hasOwn(settings, field)) candidate[field] = settings[field];
  const checked = validateSession(candidate);
  return { id: id(record.id), name, ssh: Object.fromEntries(GROUP_SSH_FIELDS.filter(field => Object.hasOwn(settings, field)).map(field => [field, checked[field]])) };
}
function validateMacro(record) {
  object(record, 'Макрос');
  return { id: id(record.id), name: text(record.name ?? 'Макрос', 'Название', 200) || 'Макрос',
    text: text(record.text ?? record.command ?? '', 'Текст', 65536, false), append_newline: bool(record.append_newline ?? true, 'Перевод строки') };
}
function validateTunnel(record) {
  object(record, 'Туннель');
  const type = record.type || 'local';
  if (!['local', 'remote', 'dynamic'].includes(type)) throw new Error('Неизвестный тип туннеля');
  const result = { id: id(record.id), name: text(record.name, 'Название', 200), type, host: host(record.host),
    port: port(record.port ?? 22), username: username(record.username ?? record.user ?? ''), key_file: text(record.key_file, 'Файл ключа'),
    jump_host: validateJumpHost(record.jump_host), bind_host: host(record.bind_host ?? '127.0.0.1', true, true),
    listen_port: port(record.listen_port ?? 8080), destination_host: '', destination_port: 0 };
  if (type !== 'dynamic') { result.destination_host = host(record.destination_host ?? '127.0.0.1'); result.destination_port = port(record.destination_port ?? 80); }
  result.name ||= `${type} :${result.listen_port} → ${result.host}`;
  return result;
}
function prefixes(name) {
  const parts = name ? name.split('/') : [];
  return parts.map((_, index) => parts.slice(0, index + 1).join('/'));
}
const inGroup = (name, parent) => name === parent || name.startsWith(`${parent}/`);
function completeGroups(groups, sessions) {
  const byName = new Map(); const used = new Set();
  for (const group of groups) {
    if (byName.has(group.name) || used.has(group.id)) throw new Error('Повторяющаяся папка или идентификатор');
    byName.set(group.name, group); used.add(group.id);
  }
  for (const name of [...byName.keys(), ...sessions.map(item => item.group)]) {
    for (const prefix of prefixes(name)) if (!byName.has(prefix)) {
      let identifier = `group_${crypto.createHash('sha256').update(prefix).digest('hex').slice(0, 32)}`;
      if (used.has(identifier)) identifier = id();
      used.add(identifier); byName.set(prefix, { id: identifier, name: prefix, ssh: {} });
    }
  }
  if (byName.size > MAX_ITEMS) throw new Error('Слишком много папок');
  return [...byName.values()].sort((a, b) => a.name.localeCompare(b.name));
}
function normalizeDocument(document) {
  if (Array.isArray(document)) document = { sessions: document };
  object(document, 'Файл подключений');
  const version = document.schema_version ?? 1;
  if (version !== 1 && version !== 2) throw new Error('Неподдерживаемая версия формата');
  const result = { schema_version: SESSION_SCHEMA_VERSION };
  for (const [field, validator] of [['sessions', validateSession], ['macros', validateMacro], ['tunnels', validateTunnel], ['groups', validateGroup]]) {
    let items = document[field] ?? [];
    if (!Array.isArray(items) || items.length > MAX_ITEMS) throw new Error(`Недопустимый список ${field}`);
    if (field === 'sessions' && version === 1) items = items.map(item => ({ ...object(item, 'Подключение'), inherit_group_ssh: item.inherit_group_ssh ?? false }));
    result[field] = items.map(validator);
    if (new Set(result[field].map(item => item.id)).size !== result[field].length) throw new Error('Повторяющиеся идентификаторы');
  }
  result.groups = completeGroups(result.groups, result.sessions);
  return result;
}
function readBytes(filename) {
  let buffer;
  try {
    const fd = fs.openSync(filename, 'r');
    try {
      if (fs.fstatSync(fd).size > MAX_BYTES) throw new Error('Файл подключений превышает 8 МиБ');
      buffer = fs.readFileSync(fd);
    } finally { fs.closeSync(fd); }
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  if (buffer.length > MAX_BYTES) throw new Error('Файл подключений превышает 8 МиБ');
  return buffer;
}
const digest = bytes => bytes == null ? null : crypto.createHash('sha256').update(bytes).digest('hex');
function renameGroups(document, oldName, newName) {
  if (oldName === newName) return;
  if (inGroup(newName, oldName)) throw new Error('Нельзя переместить папку внутрь себя');
  const affected = document.groups.filter(group => inGroup(group.name, oldName));
  if (!affected.length) throw new Error('Папка не найдена');
  const untouched = new Set(document.groups.filter(group => !inGroup(group.name, oldName)).map(group => group.name));
  if (affected.some(group => untouched.has(newName + group.name.slice(oldName.length)))) throw new Error('В месте назначения уже есть такая папка');
  for (const group of affected) group.name = newName + group.name.slice(oldName.length);
  for (const session of document.sessions) if (inGroup(session.group, oldName)) session.group = newName + session.group.slice(oldName.length);
}
function resolved(document, session) {
  const result = validateSession(session);
  if (['ssh', 'mosh'].includes(result.protocol) && result.inherit_group_ssh) {
    for (const prefix of prefixes(result.group)) Object.assign(result, document.groups.find(group => group.name === prefix)?.ssh || {});
  }
  return validateSession(result);
}
class Store {
  constructor(directory = path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'AstraTerm')) {
    this.directory = path.resolve(directory);
    this.path = path.join(this.directory, 'sessions.json');
    this._revision = null;
    this._document = normalizeDocument({ schema_version: SESSION_SCHEMA_VERSION });
    this.load();
  }
  load() {
    const bytes = readBytes(this.path);
    const next = bytes ? normalizeDocument(JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''))) : normalizeDocument({ schema_version: SESSION_SCHEMA_VERSION });
    this._document = next; this._revision = digest(bytes);
    return this.snapshot();
  }
  snapshot() { return clone(this._document); }
  exportDocument() { return this.snapshot(); }
  _commit(document) {
    const checked = normalizeDocument(document);
    const bytes = Buffer.from(`${JSON.stringify(checked, null, 2)}\n`, 'utf8');
    if (bytes.length > MAX_BYTES) throw new Error('Файл подключений превышает 8 МиБ');
    fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    const lock = path.join(this.directory, '.sessions.json.lock');
    let fd;
    try { fd = fs.openSync(lock, 'wx', 0o600); }
    catch (error) {
      if (error.code === 'EEXIST') throw new Error('Настройки заняты другим окном. При зависшей блокировке закройте окна и удалите .sessions.json.lock.');
      throw error;
    }
    const temporary = path.join(this.directory, `.sessions-${crypto.randomBytes(12).toString('hex')}.tmp`);
    try {
      if (digest(readBytes(this.path)) !== this._revision) throw new Error('Настройки изменены другим окном. Перечитайте настройки перед сохранением.');
      const target = fs.openSync(temporary, 'wx', 0o600);
      try { fs.writeFileSync(target, bytes); fs.fsyncSync(target); } finally { fs.closeSync(target); }
      fs.renameSync(temporary, this.path);
      this._document = checked; this._revision = digest(bytes);
    } finally {
      try {
        try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      } finally {
        try { fs.closeSync(fd); } finally { fs.unlinkSync(lock); }
      }
    }
    return this.snapshot();
  }
  _upsert(field, record, validator) {
    const checked = validator(record), next = this.snapshot();
    const index = next[field].findIndex(item => item.id === checked.id);
    if (index < 0) next[field].push(checked); else next[field][index] = checked;
    this._commit(next); return clone(checked);
  }
  _delete(field, identifier) {
    id(identifier);
    const next = this.snapshot(), count = next[field].length;
    next[field] = next[field].filter(item => item.id !== identifier);
    if (count === next[field].length) return false;
    this._commit(next); return true;
  }
  upsertSession(record) { return this._upsert('sessions', record, validateSession); }
  duplicateSession(identifier) {
    const source = this._document.sessions.find(item => item.id === identifier);
    if (!source) throw new Error('Подключение не найдено');
    const names = new Set(this._document.sessions.filter(item => item.group === source.group).map(item => item.name));
    const base = source.name.replace(/ \([1-9][0-9]*\)$/, '').trimEnd();
    for (let number = 1; number <= names.size + 1; number++) {
      const suffix = ` (${number})`;
      const name = base.slice(0, 200 - suffix.length).replace(/[\uD800-\uDBFF]$/, '').trimEnd() + suffix;
      if (!names.has(name)) return this.upsertSession({ ...source, id: '', name });
    }
    throw new Error('Не удалось подобрать имя подключения');
  }
  deleteSession(identifier) { return this._delete('sessions', identifier); }
  upsertMacro(record) { return this._upsert('macros', record, validateMacro); }
  deleteMacro(identifier) { return this._delete('macros', identifier); }
  upsertTunnel(record) { return this._upsert('tunnels', record, validateTunnel); }
  deleteTunnel(identifier) { return this._delete('tunnels', identifier); }
  resolveSession(recordOrId) {
    const session = typeof recordOrId === 'string' ? this._document.sessions.find(item => item.id === recordOrId) : recordOrId;
    if (!session) throw new Error('Подключение не найдено');
    return resolved(this._document, session);
  }
  upsertGroup(record, applyToSessions = false) {
    const providedId = !!record?.id, checked = validateGroup(record), next = this.snapshot();
    bool(applyToSessions, 'Применить к существующим');
    const previous = next.groups.find(group => group.id === checked.id);
    if (previous && previous.name !== checked.name) renameGroups(next, previous.name, checked.name);
    const sameName = next.groups.find(group => group.name === checked.name);
    if (sameName && (!providedId || sameName.id !== checked.id)) throw new Error('Папка с таким именем уже существует');
    next.groups = next.groups.filter(group => group.id !== checked.id); next.groups.push(checked);
    if (applyToSessions) for (const session of next.sessions) if (['ssh', 'mosh'].includes(session.protocol) && inGroup(session.group, checked.name)) session.inherit_group_ssh = true;
    this._commit(next); return clone(checked);
  }
  deleteGroup(name) {
    name = normalizeGroupName(name);
    const next = this.snapshot();
    if (!next.groups.some(group => group.name === name)) return false;
    const parent = name.includes('/') ? name.slice(0, name.lastIndexOf('/')) : '';
    next.sessions = next.sessions.map(session => {
      if (!inGroup(session.group, name)) return session;
      const result = resolved(next, session); result.group = parent;
      if (['ssh', 'mosh'].includes(result.protocol)) result.inherit_group_ssh = false;
      return result;
    });
    next.groups = next.groups.filter(group => !inGroup(group.name, name));
    this._commit(next); return true;
  }
  importDocument(document) {
    const incoming = normalizeDocument(document), next = this.snapshot();
    const explicit = new Set((Array.isArray(document) ? [] : document.groups || []).map(group => normalizeGroupName(group.name)));
    for (const field of ['sessions', 'macros', 'tunnels']) for (const record of incoming[field]) {
      const index = next[field].findIndex(item => item.id === record.id);
      if (index < 0) next[field].push(record); else next[field][index] = record;
    }
    for (let group of incoming.groups) {
      if (!explicit.has(group.name)) {
        if (next.groups.some(item => item.name === group.name)) continue;
        if (next.groups.some(item => item.id === group.id)) group = { ...group, id: id() };
      }
      const previous = next.groups.find(item => item.id === group.id) || next.groups.find(item => item.name === group.name);
      if (previous) {
        if (previous.name !== group.name) renameGroups(next, previous.name, group.name);
        next.groups[next.groups.findIndex(item => item.id === previous.id)] = { ...group, id: previous.id };
      } else next.groups.push(group);
    }
    this._commit(next); return { imported: incoming.sessions.length, ...this.snapshot() };
  }
}

module.exports = { Store, SessionStore: Store, defaultSession, validateSession, validateGroup, validateTunnel, validateMacro,
  normalizeDocument, normalizeGroupName, validateJumpHost, validateHost: host, GROUP_SSH_FIELDS };
