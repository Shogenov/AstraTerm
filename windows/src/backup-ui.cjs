'use strict';

const { BACKUP_PARAMETERS } = require('./shared.cjs');
const { captureProfile, writeBackup, readBackup, restoreProfile } = require('./backend/backup.cjs');
const extension = BACKUP_PARAMETERS.extension;
const filters = [{ name: 'Резервная копия AstraTerm', extensions: [extension] }];

function missingText(missing) {
  const lines = missing.slice(0, 8);
  if (missing.length > lines.length) lines.push('Ещё файлов: ' + (missing.length - lines.length));
  return lines.join('\n');
}

class BackupDialogs {
  constructor({ window, dialog, root, directory, documents, prompt, confirm, isClosing, flushSettings }) {
    Object.assign(this, { window, dialog, root, directory, documents, prompt, confirm, isClosing, flushSettings });
    this.busy = false;
  }

  async run(create) {
    if (this.busy || this.isClosing()) return;
    this.busy = true;
    try { await (create ? this.create() : this.restore()); }
    finally { this.busy = false; }
  }

  password(message) {
    return this.prompt({ kind: 'backup-password', title: 'Пароль резервной копии',
      label: 'Пароль резервной копии', secret: true, message });
  }

  async create() {
    const timestamp = new Date().toISOString().slice(0, 19).replace(/:/g, '-');
    const selected = await this.dialog.showSaveDialog(this.window, {
      title: 'Создать резервную копию', defaultPath: 'AstraTerm-' + timestamp + '.' + extension, filters,
    });
    if (selected.canceled || !selected.filePath || this.isClosing()) return;
    const filename = selected.filePath.toLowerCase().endsWith('.' + extension)
      ? selected.filePath : selected.filePath + '.' + extension;
    await this.flushSettings();
    const payload = captureProfile(this.directory, this.documents);
    if (payload.missing.length && !await this.confirm('Сохранить копию без недоступных файлов SSH-ключей?',
        missingText(payload.missing), 'Сохранить копию')) return;
    const password = await this.password('Задайте пароль копии, не менее 10 символов. Он понадобится при восстановлении.');
    if (password === null || this.isClosing()) return;
    if (Array.from(password).length < 10) throw Error('Пароль резервной копии должен содержать не менее 10 символов.');
    const repeated = await this.password('Повторите пароль резервной копии.');
    if (repeated === null || this.isClosing()) return;
    if (password !== repeated) throw Error('Пароли не совпадают.');
    await writeBackup(filename, payload, password, this.root);
    if (!this.isClosing()) await this.dialog.showMessageBox(this.window, {
      type: 'info', title: 'AstraTerm', message: 'Резервная копия сохранена.',
    });
  }

  async restore() {
    const selected = await this.dialog.showOpenDialog(this.window, {
      title: 'Восстановить из резервной копии', properties: ['openFile'], filters,
    });
    if (selected.canceled || !selected.filePaths[0] || this.isClosing()) return;
    const password = await this.password('Введите пароль, указанный при создании резервной копии.');
    if (password === null || this.isClosing()) return;
    const payload = await readBackup(selected.filePaths[0], password);
    const connections = payload.documents['sessions.json'].sessions.length;
    let detail = `Подключений: ${connections}. Файлов SSH-ключей: ${Object.keys(payload.keys).length}.\n\n` +
      'При следующем запуске AstraTerm откроет профиль из копии. Текущий профиль сохранится отдельно.';
    if (payload.documents['credentials.vault.json']) {
      detail += '\nДля хранилища понадобится мастер-пароль с исходного компьютера.';
    }
    if (payload.missing.length) detail += '\n\nВ копии отсутствуют файлы SSH-ключей:\n' + missingText(payload.missing);
    if (!await this.confirm('Восстановить профиль из резервной копии?', detail, 'Восстановить') || this.isClosing()) return;
    await restoreProfile(this.root, this.directory, payload);
    if (!this.isClosing()) await this.dialog.showMessageBox(this.window, {
      type: 'info', title: 'AstraTerm', message: 'Профиль восстановлен.',
      detail: 'Закройте и снова откройте AstraTerm, чтобы загрузить его.',
    });
  }
}

module.exports = { BackupDialogs };
