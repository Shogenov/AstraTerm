'use strict';
const assert=require('node:assert/strict'),{dialog}=require('electron');
module.exports=async({run,until,delay,check,terminals,files,fixture,first,second})=>{
 await check('saved SSH settings, explicit file picker and tab reconnect replace failed authentication',async()=>{
  const initial=terminals.get(first),other=terminals.get(second),otherClient=other.sshClient;
  const saved={...initial.profile,username:'fixture-wrong-user'};
  await run(`astra.invoke('session:save',{record:${JSON.stringify(saved)}})`);
  await run(`astra.invoke('terminal:reconnect',{id:${JSON.stringify(first)}}).catch(e=>{window.__expectedAuthError=e.message})`);
  assert.equal(terminals.get(first).profile.username,'fixture-wrong-user');assert.equal(terminals.get(first).status,'error');
  assert.match(await run('__expectedAuthError'),/аутентификацию/);
  await run(`__astraTest.action('edit-session',{id:${JSON.stringify(saved.id)}})`);
  assert.equal(await run('document.querySelector("dialog[open] [name=key_file]").disabled'),true);
  const picker=dialog.showOpenDialog;dialog.showOpenDialog=async()=>({canceled:false,filePaths:[fixture.keyFile]});
  try{await run(`(()=>{const d=document.querySelector('dialog[open]');d.querySelector('[name=username]').value='smoke-user';d.querySelector('[name=username]').dispatchEvent(new Event('input'));[...d.querySelectorAll('button')].find(b=>b.textContent==='Обзор').click()})()`);
   await until(()=>run('document.querySelector("dialog[open] [name=identity_profile]").value==="system"'),'File picker did not select own key');
   assert.equal(await run('document.querySelector("dialog[open] [name=key_file]").disabled'),false);
   await run('document.querySelector("dialog[open] .primary").click()');await until(()=>run('!document.querySelector("dialog[open]")'),'Session edit save');
  }finally{dialog.showOpenDialog=picker;}
  const group=await run(`__astraTest.state.groups.find(g=>g.panes.includes(${JSON.stringify(first)})).id`);
  await run(`__astraTest.focusPane(${JSON.stringify(second)})`);
  await until(()=>!files.busy,'File listing after focus');
  await run(`__astraTest.tabAction('reconnect',${JSON.stringify(group)})`);
  await until(()=>terminals.get(first)?.status==='running','Updated SSH settings did not authenticate');
  assert.equal(terminals.get(first).profile.identity_profile,'system');assert.equal(terminals.get(first).profile.key_file,fixture.keyFile);
  assert.equal(other.sshClient,otherClient);assert.equal(other.generation,1);
  const request={mode:'sftp',terminalId:first,path:'/welcome.txt'},before=await files.readText(request);
  await assert.rejects(files.writeText({...request,generation:1,text:'MUST_NOT_WRITE_TO_REPLACEMENT',revision:before.revision}),/изменилось/);
  assert.equal((await files.readText(request)).text,before.text);
 });
};
