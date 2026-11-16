'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),updates=require('../src/backend/updates.cjs');
const {UpdateController}=require('../src/update-ui.cjs');
const {download_progress: vector}=require('../../shared/contracts/updates.json');
test('update dialog downloads only verified backend metadata and handles ZIP/development mode',async t=>{
  const item={available:true,version:'99.0.0',notes:'Synthetic'},messages=[];
  t.mock.method(updates,'check',async()=>item);
  t.mock.method(updates,'download',async actual=>{assert.equal(actual,item);return 'C:\\synthetic\\verified.exe';});
  const controller=new UpdateController({app:{isPackaged:false,getVersion:()=> '0.10.0'},window:{},directory:'C:\\synthetic',dialog:{showMessageBox:async(_w,message)=>{messages.push(message);return {response:0};}},finish:()=>{throw Error('Development mode must not close the app');},canInstall:()=>true,isClosing:()=>false});
  await controller.check();assert.equal(messages.length,2);assert.match(messages[1].message,/portable/);assert.equal(controller.busy,false);
});
test('show download progress and offer Setup only after verification',async t=>{
  const events=[];
  const installer={bytes:vector.total};
  const item={available:true,version:'99.0.0',notes:'Synthetic',installer};
  const previous=process.env.PORTABLE_EXECUTABLE_FILE;
  delete process.env.PORTABLE_EXECUTABLE_FILE;
  t.after(()=>{if(previous!==undefined)process.env.PORTABLE_EXECUTABLE_FILE=previous;});
  t.mock.method(updates,'check',async()=>item);
  t.mock.method(updates,'download',async (actual,_directory,_opener,progress)=>{
    assert.equal(actual,installer);
    for(const received of vector.received)progress(received,vector.total);
    events.push('verified');
    return 'C:\\synthetic\\verified-setup.exe';
  });
  const controller=new UpdateController({
    app:{isPackaged:true},window:{},directory:'C:\\synthetic',isClosing:()=>false,
    showProgress:value=>events.push(value?.percent ?? 'closed'),
    dialog:{showMessageBox:async(_window,message)=>{
      events.push(message.buttons[0]);
      return {response:message.buttons[0]==='Скачать'?0:1};
    }},
    canInstall:()=>{throw Error('The user declined installation');},
  });
  await controller.check();
  assert.deepEqual(events,['Скачать',...vector.percent,'verified','closed','Запустить установщик']);
});
test('automatic network failures stay silent and manual checks report failure',async t=>{
  t.mock.method(updates,'check',async()=>{throw Error('offline');});let count=0;
  const controller=new UpdateController({app:{},window:{},dialog:{showMessageBox:async()=>count++},isClosing:()=>false});
  await controller.check(true);assert.equal(count,0);await controller.check(false);assert.equal(count,1);assert.equal(controller.busy,false);
});
