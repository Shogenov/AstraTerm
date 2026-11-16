'use strict';
// Explicit opt-in real-server integration. Credentials are supplied by the developer environment,
// encrypted in isolated smoke data and removed from the environment before spawning any RDP child.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
module.exports=async({window,rdp,vault,finish})=>{
  const configuration={host:process.env.ASTRA_RDP_TEST_HOST,username:process.env.ASTRA_RDP_TEST_USER,password:process.env.ASTRA_RDP_TEST_PASSWORD};
  for(const key of ['HOST','USER','PASSWORD'])delete process.env['ASTRA_RDP_TEST_'+key];
  const output=path.resolve(process.env.ASTRA_SMOKE_OUTPUT||'../artifacts/rdp/windows');fs.mkdirSync(output,{recursive:true});
  const report={platform:'windows',checks:[],result:'RUNNING'};
  const run=code=>window.webContents.executeJavaScript(code,true);
  const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const until=async(fn,timeout=90000)=>{const end=Date.now()+timeout;while(!await fn()){if(Date.now()>end)throw new Error('RDP integration timed out');await delay(150);}};
  try {
    assert(configuration.host&&configuration.username&&configuration.password,'Supply RDP test configuration.');
    await until(()=>run('!!window.__astraTest'));
    await vault.unlock('RDP smoke master password',{create:true});vault.merge([{id:'rdp-smoke',name:'RDP smoke',username:configuration.username,password:configuration.password}]);configuration.password=null;
    window.show();
    const profile={protocol:'rdp',name:'RDP integration',host:configuration.host,username:configuration.username,port:3389,credential_profile:'rdp-smoke'};
    const record=await run(`__astraTest.openSession(${JSON.stringify(profile)})`);
    await until(()=>{const item=rdp.get(record.id);if(item?.errorCode)throw new Error('RDP helper error '+item.errorCode);return item?.hwnd;},15000);report.checks.push('native child created and parented to AstraTerm');
    assert.equal(rdp.get(record.id).parent,(window.getNativeWindowHandle().readBigUInt64LE()).toString());
    fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'waiting for RDP login',helperPid:rdp.get(record.id).child.pid},null,2));
    await until(()=>{const item=rdp.get(record.id);if(item?.status==='error')throw new Error('Native RDP connection error');return item?.status==='running';},process.env.ASTRA_RDP_MANUAL==='1'?900000:90000);
    report.checks.push('real Windows desktop authenticated');
    if(process.env.ASTRA_RDP_MANUAL==='1'){
      fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'manual',helperPid:rdp.get(record.id).child.pid,parent:rdp.get(record.id).parent,surface:rdp.get(record.id).hwnd}));
      await until(()=>fs.existsSync(path.join(output,'manual.done')),1800000);report.result='PASS';return;
    }
    await delay(3000);
    window.focus();await delay(300);
    assert.equal(rdp.get(record.id).surface?.visible,true);
    assert.equal(rdp.get(record.id).surface?.frontmost,true);
    fs.writeFileSync(path.join(output,'geometry.json'),JSON.stringify({surface:rdp.get(record.id).surface,expected:rdp.get(record.id).bounds},null,2));
    for(const key of ['x','y','width','height'])assert.equal(rdp.get(record.id).surface[key],rdp.get(record.id).bounds[key]);
    report.checks.push('native HWND mapped above Chromium at the view bounds');
    fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'baseline',helperPid:rdp.get(record.id).child.pid,parent:rdp.get(record.id).parent,surface:rdp.get(record.id).hwnd}));
    if(process.env.ASTRA_RDP_CAPTURE==='1')await until(()=>fs.existsSync(path.join(output,'baseline.done')),15000);
    const {BrowserWindow}=require('electron');
    const other=new BrowserWindow({width:300,height:180,x:10,y:10,show:true,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
    const displayChanges=rdp.get(record.id).display?.changes;
    await other.loadURL('about:blank');other.focus();await delay(1000);
    assert.equal(rdp.get(record.id).bounds.visible,true);assert.equal(rdp.get(record.id).surface.visible,true);
    fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'background',helperPid:rdp.get(record.id).child.pid,parent:rdp.get(record.id).parent,surface:rdp.get(record.id).hwnd}));
    if(process.env.ASTRA_RDP_CAPTURE==='1')await until(()=>fs.existsSync(path.join(output,'background.done')),45000);
    window.focus();await delay(700);other.destroy();
    assert.equal(rdp.get(record.id).surface.visible,true);
    assert.equal(rdp.get(record.id).display?.changes,displayChanges,'Focus must not renegotiate RDP display settings');
    report.checks.push('native RDP remains visible on host blur and restores without a remote click');
    fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'authenticated',helperPid:rdp.get(record.id).child.pid,parent:rdp.get(record.id).parent,surface:rdp.get(record.id).hwnd},null,2));
    if(process.env.ASTRA_RDP_CAPTURE==='1')await until(()=>fs.existsSync(path.join(output,'capture.done')),15000);
    assert.equal(rdp.get(record.id).inputError,undefined,'Native RDP key forwarding failed: '+rdp.get(record.id).inputError);
    // capturePage excludes native HWND surfaces. Desktop screenshot is verified separately by the harness.
    await run(`__astraTest.openSession({protocol:'local',name:'Local while RDP stays connected'})`);
    await delay(500);assert.equal(rdp.get(record.id).bounds.visible,false);report.checks.push('hidden tab retains connection and hides child');
    await run(`__astraTest.focusPane(${JSON.stringify(record.id)})`);await delay(500);assert.equal(rdp.get(record.id).bounds.visible,true);
    window.setSize(1120,720);await delay(700);const first={...rdp.get(record.id).bounds};window.setSize(1400,900);await delay(700);
    assert(rdp.get(record.id).bounds.width>first.width);assert(rdp.get(record.id).bounds.height>first.height);report.checks.push('native view resizes with application');
    assert.equal(await run(`__astraTest.router.selected.has(${JSON.stringify(record.id)})`),false);report.checks.push('RDP excluded from MultiExec');
    await run(`(()=>{const d=document.createElement('dialog');d.id='rdp-smoke-modal';d.textContent='Modal test';document.body.append(d);d.showModal();})()`);await delay(300);assert.equal(rdp.get(record.id).bounds.visible,false);
    await run(`document.getElementById('rdp-smoke-modal').close()`);await delay(300);assert.equal(rdp.get(record.id).bounds.visible,true);report.checks.push('HTML modal hides native child and restores it afterwards');
    window.minimize();await until(()=>rdp.get(record.id).surface?.visible===false,5000);
    window.restore();await until(()=>rdp.get(record.id).surface?.visible===true,5000);
    report.checks.push('minimize hides the native child and restore repaints without a remote click');
    const floatingChild=rdp.get(record.id).child,generation=rdp.get(record.id).generation;
    const group=await run(`__astraTest.state.groups.find(group=>group.panes.includes(${JSON.stringify(record.id)})).id`);
    assert.equal(await run(`__astraTest.floatWindow(${JSON.stringify(group)})`),true);await delay(1700);
    const floating=BrowserWindow.getAllWindows().find(item=>item!==window);assert(floating);
    assert.equal(rdp.get(record.id).parent,floating.getNativeWindowHandle().readBigUInt64LE().toString());
    assert.equal(rdp.get(record.id).surface.visible,true);
    fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'floating',helperPid:floatingChild.pid,parent:rdp.get(record.id).parent,surface:rdp.get(record.id).hwnd}));
    if(process.env.ASTRA_RDP_CAPTURE==='1')await until(()=>fs.existsSync(path.join(output,'floating.done')),15000);
    floating.close();await until(()=>floating.isDestroyed(),7000);await delay(500);
    assert.equal(rdp.get(record.id).parent,window.getNativeWindowHandle().readBigUInt64LE().toString());
    assert.equal(rdp.get(record.id).child,floatingChild);assert.equal(rdp.get(record.id).generation,generation);
    assert.equal(rdp.get(record.id).surface.visible,true);
    report.checks.push('RDP floats and returns on native close without reconnect, reparented to verified owner HWND');
    const oldChild=rdp.get(record.id).child;rdp.reconnect(record.id);await until(()=>rdp.get(record.id).child&&rdp.get(record.id).child!==oldChild,15000);
    fs.writeFileSync(path.join(output,'progress.json'),JSON.stringify({stage:'reconnecting',helperPid:rdp.get(record.id).child.pid},null,2));
    await until(()=>rdp.get(record.id).status==='running');assert.notEqual(oldChild.exitCode,null);report.checks.push('real reconnect replaces and reaps owned native helper');
    const child=rdp.get(record.id).child;await run(`__astraTest.closePane(${JSON.stringify(record.id)},false)`);await until(()=>child.exitCode!==null,7000);report.checks.push('tab close terminates its own native helper');
    report.result='PASS';
  }catch(error){report.result='FAIL';report.error=error.message;report.states=rdp.list().map(record=>({status:record.status,generation:record.generation}));}
  finally{fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));await finish(report.result==='PASS'?0:1);}
};
