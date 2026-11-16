'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const {dialog}=require('electron');
const {createFixture}=require('./smoke-fixture.cjs');
const { defaultSession } = require('./backend/store.cjs');
const { CredentialVault } = require('./backend/vault.cjs');

function writeMobaConfig(directory, hosts, master) {
  const digest = crypto.createHash('sha512').update(master).digest();
  const entropy = 'AstraTerm synthetic MobaXterm config';
  const powershell = path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const protect = `
    $ErrorActionPreference = 'Stop'
    Add-Type -AssemblyName System.Security
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $blob = [Security.Cryptography.ProtectedData]::Protect(
      [Text.Encoding]::ASCII.GetBytes($request.digest),
      [Text.Encoding]::ASCII.GetBytes($request.entropy), 'CurrentUser')
    [Console]::Write([Convert]::ToBase64String($blob, 20, $blob.Length - 20))
  `;
  const blob = execFileSync(powershell, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(protect, 'utf16le').toString('base64')],
    { input: JSON.stringify({ entropy, digest: digest.toString('base64') }), encoding: 'utf8', windowsHide: true, timeout: 12000 });
  // Fixed CFB8 vector for the synthetic master and password in this smoke test.
  const value = '_@0123456789abcdefXY4Gms8ww0XtDXlVjKlb8Hn4xN4svSd_prrH0NgujqdVQ8@9zd@q4A0A==';
  fs.writeFileSync(path.join(directory, 'MobaXterm.ini'), [
    '[Misc]', 'SessionP=' + entropy, '[Sesspass]', 'Fixture=' + blob,
    '[Credentials]', 'Shared=smoke-user:' + value, 'NoPassword=smoke-user:',
    '[Passwords]', 'rdp3389:tester@empty.example=' + value.slice(0, 20), 'rdp3390:tester@desktop.example=' + value,
  ].join('\n') + '\n' + fs.readFileSync(hosts, 'utf8'));
}

module.exports=async function checkImports({window,terminals,files,run,until,delay,check,output}){
  const directory=await run('__astraTest.state.bootstrap.appData'),base=path.join(directory,'import-fixture');fs.mkdirSync(base,{recursive:true});
  const password='TEST_ONLY_imported_password=пароль',master='TEST_ONLY_vault_master_password';
  const fixture=await createFixture(base,{password}),previousPicker=dialog.showOpenDialog;
  const fields=Array(37).fill('');Object.assign(fields,{0:'0',1:'127.0.0.1',2:String(fixture.profilebase.port),3:'[Shared]',5:'-1',6:'-1',19:'0'});
  const hosts=path.join(base,'sessions.mxtsessions'),secrets=path.join(base,'passwords.txt');
  fs.writeFileSync(hosts,'[Bookmarks]\nSubRep=Imported\nImgNum=41\n'+['Imported A','Imported B'].map(name=>name+'=#109#'+fields.join('%')+'#font#0# #-1').join('\n')+'\n');
  fs.writeFileSync(secrets,'Shared(smoke-user) = '+password+'\n');
  const answerMaster=async value=>{
    await until(()=>run('Boolean([...document.querySelectorAll("dialog[open]")].find(d=>d.querySelector("input[type=password]")))'),'Master-password dialog');
    await run(`(()=>{const d=[...document.querySelectorAll('dialog[open]')].find(d=>d.querySelector('input[type=password]'));d.querySelector('input').value=${JSON.stringify(value)};d.querySelector('.primary').click()})()`);
    await delay(120);
  };
  let profiles;
  try{
    await check('import preview, master vault and shared password profile connect real SSH/SFTP',async()=>{
      dialog.showOpenDialog=async()=>({canceled:false,filePaths:[hosts,secrets]});
      await run('__astraTest.importDialog().catch(e=>{window.__importError=e.message});void 0');
      await until(()=>run('Boolean(document.querySelector(".import-preview"))'),'Import preview not shown');
      const preview=await run('document.querySelector(".import-preview").textContent');
      assert.ok(preview.includes('Imported A')&&preview.includes('Shared')&&!preview.includes(password));
      assert.equal(await run('__astraTest.state.store.sessions.some(s=>s.name==="Imported A")'),false);
      fs.writeFileSync(path.join(output,'windows-import-preview.png'),(await window.webContents.capturePage()).toPNG());
      await run('document.querySelector("dialog[open] .primary").click()');
      await answerMaster(master);await answerMaster(master);
      await until(()=>run('__astraTest.state.store.sessions.filter(s=>s.group==="Imported").length===2'),'Import not saved');
      await until(()=>run('!document.querySelector("dialog[open]")'),'Import dialog did not close');
      profiles=await run('__astraTest.state.store.sessions.filter(s=>s.group==="Imported")');
      assert.equal(profiles[0].credential_profile,profiles[1].credential_profile);
      assert.ok(profiles.every(profile=>!profile.x11));
      assert.ok(!JSON.stringify(profiles).includes(password));
      assert.ok(!fs.readFileSync(path.join(directory,'sessions.json'),'utf8').includes(password));
      assert.ok(!fs.readFileSync(path.join(directory,'credentials.vault.json'),'utf8').includes(password));
      for(const [index,profile]of profiles.entries()){
        await run(`__astraTest.openSession(${JSON.stringify(profile)}).then(record=>{window.__importedTerminal=record.id}).catch(e=>{window.__importError=e.message});void 0`);
        if(index===0){
          await until(()=>run('Boolean(document.querySelector("dialog[open] .fingerprint"))'),'Imported host key not verified');
          await run('document.querySelector("dialog[open] .primary").click()');
        }
        await until(()=>terminals.list().filter(item=>item.profile.group==='Imported'&&item.status==='running').length===index+1,'Saved credential did not authenticate');
        await until(()=>run('__astraTest.state.files.mode==="sftp"&&__astraTest.state.files.entries.some(e=>e.name==="welcome.txt")'),'Imported SSH SFTP did not follow active pane');
      }
      assert.equal(fixture.counters.authenticated,2);
      assert.equal(await run('Boolean(document.querySelector("dialog[open]"))'),false);
      assert.ok(!JSON.stringify(await run('window.astra.invoke("bootstrap")')).includes(password));
    });
    await check('vault lock, wrong master and later unlock never reuse unrelated credentials',async()=>{
      await run('__astraTest.vaultDialog().catch(e=>{window.__importError=e.message});void 0');
      await until(()=>run('Boolean([...document.querySelectorAll("dialog[open] h2")].find(e=>e.textContent==="Хранилище учётных данных"))'),'Vault management UI');
      assert.ok(!(await run('document.querySelector("dialog[open]").textContent')).includes(password));
      assert.ok(await run('document.querySelector("dialog[open]").textContent.includes("ключ")'),'Key profiles missing from unified vault');
      await run("[...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent==='Добавить').click()");
      await until(()=>run('Boolean(document.querySelector("dialog[open] [name=protocol]"))'),'Credential type form missing');
      await run(`(()=>{const d=[...document.querySelectorAll('dialog[open]')].at(-1);const p=d.querySelector('[name=protocol]');p.value='rdp';p.dispatchEvent(new Event('change'));d.querySelector('[name=name]').value='RDP UI fixture';d.querySelector('[name=username]').value='tester';d.querySelector('[name=password]').value='TEST_ONLY_created_rdp';if(d.querySelector('[name=password]').type!=='password')throw Error('Unmasked credential');d.querySelector('.primary').click()})()`);
      await until(()=>run('__astraTest.state.vault.profiles.some(p=>p.name==="RDP UI fixture"&&p.protocol==="rdp")'),'Typed RDP credential not saved');
      await until(()=>run('![...document.querySelectorAll("dialog[open] h2")].some(h=>h.textContent==="Новый профиль")'),'Credential form did not close');
      assert.ok(!fs.readFileSync(path.join(directory,'credentials.vault.json'),'utf8').includes('TEST_ONLY_created_rdp'));
      fs.writeFileSync(path.join(output,'windows-credentials.png'),(await window.webContents.capturePage()).toPNG());
      await run("[...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent==='Заблокировать пароли').click()");
      await until(()=>run('__astraTest.state.vault.unlocked===false'),'Vault did not lock');
      await run(`window.__unlockError=null;__astraTest.openSession(${JSON.stringify(profiles[0])}).catch(e=>{window.__unlockError=e.message});void 0`);
      await answerMaster('TEST_ONLY_wrong_master');
      await until(()=>run('Boolean(window.__unlockError)'),'Wrong master was not rejected');
      assert.equal(fixture.counters.authenticated,2);assert.equal(await run('__astraTest.state.vault.unlocked'),false);
      await run(`__astraTest.openSession(${JSON.stringify(profiles[0])}).catch(e=>{window.__importError=e.message});void 0`);
      await answerMaster(master);
      await until(()=>fixture.counters.authenticated===3,'Saved vault did not unlock again');
      await until(()=>terminals.list().filter(item=>item.profile.group==='Imported'&&item.status==='running').length===3,'Unlocked session did not become ready');
      await run('document.querySelector("dialog[open]").close()');
      await until(() => run('!document.querySelector("dialog[open]")'), 'Vault management dialog did not close');
      for(const record of terminals.list()){files.closeTerminal(record.id);terminals.close(record.id);}
      await run('for(const id of [...__astraTest.state.panes.keys()])__astraTest.removePane(id);void 0');
    });
    await check('MobaXterm menu imports local DPAPI credentials and links existing sessions', async () => {
      const mobaMaster = 'TEST_ONLY_MobaXterm_master';
      writeMobaConfig(directory, hosts, mobaMaster);
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path.join(directory, 'MobaXterm.ini')] });
      const named = { ...profiles[0], credential_profile: '', credential_label: '', remote_command: 'echo preserve-settings' };
      const desktop = { ...defaultSession('rdp'), name: 'Desktop fixture', host: 'desktop.example', port: 3390, username: 'tester' };
      await run(`astra.invoke('session:save',{record:${JSON.stringify(named)}})`);
      await run(`astra.invoke('session:save',{record:${JSON.stringify(desktop)}})`);
      const start = () => run('__astraTest.action("import-mobaxterm").catch(error=>{window.__mobaError=error.message});void 0');
      await start();
      await answerMaster('TEST_ONLY_wrong_MobaXterm_master');
      await until(() => run('Boolean(window.__mobaError)'), 'Wrong MobaXterm master was accepted');
      await start();
      await answerMaster(mobaMaster);
      await until(() => run('Boolean(document.querySelector(".import-preview"))'), 'Local MobaXterm preview missing');
      const preview = await run('document.querySelector(".import-preview").textContent');
      assert.ok(preview.includes(named.name) && preview.includes(desktop.name) && !preview.includes(password));
      assert.ok(preview.includes('Пропущено записей без пароля — 2'));
      assert.equal(await run(`__astraTest.state.store.sessions.find(s=>s.id===${JSON.stringify(named.id)}).credential_profile`), '');
      await run('document.querySelector("dialog[open] .primary").click()');
      await until(() => run('!document.querySelector(".import-preview")'), 'Local MobaXterm import did not complete');
      const imported = await run('__astraTest.state.store.sessions');
      assert.equal(imported.find(row => row.id === named.id).credential_profile, profiles[0].credential_profile);
      assert.equal(imported.find(row => row.id === named.id).remote_command, named.remote_command);
      const desktopCredential = imported.find(row => row.name === desktop.name).credential_profile;
      const vault = await run('__astraTest.state.vault.profiles');
      assert.ok(vault.some(record => record.id === desktopCredential && record.protocol === 'rdp'));
      const vaultPath = path.join(directory, 'credentials.vault.json');
      assert.ok(!fs.readFileSync(vaultPath, 'utf8').includes(password));
      const reopened = new CredentialVault(vaultPath);
      await reopened.unlock(master);
      try { assert.ok(reopened.get(desktopCredential, 'rdp').password === password, 'Imported password did not round-trip'); }
      finally { reopened.lock(); }
    });
    await check('vault manager changes and resets the master password without changing sessions', async () => {
      const changedMaster = 'TEST_ONLY_changed_master';
      const resetMaster = 'TEST_ONLY_reset_master';
      const vaultPath = path.join(directory, 'credentials.vault.json');
      const sessionsPath = path.join(directory, 'sessions.json');
      const sessionsBefore = fs.readFileSync(sessionsPath);
      const before = new CredentialVault(vaultPath);
      await before.unlock(master);
      const profilesBefore = before.metadata();
      before.lock();
      await run('__astraTest.vaultDialog().catch(error=>{window.__vaultError=error.message});void 0');
      const click = label => run(`[...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent===${JSON.stringify(label)}).click()`);
      await until(() => run('Boolean(document.querySelector("dialog[open] h2"))'), 'Credential manager did not open');
      const enter = (current, replacement) => run(`(()=>{
        const form = [...document.querySelectorAll('dialog[open]')].at(-1);
        const current = form.querySelector('[name=current]');
        if (Boolean(current) !== ${Boolean(current)}) throw Error('Wrong master-password form');
        if (current) current.value = ${JSON.stringify(current)};
        for (const input of form.querySelectorAll('input')) if (input.type !== 'password') throw Error('Unmasked field');
        form.querySelector('[name=master]').value = ${JSON.stringify(replacement)};
        form.querySelector('[name=repeat]').value = ${JSON.stringify(replacement)};
      })()`);
      await click('Изменить мастер-пароль');
      await enter(master, changedMaster);
      await click('Сохранить новый пароль');
      await until(() => run('!document.querySelector("dialog[open] [name=current]")'), 'Change-password form did not close');
      const changed = new CredentialVault(vaultPath);
      await changed.unlock(changedMaster);
      assert.deepEqual(changed.metadata(), profilesBefore);
      assert.ok(changed.get(profiles[0].credential_profile).password === password);
      changed.lock();
      await assert.rejects(changed.unlock(master));
      await click('Заблокировать пароли');
      await until(() => run('__astraTest.state.vault.unlocked===false'), 'Vault did not lock before reset');
      await click('Сбросить мастер-пароль');
      await click('Отмена');
      await changed.unlock(changedMaster);
      assert.deepEqual(changed.metadata(), profilesBefore);
      changed.lock();
      await click('Сбросить мастер-пароль');
      await enter(null, resetMaster);
      await click('Сбросить и удалить пароли');
      await until(() => run('!document.querySelector("dialog[open] [name=repeat]")'), 'Reset-password form did not close');
      await changed.unlock(resetMaster);
      assert.deepEqual(changed.metadata(), []);
      changed.lock();
      await assert.rejects(changed.unlock(changedMaster));
      assert.deepEqual(fs.readFileSync(sessionsPath), sessionsBefore);
    });
  }finally{dialog.showOpenDialog=previousPicker;await fixture.close();}
};
