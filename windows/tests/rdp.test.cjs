'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {PassThrough}=require('node:stream');
const {RdpManager}=require('../src/backend/rdp.cjs');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(prompt=async()=> 'synthetic-password') {
  const children=[],events=[];
  const manager=new RdpManager({window:{getNativeWindowHandle:()=>Buffer.from('2a00000000000000','hex'),getContentSize:()=>[1200,800],isMinimized:()=>false},
    emit:(type,payload)=>events.push({type,payload}),prompt,credentialProvider:async()=> 'synthetic-password',
    spawnProcess:(executable,argv,options)=>{const child=new EventEmitter();Object.assign(child,{stdin:new PassThrough(),stdout:new PassThrough(),stderr:new PassThrough(),exitCode:null,kill(){this.exitCode=1;this.emit('exit',1);}});child.commands=[];child.stdin.on('data',chunk=>child.commands.push(...chunk.toString().trim().split('\n').map(JSON.parse)));child.stdin.on('finish',()=>child.kill());children.push({child,argv,options});return child;}});
  return {manager,children,events};
}
const profile={protocol:'rdp',host:'desktop.example.test',username:'DOMAIN\\operator',rdp_domain:'',port:3389,rdp_resolution:'1280x800'};
test('RDP uses private stdin, does not expose secret and contains display bounds',async()=>{
  const {manager,children,events}=fixture();
  const record=manager.create(profile);await tick();const native=children[0];
  assert.deepEqual(native.argv,['42']);assert.equal(native.options.shell,false);
  manager.bounds(record.id,{x:200,y:80,width:800,height:600,scale:1.5,visible:true});
  native.child.stdout.write('{"type":"ready","hwnd":"43","parent":"42"}\n');await tick();
  const connect=native.child.commands.find(command=>command.op==='connect');assert.equal(connect.password,'synthetic-password');assert.equal(connect.domain,'DOMAIN');assert.equal(connect.username,'operator');
  assert(!JSON.stringify(events).includes('synthetic-password'));assert(!JSON.stringify(manager.list()).includes('synthetic-password'));
  assert.equal(native.child.commands.filter(command=>command.op==='bounds').at(-1).width,1200);
  assert.deepEqual([connect.width,connect.height],[1200,900]);
  assert(native.child.commands.findIndex(command=>command.op==='bounds')<native.child.commands.findIndex(command=>command.op==='connect'));
  assert.throws(()=>manager.bounds(record.id,{x:0,y:0,width:NaN,height:20,scale:1,visible:true}));
  manager.close(record.id);await tick();assert.equal(native.child.exitCode,1);assert(!manager.get(record.id));
});
test('closing pending authentication cannot create a late RDP process',async()=>{
  let resolve;const {manager,children}=fixture(()=>new Promise(done=>{resolve=done;}));
  const record=manager.create(profile);manager.close(record.id);resolve('synthetic-password');await tick();assert.equal(children.length,0);
});
test('old helper events cannot change a reconnected generation',async()=>{
  const {manager,children}=fixture();const record=manager.create(profile);await tick();
  const first=children[0].child;manager.reconnect(record.id);await tick();
  first.stdout.write('{"type":"running"}\n');await tick();assert.equal(manager.get(record.id).status,'connecting');
  const second=children[1].child;second.stdout.write('{"type":"ready","hwnd":"44","parent":"42"}\n');second.stdout.write('{"type":"running"}\n');await tick();assert.equal(manager.get(record.id).status,'running');manager.dispose();await tick();
});
test('multiline secrets and unsupported gateways never start a native client',async()=>{
  const {manager,children}=fixture(async()=> 'synthetic\nT');manager.create(profile);await tick();assert.equal(children.length,0);
  manager.create({...profile,jump_host:'required-gateway'});await tick();assert.equal(children.length,0);manager.dispose();
});

test('RDP refresh preserves desired visibility across minimize and changes only its owned native parent',async()=>{
  const {manager,children}=fixture();const snapshot=manager.create(profile);await tick();
  const record=manager.get(snapshot.id),child=children[0].child,generation=record.generation;
  child.stdout.write('{"type":"ready","hwnd":"43","parent":"42"}\n');await tick();
  const value={x:0,y:36,width:800,height:500,scale:1,visible:true};manager.bounds(record.id,value);
  manager.window.isMinimized=()=>true;manager.refresh();
  assert.equal(record.bounds.visible,true);assert.equal(child.commands.at(-1).visible,false);
  manager.window.isMinimized=()=>false;manager.refresh();
  assert.equal(child.commands.at(-1).op,'refresh');
  assert.equal(child.commands.at(-2).visible,true);
  let focused=false;const floating={getNativeWindowHandle:()=>Buffer.from('4d00000000000000','hex'),getContentSize:()=>[900,650],isMinimized:()=>false,isFocused:()=>focused};
  manager.setWindow(record.id,floating);manager.bounds(record.id,value);manager.refresh(floating);
  assert.equal(child.commands.findLast(command=>command.op==='reparent').parent,'77');
  assert.equal(record.child,child);assert.equal(record.generation,generation);
  assert.equal(manager.focus(record.id),false);
  focused=true;
  assert.equal(manager.focus(record.id),true);
  assert.equal(child.commands.at(-1).op,'focus');
  manager.bounds(record.id,{...value,visible:false});
  assert.equal(manager.focus(record.id),false);
  assert.equal(child.commands.at(-1).visible,false);
  manager.dispose();await tick();
});

test('a presentation owner cannot be released until the native helper acknowledges its new HWND parent',async()=>{
  const {manager,children}=fixture();const snapshot=manager.create(profile);await tick();
  const child=children[0].child;
  child.stdout.write('{"type":"ready","hwnd":"43","parent":"42"}\n');await tick();
  const owner={getNativeWindowHandle:()=>Buffer.from('4d00000000000000','hex')};
  manager.setWindow(snapshot.id,owner);
  let settled=false;const waiting=manager.settledWindow(snapshot.id,owner).then(()=>{settled=true;});
  await tick();assert.equal(settled,false);
  child.stdout.write('{"type":"parent","parent":"77"}\n');await waiting;assert.equal(settled,true);
  manager.dispose();await tick();
});
