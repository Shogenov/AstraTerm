document.querySelectorAll('[data-menu]').forEach(button=>{
 button.addEventListener('click',()=>{const open=button.getAttribute('aria-expanded')!=='true';button.setAttribute('aria-expanded',String(open));document.querySelector('.nav-links').classList.toggle('open',open);});
});
document.querySelectorAll('.nav-links a').forEach(link=>link.addEventListener('click',()=>{document.querySelector('.nav-links')?.classList.remove('open');document.querySelector('[data-menu]')?.setAttribute('aria-expanded','false');}));
document.querySelectorAll('[data-multiexec]').forEach(demo=>{
 const panes=[...demo.querySelectorAll('[data-host]')],status=demo.querySelector('[data-count]'),send=demo.querySelector('[data-send]');
 const update=()=>{const count=panes.filter(p=>p.querySelector('input').checked).length;status.textContent=`Выбрано ${count} из ${panes.length}`;send.disabled=count===0;panes.forEach(p=>p.classList.toggle('selected',p.querySelector('input').checked));};
 panes.forEach(p=>p.querySelector('input').addEventListener('change',update));
 demo.querySelector('[data-all]').addEventListener('click',()=>{panes.forEach(p=>p.querySelector('input').checked=true);update();});
 demo.querySelector('[data-none]').addEventListener('click',()=>{panes.forEach(p=>p.querySelector('input').checked=false);update();});
 send.addEventListener('click',()=>{panes.forEach(p=>{if(!p.querySelector('input').checked)return;const output=p.querySelector('[data-output]');output.replaceChildren();const command=document.createElement('span');command.className='demo-command';command.textContent='$ hostname';const response=document.createElement('span');response.className='demo-response';response.textContent=p.dataset.host;output.append(command,response);});demo.querySelector('[data-announcement]').textContent='Команда показана только в отмеченных демонстрационных терминалах.';});
 update();
});
document.querySelectorAll('[data-workspace-tab]').forEach(button=>{
 button.addEventListener('click',()=>{const stage=button.closest('[data-workspace]');stage.dataset.mode=button.dataset.workspaceTab;stage.querySelectorAll('[data-workspace-tab]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));});
});
document.querySelectorAll('[data-copy]').forEach(button=>{
 button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(button.dataset.copy);const old=button.textContent;button.textContent='Скопировано';setTimeout(()=>button.textContent=old,1600);}catch{button.textContent=button.dataset.copy;}});
});
if(location.protocol==='file:')document.querySelectorAll('[data-download]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();const note=document.getElementById('offline-note');if(note){note.hidden=false;note.focus();}}));
