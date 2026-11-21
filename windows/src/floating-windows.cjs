'use strict';
const crypto = require('node:crypto');
const path = require('node:path');

// Windows host presentation only. Panes, transports and the input router stay in the opener.
class FloatingWindows {
  constructor({owner, url, terminals, rdp, emit, backgroundColor}) {
    Object.assign(this,{owner,url,terminals,rdp,emit});
    this.records=new Map();this.closing=false;
    owner.webContents.setWindowOpenHandler(({url,frameName})=>{
      const record=[...this.records.values()].find(item=>url===this.url+'#'+item.token && frameName==='astraterm-'+item.token && !item.window);
      if(!record || this.closing)return {action:'deny'};
      return {action:'allow',overrideBrowserWindowOptions:{width:1000,height:720,minWidth:420,minHeight:300,
        title:record.title,icon:path.join(__dirname,'..','assets','astraterm.ico'),
        backgroundColor:backgroundColor(),autoHideMenuBar:true,
        webPreferences:{contextIsolation:true,sandbox:true,nodeIntegration:false,webviewTag:false,spellcheck:false,backgroundThrottling:false}}};
    });
    owner.webContents.on('did-create-window',(child,{url})=>{
      const record=[...this.records.values()].find(item=>url===this.url+'#'+item.token && !item.window);
      if(!record){child.destroy();return;}
      record.window=child;child.setTitle(record.title);
      child.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      child.webContents.on('will-navigate',(event,target)=>{if(record.loaded||target!==this.url+'#'+record.token)event.preventDefault();});
      child.webContents.once('did-finish-load',()=>{record.loaded=true;});
      child.webContents.on('will-attach-webview',event=>event.preventDefault());
      child.on('close',event=>{if(!this.closing&&!record.allowClose){event.preventDefault();this.emit('floating:return',{id:record.id});}});
    for(const event of ['focus','blur','show','restore','resize','minimize'])child.on(event,()=>this.rdp.refresh(child));
      child.on('closed',()=>{this.records.delete(record.token);});
    });
  }
  prepare({id,panes,title}) {
    if(this.closing||typeof id!=='string'||!id||id.length>128||!Array.isArray(panes)||!panes.length||panes.length>64||new Set(panes).size!==panes.length||panes.some(id=>typeof id!=='string'||(!this.terminals.get(id)&&!this.rdp.get(id))))throw new Error('Вкладка недоступна.');
    if(this.records.size>=16||[...this.records.values()].some(item=>item.id===id||item.panes.some(id=>panes.includes(id))))throw new Error('Вкладка уже открыта в отдельном окне.');
    const token=crypto.randomUUID();this.records.set(token,{id,panes:[...panes],token,title:String(title||'AstraTerm').slice(0,160)});
    return {token,url:this.url+'#'+token};
  }
  async ready(token) {
    const record=this.records.get(token);if(!record?.window||record.window.isDestroyed())throw new Error('Окно недоступно.');
    for(const id of record.panes)this.rdp.setWindow(id,record.window);
    await Promise.all(record.panes.map(id=>this.rdp.settledWindow(id,record.window)));
    return true;
  }
  async dock(token) {
    const record=this.records.get(token);if(!record)return false;
    for(const id of record.panes)this.rdp.setWindow(id,this.owner);
    await Promise.all(record.panes.map(id=>this.rdp.settledWindow(id,this.owner)));
    record.allowClose=true;record.window?.close();this.records.delete(token);return true;
  }
  dispose(){this.closing=true;for(const record of this.records.values())record.window?.destroy();this.records.clear();}
}
module.exports={FloatingWindows};
