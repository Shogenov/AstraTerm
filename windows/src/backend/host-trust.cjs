'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

function fingerprint(key) {
  return 'SHA256:' + crypto.createHash('sha256').update(key).digest('base64').replace(/=+$/, '');
}

function keyType(key) {
  if (!Buffer.isBuffer(key) || key.length < 5) throw new Error('Некорректный ключ сервера.');
  const size = key.readUInt32BE(0);
  if (size < 1 || size > 256 || size + 4 > key.length) throw new Error('Некорректный ключ сервера.');
  return key.subarray(4, size + 4).toString('ascii');
}

function hostToken(host, port) { return Number(port) === 22 ? host.toLowerCase() : `[${host.toLowerCase()}]:${port}`; }

function matchesHost(pattern, target) {
  if (pattern.startsWith('|1|')) {
    const parts = pattern.split('|');
    if (parts.length !== 4) return false;
    const expected = Buffer.from(parts[3], 'base64');
    const actual = crypto.createHmac('sha1', Buffer.from(parts[2], 'base64')).update(target).digest();
    return actual.length === expected.length && crypto.timingSafeEqual(actual, expected);
  }
  const expression = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${expression}$`, 'i').test(target);
}

function knownHostKeys(filename, target) {
  let text;
  try { text = fs.readFileSync(filename, 'utf8'); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  const entries = [];
  for (const line of text.split(/\r?\n/)) {
    const parts = line.trim().split(/\s+/);
    if (!parts[0] || parts[0].startsWith('#')) continue;
    const marker = parts[0].startsWith('@') ? parts.shift() : '';
    if (parts.length < 3) continue;
    let match = false;
    let excluded = false;
    for (const pattern of parts[0].split(',')) {
      if (pattern.startsWith('!') && matchesHost(pattern.slice(1), target)) excluded = true;
      else if (matchesHost(pattern, target)) match = true;
    }
    if (!match || excluded) continue;
    const key = Buffer.from(parts[2], 'base64');
    if (key.length) entries.push({ marker, key, algorithm: parts[1] });
  }
  return entries;
}

class HostTrustStore {
  constructor({ directory, knownHostsPath, prompt } = {}) {
    if (!directory) throw new Error('Не задан каталог настроек доверия SSH.');
    this.filename = path.join(directory, 'known-hosts.json');
    this.knownHostsPath = knownHostsPath || path.join(os.homedir(), '.ssh', 'known_hosts');
    this.prompt = prompt;
    this.pending = new Map();
    this.closed = false;
    this.entries = {};
    try {
      const data = JSON.parse(fs.readFileSync(this.filename, 'utf8'));
      if (data.schema !== 1 || !data.hosts || typeof data.hosts !== 'object') throw new Error('format');
      this.entries = data.hosts;
    } catch (error) {
      if (error.code !== 'ENOENT') throw new Error('Не удалось прочитать сохранённые ключи серверов.');
    }
  }

  async verify({ host, port = 22, key }) {
    if (this.closed) return false;
    const target = hostToken(host, port);
    const algorithm = keyType(key);
    const encoded = key.toString('base64');
    const existing = this.entries[target];
    const nativeKeys = knownHostKeys(this.knownHostsPath, target);
    if (nativeKeys.some(entry => entry.marker === '@revoked' && entry.key.equals(key))) throw new Error(`Ключ сервера ${target} отозван в known_hosts.`);
    if (existing && existing.key !== encoded) throw new Error(`Ключ сервера ${target} изменился. Подключение остановлено; проверьте отпечаток у администратора.`);
    // A matching host entry with a different key is never treated as a new host.
    const ordinary = nativeKeys.filter(entry => entry.marker !== '@cert-authority' && entry.marker !== '@revoked');
    if (ordinary.length && !ordinary.some(entry => entry.key.equals(key))) throw new Error(`Ключ сервера ${target} отличается от known_hosts. Подключение остановлено.`);
    if (nativeKeys.some(entry => entry.marker === '@cert-authority') && !ordinary.length && !existing) throw new Error(`Для ${target} настроен центр сертификации SSH. Сертификаты серверов пока не поддерживаются.`);
    if (existing || ordinary.some(entry => entry.key.equals(key))) return true;
    const pendingId = target + '\0' + encoded;
    if (this.pending.has(pendingId)) return this.pending.get(pendingId);
    const operation = (async () => {
      if (!this.prompt) return false;
      const accepted = await this.prompt({ kind: 'host-key', title: 'Новый SSH-сервер', host: target, fingerprint: fingerprint(key), algorithm, message: `Первое подключение к ${target}. Сверьте отпечаток ключа сервера перед подтверждением.` });
      if (accepted !== true || this.closed) return false;
      // Recheck after the dialog: another parallel connection may have pinned a key.
      if (this.entries[target] && this.entries[target].key !== encoded) throw new Error(`Ключ сервера ${target} изменился во время подключения.`);
      this.entries[target] = { algorithm, key: encoded, fingerprint: fingerprint(key) };
      fs.mkdirSync(path.dirname(this.filename), { recursive: true, mode: 0o700 });
      const temporary = this.filename + '.' + crypto.randomUUID() + '.tmp';
      try {
        fs.writeFileSync(temporary, JSON.stringify({ schema: 1, hosts: this.entries }, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
        fs.renameSync(temporary, this.filename);
      } catch (error) { try { fs.unlinkSync(temporary); } catch {} delete this.entries[target]; throw new Error('Не удалось сохранить подтверждённый ключ сервера.'); }
      return true;
    })();
    this.pending.set(pendingId, operation);
    try { return await operation; } finally { this.pending.delete(pendingId); }
  }

  dispose() { this.closed = true; }
}

module.exports = { HostTrustStore, fingerprint, keyType, hostToken, knownHostKeys, matchesHost };
