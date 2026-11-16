'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {FloatingWindows}=require('../src/floating-windows.cjs');
test('only a prepared local presentation window opens; closing returns ownership without closing a transport',async()=>{
  const contents=new EventEmitter();contents.setWindowOpenHandler=fn=>{contents.open=fn;};
  const owner={webContents:contents},events=[],moves=[];
  const manager=new FloatingWindows({owner,url:'file:///app/floating.html',terminals:{get:id=>id==='pty'},
    rdp:{get:id=>id==='rdp',setWindow:(...args)=>moves.push(args),refresh(){},settledWindow:async()=>true},emit:(...args)=>events.push(args)});
  assert.throws(()=>manager.prepare({id:'tab',panes:['missing']}));
  assert.equal(contents.open({url:'https://example.test',frameName:'anything'}).action,'deny');
  const allocation=manager.prepare({id:'tab',panes:['pty','rdp'],title:'Test'});
  assert.throws(()=>manager.prepare({id:'tab2',panes:['rdp']}));
  assert.equal(contents.open({url:allocation.url,frameName:'wrong'}).action,'deny');
  const result=contents.open({url:allocation.url,frameName:'astraterm-'+allocation.token});
  assert.equal(result.action,'allow');assert.equal(result.overrideBrowserWindowOptions.webPreferences.sandbox,true);
  const child=new EventEmitter();child.webContents=new EventEmitter();child.webContents.setWindowOpenHandler=()=>{};
  child.setTitle=()=>{};child.isDestroyed=()=>false;child.close=()=>child.emit('closed');
  contents.emit('did-create-window',child,{url:allocation.url});
  assert.equal(contents.open({url:allocation.url,frameName:'astraterm-'+allocation.token}).action,'deny');
  await manager.ready(allocation.token);assert.deepEqual(moves,[['pty',child],['rdp',child]]);
  let prevented=false;child.emit('close',{preventDefault(){prevented=true;}});
  assert(prevented);assert.deepEqual(events,[['floating:return',{id:'tab'}]]);
  await manager.dock(allocation.token);assert.deepEqual(moves.slice(-2),[['pty',owner],['rdp',owner]]);
  assert.equal(manager.records.size,0);
});
