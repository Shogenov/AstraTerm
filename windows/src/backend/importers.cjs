'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),iconv=require('iconv-lite');
const {defaultSession,validateSession,normalizeDocument,normalizeGroupName}=require('./store.cjs');
const {validateCredentials}=require('./vault.cjs');
const { isMobaKeyPath, resolveMobaDocumentsKey, MOBA_KEY_PATH_WARNING } = require('./moba-key-path.cjs');
const MAX=8*1024*1024,MAX_ITEMS=10000;
const stableId=(source,kind,name)=>'imp_'+crypto.createHash('sha256').update(source+'\0'+kind+'\0'+name).digest('hex').slice(0,40);
const credentialId=name=>stableId('mobaxterm','credential',name);
const group=value=>normalizeGroupName(String(value||'').replaceAll('\\','/').replace(/^\/+|\/+$/g,''),true);
function decode(bytes){
  if(bytes.length>MAX)throw Error('Файл импорта превышает 8 МиБ.');
  if(bytes[0]===255&&bytes[1]===254)return iconv.decode(bytes,'utf16le').replace(/^\uFEFF/,'');
  if(bytes[0]===254&&bytes[1]===255)return iconv.decode(bytes,'utf16be').replace(/^\uFEFF/,'');
  try{return new TextDecoder('utf-8',{fatal:true}).decode(bytes).replace(/^\uFEFF/,'');}catch{return iconv.decode(bytes,'cp1251');}
}
function unescape(value){for(const [before,after]of [['__PTVIRG__',';'],['__DBLQUO__','"'],['__PIPE__','|'],['__DIEZE__','#']])value=value.replaceAll(before,after);return value;}
class ImportPlan{
  constructor(){this.sessions=[];this.groups=new Map();this.credentials=new Map();this.warnings=[];this.sources=[];this.macros=[];this.tunnels=[];this.nativeGroups=new Set();}
  group(value,source){const parts=value?group(value).split('/'):[];for(let size=1;size<=parts.length;size++){const name=parts.slice(0,size).join('/');this.groups.set(name,{id:stableId(source,'group',name),name,ssh:{}});}}
  add(session,source,identity){if(this.sessions.length>=MAX_ITEMS)throw Error('В файле слишком много подключений.');session.id=stableId(source,'session',identity);session.inherit_group_ssh=false;this.sessions.push(validateSession(session));this.group(session.group,source);}
  secret(record){if(this.credentials.size>=MAX_ITEMS)throw Error('Слишком много профилей паролей.');const checked=validateCredentials([record])[0],old=this.credentials.get(checked.id);if(old&&(old.username!==checked.username||old.password!==checked.password))throw Error('Конфликт учётных данных в импортируемых файлах; ничего не сохранено.');this.credentials.set(checked.id,checked);}
  document(store){const existing=new Map((store?.snapshot().groups||[]).map(item=>[item.name,item]));return normalizeDocument({version:2,sessions:this.sessions,groups:[...this.groups].sort(([a],[b])=>a<b?-1:a>b?1:0).map(([name,item])=>this.nativeGroups.has(name)?item:existing.get(name)||item),macros:this.macros,tunnels:this.tunnels});}
  preview(metadata=[]){const known=new Map(metadata.map(item=>[item.id,item]));for(const [id,item]of this.credentials)known.set(id,item);const missing=new Set();
    const sessions=this.sessions.map(session=>{const id=session.credential_profile,credential=known.get(id);if(id&&!credential)missing.add(id);return {name:session.name,group:session.group,protocol:session.protocol,host:session.host,username:credential?credential.username:session.username,credential:session.credential_label||'',missing:Boolean(id&&!credential)};});
    return {sessions,credentials:this.credentials.size,groups:this.groups.size,macros:this.macros.length,tunnels:this.tunnels.length,warnings:[...this.warnings,...this.sessions.filter(item=>item.import_warning).map(item=>item.name+': '+item.import_warning)],sources:[...this.sources],missing:missing.size};
  }
}
function mobaPasswords(text,plan){let found=0;
  for(const [index,line]of text.split(/\r?\n/).entries()){
    if(!line.trim()||/^[\s]*[#;\[]/.test(line))continue;
    const at=line.indexOf('=');if(at<0)throw Error(`Строка ${index+1}: ожидается запись MobaXterm «профиль(пользователь) = пароль».`);
    const label=line.slice(0,at).trim();let password=line.slice(at+1);if(password.startsWith(' '))password=password.slice(1);
    const match=/^(.*)\(([^()]*)\)$/.exec(label);let name,username,id;
    if(match){[,name,username]=match;id=credentialId(name);}else{
      const endpoint=/^(?:(ssh|rdp|vnc|telnet)([0-9]*):)?(.+)@(.+)$/i.exec(label);
      if(!endpoint)throw Error(`Строка ${index+1}: неизвестный формат учётных данных MobaXterm.`);
      const protocol=(endpoint[1]||'ssh').toLowerCase(),port=Number(endpoint[2]||defaultSession(protocol).port);if(port<1||port>65535)throw Error('Некорректный порт учётных данных MobaXterm.');username=endpoint[3];name=label;id=stableId('mobaxterm','endpoint',protocol+':'+port+':'+username+'@'+endpoint[4].toLowerCase());
    }
    plan.secret({id,name,username,password});found++;
  }
  if(!found)throw Error('В файле не найдены учётные данные MobaXterm.');
}
function mobaSessions(text,plan,{documents}={}){let folder='',section='';const seen=new Set();
  for(const [index,line]of text.split(/\r?\n/).entries()){
    const number=index+1;if(!line.trim()||/^\s*;/.test(line))continue;
    if(line.startsWith('[')){section=line.trim();folder='';continue;}if(!/^\[Bookmarks(?:_[0-9]+)?\]$/.test(section))continue;
    const at=line.indexOf('=');if(at<0)throw Error(`Строка ${number}: повреждена запись MobaXterm.`);
    const name=line.slice(0,at),value=line.slice(at+1);if(name==='SubRep'){folder=group(value);plan.group(folder,'mobaxterm');continue;}if(name==='ImgNum')continue;
    const blocks=value.split('#');if(blocks.length<3)throw Error(`Строка ${number}: повреждена сессия MobaXterm.`);
    const fields=blocks[2].split('%'),get=(index,fallback='')=>fields[index]??fallback,protocol={'0':'ssh','1':'telnet','4':'rdp','5':'vnc'}[get(0)];
    if(!protocol){plan.warnings.push(`Строка ${number}: тип MobaXterm ${get(0)} не поддерживается; сессия пропущена.`);continue;}
    const record=defaultSession(protocol);Object.assign(record,{name:unescape(name.trim()),host:get(1),port:get(2)||record.port,group:folder});const login=get(3);
    if(login.startsWith('[')&&login.endsWith(']')){record.credential_label=login.slice(1,-1);record.credential_profile=credentialId(record.credential_label);}
    else if(!['<default>','<none>'].includes(login)){record.username=login;if(login){record.credential_profile=stableId('mobaxterm','endpoint',protocol+':'+Number(record.port)+':'+login+'@'+record.host.toLowerCase());record.credential_label=login+'@'+record.host;}}
    if(protocol==='ssh'){
      Object.assign(record,{x11:get(5)==='-1',compression:get(6)==='-1',key_file:get(14)});
      if(get(7))record.import_warning='Импортированная команда входа не запускается автоматически; настройте её вручную.';
      const [hosts,ports,users]=[8,9,10].map(i=>get(i)?get(i).split('__PIPE__'):[]);
      if(hosts.length)record.jump_host=hosts.map((host,i)=>(users[i]?users[i]+'@':'')+(host.includes(':')?'['+host+']':host)+':'+(ports[i]||'22')).join(',');
      if(!['','0'].includes(get(19,'0')))record.import_warning='Прокси MobaXterm требует ручной настройки; подключение заблокировано до проверки.';
    }
    if(['rdp','vnc'].includes(protocol)&&(protocol==='rdp'?[13,20]:[5]).some(i=>get(i)))record.import_warning='Шлюз удалённого рабочего стола требует ручной настройки; подключение заблокировано до проверки.';
    if (isMobaKeyPath(record.key_file)) {
      const resolved = resolveMobaDocumentsKey(record.key_file, documents);
      if (resolved) record.key_file = resolved;
      else record.import_warning = [record.import_warning, MOBA_KEY_PATH_WARNING].filter(Boolean).join(' ');
    }
    const identity=folder+'\0'+record.name;if(seen.has(identity))throw Error('В файле повторяется имя сессии в одной папке.');seen.add(identity);plan.add(record,'mobaxterm',identity);
  }
}
function boundedTree(value,depth=0,budget={count:0}){if(depth>35||++budget.count>150000)throw Error('Слишком сложная структура файла импорта.');if(value&&typeof value==='object'){for(const child of Object.values(value))boundedTree(child,depth+1,budget);}}
function asbruSessions(text,plan){let data;try{const document=require('yaml').parseDocument(text,{uniqueKeys:true,version:'1.1'});if(document.errors.length||document.warnings.length)throw Error();data=document.toJS({maxAliasCount:20});}catch{throw Error('Повреждённый или небезопасный YAML Ásbrú.');}boundedTree(data);
  if(!data||typeof data!=='object'||Array.isArray(data))throw Error('Ожидается экспорт YAML Ásbrú.');const nodes=data.environments||data;
  if(!nodes||typeof nodes!=='object'||!Object.keys(nodes).some(key=>key.startsWith('__PAC__')))throw Error('В YAML не найдены подключения Ásbrú.');
  const roots=['__PAC__ROOT__','__PAC__EXPORTED__','__PAC_SHELL__'];
  const pathFor=(id,visited=new Set())=>{if(!id||roots.includes(id))return '';if(visited.has(id)||visited.size>30)throw Error('Цикл папок в экспорте Ásbrú.');const node=nodes[id];if(!node||typeof node!=='object')throw Error('В экспорте Ásbrú отсутствует родительская папка.');visited.add(id);return group([pathFor(node.parent,visited),String(node.name||'')].filter(Boolean).join('/'));};
  for(const [id,node]of Object.entries(nodes)){
    if(roots.includes(id)||!node||typeof node!=='object'||!node.name)continue;
    if([1,true,'1'].includes(node._is_group)){plan.group(pathFor(id),'asbru');continue;}
    const protocol={SSH:'ssh',SFTP:'ssh',TELNET:'telnet',RDP:'rdp',VNC:'vnc',MOSH:'mosh'}[String(node.method||'').toUpperCase()];if(!protocol){plan.warnings.push('Тип соединения Ásbrú не поддерживается; одна сессия пропущена.');continue;}
    const record=defaultSession(protocol);Object.assign(record,{name:String(node.name),host:String(node.ip||''),port:node.port||record.port,group:pathFor(node.parent),username:String(node.user||node['passphrase user']||''),key_file:String(node['public key']||'')});
    if([1,true,'1'].includes(node['use proxy']))record.import_warning='Прокси или шлюз Ásbrú требует ручной настройки; подключение заблокировано до проверки.';
    if(node.options)record.import_warning='Дополнительные параметры Ásbrú требуют ручной настройки; подключение заблокировано до проверки.';
    if(node.pass){const value=String(node.pass);if(/^(?:53616c7465645f5f|Salted__)/i.test(value))plan.warnings.push('Зашифрованный пароль Ásbrú пропущен; экспортируйте без опции шифрования.');else{record.credential_profile=stableId('asbru','credential',id);record.credential_label=record.name;plan.secret({id:record.credential_profile,name:record.name,username:record.username,password:value});}}
    if(node.options||node.expect?.length||node['use prepend command']||node['use postpend command'])plan.warnings.push('Команды, Expect и дополнительные параметры Ásbrú не выполняются при импорте.');
    plan.add(record,'asbru',id);
  }
}
function parseCsv(text,delimiter){const rows=[];let row=[],field='',quoted=false,ended=false;
  for(let i=0;i<text.length;i++){const c=text[i];if(quoted){if(c==='"'){if(text[i+1]==='"'){field+='"';i++;}else{quoted=false;ended=true;}}else field+=c;}
    else if(c==='"'&&!field&&!ended)quoted=true;else if(c===delimiter){row.push(field);field='';ended=false;}
    else if(c==='\n'||c==='\r'){if(c==='\r'&&text[i+1]==='\n')i++;row.push(field);if(row.some(Boolean))rows.push(row);row=[];field='';ended=false;}
    else{if(ended)throw Error('Повреждённый CSV Termius.');field+=c;}if(rows.length>MAX_ITEMS+1)throw Error('В файле слишком много подключений.');
  }if(quoted)throw Error('Повреждённый CSV Termius.');if(field||row.length){row.push(field);rows.push(row);}return rows;
}
function termiusCsv(text,plan){const first=text.split(/\r?\n/)[0],delimiter=(first.match(/;/g)||[]).length>(first.match(/,/g)||[]).length?';':',';
  const [headers,...rows]=parseCsv(text,delimiter);if(!headers)throw Error('Файл импорта пуст.');const keys=headers.map(key=>key.toLowerCase().replace(/[^a-z0-9]/g,''));
  if(new Set(keys).size!==keys.length)throw Error('Повторяющиеся колонки CSV.');
  for(const [index,values]of rows.entries()){
    if(values.length!==headers.length)throw Error(`Строка ${index+2}: неверное число колонок CSV.`);
    const row=Object.fromEntries(keys.map((key,i)=>[key,values[i]])),address=row.address||row.hostname||row.ip||row.ipaddress;if(!address)throw Error(`Строка ${index+2}: CSV Termius требует колонку Address/Hostname.`);
    const protocol=(row.protocol||'ssh').toLowerCase();if(!['ssh','telnet','rdp','vnc','mosh'].includes(protocol)){plan.warnings.push(`Строка ${index+2}: протокол CSV не поддерживается.`);continue;}
    const record=defaultSession(protocol);Object.assign(record,{name:row.label||row.name||address,host:address,port:row.port||record.port,group:group(row.group||row.groups||''),username:row.username||row.user||'',key_file:row.identityfile||row.keyfile||''});const identity=record.group+'\0'+record.name;
    if(row.password){record.credential_profile=stableId('termius','credential',identity);record.credential_label=record.name;plan.secret({id:record.credential_profile,name:record.name,username:record.username,password:row.password});}plan.add(record,'termius',identity);
  }
}
function sshConfig(text,plan){let aliases=[],options={};const blocks=[];
  const allowed=new Set(['hostname','port','user','identityfile','proxyjump','forwardx11','compression']);
  for(const raw of text.split(/\r?\n/)){const line=raw.trim();if(!line||line.startsWith('#'))continue;
    const match=/^(\S+?)(?:\s*=\s*|\s+)(.*)$/.exec(line);if(!match)throw Error('Повреждённый SSH config.');const key=match[1].toLowerCase(),value=match[2].replace(/^"(.*)"$/,'$1');
    if(key==='host'){blocks.push({aliases,options});aliases=value.split(/\s+/);options={};}
    else if(key==='include'||key==='match'||!allowed.has(key))throw Error('SSH config содержит непереносимый параметр; упростите экспорт перед импортом.');else{if(key==='identityfile')options.__identity_count__=(options.__identity_count__||0)+1;if(options[key]===undefined)options[key]=value;}
  }blocks.push({aliases,options});
  const names=[...new Set(blocks.flatMap(block=>block.aliases).filter(name=>!/[*!?]/.test(name)))];
  const matches=(pattern,name)=>new RegExp('^'+pattern.replace(/[.+^${}()|[\]\\]/g,'\\$&').replace(/\*/g,'.*').replace(/\?/g,'.')+'$').test(name);
  for(const name of names){const o={};let identities=0;
    for(const block of blocks){if(!block.aliases.some(pattern=>!pattern.startsWith('!')&&matches(pattern,name))||block.aliases.some(pattern=>pattern.startsWith('!')&&matches(pattern.slice(1),name)))continue;
      identities+=block.options.__identity_count__||0;for(const [key,value]of Object.entries(block.options))if(o[key]===undefined)o[key]=value;
    }
    if(identities>1)throw Error('SSH config: несколько IdentityFile требуют ручной настройки.');
    plan.add({...defaultSession('ssh'),name,group:'SSH config',host:o.hostname||name,port:o.port||22,username:o.user||'',key_file:o.identityfile||'',jump_host:o.proxyjump||'',x11:o.forwardx11==='yes',compression:o.compression!=='no'},'termius',name);
  }
}
function parseImport(paths,{documents}={}){if(!Array.isArray(paths)||!paths.length||paths.length>8)throw Error('Выберите от одного до восьми файлов импорта.');const plan=new ImportPlan();
  for(const filename of paths){const fd=fs.openSync(filename,'r');let bytes;try{const stat=fs.fstatSync(fd);if(!stat.isFile()||stat.size>MAX)throw Error('Файл импорта превышает 8 МиБ.');bytes=fs.readFileSync(fd);}finally{fs.closeSync(fd);}
    const text=decode(bytes),name=path.basename(filename),lower=name.toLowerCase();if(!text.trim())throw Error('Файл импорта пуст.');plan.sources.push(name);
    if(text.slice(0,4096).includes('[Bookmarks')||/\.(mxtsessions|moba)$/.test(lower))mobaSessions(text,plan,{documents});
    else if(/\.ya?ml$/.test(lower)||text.slice(0,4096).includes('__PAC__'))asbruSessions(text,plan);
    else if(lower.endsWith('.csv'))termiusCsv(text,plan);
    else if(/^[\s]*[\[{]/.test(text)){let raw;try{raw=JSON.parse(text);}catch{throw Error('Некорректный JSON файла импорта.');}const document=normalizeDocument(raw);plan.sessions.push(...document.sessions);plan.macros.push(...document.macros);plan.tunnels.push(...document.tunnels);for(const item of document.groups){plan.groups.set(item.name,item);plan.nativeGroups.add(item.name);}}
    else if(/^\s*Host\s+/mi.test(text)){sshConfig(text,plan);plan.warnings.push('SSH config: команды ProxyCommand не выполняются; проверьте неперенесённые параметры.');}
    else mobaPasswords(text,plan);
  }
  if(new Set(plan.sessions.map(item=>item.id)).size!==plan.sessions.length)throw Error('В выбранных файлах повторяются сессии.');
  if(!plan.sessions.length&&!plan.credentials.size&&!plan.groups.size&&!plan.macros.length&&!plan.tunnels.length)throw Error('Подключения и учётные данные не найдены.');plan.document();return plan;
}
module.exports={parseImport,ImportPlan,stableId,credentialId,mobaPasswords,mobaSessions,asbruSessions,termiusCsv,parseCsv,decodeImportText:decode};
