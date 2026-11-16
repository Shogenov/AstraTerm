(function(root){
 'use strict';
 function amount(value,rate=false){if(value==null)return '—';const units=['Б','КиБ','МиБ','ГиБ','ТиБ'];let index=0;while(value>=1024&&index<4){value/=1024;index++;}return String(Number(value.toFixed(2))).replace('.',',')+' '+units[index]+(rate?'/с':'');}
 function fields(data){const users=data.users==null?'—':data.users.map(u=>u.name+(u.count>1?` (x${u.count})`:'')).join(', ')||'Нет сеансов';
  return [['hostname',data.hostname||'—','Hostname'],['cpu','CPU '+(data.cpu==null?'—':Math.round(data.cpu)+'%'),'Загрузка CPU'],['memory',amount(data.memory_used)+' / '+amount(data.memory_total),'Занято ОЗУ / всего'],
   ['upload','↑ '+amount(data.upload,true),'Отдача · '+(data.interfaces||[]).join(', ')],['download','↓ '+amount(data.download,true),'Загрузка · '+(data.interfaces||[]).join(', ')],
   ['uptime',data.uptime==null?'—':Math.floor(data.uptime/86400)+' д '+Math.floor(data.uptime%86400/3600)+' ч','Uptime'],['users',users,'Пользователи и число сеансов'],
   ['disks',(data.disks||[]).map(d=>d.path+' '+(d.percent==null?'—':Math.round(d.percent)+'%')).join(' · ')||'—','Занято на дисках']];
 }
 class HostStatsView{
  constructor(api,container,getPane){this.api=api;this.container=container;this.getPane=getPane;this.key=null;this.busy=false;this.timer=setInterval(()=>this.refresh(),3000);}
  async refresh(){const pane=this.getPane();if(!pane||!['ssh','local'].includes(pane.profile.protocol)){this.container.hidden=true;this.key=null;return;}
   const key=pane.id+':'+pane.generation;if(this.key!==key){this.key=key;this.container.replaceChildren();}this.container.hidden=false;if(this.busy)return;this.busy=true;
   try{const data=await this.api.invoke('host:stats',{id:pane.id});if(this.key!==key||this.getPane()?.id!==pane.id)return;if(!data){this.container.replaceChildren();return;}if(data.generation!==this.getPane()?.generation)return;
    this.container.replaceChildren(...fields(data).map(([name,value,title])=>{const node=document.createElement('span');node.className='host-stat host-'+name;node.textContent=value;node.title=title+'\n'+value;return node;}));
    this.container.title=fields(data).map(([,value,title])=>title+': '+value).join('\n');
   }catch{if(this.key===key){this.container.replaceChildren();this.container.title='Статистика временно недоступна. Для SSH требуется SFTP и доступ к /proc.';}}finally{this.busy=false;}
  }
 }
 root.HostStatsView=HostStatsView;if(typeof module!=='undefined')module.exports={amount,fields};
})(typeof window!=='undefined'?window:globalThis);
