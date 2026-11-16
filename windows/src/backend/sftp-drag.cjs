'use strict';

const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const STAGING_LIFETIME = 10 * 60 * 1000;

class SftpDrag {
  constructor(files) {
    this.files = files;
    this.entries = new Map();
    this.pending = new Set();
    this.closed = false;
  }

  async prepare(request, check) {
    if (this.closed || request.mode !== 'sftp' || typeof request.path !== 'string' ||
        !path.posix.isAbsolute(request.path)) throw Error('Выберите файл в SFTP.');
    const name = path.posix.basename(request.path);
    if (!name || /[<>:"/\\|?*\x00-\x1f]|[. ]$/.test(name) ||
        /^(?:con|prn|aux|nul|(?:com|lpt)[1-9¹²³])(?:\.|$)/i.test(name)) {
      throw Error('Это имя файла недопустимо в Windows. Скачайте файл с другим именем.');
    }
    const task = this._prepare(request, name, check);
    this.pending.add(task);
    try { return await task; } finally { this.pending.delete(task); }
  }

  async _prepare(request, name, check) {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'astraterm-drag-'));
    const filename = path.join(directory, name);
    try {
      check();
      await this.files.download({ ...request, remotePath: request.path, localPath: filename, overwrite: false });
      check();
      if (this.closed) throw Error('Приложение закрывается.');
      const token = randomUUID();
      const entry = { directory, filename, check };
      this.entries.set(token, entry);
      this._renew(token, entry);
      return token;
    } catch (error) {
      await this._remove({ directory, filename });
      throw error;
    }
  }

  start(token, drag) {
    const entry = this.entries.get(token);
    if (!entry || this.closed) return false;
    entry.check();
    this._renew(token, entry);
    drag(entry.filename);
    return true;
  }

  owns(filename) {
    return [...this.entries.values()].some(entry => entry.filename === filename);
  }

  _renew(token, entry) {
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      this.entries.delete(token);
      void this._remove(entry);
    }, STAGING_LIFETIME);
    entry.timer.unref();
  }

  async _remove(entry) {
    clearTimeout(entry.timer);
    await fs.unlink(entry.filename).catch(() => {});
    await fs.rmdir(entry.directory).catch(() => {});
  }

  async dispose() {
    this.closed = true;
    await Promise.allSettled(this.pending);
    await Promise.all([...this.entries.values()].map(entry => this._remove(entry)));
    this.entries.clear();
  }
}

module.exports = { SftpDrag };
