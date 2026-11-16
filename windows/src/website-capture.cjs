'use strict';
// Explicit capture fixture: real SSH2/xterm/SFTP with generated keys and demo-only files.
const fs=require('node:fs'),path=require('node:path');
const {createFixture}=require('./smoke-fixture.cjs');
module.exports=async function({window,store,terminals,identities,files,finish}){
  const run=code=>window.webContents.executeJavaScript(code,true),delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const until=async predicate=>{for(let n=0;n<300;n++){if(await predicate())return;await delay(100);}throw Error('Website fixture timed out');};
  const fixtures=[];
  try{
    await until(()=>run('Boolean(window.__astraTest?.ready)'));
    window.setSize(1440,900);window.center();window.show();
    const initial=await run('[...__astraTest.state.panes.keys()][0]');
    await until(()=>!files.busy);
    await run(`__astraTest.closePane(${JSON.stringify(initial)},false)`);
    const opened=[];
    for(const [index,name]of ['web-01','db-01','stage-01'].entries()){
      const banner=`\x1b[2J\x1b[H\x1b[38;5;114mdemo@${name}\x1b[0m ~\r\n\r\n$ hostname\r\n${name}.example.test\r\n\r\n$ systemctl status ${index===1?'postgresql':'nginx'}\r\n\x1b[38;5;114m● ${index===1?'postgresql':'nginx'}.service\r\n  Active: active (running)\x1b[0m\r\n\r\n$ ls -lh /srv/app\r\n\x1b[38;5;110mbackups/  config/  logs/\x1b[0m\r\nREADME.md  deploy.sh\r\n\r\n\x1b[38;5;114mdemo@${name}\x1b[0m:~$ `;
      const fixture=await createFixture(path.join(path.dirname(identities.filename),'photo-'+name),{banner});fixtures.push(fixture);
      for(const dirname of ['config','backups','releases'])fs.mkdirSync(path.join(fixture.remoteRoot,dirname),{recursive:true});
      for(const [file,value]of Object.entries({'README.md':'Demo deployment workspace\n','deploy.sh':'# Demo deployment script\n','nginx.conf':'# Demo configuration\n'}))fs.writeFileSync(path.join(fixture.remoteRoot,file),value);
      const stats=require('./host-stats-contract.cjs');
      for(const [file,value]of Object.entries(stats.files)){const filename=path.join(fixture.remoteRoot,file);fs.mkdirSync(path.dirname(filename),{recursive:true});fs.writeFileSync(filename,file==='/proc/sys/kernel/hostname'?name+'.example.test\n':value);}
      const saved=await run(`astra.invoke('profile:save',{record:{name:'Demo key ${index}',key_file:${JSON.stringify(fixture.keyFile)}}})`);
      const identity=saved.profiles.find(p=>p.key_file===fixture.keyFile).id;
      await run(`window.__photoAnswer=setInterval(()=>{const d=[...document.querySelectorAll('dialog[open]')].at(-1);if(!d)return;const p=d.querySelector('input[type=password]');if(p){p.value=${JSON.stringify(fixture.passphrase)};d.querySelector('.primary').click()}else if(d.querySelector('.fingerprint'))d.querySelector('.primary').click()},60)`);
      const profile={...fixture.profilebase,name,group:'Demo / Infrastructure',identity_profile:identity};
      await run(`astra.invoke('session:save',{record:${JSON.stringify(profile)}})`);
      const id=await run(`__astraTest.openSession(${JSON.stringify(profile)}).then(r=>r.id)`);opened.push(id);
      await run('clearInterval(__photoAnswer)');
    }
    await until(()=>opened.every(id=>terminals.get(id)?.status==='running'));
    await run(`__astraTest.overview(true);__astraTest.multi(true);__astraTest.router.select(${JSON.stringify(opened[0])},true);__astraTest.router.select(${JSON.stringify(opened[2])},true);__astraTest.focusPane(${JSON.stringify(opened[0])});__astraTest.renderLayout();__astraTest.syncFiles()`);
    // Normal renderer receives only the isolated fixture session store.
    await delay(1800);
    const out=process.env.ASTRA_WEBSITE_CAPTURE;
    if(out){fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'windows-app.png'),(await window.webContents.capturePage()).toPNG());}
    process.stdout.write('WEBSITE_CAPTURE_READY\n');
    // Leave this real fixture window open for native visual inspection.
    await delay(300000);
  }catch(error){process.stderr.write('WEBSITE_CAPTURE_FAILED '+error.message+'\n');}
  finally{for(const fixture of fixtures)await fixture.close();await finish();}
};
