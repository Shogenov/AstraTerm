'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const updates=require('../src/backend/updates.cjs');
const contract=require('../../shared/contracts/updates.json');
test('shared signed update contract on both platforms',()=>{
  for(const c of contract.cases)for(const platform of ['windows','linux']){
    const work=()=>updates.readManifest(Buffer.from(JSON.stringify(c.envelope)),platform,c.current,contract.public_key);
    if(c.valid)assert.equal(work().available,c.available,c.name);else assert.throws(work,undefined,c.name);
  }
});
test('signed Windows installer is bound to the version and legacy portable package',()=>{
  const {privateKey,publicKey}=crypto.generateKeyPairSync('rsa',{modulusLength:2048});
  const artifact={url:'https://astraterminal.ru/releases/1.0.0/AstraTerm-1.0.0-Windows-x64.exe',bytes:10,sha256:'a'.repeat(64)};
  const installer={...artifact,url:artifact.url.replace('.exe','-Setup.exe'),sha256:'b'.repeat(64)};
  function read(setup) {
    const payload=Buffer.from(JSON.stringify({schema:1,product:'AstraTerm',releases:{windows:{version:'1.0.0',...artifact,installer:setup}}}));
    const signature=crypto.sign('sha256',payload,{key:privateKey,padding:crypto.constants.RSA_PKCS1_PSS_PADDING,saltLength:32});
    return updates.readManifest(Buffer.from(JSON.stringify({payload:payload.toString('base64'),signature:signature.toString('base64')})),'windows','0.10.8',publicKey);
  }
  const result=read(installer);
  assert.equal(result.url,artifact.url);
  assert.deepEqual(result.installer,installer);
  assert.equal(read(undefined).installer,undefined);
  assert.throws(()=>read({...installer,url:artifact.url}));
  assert.throws(()=>read({...installer,bytes:0}));
});
test('streamed update checks hash, size and removes rejected files',async()=>{
  const good=Buffer.from('synthetic update'),directory=await fs.promises.mkdtemp(path.join(os.tmpdir(),'astraterm-update-test-'));
  const item={url:'https://astraterminal.ru/releases/0.10.0/AstraTerm-0.10.0-Windows-x64.exe',bytes:good.length,sha256:crypto.createHash('sha256').update(good).digest('hex')};
  try{
    for(const bad of [Buffer.from('corrupted update'),good.subarray(1),Buffer.concat([good,Buffer.from('overflow')])]){
      await assert.rejects(updates.download(item,directory,async(_url,consume)=>consume((async function*(){yield bad;})())));
      assert.deepEqual(await fs.promises.readdir(directory),[]);
    }
    const filename=await updates.download(item,directory,async(_url,consume)=>consume((async function*(){yield good;})()));
    assert.deepEqual(await fs.promises.readFile(filename),good);
  }finally{await fs.promises.rm(directory,{recursive:true,force:true});}
});
test('update URL and feed limits',()=>{
  assert.throws(()=>updates.readManifest(Buffer.alloc(131073)));
  for(const url of ['https://evil.invalid/releases/1.0.0/a.exe','http://astraterminal.ru/releases/1.0.0/a.exe','https://astraterminal.ru/releases/1.0.0/a.exe?x=1'])assert.throws(()=>updates.releaseURL(url));
});
