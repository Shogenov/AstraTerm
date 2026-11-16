'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { utils } = require('ssh2');
const { activeProfile, captureProfile, writeBackup, readBackup, restoreProfile } = require('../src/backend/backup.cjs');
const { CredentialVault } = require('../src/backend/vault.cjs');
const { Store } = require('../src/backend/store.cjs');
const { IdentityManager } = require('../src/backend/identities.cjs');
const contract = require('../../shared/contracts/backup.json');

test('portable backup restores locked vault, links and protected SSH key files', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-backup-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, 'source'), target = path.join(root, 'target'), archive = path.join(root, 'fixture.astraterm-backup');
  fs.mkdirSync(source); fs.mkdirSync(target);
  const keyFile = path.join(source, 'Testing key'), missing = path.join(source, 'missing key');
  const phrase = 'Synthetic key passphrase';
  const key = utils.generateKeyPairSync('ed25519', { passphrase: phrase, cipher: 'aes256-ctr', rounds: 16 });
  const certificate = 'ssh-ed25519-cert-v01@openssh.com synthetic-fixture\n';
  fs.writeFileSync(keyFile, key.private);
  fs.writeFileSync(keyFile + '.pub', key.public);
  fs.writeFileSync(keyFile + '-cert.pub', certificate);
  for (const [name, field] of [['sessions.json', 'sessions'], ['ssh-profiles.json', 'identities'],
    ['sidebar.json', 'sidebar'], ['logging.json', 'logging']]) {
    fs.writeFileSync(path.join(source, name), JSON.stringify(contract[field], (_key, value) =>
      value === '$KEY' ? keyFile : value === '$MISSING' ? missing : value));
  }
  const vault = new CredentialVault(path.join(source, 'credentials.vault.json'));
  await vault.unlock(contract.master_password, { create: true });
  vault.merge([contract.credential]);
  vault.lock();
  const old = path.join(target, 'sessions.json');
  fs.writeFileSync(old, '{"sessions":[]}');
  const payload = captureProfile(source);
  assert.deepEqual(payload.missing, [missing]);
  assert.equal(Object.keys(payload.keys).length, 1);
  await writeBackup(archive, payload, contract.password, source);
  const ciphertext = fs.readFileSync(archive, 'utf8');
  assert(!ciphertext.includes('PRIVATE KEY') && !ciphertext.includes('backup.test'));
  await assert.rejects(readBackup(archive, 'incorrect password'), /Неверный пароль/);
  assert.equal(activeProfile(target), target);

  const next = await restoreProfile(target, target, await readBackup(archive, contract.password));
  assert.equal(activeProfile(target), next);
  assert.equal(fs.readFileSync(old, 'utf8'), '{"sessions":[]}');
  const identities = new IdentityManager({ directory: next });
  t.after(() => identities.dispose());
  const profile = identities.list().profiles[0];
  assert.equal(fs.readFileSync(profile.key_file, 'utf8'), key.private);
  assert.equal(fs.readFileSync(profile.key_file + '.pub', 'utf8'), key.public);
  assert.equal(fs.readFileSync(profile.key_file + '-cert.pub', 'utf8'), certificate);
  assert(!(utils.parseKey(fs.readFileSync(profile.key_file), phrase) instanceof Error));
  const store = new Store(next);
  const document = store.snapshot();
  assert.equal(document.sessions[0].key_file, profile.key_file);
  assert.equal(document.groups[0].ssh.key_file, profile.key_file);
  assert.equal(document.tunnels[0].key_file, profile.key_file);
  assert.equal(document.macros[0].text, 'date');
  assert.equal(identities.list().default_id, contract.identities.default_id);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(next, 'sidebar.json'), 'utf8')), contract.sidebar);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(next, 'logging.json'), 'utf8')), contract.logging);
  const restoredVault = new CredentialVault(path.join(next, 'credentials.vault.json'));
  await restoredVault.unlock(contract.master_password);
  assert.deepEqual(restoredVault.get(document.sessions[0].credential_profile, 'ssh'), contract.credential);
  restoredVault.lock();
  await assert.rejects(restoreProfile(target, target, payload), /Активный профиль изменился/);
  assert.deepEqual(fs.readdirSync(path.dirname(next)), [path.basename(next)]);
  assert.equal(activeProfile(target), next);

  await assert.rejects(writeBackup(archive, payload, contract.password, source), /Файл уже существует/);
  assert.equal(fs.readFileSync(archive, 'utf8'), ciphertext);
  const damaged = JSON.parse(ciphertext);
  damaged.tag = (damaged.tag[0] === 'A' ? 'B' : 'A') + damaged.tag.slice(1);
  fs.writeFileSync(archive, JSON.stringify(damaged));
  await assert.rejects(readBackup(archive, contract.password), /повреждён/);
});
