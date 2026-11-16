'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { dialog, Menu } = require('electron');
const { utils } = require('ssh2');
const { activeProfile } = require('./backend/backup.cjs');
const { CredentialVault } = require('./backend/vault.cjs');
const { IdentityManager } = require('./backend/identities.cjs');
const { Store } = require('./backend/store.cjs');

module.exports = async function checkBackup({ window, store, vault, identities, run, until, check }) {
  await check('file menu creates encrypted backup and restores a locked vault and OpenSSH key', async () => {
    const root = path.dirname(identities.filename);
    const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-backup-ui-'));
    const archive = path.join(fixture, 'copy.astraterm-backup');
    const password = 'Пароль резервной копии для теста';
    const phrase = 'Fixture OpenSSH passphrase';
    const key = utils.generateKeyPairSync('ed25519', { passphrase: phrase, cipher: 'aes256-ctr', rounds: 16 });
    const keyFile = path.join(fixture, 'fixture key');
    const credential = { id: 'backup-ui-account', name: 'Backup account', username: 'fixture', password: 'fixture-backup-secret' };
    const master = 'Backup UI vault password';
    const id = 'backup-ui-profile', sessionId = 'backup-ui-session';
    const original = { popup: Menu.prototype.popup, save: dialog.showSaveDialog,
      open: dialog.showOpenDialog, message: dialog.showMessageBox };
    let captured, saved, restored;
    try {
      fs.writeFileSync(keyFile, key.private);
      fs.writeFileSync(keyFile + '.pub', key.public);
      identities.upsert({ id, name: 'Backup key', key_file: keyFile });
      store.upsertSession({ id: sessionId, host: 'backup.test', identity_profile: id, credential_profile: credential.id });
      await vault.resetMaster(master);
      vault.merge([credential]);
      vault.lock();
      Menu.prototype.popup = function () { captured = this; };
      dialog.showSaveDialog = async () => ({ canceled: false, filePath: archive });
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [archive] });
      dialog.showMessageBox = async (_owner, options) => {
        if (options.message === 'Резервная копия сохранена.') saved = true;
        if (options.message === 'Профиль восстановлен.') restored = true;
        return { response: options.type === 'question' ? 1 : 0 };
      };
      const menu = async label => {
        captured = null;
        await run('document.querySelector("#menu").click()');
        await until(() => Boolean(captured), 'File menu opened');
        const file = captured.items.find(item => item.label === 'Файл').submenu;
        file.items.find(item => item.label === label).click();
      };
      const passwordInput = 'dialog[open] input[aria-label="Пароль резервной копии"]';
      const enterPassword = async instruction => {
        await until(() => run(`Boolean(document.querySelector(${JSON.stringify(passwordInput)})?.closest('dialog')
          .querySelector('.dialog-description').textContent.startsWith(${JSON.stringify(instruction)}))`), 'Backup password prompt opened');
        await run(`(()=>{const input=document.querySelector(${JSON.stringify(passwordInput)});input.value=${JSON.stringify(password)};input.closest('dialog').querySelector('button.primary').click()})()`);
      };
      await menu('Создать резервную копию…');
      await enterPassword('Задайте пароль');
      await enterPassword('Повторите пароль');
      await until(() => saved, 'Encrypted backup saved');
      assert(!fs.readFileSync(archive).includes(credential.password));
      await menu('Восстановить из резервной копии…');
      await enterPassword('Введите пароль');
      await until(() => restored, 'Backup profile restored');
      const next = activeProfile(root);
      assert.notEqual(next, root);
      assert.equal(path.dirname(identities.filename), root);
      const startup = new Store(next);
      const session = startup.snapshot().sessions.find(record => record.id === sessionId);
      assert.equal(session.identity_profile, id);
      assert.equal(session.credential_profile, credential.id);
      const nextIdentities = new IdentityManager({ directory: next });
      const restoredKey = nextIdentities.list().profiles.find(record => record.id === id).key_file;
      nextIdentities.dispose();
      fs.unlinkSync(keyFile);
      assert(!(utils.parseKey(fs.readFileSync(restoredKey), phrase) instanceof Error));
      const restoredVault = new CredentialVault(path.join(next, 'credentials.vault.json'));
      await restoredVault.unlock(master);
      assert.deepEqual(restoredVault.get(credential.id), credential);
      restoredVault.lock();
    } finally {
      Menu.prototype.popup = original.popup;
      dialog.showSaveDialog = original.save;
      dialog.showOpenDialog = original.open;
      dialog.showMessageBox = original.message;
      fs.rmSync(fixture, { recursive: true, force: true });
    }
  });
};
