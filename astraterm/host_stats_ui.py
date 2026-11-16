"""Active-host status strip; asynchronous and generation-bound."""
import threading
import time
from gi.repository import Gtk, GLib, Pango
from .host_stats import Sampler, local_snapshot, sftp_snapshot
from .files import _sftp_profile_key


def amount(value, rate=False):
    if value is None: return '—'
    labels=['Б','КиБ','МиБ','ГиБ','ТиБ']; index=0
    while value>=1024 and index<4: value/=1024; index+=1
    return ('{:.2f}'.format(value).rstrip('0').rstrip('.').replace('.',',')+' '+labels[index]+('/с' if rate else ''))


def fields(data):
    seconds=data.get('uptime'); uptime='—' if seconds is None else '{} д {} ч'.format(int(seconds)//86400,int(seconds)%86400//3600)
    users=data.get('users'); users='—' if users is None else ', '.join(item['name']+(' (x{})'.format(item['count']) if item['count']>1 else '') for item in users) or 'Нет сеансов'
    disks=' · '.join(item['path']+' '+('—' if item['percent'] is None else '{}%'.format(round(item['percent']))) for item in data.get('disks',[])) or '—'
    return [('hostname',data.get('hostname') or '—','Hostname'),('cpu','CPU '+('—' if data.get('cpu') is None else '{}%'.format(round(data['cpu']))),'Загрузка CPU'),
            ('memory',amount(data.get('memory_used'))+' / '+amount(data.get('memory_total')),'Занято ОЗУ / всего'),
            ('upload','↑ '+amount(data.get('upload'),True),'Отдача · '+', '.join(data.get('interfaces',[]))),('download','↓ '+amount(data.get('download'),True),'Загрузка · '+', '.join(data.get('interfaces',[]))),
            ('uptime',uptime,'Uptime'),('users',users,'Пользователи и число сеансов (utmp)'),('disks',disks,'Занято на дисках')]


class HostStatsBar(Gtk.Box):
    def __init__(self, window):
        Gtk.Box.__init__(self,spacing=12)
        self.window=window;self.sampler=Sampler();self.target=None;self.epoch=0;self.busy=False;self.closed=False
        self.labels={};self.get_style_context().add_class('host-stats')
        for name in ['hostname','cpu','memory','upload','download','uptime','users','disks']:
            label=Gtk.Label(label='—',xalign=0);label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars({'hostname':18,'users':26,'disks':46}.get(name,24))
            self.pack_start(label,name=='disks',name=='disks',0);self.labels[name]=label
        self.set_no_show_all(True)
        self.timer=GLib.timeout_add_seconds(3,self.refresh)

    def refresh(self):
        if self.closed or self.window.closed:return False
        pane=self.window.active_pane();protocol=pane.profile.get('protocol') if pane else None
        if protocol not in ('local','ssh'):
            self.hide();self.target=None;self.epoch+=1;return True
        target=(id(pane),pane.connection_generation,self.window.auth_generation)
        if target!=self.target:
            self.target=target;self.epoch+=1
            for label in self.labels.values():label.set_text('—')
        for label in self.labels.values():label.show()
        self.show()
        if self.busy:return True
        browser=self.window.file_browser
        if protocol=='ssh':
            profile=dict(pane.profile)
            profile.update(profile.get('_sftp_endpoint',{}))
            token=(profile.get('_auth_token'),self.window.auth_generation,pane.pane_drag_id)
            if (not browser._sftp or browser._busy or browser._pending_profile is not None or
                    _sftp_profile_key(profile,token)!=_sftp_profile_key(browser.profile,browser._auth_generation)):return True
        epoch=self.epoch;sftp=browser._sftp;generation=browser._generation;self.busy=True
        def work():
            result=None;lock=False
            try:
                if protocol=='local': raw=local_snapshot()
                else:
                    lock=browser._io_lock.acquire(False)
                    if not lock:return
                    if browser._sftp is not sftp or browser._generation!=generation:return
                    channel=sftp.get_channel();old=channel.gettimeout() if hasattr(channel,'gettimeout') else 20
                    try:channel.settimeout(2);raw=sftp_snapshot(sftp)
                    finally:
                        try:channel.settimeout(old)
                        except Exception:pass
                result=self.sampler.sample(target,raw)
            except Exception:pass
            finally:
                if lock:browser._io_lock.release()
                def done():
                    self.busy=False
                    if self.closed or epoch!=self.epoch or self.target!=target:return False
                    if result is not None and (protocol=='local' or browser._sftp is sftp and browser._generation==generation):
                        for name,value,title in fields(result):
                            self.labels[name].set_text(value);self.labels[name].set_tooltip_text(title+'\n'+value)
                        self.set_tooltip_text('Обновлено · локальная машина' if protocol=='local' else 'Обновлено · '+pane.profile.get('host',''))
                    else:
                        for label in self.labels.values():label.set_text('—')
                        self.set_tooltip_text('Статистика временно недоступна. Для SSH требуется SFTP и доступ к /proc.')
                    return False
                GLib.idle_add(done)
        thread=threading.Thread(target=work,name='astraterm-host-stats');thread.daemon=True;thread.start();return True

    def close(self):
        self.closed=True;self.epoch+=1
        if self.timer:GLib.source_remove(self.timer);self.timer=None
