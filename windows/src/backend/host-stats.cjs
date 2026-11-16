'use strict';
const os=require('node:os'),path=require('node:path'),{execFile}=require('node:child_process');
const VIRTUAL=new Set('proc sysfs devtmpfs tmpfs devpts cgroup cgroup2 squashfs securityfs debugfs tracefs fusectl pstore autofs mqueue hugetlbfs nsfs rpc_pipefs binfmt_misc configfs bpf'.split(' '));
function usersFromUtmp(data){
  if(!data || data.length%384)return null;const counts=new Map();
  for(let offset=0;offset<data.length;offset+=384)if(data.readInt16LE(offset)===7){const name=data.subarray(offset+44,offset+76).toString('utf8').split('\0')[0].replace(/[\x00-\x1f\x7f]/g,'');if(name)counts.set(name,(counts.get(name)||0)+1);}
  return [...counts].sort(([a],[b])=>a.localeCompare(b)).map(([name,count])=>({name,count}));
}
function mountPaths(source){
 const paths=['/'],seen=new Set(paths);
 for(const line of String(source||'').split('\n')){const fields=line.trim().split(/\s+/);if(fields.length<3)continue;
  const [device,encoded,kind]=fields,target=encoded.replace(/\\([0-7]{3})/g,(_,n)=>String.fromCharCode(parseInt(n,8)));
  if(VIRTUAL.has(kind)||seen.has(target)||!target.startsWith('/')||target.length>4096||/[\x00-\x1f\x7f]/.test(target)||!(device.startsWith('/dev/')||/^(nfs|cifs|fuse\.)/.test(kind)))continue;
  paths.push(target);seen.add(target);if(paths.length>=32)break;
 }return paths;
}
async function linuxSnapshot(read,statvfs){
 const optional=async(p,limit=262144)=>{try{return await read(p,limit);}catch{return null;}},text=b=>b?.toString('utf8')||'';
 const [stat,mem,up,route,net,mounts,host]=await Promise.all(['/proc/stat','/proc/meminfo','/proc/uptime','/proc/net/route','/proc/net/dev','/proc/mounts','/proc/sys/kernel/hostname'].map(p=>optional(p)));
 if(!text(stat).startsWith('cpu '))throw Error('Статистика Linux недоступна');
 const ticks=text(stat).split('\n')[0].trim().split(/\s+/).slice(1,9).map(Number);if(ticks.length<4||ticks.some(n=>!Number.isFinite(n)||n<0))throw Error('Некорректные счётчики CPU');
 const memory=Object.fromEntries([...text(mem).matchAll(/^(\w+):\s+(\d+)/gm)].map(m=>[m[1],Number(m[2])*1024]));
 const total=memory.MemTotal||0,available=memory.MemAvailable??(['MemFree','Buffers','Cached','SReclaimable'].reduce((s,k)=>s+(memory[k]||0),0)-(memory.Shmem||0));
 const defaults=new Set(text(route).split('\n').slice(1).map(l=>l.trim().split(/\s+/)).filter(f=>f.length>3&&f[1]==='00000000').map(f=>f[0]));
 const interfaces=new Map();for(const line of text(net).split('\n')){const i=line.indexOf(':');if(i<0)continue;const name=line.slice(0,i).trim(),v=line.slice(i+1).trim().split(/\s+/).map(Number);if(name!=='lo'&&v.length>=9&&Number.isFinite(v[0])&&Number.isFinite(v[8]))interfaces.set(name,[v[0],v[8]]);}
 const selected=[...interfaces.keys()].filter(k=>defaults.has(k));if(!selected.length)selected.push(...interfaces.keys());selected.sort();
 const disks=[],deadline=Date.now()+2000;for(const target of mountPaths(text(mounts))){if(Date.now()>deadline)break;try{const s=await statvfs(target),block=s.frsize||s.bsize,used=(s.blocks-s.bfree)*block,total=s.blocks*block,available=s.bavail*block;disks.push({path:target,used,total,percent:used+available>0?Math.round(1000*used/(used+available))/10:0});}catch{disks.push({path:target,used:null,total:null,percent:null});}}
 const uptime=Number(text(up).split(/\s+/)[0]);
 return {hostname:text(host).replace(/[\x00-\x1f\x7f]/g,'').trim().slice(0,253),cpu_total:ticks.reduce((a,b)=>a+b,0),cpu_idle:ticks[3]+(ticks[4]||0),memory_total:total||null,memory_used:total?Math.max(0,total-Math.max(0,Math.min(total,available))):null,
 uptime:Number.isFinite(uptime)&&uptime>=0?uptime:null,rx:interfaces.size?selected.reduce((s,k)=>s+interfaces.get(k)[0],0):null,tx:interfaces.size?selected.reduce((s,k)=>s+interfaces.get(k)[1],0):null,interfaces:selected,users:usersFromUtmp(await optional('/run/utmp',1048576)??await optional('/var/run/utmp',1048576)),disks};
}
class Sampler{
 constructor(){this.previous=new Map();}
 sample(key,raw,now=performance.now()/1000){const old=this.previous.get(key);this.previous.set(key,[now,raw]);if(this.previous.size>64)this.previous.delete(this.previous.keys().next().value);
  const result={...raw,cpu:null,upload:raw.tx_rate??null,download:raw.rx_rate??null};if(old&&now>old[0]&&now-old[0]<=30){const elapsed=now-old[0],total=raw.cpu_total-old[1].cpu_total,idle=raw.cpu_idle-old[1].cpu_idle;if(total>0&&idle>=0&&idle<=total)result.cpu=Math.round(1000*(total-idle)/total)/10;
   for(const [source,target] of [['rx','download'],['tx','upload']])if(JSON.stringify(raw.interfaces)===JSON.stringify(old[1].interfaces)&&raw[source]!=null&&old[1][source]!=null&&raw[source]>=old[1][source]&&raw[source+'_rate']==null)result[target]=Math.round(10*(raw[source]-old[1][source])/elapsed)/10;
  }return result;
 }
}
class HostStats{
 constructor(files,terminals){this.terminals=terminals;this.sampler=new Sampler();this.pending=new Map();this.channels=new Set();this.localExtra=null;this.localTime=0;this.child=null;this.closed=false;}
 async local(){
  if(Date.now()-this.localTime>5000&&!this.child){this.localTime=Date.now();const root=process.env.SystemRoot||'C:\\Windows';
   this.child=execFile(path.join(root,'System32','WindowsPowerShell','v1.0','powershell.exe'),['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',path.join(__dirname,'host-stats.ps1').replace('app.asar'+path.sep,'app.asar.unpacked'+path.sep)],{windowsHide:true,timeout:8000,maxBuffer:262144,encoding:'utf8'},(error,stdout)=>{this.child=null;if(error)this.localExtra=null;if(!error&&!this.closed)try{const value=JSON.parse(stdout.replace(/^\uFEFF/,'')),old=this.localCounters;this.localCounters=value;this.localExtra={users:value.users,disks:value.disks,interfaces:value.interfaces,rx_rate:null,tx_rate:null};if(old&&JSON.stringify(value.interfaces)===JSON.stringify(old.interfaces)&&value.captured>old.captured)for(const name of ['rx','tx'])if(value[name]!=null&&old[name]!=null&&value[name]>=old[name])this.localExtra[name+'_rate']=(value[name]-old[name])/(value.captured-old.captured);}catch{}});
  }
  const cpus=os.cpus(),cpu_total=cpus.reduce((sum,c)=>sum+Object.values(c.times).reduce((a,b)=>a+b,0),0),cpu_idle=cpus.reduce((sum,c)=>sum+c.times.idle,0);
  return {hostname:os.hostname(),cpu_total,cpu_idle,memory_total:os.totalmem(),memory_used:os.totalmem()-os.freemem(),uptime:os.uptime(),rx:null,tx:null,interfaces:[],users:null,disks:[],...(this.localExtra||{})};
 }
 async read(id){
  const record=this.terminals.get(id);if(!record||!['ssh','local'].includes(record.profile.protocol)||record.status!=='running')return null;
  const generation=record.generation,client=record.sshClient,local=record.profile.protocol==='local',key=id+':'+generation;if(this.pending.has(key))return this.pending.get(key);
  const work=(async()=>{let raw;
   if(local)raw=await this.local();else{let channel,timer,expired=false;
    // A separate SFTP channel shares the authenticated SSH transport. Its timeout
    // must never close the file browser's channel or prompt for credentials.
    try{raw=await Promise.race([new Promise((resolve,reject)=>{timer=setTimeout(()=>{expired=true;reject(Error('Истекло время сбора статистики'));},6000);}),new Promise((resolve,reject)=>{
     client.sftp(async(error,sftp)=>{if(error)return reject(error);if(expired||this.closed){sftp.end();return;}
      channel=sftp;this.channels.add(sftp);sftp.on('error',reject);sftp.once('close',()=>reject(Error('Канал статистики закрыт')));
      const read=(filename,limit)=>new Promise((resolve,reject)=>sftp.open(filename,'r',(error,handle)=>{if(error)return reject(error);const bytes=Buffer.alloc(limit);let offset=0;
       const finish=error=>sftp.close(handle,()=>error?reject(error):resolve(bytes.subarray(0,offset)));
       const next=()=>sftp.read(handle,bytes,offset,Math.min(32768,limit-offset),offset,(error,count)=>{if(error)return finish(error);offset+=count;if(!count||offset>=limit)finish();else next();});next();}));
      const stat=target=>new Promise((resolve,reject)=>sftp.ext_openssh_statvfs(target,(error,value)=>error?reject(error):resolve({bsize:value.f_bsize,frsize:value.f_frsize,blocks:value.f_blocks,bfree:value.f_bfree,bavail:value.f_bavail})));
      try{resolve(await linuxSnapshot(read,stat));}catch(error){reject(error);}
     });
    })]);}finally{clearTimeout(timer);expired=true;if(channel){this.channels.delete(channel);channel.end();}}
   }
   if(this.closed||this.terminals.get(id)!==record||generation!==record.generation||record.status!=='running'||!local&&client!==record.sshClient)return null;
   return {...this.sampler.sample(local?'local':key,raw),id,generation};
  })();this.pending.set(key,work);try{return await work;}finally{if(this.pending.get(key)===work)this.pending.delete(key);}
 }
 dispose(){this.closed=true;if(this.child)this.child.kill();for(const channel of this.channels)channel.end();this.channels.clear();}
}
module.exports={Sampler,linuxSnapshot,mountPaths,usersFromUtmp,HostStats};
