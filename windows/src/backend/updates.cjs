'use strict';
const crypto = require('node:crypto');
const https = require('node:https');
const fs = require('node:fs');
const path = require('node:path');
const { UPDATE_PARAMETERS: P, VERSION } = require('../shared.cjs');

function versionTuple(value) {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$/.test(value)) throw Error('Некорректная версия обновления');
  return value.split('.').map(Number);
}
function newer(a, b) { const x=versionTuple(a),y=versionTuple(b);for(let i=0;i<3;i++){if(x[i]!==y[i])return x[i]>y[i];}return false; }
function releaseURL(value) {
  if(typeof value!=='string'||value.length>512||value.includes('\\')||value.includes('%'))throw Error('Некорректная ссылка обновления');
  const u=new URL(value);
  if(u.origin!==P.origin||u.username||u.password||u.search||u.hash||!/^\/releases\/[0-9]+\.[0-9]+\.[0-9]+\/[A-Za-z0-9_.-]+$/.test(u.pathname))throw Error('Обновление должно находиться на официальном сайте');
  return value;
}
function decode(value){if(typeof value!=='string'||!value.length||!/^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value))throw Error('Неверный base64');return Buffer.from(value,'base64');}
function readManifest(contents, platform='windows', current=VERSION, publicKey=P.public_key) {
  try {
    if(contents.length>P.manifest_max_bytes)throw Error('Список слишком большой');
    const envelope=JSON.parse(contents.toString('utf8')),payload=decode(envelope.payload),signature=decode(envelope.signature);
    if(!crypto.verify('sha256',payload,{key:publicKey,padding:crypto.constants.RSA_PKCS1_PSS_PADDING,saltLength:32},signature))throw Error('Неверная подпись');
    const data=JSON.parse(payload.toString('utf8'));
    if(data.schema!==1||data.product!=='AstraTerm')throw Error('Неверный продукт');
    const item=data.releases[platform];versionTuple(item.version);versionTuple(current);
    const filename=platform==='windows'?`AstraTerm-${item.version}-Windows-x64.exe`:`astraterm_${item.version}_all.deb`;
    const root = `${P.origin}/releases/${item.version}/`;
    const result = {version:item.version,...readArtifact(item,root+filename),notes:item.notes||'',available:newer(item.version,current)};
    if(typeof(item.notes??'')!=='string'||(item.notes??'').length>4000)throw Error('Неверное описание выпуска');
    if(platform==='windows' && Object.hasOwn(item,'installer'))
      result.installer=readArtifact(item.installer,root+`AstraTerm-${item.version}-Windows-x64-Setup.exe`);
    return result;
  }catch{throw Error('Не удалось проверить подпись или формат списка обновлений');}
}
function readArtifact(item, expectedURL) {
  if(releaseURL(item.url)!==expectedURL||!Number.isSafeInteger(item.bytes)||item.bytes<=0||item.bytes>P.artifact_max_bytes||typeof item.sha256!=='string'||!/^[0-9a-f]{64}$/.test(item.sha256))
    throw Error('Неверные параметры пакета');
  return {url:item.url,bytes:item.bytes,sha256:item.sha256};
}
function request(url, consume) {
  return new Promise((resolve,reject)=>{
    const req=https.get(url,{timeout:30000,headers:{'User-Agent':`AstraTerm/${VERSION}`}},response=>{
      if(response.statusCode!==200){response.resume();reject(Error('Сервер обновлений вернул ошибку'));return;}
      response.on('error',reject);
      consume(response).then(resolve,reject);
    });
    req.on('timeout',()=>req.destroy(Error('Сервер обновлений не отвечает')));req.on('error',reject);
  });
}
async function check(current=VERSION) {
  return request(P.feed_url,async response=>{const parts=[];let size=0;for await(const block of response){size+=block.length;if(size>P.manifest_max_bytes){response.destroy();throw Error('Список обновлений слишком большой');}parts.push(block);}return readManifest(Buffer.concat(parts),'windows',current);});
}
async function download(item,directory,opener=request,progress=()=>{}) {
  releaseURL(item.url);await fs.promises.mkdir(directory,{recursive:true,mode:0o700});
  if((await fs.promises.lstat(directory)).isSymbolicLink())throw Error('Каталог обновлений не может быть ссылкой');
  const filename=path.join(directory,`update-${crypto.randomUUID()}.exe`),handle=await fs.promises.open(filename,'wx',0o600);
  try {
    await opener(item.url, async response => {
      const hash = crypto.createHash('sha256');
      let size = 0;
      for await (const block of response) {
        size += block.length;
        if (size > item.bytes) {
          response.destroy?.();
          throw Error('Размер обновления не совпадает');
        }
        hash.update(block);
        let offset = 0;
        while (offset < block.length) {
          const result = await handle.write(block, offset, block.length - offset);
          if (!result.bytesWritten) throw Error('Не удалось записать обновление');
          offset += result.bytesWritten;
        }
        progress(size, item.bytes);
      }
      if (size !== item.bytes || hash.digest('hex') !== item.sha256) throw Error('Контрольная сумма обновления не совпадает');
    });
    await handle.sync();await handle.close();return filename;
  }catch(error){await handle.close().catch(()=>{});await fs.promises.unlink(filename).catch(()=>{});throw error;}
}
module.exports={versionTuple,newer,releaseURL,readManifest,check,download};
