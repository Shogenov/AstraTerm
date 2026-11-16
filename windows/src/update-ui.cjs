'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const updates=require('./backend/updates.cjs');

const execute = promisify(execFile);
async function handoffUpdate(config) {
  const scriptDirectory = __dirname.replace('app.asar'+path.sep,'app.asar.unpacked'+path.sep);
  const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  await execute(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(scriptDirectory, 'update-launch.ps1'), '-Helper', path.join(scriptDirectory, 'update-install.ps1'), '-Config', config],
    { windowsHide: true, timeout: 35000 });
  await fs.promises.writeFile(config + '.commit', '', { flag: 'wx', mode: 0o600 });
}

class UpdateController {
  constructor({app,window,directory,dialog,canInstall,finish,isClosing,endInstall,showProgress=()=>{}}){Object.assign(this,{app,window,directory,dialog,canInstall,finish,isClosing,endInstall,showProgress});this.busy=false;}
  async check(automatic=false){
    if(this.busy||this.isClosing())return;
    this.busy=true;
    let locked=false, accepted=false, filename, config, handedOff=false, keepDownload=false;
    try{
      const item=await updates.check();if(this.isClosing())return;
      const target=process.env.PORTABLE_EXECUTABLE_FILE;
      const installed=this.app.isPackaged&&!target&&process.resourcesPath&&fs.existsSync(path.join(process.resourcesPath,'astraterm-installed'));
      if(!item.available){if(!automatic)await this.message('Установлена актуальная версия AstraTerm '+this.app.getVersion()+'.');return;}
      if(installed&&!item.installer){if(!automatic)await this.message('Для этой версии пока нет обновления установленной копии.');return;}
      const answer=await this.dialog.showMessageBox(this.window,{type:'info',title:'Обновления AstraTerm',message:'Доступна версия '+item.version,detail:item.notes+'\n\nСкачать обновление? Файл будет проверен перед установкой.',buttons:['Скачать','Позже'],defaultId:0,cancelId:1});
      if(answer.response!==0||this.isClosing())return;
      accepted=true;
      const useInstaller=this.app.isPackaged&&!target&&Boolean(item.installer);
      const artifact=useInstaller?item.installer:item;
      filename=await this.download(artifact,item.version);
      if(this.isClosing())return;
      if(!this.app.isPackaged||(!target&&!useInstaller)){
        await this.dialog.showMessageBox(this.window,{type:'info',title:'Обновления AstraTerm',message:'Проверенный portable EXE скачан',detail:'Для перехода на portable-версию закройте приложение и запустите:\n'+filename});
        keepDownload=true;
        return;
      }
      const detail=useInstaller?'Активные подключения будут завершены. Откроется окно установщика AstraTerm.':'Активные подключения будут завершены. Предыдущий EXE останется рядом с расширением .previous.';
      const ready=await this.dialog.showMessageBox(this.window,{type:'question',title:'Обновления AstraTerm',message:'AstraTerm '+item.version+' скачан и проверен',detail,buttons:[useInstaller?'Запустить установщик':'Обновить и перезапустить','Позже'],defaultId:1,cancelId:1});
      if(ready.response!==0||this.isClosing()||!await this.canInstall())return;
      locked=true;
      config=path.join(this.directory,'updates','install-'+crypto.randomUUID()+'.json');
      await fs.promises.writeFile(config,JSON.stringify({pid:process.pid,source:filename,
        ...(useInstaller?{mode:'installer'}:{target:path.resolve(target)}),
        sha256:artifact.sha256,bytes:artifact.bytes}),{flag:'wx',mode:0o600});
      await handoffUpdate(config);
      handedOff=true;
      await this.finish();
    }catch{
      if((accepted||!automatic)&&!this.isClosing())await this.message(locked&&!handedOff
        ?'Не удалось запустить установку. AstraTerm продолжает работать. Попробуйте снова или запустите установщик с сайта.'
        :'Не удалось получить или установить проверенное обновление. Проверьте доступ к сайту и папке приложения, затем повторите попытку.');
    }
    finally{
      if(filename&&!handedOff&&!keepDownload)await fs.promises.unlink(filename).catch(()=>{});
      if(config&&!handedOff)await fs.promises.unlink(config).catch(()=>{});
      if(locked)this.endInstall?.();
      this.busy=false;
    }
  }
  async download(artifact, version) {
    let last = 0;
    this.showProgress({version, percent: 0});
    try {
      return await updates.download(artifact, path.join(this.directory, 'updates'), undefined, (received, total) => {
        const percent = Math.floor(received * 100 / total);
        if (percent === last) return;
        last = percent;
        this.showProgress({version, percent});
      });
    } finally {
      this.showProgress(null);
    }
  }
  message(message){return this.dialog.showMessageBox(this.window,{type:'info',title:'Обновления AstraTerm',message});}
}
module.exports={UpdateController,handoffUpdate};
