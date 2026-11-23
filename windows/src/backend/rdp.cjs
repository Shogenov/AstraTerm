'use strict';
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');

// One owned native child per tab. Private pipes carry passwords, never argv/environment/files.
class RdpManager {
  constructor({window, emit, prompt, credentialProvider, executable, spawnProcess=spawn}) {
    Object.assign(this,{window,emit,prompt,credentialProvider});
    this.executable=executable || path.join(__dirname,'..','..','native','AstraTerm.RdpHost.exe').replace('app.asar'+path.sep,'app.asar.unpacked'+path.sep);
    this.records=new Map();this.disposed=false;
    this.spawnProcess=spawnProcess;
  }
  get(id){return this.records.get(id);}
  list(){return [...this.records.values()].map(record=>this.snapshot(record));}
  snapshot(record){return {id:record.id,profile:{...record.profile},generation:record.generation,status:record.status};}
  create(profile) {
    if(this.disposed)throw new Error('Приложение закрывается.');
    if(this.records.size>=32)throw new Error('Закройте лишние RDP-вкладки.');
    const record={id:crypto.randomUUID(),profile:{...profile},generation:0,status:'connecting',child:null,bounds:null};
    this.records.set(record.id,record);this.emit('terminal:created',this.snapshot(record));
    void this.start(record);return this.snapshot(record);
  }
  current(record,generation){return !this.disposed&&this.get(record.id)===record&&record.generation===generation&&record.status!=='closed';}
  status(record,status,message){record.status=status;this.emit('terminal:status',{id:record.id,generation:record.generation,status,...(message?{message}:{})});}
  async start(record) {
    const generation=++record.generation;
    this.status(record,'connecting');
    try {
      const profile=record.profile;
      if(profile.jump_host)throw new Error('RDP gateway is not supported.');
      if(!profile.username) {
        const username=await this.prompt({kind:'rdp-username',title:'Пользователь RDP',message:`Пользователь · ${profile.host}`,secret:false,textInput:true,label:'Пользователь'});
        if(!this.current(record,generation))return;
        if(username===null){this.status(record,'exited','Подключение RDP отменено.');return;}
        if(typeof username!=='string'||!username.trim()||username.length>255||/[\r\n\0]/.test(username))throw new Error('Invalid RDP username');
        profile.username=username.trim();
      }
      let password=profile.credential_profile?await this.credentialProvider(profile):await this.prompt({kind:'rdp-password',title:'Подключение RDP',message:`Пароль ${profile.username || 'пользователя'} · ${profile.host}`,secret:true});
      if(!this.current(record,generation))return;
      if(password===null){this.status(record,'exited','Подключение RDP отменено.');return;}
      if(typeof password!=='string'||/[\r\n\0]/.test(password)||Buffer.byteLength(password,'utf8')>511)throw new Error('Unsupported RDP password format');
      const handle=(record.window||this.window).getNativeWindowHandle();
      const owner=(handle.length===8?handle.readBigUInt64LE():BigInt(handle.readUInt32LE())).toString();
      const child=this.spawnProcess(this.executable,[owner],{stdio:['pipe','pipe','pipe'],windowsHide:true,shell:false});
      record.child=child;delete record.nativeParent;
      child.stdin.on('error',()=>{});child.stderr.resume();
      const lines=createInterface({input:child.stdout});
      const readyTimeout=setTimeout(()=>{if(this.current(record,generation)&&!record.hwnd){this.status(record,'error','Встроенный RDP-клиент не отвечает.');this.stop(record);}},15000);readyTimeout.unref();
      lines.on('line',line=>{
        if(!this.current(record,generation)||line.length>4096)return;
        let event;try{event=JSON.parse(line);}catch{return;}
        if(event.type==='ready') {
          clearTimeout(readyTimeout);
          record.hwnd=event.hwnd;record.parent=event.parent;record.nativeParent=event.parent;
          if(record.window){const bounds=record.bounds;this.setWindow(record.id,record.window);record.bounds=bounds;}
          const viewport=record.bounds;
          const [width,height]=viewport?.width>=200&&viewport.height>=200
            ? [Math.min(8192,viewport.width - viewport.width % 2),Math.min(8192,viewport.height)]
            : profile.rdp_resolution.split('x').map(Number);
          let username=profile.username,domain=profile.rdp_domain;
          if(!domain&&username.includes('\\')){[domain,username]=username.split('\\',2);}
          if(record.bounds)this.send(record,{op:'bounds',...record.bounds});
          this.send(record,{op:'connect',host:profile.host,username,domain,port:profile.port,width,height,password});password=null;
        } else if(event.type==='running')this.status(record,'running');
        else if(event.type==='surface')record.surface=event;
        else if(event.type==='display')record.display={width:event.width,height:event.height,changes:event.changes};
        else if(event.type==='input-error')record.inputError=Number(event.code);
        else if(event.type==='parent'){record.parent=event.parent;record.nativeParent=event.parent;}
        else if(event.type==='disconnected')this.status(record,'exited',`RDP отключён (код ${Number(event.code)}). Переподключение — в меню.`);
        else if(event.type==='error'){record.errorCode=Number(event.code);this.status(record,'error',`Ошибка встроенного RDP (код ${Number(event.code)}). Проверьте адрес и учётные данные.`);}
      });
      child.once('error',()=>{password=null;if(this.current(record,generation))this.status(record,'error','Не удалось запустить встроенный RDP. Пересоберите Windows-версию.');});
      child.once('exit',()=>{clearTimeout(readyTimeout);password=null;lines.close();if(record.child===child)record.child=null;if(this.current(record,generation)&&!['error','exited'].includes(record.status))this.status(record,'exited','RDP-клиент завершён.');});
    }catch {if(this.current(record,generation))this.status(record,'error','Не удалось открыть RDP. Проверьте хранилище учётных данных.');}
  }
  send(record,command){if(record.child?.stdin.writable)record.child.stdin.write(JSON.stringify(command)+'\n');}
  bounds(id,value) {
    const record=this.get(id);if(!record)return false;
    if(typeof value.visible!=='boolean'||!['x','y','width','height','scale'].every(key=>Number.isFinite(value[key])))throw new Error('Неверная область RDP.');
    const owner=record.window||this.window;
    const [vw,vh]=owner.getContentSize();
    const scale=Math.max(0.25,Math.min(8,value.scale));
    const x=Math.max(0,Math.min(vw,value.x)),y=Math.max(0,Math.min(vh,value.y));
    // Electron content size is in DIP; renderer bounds use CSS px. Clamp before conversion.
    const width=Math.max(1,Math.min(vw-x,value.width)),height=Math.max(1,Math.min(vh-y,value.height));
    record.bounds={x:Math.round(x*scale),y:Math.round(y*scale),width:Math.round(width*scale),height:Math.round(height*scale),visible:value.visible};
    this.send(record,{op:'bounds',...record.bounds,visible:record.bounds.visible&&!owner.isMinimized()});return true;
  }
  focus(id) {
    const record=this.get(id);
    if(!record?.bounds?.visible)return false;
    const owner=record.window||this.window;
    if(owner.isMinimized()||!owner.isFocused())return false;
    this.send(record,{op:'focus'});
    return true;
  }
  setWindow(id,owner) {
    const record=this.get(id);if(!record)return false;
    record.window=owner;
    const handle=owner.getNativeWindowHandle();const parent=(handle.length===8?handle.readBigUInt64LE():BigInt(handle.readUInt32LE())).toString();
    record.parent=parent;record.bounds=null;
    this.send(record,{op:'reparent',parent});return true;
  }
  async settledWindow(id,owner) {
    const handle=owner.getNativeWindowHandle();const parent=(handle.length===8?handle.readBigUInt64LE():BigInt(handle.readUInt32LE())).toString();
    const record=this.get(id),end=Date.now()+5000;
    while(this.get(id)===record&&record?.child&&record.nativeParent!==parent) {
      if(Date.now()>end)throw new Error('RDP-клиент не подтвердил перенос. Окно оставлено открытым.');
      await new Promise(resolve=>setTimeout(resolve,20));
    }
    return true;
  }
  refresh(owner=this.window) {
    for(const record of this.records.values())if((record.window||this.window)===owner && record.bounds) {
      this.send(record,{op:'bounds',...record.bounds,visible:record.bounds.visible&&!owner.isMinimized()});
      if(record.bounds.visible&&!owner.isMinimized())this.send(record,{op:'refresh'});
    }
  }
  stop(record) {
    const child=record.child;record.child=null;if(!child)return;
    if(child.stdin.writable)child.stdin.write('{"op":"bounds","visible":false}\n');
    this.sendChildClose(child);
    const timer=setTimeout(()=>{if(child.exitCode===null&&!child.killed)child.kill();},2000);timer.unref();child.once('exit',()=>clearTimeout(timer));
  }
  sendChildClose(child){if(child.stdin.writable){child.stdin.end('{"op":"close"}\n');}}
  close(id){const record=this.get(id);if(!record)return false;record.generation++;this.status(record,'closed');this.records.delete(id);this.stop(record);return true;}
  reconnect(id,profile){const record=this.get(id);if(!record)return false;record.generation++;this.stop(record);if(profile){record.profile={...profile};this.emit('terminal:profile',{id,profile:{...profile},generation:record.generation});}delete record.hwnd;void this.start(record);return true;}
  dispose(){this.disposed=true;for(const id of [...this.records.keys()])this.close(id);}
}
module.exports={RdpManager};
