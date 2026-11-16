'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {promisify}=require('node:util');
const {VAULT_PARAMETERS:P}=require('../shared.cjs');
const pbkdf2=promisify(crypto.pbkdf2),AAD=Buffer.from('AstraTerm credential vault v1');
const digest=bytes=>bytes===null?null:crypto.createHash('sha256').update(bytes).digest('hex');
function read(filename){
  try { const fd=fs.openSync(filename,'r');try{
    const info=fs.fstatSync(fd);if(!info.isFile()||info.size>P.maximum_bytes)throw Error('Недопустимый размер хранилища.');
    const bytes=fs.readFileSync(fd);if(bytes.length>P.maximum_bytes)throw Error('Недопустимый размер хранилища.');return bytes;
  }finally{fs.closeSync(fd);}}catch(e){if(e.code==='ENOENT')return null;throw e;}
}
function field(value,max=200){
  if(typeof value!=='string'||value.length>max||/[\x00-\x1f\x7f]/.test(value))throw Error('Некорректная запись хранилища.');return value.trim();
}
function validateCredentials(records){
  if(!Array.isArray(records)||records.length>10000)throw Error('Недопустимое содержимое хранилища.');
  const seen=new Set();return records.map(record=>{
    if(!record||typeof record!=='object')throw Error('Некорректная запись хранилища.');
    const id=field(record.id,80),name=field(record.name),username=field(record.username||'',255),password=record.password??'';
    if(!/^[a-zA-Z0-9_-]{1,80}$/.test(id)||!name||seen.has(id)||typeof password!=='string'||password.length>65536||/[\0\r\n]/.test(password)||(username&&(!/^[A-Za-z0-9_.@\\-]+$/.test(username)||username.startsWith('-'))))throw Error('Некорректная запись хранилища.');
    if('protocol' in record&&!['ssh','rdp','both'].includes(record.protocol))throw Error('Недопустимый тип учётных данных.');
    seen.add(id);return {id,name,username,password,...('protocol' in record?{protocol:record.protocol}:{})};
  });
}
function decode(value,length){
  if(typeof value!=='string'||value.length>P.maximum_bytes*2||!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value))throw Error('Некорректный формат хранилища.');
  const bytes=Buffer.from(value,'base64');if(length!==undefined&&bytes.length!==length)throw Error('Некорректный формат хранилища.');return bytes;
}
function validateMaster(master, fresh = false) {
  if (typeof master !== 'string' || !master || master.length > 4096 || master.includes('\0')) {
    throw Error('Введите мастер-пароль.');
  }
  if (fresh && Array.from(master).length < 10) {
    throw Error('Мастер-пароль должен содержать не менее 10 символов.');
  }
}
class CredentialVault{
  constructor(filename){this.path=path.resolve(filename);this.key=null;this.records=null;this.salt=null;this.epoch=0;this.revision=null;}
  get exists(){return fs.existsSync(this.path);}
  get unlocked(){return this.key!==null;}
  lock(){this.epoch++;if(this.key)this.key.fill(0);this.key=this.records=this.salt=null;}
  reloadIfChanged(){if(this.unlocked&&digest(read(this.path))!==this.revision)this.lock();}
  metadata(){return (this.records||[]).map(({id,name,username,protocol})=>({id,name,username,...(protocol?{protocol}:{})}));}
  async unlock(master,{create=false}={}){
    validateMaster(master);
    const epoch=this.epoch,bytes=read(this.path),revision=digest(bytes);let salt,document,records=[];
    if(bytes===null){if(!create)throw Error('Хранилище ещё не создано.');validateMaster(master,true);salt=crypto.randomBytes(P.salt_bytes);}
    else{try{document=JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''));
      for(const key of ['version','cipher','kdf','iterations'])if(document[key]!==P[key])throw Error();
      salt=decode(document.salt,P.salt_bytes);
    }catch{throw Error('Формат хранилища повреждён или не поддерживается.');}}
    const key=await pbkdf2(master,salt,P.iterations,32,'sha256');
    try{
      if (document) {
        let decrypted, raw;
        try {
          const decipher = crypto.createDecipheriv('aes-256-gcm', key, decode(document.nonce, P.nonce_bytes));
          decipher.setAAD(AAD);
          decipher.setAuthTag(decode(document.tag, P.tag_bytes));
          decrypted = decipher.update(decode(document.ciphertext));
          raw = Buffer.concat([decrypted, decipher.final()]);
        } catch {
          throw Error('Неверный мастер-пароль или повреждено хранилище.');
        } finally { decrypted?.fill(0); }
        try { records = validateCredentials(JSON.parse(raw.toString('utf8')).credentials); }
        catch { throw Error('Зашифрованный файл открыт, но содержит неподдерживаемые профили AstraTerm.'); }
        finally { raw.fill(0); }
      }
      if(this.epoch!==epoch||digest(read(this.path))!==revision)throw Error('Хранилище изменилось или было заблокировано. Повторите разблокировку.');
      if(this.key)this.key.fill(0);this.key=Buffer.from(key);this.salt=salt;this.records=records;this.revision=revision;
      return this.metadata();
    }finally{key.fill(0);}
  }
  async changeMaster(current, master) {
    validateMaster(master, true);
    const epoch = this.epoch;
    const previous = new CredentialVault(this.path);
    try {
      await previous.unlock(current);
      await this._replaceMaster(master, previous.revision, previous.records, epoch);
    } finally { previous.lock(); }
  }
  async resetMaster(master) {
    validateMaster(master, true);
    const revision = digest(read(this.path));
    if (revision === null) throw Error('Хранилище ещё не создано.');
    await this._replaceMaster(master, revision, [], this.epoch);
  }
  async _replaceMaster(master, revision, records, epoch) {
    const salt = crypto.randomBytes(P.salt_bytes);
    const key = await pbkdf2(master, salt, P.iterations, 32, 'sha256');
    try {
      if (this.epoch !== epoch) throw Error('Состояние хранилища изменилось. Повторите операцию.');
      const nextRevision = this._write(records, key, salt, revision);
      this.lock();
      this.key = Buffer.from(key);
      this.salt = salt;
      this.records = records;
      this.revision = nextRevision;
    } finally { key.fill(0); }
  }
  get(id,protocol){
    if(!this.unlocked)throw Error('Разблокируйте хранилище учётных данных.');
    const record=this.records.find(item=>item.id===id);if(!record)throw Error('Пароль этого профиля не импортирован. Импортируйте файл учётных данных.');if(protocol&&!['both',protocol].includes(record.protocol||'both'))throw Error('Профиль учётных данных не подходит для этого протокола.');return {...record};
  }
  merge(records,removeId){
    const checked=validateCredentials(records);if(!this.unlocked)throw Error('Разблокируйте хранилище учётных данных.');
    const merged=new Map(this.records.map(item=>[item.id,{...item}]));if(removeId!==undefined)merged.delete(removeId);for(const item of checked)merged.set(item.id,item);
    const updated = [...merged.values()];
    this.revision = this._write(updated, this.key, this.salt, this.revision);
    this.records = updated;
    return checked.length;
  }
  _write(records, key, salt, revision) {
    const raw=Buffer.from(JSON.stringify({credentials:records}));if(raw.length>P.maximum_bytes/2)throw Error('Хранилище превышает допустимый размер.');
    const nonce=crypto.randomBytes(P.nonce_bytes),cipher=crypto.createCipheriv('aes-256-gcm',key,nonce);cipher.setAAD(AAD);
    let encrypted;try{encrypted=Buffer.concat([cipher.update(raw),cipher.final()]);}finally{raw.fill(0);}
    const document={};for(const key of ['version','cipher','kdf','iterations'])document[key]=P[key];
    Object.assign(document,{salt:salt.toString('base64'),nonce:nonce.toString('base64'),tag:cipher.getAuthTag().toString('base64'),ciphertext:encrypted.toString('base64')});
    const bytes=Buffer.from(JSON.stringify(document,null,2)+'\n'),directory=path.dirname(this.path),lock=this.path+'.lock',temporary=path.join(directory,'.vault-'+crypto.randomBytes(12).toString('hex')+'.tmp');
    fs.mkdirSync(directory,{recursive:true,mode:0o700});let fd;
    try{fd=fs.openSync(lock,'wx',0o600);}catch(e){if(e.code==='EEXIST')throw Error('Хранилище занято другим окном.');throw e;}
    try{
      if(digest(read(this.path))!==revision)throw Error('Хранилище изменено другим окном. Заблокируйте и откройте его снова.');
      const target=fs.openSync(temporary,'wx',0o600);try{fs.writeFileSync(target,bytes);fs.fsyncSync(target);}finally{fs.closeSync(target);}
      fs.renameSync(temporary,this.path);
      return digest(bytes);
    }finally{try{if(fs.existsSync(temporary))fs.unlinkSync(temporary);}finally{fs.closeSync(fd);fs.unlinkSync(lock);}}
  }
  remove(id){this.get(id);this.merge([],id);}
}
module.exports={CredentialVault,validateCredentials};
