'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { IdentityManager } = require('../src/backend/identities.cjs');
const { HostTrustStore, fingerprint, knownHostKeys } = require('../src/backend/host-trust.cjs');
const { utils, BaseAgent } = require('ssh2');

test('generated SSH keys follow the shared contract and never overwrite files', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-keygen-'));
  const manager = new IdentityManager({ directory, sshDirectory: directory, agentPath: path.join(directory, 'missing-agent') });
  t.after(() => { manager.dispose(); fs.rmSync(directory, { recursive: true, force: true }); });
  const contract = require('../../shared/contracts/sessions.json').key_generation;
  for (const { filename, algorithm, public_type, protected: encrypted } of contract.algorithms) {
    const passphrase = encrypted ? 'synthetic-key-phrase' : '';
    const args = { name: 'Synthetic key', filename, algorithm, passphrase };
    const profile = await manager.generate(args);
    const privateKey = fs.readFileSync(profile.key_file);
    assert.equal(utils.parseKey(privateKey) instanceof Error, encrypted);
    const parsed = utils.parseKey(privateKey, passphrase || undefined);
    assert.equal(parsed.type, public_type);
    assert.equal(utils.parseKey(manager.publicKey(profile.id)).getPublicSSH().equals(parsed.getPublicSSH()), true);
    await assert.rejects(manager.generate(args), /уже существует/);
    assert.deepEqual(fs.readFileSync(profile.key_file), privateKey);
  }
  assert.equal(manager.data.default_id, '');
  assert.doesNotMatch(fs.readFileSync(manager.filename, 'utf8'), /synthetic-key-phrase|BEGIN OPENSSH/);
  for (const filename of contract.invalid_filenames)
    await assert.rejects(manager.generate({ name: 'Rejected', filename, algorithm: 'ed25519', passphrase: '' }));
  const file = path.join(directory, 'id_public_only.pub');
  fs.writeFileSync(file, 'keep');
  const input = { name: 'Collision', filename: 'id_public_only', algorithm: 'ed25519', passphrase: '' };
  await assert.rejects(manager.generate(input), /уже существует/);
  assert.equal(fs.readFileSync(file, 'utf8'), 'keep');
  assert.equal(fs.existsSync(file.slice(0, -4)), false);
  t.mock.method(manager, 'upsert', () => { throw Error('fixture save failure'); });
  await assert.rejects(manager.generate({ ...input, filename: 'id_rollback' }));
  assert.ok(fs.readdirSync(directory).every(name => !name.startsWith('id_rollback') && !name.startsWith('.astraterm-key-')));
});

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-identity-'));
  const key = crypto.generateKeyPairSync('rsa', { modulusLength: 2048, publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs1', format: 'pem', cipher: 'aes-256-cbc', passphrase: 'synthetic test passphrase' } });
  const filename = path.join(directory, 'id_rsa');
  fs.writeFileSync(filename, key.privateKey);
  const parsed = utils.parseKey(key.privateKey, 'synthetic test passphrase');
  fs.writeFileSync(filename + '.pub', parsed.type + ' ' + parsed.getPublicSSH().toString('base64') + '\n');
  return { directory, filename, parsed };
}

test('encrypted selected key unlocks once, metadata contains no secret, disposed PEM is wiped', async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  let prompts = 0;
  const manager = new IdentityManager({ directory: data.directory, sshDirectory: data.directory, agentPath: path.join(data.directory, 'missing-agent'), prompt: async request => { assert.equal(request.kind, 'passphrase'); prompts++; return 'synthetic test passphrase'; } });
  t.after(() => manager.dispose());
  const profile = manager.upsert({ name: 'Synthetic', key_file: data.filename }, { makeDefault: true });
  assert.equal(manager.list().keys.includes(data.filename), true);
  const [one, two] = await Promise.all([manager.ensure({}), manager.ensure({ identity_profile: profile.id })]);
  assert.equal(prompts, 1);
  assert.equal(one.privateKey, two.privateKey);
  assert.equal(utils.parseKey(one.privateKey).getPublicSSH().equals(data.parsed.getPublicSSH()), true);
  assert.equal((await manager.ensure({})).privateKey, one.privateKey);
  assert.equal(prompts, 1);
  const metadata = fs.readFileSync(path.join(data.directory, 'ssh-profiles.json'), 'utf8');
  assert.doesNotMatch(metadata, /PRIVATE KEY|synthetic test passphrase/);
  assert.equal(manager.list().unlocked.includes(profile.id), true);
  manager.dispose();
  assert.equal(one.privateKey.every(byte => byte === 0), true);
});

test('missing explicit identity fails closed, canceled and wrong passphrases do not cache key', async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  let answer = null;
  const manager = new IdentityManager({ directory: data.directory, agentPath: path.join(data.directory, 'missing-agent'), prompt: async () => answer });
  t.after(() => manager.dispose());
  const profile = manager.upsert({ name: 'Synthetic', key_file: data.filename });
  await assert.rejects(manager.ensure({ identity_profile: 'nonexistent' }), /недоступен/);
  await assert.rejects(manager.ensure({ identity_profile: profile.id }), /отменена/);
  answer = 'incorrect fixture password';
  await assert.rejects(manager.ensure({}), /passphrase/);
  assert.equal(manager.cache.size, 0);
  manager.remove(profile.id);
  assert.equal(manager.list().profiles.length, 0);
  assert.equal(manager.setDefault('system').default_id, 'system');
  assert.equal((await manager.ensure({})).identityId, 'system');
});

test('legacy MobaXterm key paths unlock from the Windows Documents directory', { skip: process.platform !== 'win32' }, async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  let prompts = 0;
  const manager = new IdentityManager({
    directory: data.directory,
    documents: data.directory,
    agentPath: path.join(data.directory, 'missing-agent'),
    prompt: async () => { prompts++; return 'synthetic test passphrase'; },
  });
  t.after(() => manager.dispose());
  const imported = await manager.ensure({ key_file: '_MyDocuments_\\id_rsa' });
  assert.ok(utils.parseKey(imported.privateKey).getPublicSSH().equals(data.parsed.getPublicSSH()));
  assert.equal((await manager.ensure({ key_file: data.filename })).privateKey, imported.privateKey);
  assert.equal(prompts, 1);
  await assert.rejects(manager.ensure({ key_file: '_MyDocuments_\\missing' }), /Не найден файл SSH-ключа/);
  await assert.rejects(manager.ensure({ key_file: '_CurrentDrive_\\id_rsa' }), /Путь ключа MobaXterm/);
});

test('server trust requires confirmation, survives restart, changed and revoked keys are blocked', async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  const knownHostsPath = path.join(data.directory, 'known_hosts');
  const key = data.parsed.getPublicSSH();
  let calls = 0;
  const store = new HostTrustStore({ directory: data.directory, knownHostsPath, prompt: async request => { assert.equal(request.fingerprint, fingerprint(key)); calls++; return true; } });
  assert.equal(await store.verify({ host: 'synthetic.invalid', key }), true);
  assert.equal(await store.verify({ host: 'synthetic.invalid', key }), true);
  assert.equal(calls, 1);
  const restarted = new HostTrustStore({ directory: data.directory, knownHostsPath, prompt: async () => { throw new Error('unexpected prompt'); } });
  assert.equal(await restarted.verify({ host: 'synthetic.invalid', key }), true);
  const altered = Buffer.from(key); altered[altered.length - 1] ^= 1;
  await assert.rejects(restarted.verify({ host: 'synthetic.invalid', key: altered }), /изменился/);
  fs.writeFileSync(knownHostsPath, `@revoked synthetic.invalid ${data.parsed.type} ${key.toString('base64')}\n`);
  await assert.rejects(restarted.verify({ host: 'synthetic.invalid', key }), /отозван/);
});

test('OpenSSH plain and hashed known_hosts are respected without new trust prompts', async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  const knownHostsPath = path.join(data.directory, 'known_hosts');
  const key = data.parsed.getPublicSSH();
  const target = '[localhost]:2234';
  const salt = crypto.randomBytes(20);
  const hash = crypto.createHmac('sha1', salt).update(target).digest('base64');
  fs.writeFileSync(knownHostsPath, `|1|${salt.toString('base64')}|${hash} ${data.parsed.type} ${key.toString('base64')}\n*.synthetic.invalid,!denied.synthetic.invalid ${data.parsed.type} ${key.toString('base64')}\n`);
  assert.equal(knownHostKeys(knownHostsPath, target).length, 1);
  assert.equal(knownHostKeys(knownHostsPath, 'host.synthetic.invalid').length, 1);
  assert.equal(knownHostKeys(knownHostsPath, 'denied.synthetic.invalid').length, 0);
  const store = new HostTrustStore({ directory: data.directory, knownHostsPath, prompt: async () => { throw new Error('unexpected prompt'); } });
  assert.equal(await store.verify({ host: 'localhost', port: 2234, key }), true);
  const altered = Buffer.from(key); altered[altered.length - 2] ^= 1;
  await assert.rejects(store.verify({ host: 'localhost', port: 2234, key: altered }), /known_hosts/);
});

test('already-loaded agent offers only selected identity without an unlock or decrypted key cache', async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  const unrelated = utils.parseKey(utils.generateKeyPairSync('ed25519').private);
  class SyntheticAgent extends BaseAgent {
    constructor() { super(); this.keys = [unrelated, data.parsed]; this.signs = 0; }
    getIdentities(callback) { callback(null, this.keys); }
    sign(key, payload, options, callback) { this.signs++; callback(null, Buffer.from('synthetic signature')); }
  }
  const agent = new SyntheticAgent();
  let prompts = 0;
  const manager = new IdentityManager({ directory: data.directory, agentPath: path.join(data.directory, 'synthetic-agent'), prompt: async () => { prompts++; return 'synthetic test passphrase'; } });
  manager.agent = agent;
  t.after(() => manager.dispose());
  const profile = manager.upsert({ name: 'Synthetic agent', key_file: data.filename });
  const selected = await manager.ensure({});
  assert.equal(selected.unlockScope, 'windows-agent');
  assert.equal(selected.privateKey, undefined);
  assert.equal(manager.cache.size, 0);
  assert.equal(manager.list().unlocked_scopes[profile.id], 'windows-agent');
  const keys = await new Promise((resolve, reject) => selected.agent.getIdentities((error, keys) => error ? reject(error) : resolve(keys)));
  assert.equal(keys.length, 1);
  assert.equal(keys[0].getPublicSSH().equals(data.parsed.getPublicSSH()), true);
  await assert.rejects(new Promise((resolve, reject) => selected.agent.sign(unrelated, Buffer.from('test'), {}, (error, signature) => error ? reject(error) : resolve(signature))), /другой ключ/);
  assert.equal(agent.signs, 0);
  const restarted = new IdentityManager({ directory: data.directory, agentPath: path.join(data.directory, 'synthetic-agent'), prompt: async () => { throw new Error('unexpected prompt'); } });
  restarted.agent = agent;
  t.after(() => restarted.dispose());
  assert.equal((await restarted.ensure({})).unlockScope, 'windows-agent');
  assert.equal(prompts, 0);
  assert.equal(agent.keys.length, 2);
});

test('available agent without selected identity is never mutated and unlocked key stays in application memory', async t => {
  const data = fixture();
  t.after(() => fs.rmSync(data.directory, { recursive: true, force: true }));
  const unrelated = utils.parseKey(utils.generateKeyPairSync('ed25519').private);
  const agent = new BaseAgent();
  agent.getIdentities = callback => callback(null, [unrelated]);
  const manager = new IdentityManager({ directory: data.directory, agentPath: path.join(data.directory, 'synthetic-agent'), prompt: async () => 'synthetic test passphrase' });
  manager.agent = agent;
  t.after(() => manager.dispose());
  const profile = manager.upsert({ name: 'Synthetic local cache', key_file: data.filename });
  const selected = await manager.ensure({});
  assert.equal(selected.unlockScope, 'application');
  assert.equal(Buffer.isBuffer(selected.privateKey), true);
  assert.equal(manager.list().unlocked_scopes[profile.id], 'application');
  assert.equal(manager.cache.size, 1);
  assert.equal(typeof manager._addToWindowsAgent, 'undefined');
  const keys = await new Promise(resolve => agent.getIdentities((error, keys) => resolve(keys)));
  assert.equal(keys.length, 1);
  assert.equal(keys[0], unrelated);
});

test('explicit profile wins, otherwise direct key wins default and system mode only uses agent without a key file', async t => {
  const one = fixture(), two = fixture();
  t.after(() => { fs.rmSync(one.directory, { recursive: true, force: true }); fs.rmSync(two.directory, { recursive: true, force: true }); });
  let prompts = 0;
  const manager = new IdentityManager({ directory: one.directory, agentPath: path.join(one.directory, 'missing-agent'), prompt: async () => { prompts++; return 'synthetic test passphrase'; } });
  t.after(() => manager.dispose());
  const profile = manager.upsert({ name: 'Default key', key_file: one.filename });
  const publicOf = result => utils.parseKey(result.privateKey).getPublicSSH();
  assert.equal(publicOf(await manager.ensure({ key_file: two.filename })).equals(two.parsed.getPublicSSH()), true);
  assert.equal(publicOf(await manager.ensure({ identity_profile: 'system', key_file: two.filename })).equals(two.parsed.getPublicSSH()), true);
  assert.equal(publicOf(await manager.ensure({ identity_profile: profile.id, key_file: two.filename })).equals(one.parsed.getPublicSSH()), true);
  assert.equal(publicOf(await manager.ensure({})).equals(one.parsed.getPublicSSH()), true);
  assert.equal((await manager.ensure({ identity_profile: 'system' })).privateKey, undefined);
  await assert.rejects(manager.ensure({ identity_profile: 'missing', key_file: two.filename }), /недоступен/);
  assert.equal(prompts, 2);
});
test('the same unlocked file is reused through profile and own-file choices without sharing wipe buffers',async t=>{
 const data=fixture();t.after(()=>fs.rmSync(data.directory,{recursive:true,force:true}));let prompts=0;
 const manager=new IdentityManager({directory:data.directory,agentPath:path.join(data.directory,'missing-agent'),prompt:async()=>{prompts++;return 'synthetic test passphrase';}});t.after(()=>manager.dispose());
 const profile=manager.upsert({name:'Named fixture',key_file:data.filename});const named=await manager.ensure({identity_profile:profile.id});
 const own=await manager.ensure({identity_profile:'system',key_file:data.filename});assert.equal(prompts,1);assert.deepEqual(own.privateKey,named.privateKey);assert.notEqual(own.privateKey,named.privateKey);
 manager._clear(profile.id);assert.ok(own.privateKey.some(byte=>byte!==0));
});
