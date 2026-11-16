'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {parseImport,credentialId,stableId}=require('../src/backend/importers.cjs');
const {Store}=require('../src/backend/store.cjs');
const contract=require('../../shared/contracts/imports.json');
function fixture(directory,files){return Object.entries(files).map(([name,text])=>{const target=path.join(directory,name);fs.writeFileSync(target,text);return target;});}
function temporary(t){const directory=fs.mkdtempSync(path.join(os.tmpdir(),'astraterm-import-test-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));return directory;}
for(const scenario of contract.cases)test('shared import: '+scenario.name,t=>{
  const plan=parseImport(fixture(temporary(t),scenario.files)),preview=plan.preview();
  for(const key of ['sessions','credentials','groups','missing'])assert.equal(Array.isArray(preview[key])?preview[key].length:preview[key],scenario[key]);
  for(const [key,value]of Object.entries(scenario.first||{}))assert.equal((scenario.expected_session?plan.sessions.find(item=>item.name===scenario.expected_session):plan.sessions[0])[key],value);
  for(const key of ['macros','tunnels'])if(scenario[key]!==undefined)assert.equal(plan.document()[key].length,scenario[key]);
  const document=JSON.stringify(plan.document());
  for(const credential of plan.credentials.values()){assert.ok(!document.includes(credential.password));assert.ok(!JSON.stringify(preview).includes(credential.password));}
  assert.ok(!JSON.stringify(preview).includes('"password":'));
});
test('profile identity survives either file order; exact host/user/port scope',t=>{
  const paths=fixture(temporary(t),contract.cases[0].files),plan=parseImport(paths),reverse=parseImport([...paths].reverse());
  assert.deepEqual(plan.document(),reverse.document());assert.deepEqual(plan.credentials,reverse.credentials);
  const [web,db,edge,rdp,missing]=plan.sessions;
  assert.equal(web.credential_profile,db.credential_profile);assert.equal(web.credential_profile,credentialId('Shared'));
  assert.equal(plan.credentials.get(web.credential_profile).password,'TEST_ONLY_alpha=equals ');
  assert.equal(plan.credentials.get(rdp.credential_profile).username,'LAB\\operator');
  assert.notEqual(edge.credential_profile,stableId('mobaxterm','endpoint','ssh:22:ops@edge.example.invalid'));
  assert.equal(plan.credentials.has(missing.credential_profile),false);
});
test('MobaXterm Documents paths follow the Windows folder, and unresolved tokens keep their warning', t => {
  const documents = 'D:\\Profiles\\Документы';
  const bookmark = (name, keyFile) => {
    const fields = Array(37).fill('');
    Object.assign(fields, { 0: '0', 1: 'node.example.invalid', 2: '22', 3: 'ops', 14: keyFile, 19: '0' });
    return name + '=#109#' + fields.join('%') + '#font#0# #-1';
  };
  const paths = fixture(temporary(t), { 'paths.mxtsessions': [
    '[Bookmarks]', 'SubRep=Lab', 'ImgNum=41',
    bookmark('Redirected', '_MyDocuments_\\ssh\\id_rsa'),
    bookmark('Traversal', '_MyDocuments_/../id_rsa'),
    bookmark('Drive', '_CurrentDrive_\\ssh\\id_rsa'),
    bookmark('Explicit', 'E:\\keys\\id_rsa'),
  ].join('\n') });
  const [mapped, traversal, drive, explicit] = parseImport(paths, { documents }).sessions;
  assert.equal(mapped.key_file, documents + '\\ssh\\id_rsa');
  assert.equal(mapped.import_warning, '');
  assert.match(traversal.import_warning, /Путь ключа MobaXterm/);
  assert.equal(traversal.key_file, '_MyDocuments_/../id_rsa');
  assert.match(drive.import_warning, /Путь ключа MobaXterm/);
  assert.equal(explicit.key_file, 'E:\\keys\\id_rsa');
  assert.equal(parseImport(paths).sessions[0].key_file, '_MyDocuments_\\ssh\\id_rsa');
});
test('reimport is idempotent and retains existing folder defaults',t=>{
  const directory=temporary(t),plan=parseImport(fixture(directory,contract.cases[0].files)),store=new Store(path.join(directory,'app'));
  store.upsertGroup({name:'Lab',ssh:{username:'owner'}});store.importDocument(plan.document(store));const ids=store.snapshot().sessions.map(item=>item.id);
  store.importDocument(plan.document(store));assert.deepEqual(store.snapshot().sessions.map(item=>item.id),ids);
  assert.equal(store.snapshot().groups.find(item=>item.name==='Lab').ssh.username,'owner');assert.ok(store.snapshot().sessions.every(item=>!item.inherit_group_ssh));
});
test('conflicts, executable YAML, duplicate fields, malformed CSV and ProxyCommand fail closed',t=>{
  const directory=temporary(t);
  for(const [name,text]of [
    ['passwords.txt','Shared(ops) = TEST_ONLY_first\nShared(ops) = TEST_ONLY_other\n'],
    ['asbru.yml','!!python/object/apply:os.system [echo UNSAFE]\n'],
    ['asbru.yml','__PAC__ROOT__: {}\na: {name: first, name: second}\n'],
    ['hosts.csv','Address,Username,Username\nnode.example.invalid,ops,other\n'],
    ['hosts.csv','Address,Label\nnode.example.invalid,"unterminated\n'],
    ['ssh_config','Host node\n HostName node.example.invalid\n ProxyCommand echo UNSAFE\n'],
    ['cycle.yml','__PAC__ROOT__: {}\ng: {_is_group: 1, name: Group, parent: g}\n']])assert.throws(()=>parseImport(fixture(directory,{[name]:text})));
});
