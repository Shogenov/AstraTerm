'use strict';
const image=document.querySelector('[data-shot-image]');
document.querySelectorAll('[data-shot]').forEach(button=>button.addEventListener('click',()=>{
  const platform=button.dataset.shot,filename=platform==='linux'?'assets/linux-app.png':'assets/windows-app.png';
  image.src=filename;
  image.alt=platform==='linux'?'Настоящий AstraTerm в Astra Linux 1.6 с тестовыми данными':'Настоящий AstraTerm в Windows с тестовыми SSH-сессиями и SFTP';
  image.width=platform==='linux'?1024:1440;image.height=platform==='linux'?690:900;
  document.querySelectorAll('[data-shot-link]').forEach(link=>link.href=filename);
  document.querySelectorAll('[data-shot]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));
}));
const userAgent=navigator.userAgent.toLowerCase();
const platform=userAgent.includes('windows')?'windows':userAgent.includes('linux')&&!userAgent.includes('android')?'linux':null;
if(platform){document.querySelector(`[data-platform="${platform}"]`)?.classList.add('recommended');const note=document.querySelector(`[data-rec="${platform}"]`);if(note)note.textContent='ДЛЯ ВАШЕЙ СИСТЕМЫ';}
document.getElementById('year').textContent=new Date().getFullYear();
