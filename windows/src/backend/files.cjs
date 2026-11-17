'use strict';

const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { TextDecoder } = require('node:util');
const { permissions, remoteBirthTimes } = require('./file-metadata.cjs');
const { directoryReport, resolveTerminalDirectory, REMOTE_HOSTNAME, UNKNOWN_DIRECTORY } = require('./terminal-directory.cjs');

const MAX_TEXT_BYTES = 2 * 1024 * 1024;
const token = () => crypto.randomBytes(12).toString('hex');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function validPath(value) {
  if (typeof value !== 'string' || !value || value.includes('\0') || value.length > 32767) throw new Error('Недопустимый путь');
  return value;
}
function missing(error) { return error?.code === 'ENOENT' || error?.code === 2; }
function unsupported(error) { return error?.code === 8 || /unsupported|not support/i.test(error?.message || ''); }
function isSymlink(stats) { return stats.isSymbolicLink(); }
function sameStats(a, b) {
  return a.size === b.size && a.mode === b.mode && (a.mtimeMs ?? a.mtime) === (b.mtimeMs ?? b.mtime) && a.ino === b.ino && a.uid === b.uid && a.gid === b.gid;
}
function revision(bytes, stats) {
  return hash(Buffer.concat([Buffer.from(JSON.stringify([stats.size, stats.mode, stats.mtimeMs ?? stats.mtime, stats.ino, stats.uid, stats.gid])), Buffer.from('\n'), bytes]));
}
function decode(bytes) {
  if (bytes.includes(0)) throw new Error('Двоичный файл нельзя открыть в текстовом редакторе');
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch (_) { throw new Error('Редактор поддерживает текст UTF-8'); }
}
function textBytes(text) {
  if (typeof text !== 'string' || text.includes('\0')) throw new Error('Недопустимый текст файла');
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length > MAX_TEXT_BYTES) throw new Error('Редактор поддерживает файлы до 2 МиБ');
  return bytes;
}
function checkRevision(expected, actual) {
  if (typeof expected !== 'string' && expected !== null) throw new Error('Перед сохранением перечитайте файл');
  if (expected !== actual) throw new Error('Файл изменён другим приложением. Перечитайте его перед сохранением.');
}
function checkOrdinary(stats) {
  if (isSymlink(stats)) throw new Error('Запись и передача через символьную ссылку запрещены. Выберите обычный файл.');
  if (!stats.isFile()) throw new Error('Операция доступна только для обычного файла');
}
class FilesService {
  constructor(options = {}) {
    this.terminals = options.terminals || options;
    if (typeof this.terminals.get !== 'function') throw new Error('Не указан менеджер терминалов');
    this._connections = new Map();
    this._directories = new Map();
    this._busy = new Set();
    this._mutating = false;
  }
  get busy() { return this._busy.size > 0; }
  async _operation(task) {
    const marker = {}; this._busy.add(marker);
    try { return await task(); } finally { this._busy.delete(marker); }
  }
  async _mutation(task) {
    if (this._mutating) throw new Error('Дождитесь завершения текущей файловой операции');
    this._mutating = true;
    try { return await this._operation(task); } finally { this._mutating = false; }
  }
  _record(terminalId) {
    const record = this.terminals.get(terminalId);
    if (!record || record.profile?.protocol !== 'ssh' || !record.sshClient || !['running', 'ready'].includes(record.status)) throw new Error('Для SFTP откройте активное SSH-подключение');
    return record;
  }
  async _binding(request) {
    const mode = request.mode || 'local';
    if (mode === 'local') return { mode, terminalId: null };
    if (mode !== 'sftp') throw new Error('Неизвестный режим файлов');
    const record = this._record(request.terminalId), sshClient = record.sshClient, generation = record.generation;
    if(request.generation!==undefined&&request.generation!==generation)throw new Error('SSH-подключение изменилось; откройте файл или операцию заново.');
    let cached = this._connections.get(record.id || request.terminalId);
    if (!cached || cached.sshClient !== sshClient || cached.generation !== generation) {
      const identifier = record.id || request.terminalId;
      cached = { sshClient, generation, terminalId: identifier };
      cached.pending = new Promise((resolve, reject) => {
        sshClient.sftp((error, sftp) => {
          if (error) { reject(new Error('Не удалось открыть SFTP в текущем SSH-соединении')); return; }
          const current = this.terminals.get(identifier);
          if (current?.sshClient !== sshClient || current.generation !== generation || !['running', 'ready'].includes(current.status)) {
            sftp.end(); reject(new Error('SSH-подключение уже изменилось')); return;
          }
          cached.sftp = sftp;
          if (typeof sftp.once === 'function') {
            const invalidate = () => { if (this._connections.get(identifier) === cached) this._connections.delete(identifier); };
            sftp.once('close', invalidate);
            sftp.on('error', invalidate);
          }
          resolve(sftp);
        });
      });
      this._connections.set(identifier, cached);
      sshClient.once('close', () => {
        if (this._connections.get(identifier) === cached) {
          this._connections.delete(identifier);
          this._directories.delete(identifier);
        }
        if (cached.sftp) { try { cached.sftp.end(); } catch (_) { /* transport is already closed */ } }
      });
    }
    let sftp;
    try { sftp = await cached.pending; }
    catch (error) { if (this._connections.get(cached.terminalId) === cached) this._connections.delete(cached.terminalId); throw error; }
    const binding = { mode, terminalId: cached.terminalId, sshClient, generation, sftp };
    this._assertBinding(binding); return binding;
  }
  _assertBinding(binding) {
    if (binding.mode === 'local') return;
    const current = this.terminals.get(binding.terminalId);
    if (!current || current.sshClient !== binding.sshClient || current.generation !== binding.generation || !['running', 'ready'].includes(current.status)) throw new Error('SSH-подключение изменилось; операция отменена');
  }
  _call(binding, method, ...args) {
    this._assertBinding(binding);
    return new Promise((resolve, reject) => binding.sftp[method](...args, (error, ...values) => error ? reject(error) : resolve(values.length > 1 ? values : values[0])));
  }
  async _path(binding, value) {
    if (binding.mode === 'local') return path.resolve(value ? validPath(value) : this._directories.get('local') || os.homedir());
    if (value) {
      validPath(value);
      if (path.posix.isAbsolute(value)) return path.posix.normalize(value);
      const home = await this._call(binding, 'realpath', '.');
      return path.posix.resolve(home, value);
    }
    return this._directories.get(binding.terminalId) || await this._call(binding, 'realpath', '.');
  }
  _join(binding, directory, name) { return binding.mode === 'local' ? path.join(directory, name) : path.posix.join(directory, name); }
  _temporary(binding, filename, suffix = 'tmp') {
    const library = binding.mode === 'local' ? path : path.posix;
    return library.join(library.dirname(filename), `.astraterm-${token()}.${suffix}`);
  }
  async _lstat(binding, filename) {
    try { return binding.mode === 'local' ? await fsp.lstat(filename) : await this._call(binding, 'lstat', filename); }
    catch (error) { if (missing(error)) return null; throw error; }
  }
  async _readHostname(binding) {
    let handle;
    try {
      handle = await this._call(binding, 'open', REMOTE_HOSTNAME, 'r');
      const bytes = Buffer.alloc(256);
      const result = await this._call(binding, 'read', handle, bytes, 0, bytes.length, 0);
      return bytes.subarray(0, Array.isArray(result) ? result[0] : result).toString('utf8').trim();
    } catch {
      return '';
    } finally {
      if (handle) await this._call(binding, 'close', handle).catch(() => {});
    }
  }
  async list(request = {}, terminalDirectory) {
    return this._operation(async () => {
      const report = terminalDirectory === undefined ? null : directoryReport(terminalDirectory?.kind, terminalDirectory?.value);
      if (terminalDirectory !== undefined && (!report || request.mode !== 'sftp')) throw new Error(UNKNOWN_DIRECTORY);
      const binding = await this._binding(request);
      let directory;
      if (!report) directory = await this._path(binding, request.path);
      else {
        const profile = this._record(binding.terminalId).profile;
        const requested = await resolveTerminalDirectory(report, {
          host: profile.host || '', user: profile.username,
          readHome: () => this._call(binding, 'realpath', '.'),
          readHostname: () => this._readHostname(binding),
        });
        directory = await this._call(binding, 'realpath', requested);
        if (!path.posix.isAbsolute(directory)) throw new Error('Текущий каталог терминала недоступен в SFTP.');
      }
      let entries;
      if (binding.mode === 'local') {
        const names = await fsp.readdir(directory);
        entries = (await Promise.all(names.map(async name => {
          const filename = path.join(directory, name);
          try {
            const stats = await fsp.lstat(filename);
            return { name, path: filename, isDirectory: stats.isDirectory(), isSymlink: stats.isSymbolicLink(), size: stats.size, mtime: stats.mtimeMs };
          } catch (error) { if (missing(error)) return null; throw error; }
        }))).filter(Boolean);
      } else {
        const data = await this._call(binding, 'readdir', directory);
        const births = data.length && !report ? await remoteBirthTimes(binding.sshClient, directory) : new Map();
        entries = data.filter(entry => entry.filename !== '.' && entry.filename !== '..').map(entry => ({ name: entry.filename,
          path: path.posix.join(directory, entry.filename), isDirectory: entry.attrs.isDirectory(), isSymlink: entry.attrs.isSymbolicLink(),
          isFile: entry.attrs.isFile(), size: entry.attrs.size, permissions: permissions(entry.attrs.mode),
          created: births.get(path.posix.join(directory, entry.filename)) ?? null,
          mtime: Number.isFinite(entry.attrs.mtime) ? entry.attrs.mtime * 1000 : null }));
      }
      this._assertBinding(binding);
      if (!report) this._directories.set(binding.mode === 'local' ? 'local' : binding.terminalId, directory);
      entries.sort((a, b) => Number(b.isDirectory) - Number(a.isDirectory) || a.name.localeCompare(b.name));
      return { mode: binding.mode, terminalId: binding.terminalId, path: directory, entries };
    });
  }
  async mkdir(request) {
    return this._mutation(async () => {
      validPath(request.path);
      const binding = await this._binding(request), filename = await this._path(binding, request.path);
      if (binding.mode === 'local') await fsp.mkdir(filename);
      else await this._call(binding, 'mkdir', filename, { mode: 0o755 });
      return { path: filename, mode: binding.mode, terminalId: binding.terminalId };
    });
  }
  async rename(request) {
    return this._mutation(async () => {
      validPath(request.path); validPath(request.targetPath || request.newPath);
      const binding = await this._binding(request), source = await this._path(binding, request.path), target = await this._path(binding, request.targetPath || request.newPath);
      if (source === target) return { path: target };
      if (await this._lstat(binding, target)) throw new Error('Файл или папка с таким именем уже существует');
      if (binding.mode === 'local') await fsp.rename(source, target);
      else await this._call(binding, 'rename', source, target);
      return { path: target, mode: binding.mode, terminalId: binding.terminalId };
    });
  }
  async remove(request) {
    return this._mutation(async () => {
      validPath(request.path);
      const binding = await this._binding(request), filename = await this._path(binding, request.path), stats = await this._lstat(binding, filename);
      if (!stats) throw new Error('Файл не найден');
      // lstat deliberately means a directory link is unlinked, never traversed.
      if (binding.mode === 'local') await (stats.isDirectory() ? fsp.rmdir(filename) : fsp.unlink(filename));
      else await this._call(binding, stats.isDirectory() ? 'rmdir' : 'unlink', filename);
      return { path: filename, mode: binding.mode, terminalId: binding.terminalId };
    });
  }
  async _read(binding, filename) {
    const before = await this._lstat(binding, filename);
    if (!before) return null;
    checkOrdinary(before);
    if (before.size > MAX_TEXT_BYTES) throw new Error('Редактор поддерживает файлы до 2 МиБ');
    let bytes;
    if (binding.mode === 'local') {
      const handle = await fsp.open(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
      try {
        const opened = await handle.stat();
        if (!sameStats(opened, before)) throw new Error('Файл изменился во время чтения');
        const chunks = []; let count = 0;
        while (count <= MAX_TEXT_BYTES) {
          const chunk = Buffer.alloc(Math.min(65536, MAX_TEXT_BYTES + 1 - count));
          const result = await handle.read(chunk, 0, chunk.length, count);
          if (!result.bytesRead) break;
          chunks.push(chunk.subarray(0, result.bytesRead)); count += result.bytesRead;
        }
        bytes = Buffer.concat(chunks);
      } finally { await handle.close(); }
    } else {
      const handle = await this._call(binding, 'open', filename, 'r');
      try {
        const chunks = []; let count = 0;
        while (count <= MAX_TEXT_BYTES) {
          const chunk = Buffer.alloc(Math.min(65536, MAX_TEXT_BYTES + 1 - count));
          const result = await this._call(binding, 'read', handle, chunk, 0, chunk.length, count);
          const bytesRead = Array.isArray(result) ? result[0] : result;
          if (!bytesRead) break;
          chunks.push(chunk.subarray(0, bytesRead)); count += bytesRead;
        }
        bytes = Buffer.concat(chunks);
      } finally { await this._call(binding, 'close', handle).catch(() => {}); }
    }
    if (bytes.length > MAX_TEXT_BYTES) throw new Error('Редактор поддерживает файлы до 2 МиБ');
    const after = await this._lstat(binding, filename);
    if (!after || !sameStats(before, after)) throw new Error('Файл изменился во время чтения');
    return { bytes, stats: after, revision: revision(bytes, after) };
  }
  async readText(request) {
    return this._operation(async () => {
      validPath(request.path);
      const binding = await this._binding(request), filename = await this._path(binding, request.path), data = await this._read(binding, filename);
      if (!data) throw new Error('Файл не найден');
      return { text: decode(data.bytes), revision: data.revision, path: filename, mode: binding.mode, terminalId: binding.terminalId };
    });
  }
  async _unlink(binding, filename) {
    try { if (binding.mode === 'local') await fsp.unlink(filename); else await this._call(binding, 'unlink', filename); }
    catch (error) { if (!missing(error)) throw error; }
  }
  async _revision(binding, filename) { return (await this._read(binding, filename))?.revision ?? null; }
  async _commitRemote(binding, temporary, filename, existing, verify) {
    await verify();
    if (typeof binding.sftp.ext_openssh_rename === 'function') {
      try { await this._call(binding, 'ext_openssh_rename', temporary, filename); return {}; }
      catch (error) { if (!unsupported(error)) throw error; }
    }
    // SFTP v3 rename cannot replace a target on every server. Retain a backup
    // until the new file is in place; a failed rollback reports the backup path.
    await verify();
    if (!existing) { await this._call(binding, 'rename', temporary, filename); return {}; }
    const backup = this._temporary(binding, filename, 'bak');
    await this._call(binding, 'rename', filename, backup);
    try { await this._call(binding, 'rename', temporary, filename); }
    catch (error) {
      try { await this._call(binding, 'rename', backup, filename); }
      catch (_) { throw new Error(`Не удалось завершить запись. Исходный файл сохранён: ${backup}`); }
      throw error;
    }
    try { await this._unlink(binding, backup); return {}; }
    catch (_) { return { warning: `Запись завершена; резервная копия осталась: ${backup}`, backupPath: backup }; }
  }
  async _write(binding, filename, bytes, expectedRevision) {
    const existing = await this._read(binding, filename);
    checkRevision(expectedRevision, existing?.revision ?? null);
    const temporary = this._temporary(binding, filename);
    let warning = {};
    try {
      if (binding.mode === 'local') {
        const handle = await fsp.open(temporary, 'wx', existing ? existing.stats.mode & 0o777 : 0o600);
        try {
          await handle.writeFile(bytes);
          if (existing && process.platform !== 'win32') {
            const own = await handle.stat();
            if (own.uid !== existing.stats.uid || own.gid !== existing.stats.gid) await handle.chown(existing.stats.uid, existing.stats.gid);
            await handle.chmod(existing.stats.mode & 0o7777);
          }
          await handle.sync();
        } finally { await handle.close(); }
        checkRevision(expectedRevision, await this._revision(binding, filename));
        await fsp.rename(temporary, filename);
      } else {
        await this._call(binding, 'writeFile', temporary, bytes, { flag: 'wx', mode: existing ? existing.stats.mode & 0o7777 : 0o600 });
        if (existing) {
          // Abort instead of silently changing ownership when the server refuses.
          const created = await this._call(binding, 'lstat', temporary);
          if (created.uid !== existing.stats.uid || created.gid !== existing.stats.gid) await this._call(binding, 'chown', temporary, existing.stats.uid, existing.stats.gid);
          await this._call(binding, 'chmod', temporary, existing.stats.mode & 0o7777);
        }
        warning = await this._commitRemote(binding, temporary, filename, existing, async () => checkRevision(expectedRevision, await this._revision(binding, filename)));
      }
      return { ...warning, revision: await this._revision(binding, filename) };
    } finally { await this._unlink(binding, temporary).catch(() => {}); }
  }
  async writeText(request) {
    return this._mutation(async () => {
      validPath(request.path);
      const bytes = textBytes(request.text), binding = await this._binding(request), filename = await this._path(binding, request.path);
      const result = await this._write(binding, filename, bytes, request.revision);
      return { ...result, path: filename, mode: binding.mode, terminalId: binding.terminalId };
    });
  }
  async upload(request) {
    return this._mutation(async () => {
      if (request.mode !== 'sftp') throw new Error('Загрузка доступна в режиме SFTP');
      validPath(request.targetPath || request.path);
      const binding = await this._binding(request), source = path.resolve(validPath(request.localPath)), target = await this._path(binding, request.targetPath || request.path);
      const sourceStats = await fsp.lstat(source); checkOrdinary(sourceStats);
      const existing = await this._lstat(binding, target);
      if (existing) { checkOrdinary(existing); if (request.overwrite !== true) throw new Error('Подтвердите замену существующего файла'); }
      const temporary = this._temporary(binding, target);
      const verify = async () => {
        const current = await this._lstat(binding, target);
        if ((existing && (!current || !sameStats(existing, current))) || (!existing && current)) throw new Error('Файл назначения изменился во время передачи');
      };
      try {
        await this._call(binding, 'fastPut', source, temporary, { mode: existing ? existing.mode & 0o7777 : sourceStats.mode & 0o777 });
        if (!sameStats(sourceStats, await fsp.lstat(source))) throw new Error('Исходный файл изменился во время передачи');
        if (existing) {
          const created = await this._call(binding, 'lstat', temporary);
          if (created.uid !== existing.uid || created.gid !== existing.gid) await this._call(binding, 'chown', temporary, existing.uid, existing.gid);
          await this._call(binding, 'chmod', temporary, existing.mode & 0o7777);
        }
        const result = await this._commitRemote(binding, temporary, target, existing, verify);
        return { ...result, mode: binding.mode, terminalId: binding.terminalId, path: target, bytes: sourceStats.size };
      } finally { await this._unlink(binding, temporary).catch(() => {}); }
    });
  }
  async download(request) {
    return this._mutation(async () => {
      if (request.mode !== 'sftp') throw new Error('Скачивание доступно в режиме SFTP');
      validPath(request.remotePath || request.path);
      const binding = await this._binding(request), source = await this._path(binding, request.remotePath || request.path), target = path.resolve(validPath(request.localPath));
      const stats = await this._lstat(binding, source);
      if (!stats) throw new Error('Файл не найден');
      checkOrdinary(stats);
      const local = { mode: 'local' }, existing = await this._lstat(local, target);
      if (existing) { checkOrdinary(existing); if (request.overwrite !== true) throw new Error('Подтвердите замену существующего файла'); }
      const temporary = this._temporary(local, target);
      // Reserve the random destination before asking ssh2 to open it.
      const handle = await fsp.open(temporary, 'wx', existing ? existing.mode & 0o777 : 0o600); await handle.close();
      try {
        await this._call(binding, 'fastGet', source, temporary);
        const current = await this._lstat(binding, source), currentLocal = await this._lstat(local, target);
        if (!current || !sameStats(stats, current)) throw new Error('Удалённый файл изменился во время передачи');
        if ((existing && (!currentLocal || !sameStats(existing, currentLocal))) || (!existing && currentLocal)) throw new Error('Локальный файл изменился во время передачи');
        if (existing) {
          if (process.platform !== 'win32') await fsp.chown(temporary, existing.uid, existing.gid);
          await fsp.chmod(temporary, existing.mode & 0o7777);
        }
        const fd = await fsp.open(temporary, 'r+'); try { await fd.sync(); } finally { await fd.close(); }
        await fsp.rename(temporary, target);
        return { mode: binding.mode, terminalId: binding.terminalId, path: target, bytes: stats.size };
      } finally { await this._unlink(local, temporary).catch(() => {}); }
    });
  }
  async operate(request) {
    const methods = ['mkdir', 'rename', 'remove', 'readText', 'writeText', 'upload', 'download'];
    if (!methods.includes(request.action)) throw new Error('Неизвестная файловая операция');
    return this[request.action](request);
  }
  closeTerminal(terminalId) {
    const entry = this._connections.get(terminalId);
    this._connections.delete(terminalId); this._directories.delete(terminalId);
    if (entry?.sftp) { try { entry.sftp.end(); } catch (_) { /* close is idempotent */ } }
  }
  dispose() {
    for (const identifier of [...this._connections.keys()]) this.closeTerminal(identifier);
    this._directories.clear();
  }
}

module.exports = { FilesService, MAX_TEXT_BYTES };
