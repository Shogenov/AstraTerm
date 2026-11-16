'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {CredentialVault}=require('../src/backend/vault.cjs');
const vector=require('../../shared/contracts/vault.json');
const typed=require('../../shared/contracts/credential-types.json');
function temporary(t){const directory=fs.mkdtempSync(path.join(os.tmpdir(),'astraterm-vault-test-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));return path.join(directory,'vault.json');}
for(const field of ['document','windows_document'])test('vault reads native '+field+' with shared authenticated encryption',async t=>{
  const filename=temporary(t);fs.writeFileSync(filename,JSON.stringify(vector[field]));const vault=new CredentialVault(filename);
  await assert.rejects(vault.unlock('TEST_ONLY_wrong_master'));assert.equal(vault.unlocked,false);
  await vault.unlock(vector.master);assert.deepEqual(vault.get('fixture'),vector.credential);assert.ok(!JSON.stringify(vault.metadata()).includes('password'));
  vault.lock();assert.throws(()=>vault.get('fixture'));
});
test('vault survives restart, uses fresh nonces and refuses a stale writer atomically',async t=>{
  const filename=temporary(t),vault=new CredentialVault(filename);await vault.unlock(vector.master,{create:true});vault.merge([vector.credential]);
  const first=JSON.parse(fs.readFileSync(filename));assert.ok(!fs.readFileSync(filename,'utf8').includes(vector.credential.password));
  const other=new CredentialVault(filename);await other.unlock(vector.master);vault.merge([vector.credential]);
  assert.notEqual(first.nonce,JSON.parse(fs.readFileSync(filename)).nonce);const before=fs.readFileSync(filename);
  assert.throws(()=>other.merge([]));assert.deepEqual(fs.readFileSync(filename),before);
  vault.lock();await vault.unlock(vector.master);assert.deepEqual(vault.get('fixture'),vector.credential);
});
test('tampering, absent profiles and lock during PBKDF2 are rejected',async t=>{
  const filename=temporary(t);fs.writeFileSync(filename,JSON.stringify({...vector.document,tag:'AAAAAAAAAAAAAAAAAAAAAA=='}));
  const vault=new CredentialVault(filename);await assert.rejects(vault.unlock(vector.master));assert.equal(vault.unlocked,false);
  fs.unlinkSync(filename);const pending=vault.unlock(vector.master,{create:true});vault.lock();await assert.rejects(pending);assert.equal(vault.unlocked,false);
  await vault.unlock(vector.master,{create:true});assert.throws(()=>vault.get('missing'));
});
test('shared credential types survive encryption, retain legacy links and delete atomically',async t=>{
 const filename=temporary(t),vault=new CredentialVault(filename);await vault.unlock(vector.master,{create:true});vault.merge(typed.records);vault.lock();await vault.unlock(vector.master);
 for(const [id,protocol]of typed.allowed)assert.equal(vault.get(id,protocol).id,id);
 for(const [id,protocol]of typed.rejected)assert.throws(()=>vault.get(id,protocol));
 assert.ok(!JSON.stringify(vault.metadata()).includes('password'));const before=fs.readFileSync(filename);
 assert.throws(()=>vault.merge([{...typed.records[0],protocol:'telnet'}]));assert.deepEqual(fs.readFileSync(filename),before);
 vault.remove('ssh-only');vault.lock();await vault.unlock(vector.master);assert.deepEqual(vault.metadata().map(p=>p.id).sort(),['legacy','rdp-only']);
});
test('master change re-encrypts profiles and locked reset creates an empty vault', async t => {
  const filename = temporary(t);
  fs.writeFileSync(filename, JSON.stringify(vector.document));
  const vault = new CredentialVault(filename);
  const masters = vector.master_management;
  await vault.unlock(vector.master);
  const before = fs.readFileSync(filename);
  await assert.rejects(vault.changeMaster(masters.wrong, masters.changed), /Неверный мастер-пароль/);
  assert.deepEqual(fs.readFileSync(filename), before);
  assert.deepEqual(vault.get('fixture'), vector.credential);
  const other = new CredentialVault(filename);
  await other.unlock(vector.master);
  vault.lock();
  await vault.changeMaster(vector.master, masters.changed);
  assert.deepEqual(vault.get('fixture'), vector.credential);
  assert.notEqual(JSON.parse(before).salt, JSON.parse(fs.readFileSync(filename)).salt);
  other.reloadIfChanged();
  assert.equal(other.unlocked, false);
  await assert.rejects(other.unlock(vector.master));
  await other.unlock(masters.changed);
  assert.deepEqual(other.get('fixture'), vector.credential);
  vault.lock();
  const canceled = vault.resetMaster(masters.reset);
  vault.lock();
  await assert.rejects(canceled, /Состояние хранилища изменилось/);
  await other.unlock(masters.changed);
  await vault.resetMaster(masters.reset);
  assert.equal(vault.unlocked, true);
  assert.deepEqual(vault.metadata(), []);
  await assert.rejects(other.unlock(masters.changed));
  vault.lock();
  await vault.unlock(masters.reset);
  assert.deepEqual(vault.metadata(), []);
});
test('reset can replace an unreadable vault', async t => {
  const filename = temporary(t);
  fs.writeFileSync(filename, 'invalid vault envelope');
  const vault = new CredentialVault(filename);
  await vault.resetMaster(vector.master_management.reset);
  vault.lock();
  await vault.unlock(vector.master_management.reset);
  assert.deepEqual(vault.metadata(), []);
});
