'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {linuxSnapshot,Sampler,usersFromUtmp,HostStats}=require('../src/backend/host-stats.cjs');
const fixture=require('../../shared/contracts/host-stats.json');
function files(){const result=Object.fromEntries(Object.entries(fixture.files).map(([k,v])=>[k,Buffer.from(v)]));result['/run/utmp']=Buffer.concat(fixture.utmp_users.map(name=>{const b=Buffer.alloc(384);b.writeInt16LE(7);b.write(name,44);return b;}));return result;}
const snapshot=source=>linuxSnapshot(async p=>{if(!source[p])throw Error('missing');return source[p];},async()=>fixture.statvfs);
test('shared proc counters, memory, utmp aggregation and mounted disks',async()=>{
 const data=await snapshot(files());for(const [key,value]of Object.entries(fixture.expected))assert.deepEqual(data[key],value,key);
 assert.deepEqual(data.disks.map(d=>d.path),fixture.mounts);assert.ok(data.disks.every(d=>d.percent===66.7&&d.used===614400));
});
test('old kernels, optional permissions failures and unsupported utmp ABI',async()=>{
 const source=files();source['/proc/meminfo']=Buffer.from(fixture.files['/proc/meminfo'].replace('MemAvailable: 2048 kB\n',''));source['/proc/uptime']=Buffer.from('nan');delete source['/proc/net/dev'];
 const data=await snapshot(source);assert.equal(data.memory_used,fixture.fallback_memory_used);assert.equal(data.uptime,null);assert.equal(data.rx,null);
 assert.equal(usersFromUtmp(Buffer.from('bad ABI')),null);assert.deepEqual(usersFromUtmp(Buffer.alloc(0)),[]);await assert.rejects(snapshot({}));
});
test('rates reset on counter rollback, interface changes and stale/other hosts',async()=>{
 const sampler=new Sampler(),first=await snapshot(files());assert.equal(sampler.sample('a',first,0).cpu,null);
 const later={...first,cpu_total:721,cpu_idle:480,rx:3024,tx:6048};const data=sampler.sample('a',later,2);
 assert.equal(data.cpu,70);assert.equal(data.download,1000);assert.equal(data.upload,2000);assert.equal(sampler.sample('b',later,2).cpu,null);
 assert.equal(sampler.sample('a',{...later,interfaces:['eth1'],rx:10000},4).download,null);assert.equal(sampler.sample('a',first,6).download,null);assert.equal(sampler.sample('a',later,60).cpu,null);
});
test('stats uses a separate channel, reads partial SFTP packets and rejects stale generations',async()=>{
 const source=files();source['/run/utmp']=Buffer.concat(Array(100).fill(source['/run/utmp']));let ended=0,opened=0;
 const channel={on(){},once(){},end(){ended++;},open(p,flags,cb){queueMicrotask(()=>source[p]?cb(null,p):cb(Error('missing')));},read(p,b,off,len,pos,cb){const count=Math.min(137,len,Math.max(0,source[p].length-pos));source[p].copy(b,off,pos,pos+count);queueMicrotask(()=>cb(null,count));},close(p,cb){cb();},ext_openssh_statvfs(p,cb){cb(null,Object.fromEntries(Object.entries(fixture.statvfs).map(([k,v])=>['f_'+k,v])));}};
 const record={profile:{protocol:'ssh'},status:'running',generation:1,sshClient:{sftp(cb){opened++;setImmediate(()=>cb(null,channel));}}};
 const stats=new HostStats(null,{get:()=>record});const [one,two]=await Promise.all([stats.read('a'),stats.read('a')]);assert.deepEqual(one,two);assert.equal(one.users.find(u=>u.name==='tester').count,200);assert.equal(opened,1);assert.equal(ended,1);
 const pending=stats.read('a');record.generation++;assert.equal(await pending,null);stats.dispose();
});
