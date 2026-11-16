"""Bounded, read-only Linux host snapshots; no shell commands or terminal input."""
import collections
import math
import os
import re
import struct
import time

VIRTUAL = set('proc sysfs devtmpfs tmpfs devpts cgroup cgroup2 squashfs securityfs debugfs tracefs fusectl pstore autofs mqueue hugetlbfs nsfs rpc_pipefs binfmt_misc configfs bpf'.split())


def text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value or ''


def users_from_utmp(data):
    if data is None: return None
    if len(data) % 384: return None
    users = collections.Counter()
    for offset in range(0, len(data), 384):
        if struct.unpack_from('<h', data, offset)[0] != 7: continue
        name = data[offset+44:offset+76].split(b'\0', 1)[0].decode('utf-8', 'replace')
        name = re.sub(r'[\x00-\x1f\x7f]', '', name)
        if name: users[name] += 1
    return [{'name': name, 'count': users[name]} for name in sorted(users)]


def mount_paths(source):
    paths = ['/']; seen = set(paths)
    for line in text(source).splitlines():
        parts = line.split()
        if len(parts) < 3: continue
        device, target, kind = parts[:3]
        target = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), target)
        if (kind in VIRTUAL or target in seen or not target.startswith('/') or len(target)>4096 or
                re.search(r'[\x00-\x1f\x7f]', target) or
                not (device.startswith('/dev/') or kind.startswith(('nfs','cifs','fuse.')))): continue
        paths.append(target); seen.add(target)
        if len(paths) >= 32: break
    return paths


def linux_snapshot(read, statvfs, clock=time.monotonic):
    def optional(path, limit=262144):
        try: return read(path, limit)
        except Exception: return None
    cpu = text(optional('/proc/stat')).splitlines()
    if not cpu or not cpu[0].startswith('cpu '): raise ValueError('Статистика Linux недоступна')
    ticks = [int(n) for n in cpu[0].split()[1:9]]
    if len(ticks)<4 or any(n<0 for n in ticks): raise ValueError('Некорректные счётчики CPU')
    memory = dict((name,int(value)*1024) for name,value in re.findall(r'^(\w+):\s+(\d+)',text(optional('/proc/meminfo')),re.M))
    total = memory.get('MemTotal', 0)
    available = memory.get('MemAvailable', sum(memory.get(k,0) for k in ['MemFree','Buffers','Cached','SReclaimable'])-memory.get('Shmem',0))
    try: uptime = float(text(optional('/proc/uptime')).split()[0])
    except (ValueError,IndexError): uptime = None
    if uptime is not None and (not math.isfinite(uptime) or uptime < 0): uptime = None
    default = set()
    for line in text(optional('/proc/net/route')).splitlines()[1:]:
        fields=line.split()
        if len(fields)>3 and fields[1]=='00000000': default.add(fields[0])
    interfaces={}
    for line in text(optional('/proc/net/dev')).splitlines():
        if ':' not in line: continue
        name, values=line.split(':',1); name=name.strip(); fields=values.split()
        if name!='lo' and len(fields)>=9:
            try: interfaces[name]=(int(fields[0]),int(fields[8]))
            except ValueError: pass
    selected=sorted(default.intersection(interfaces) or set(interfaces))
    rx=sum(interfaces[name][0] for name in selected) if interfaces else None
    tx=sum(interfaces[name][1] for name in selected) if interfaces else None
    utmp=optional('/run/utmp',1048576)
    if utmp is None: utmp=optional('/var/run/utmp',1048576)
    disks=[]; deadline=clock()+2
    for path in mount_paths(optional('/proc/mounts')):
        if clock()>deadline: break
        try:
            size=statvfs(path); block=size['frsize'] or size['bsize']
            used=(size['blocks']-size['bfree'])*block; capacity=size['blocks']*block; disk_available=size['bavail']*block
            percent=100*used/(used+disk_available) if used+disk_available>0 else 0
            disks.append({'path':path,'used':used,'total':capacity,'percent':round(percent,1)})
        except Exception: disks.append({'path':path,'used':None,'total':None,'percent':None})
    hostname=re.sub(r'[\x00-\x1f\x7f]', '',text(optional('/proc/sys/kernel/hostname',1024))).strip()[:253]
    return {'hostname':hostname,'cpu_total':sum(ticks),'cpu_idle':ticks[3]+(ticks[4] if len(ticks)>4 else 0),
            'memory_total':total or None,'memory_used':max(0,total-max(0,min(total,available))) if total else None,
            'uptime':uptime,'rx':rx,'tx':tx,'interfaces':selected,'users':users_from_utmp(utmp),'disks':disks}


class Sampler(object):
    def __init__(self): self.previous={}

    def sample(self, key, raw, now=None):
        now=time.monotonic() if now is None else now
        previous=self.previous.get(key); self.previous[key]=(now,raw)
        if len(self.previous)>64: self.previous.pop(next(iter(self.previous)))
        result=dict(raw,cpu=None,upload=raw.get('tx_rate'),download=raw.get('rx_rate'))
        if previous and 0<now-previous[0]<=30:
            elapsed=now-previous[0]; old=previous[1]
            total=raw['cpu_total']-old['cpu_total']; idle=raw['cpu_idle']-old['cpu_idle']
            if total>0 and 0<=idle<=total: result['cpu']=round(100*(total-idle)/total,1)
            for source,target in [('rx','download'),('tx','upload')]:
                if raw.get('interfaces') == old.get('interfaces') and raw.get(source) is not None and old.get(source) is not None and raw[source]>=old[source] and raw.get(source+'_rate') is None:
                    result[target]=round((raw[source]-old[source])/elapsed,1)
        return result


def local_snapshot():
    def read(path,limit):
        with open(path,'rb') as stream: return stream.read(limit)
    def stats(path):
        value=os.statvfs(path)
        return dict((name,getattr(value,'f_'+name)) for name in ['bsize','frsize','blocks','bfree','bavail'])
    return linux_snapshot(read,stats)


def sftp_snapshot(sftp):
    from paramiko.sftp import CMD_EXTENDED, CMD_EXTENDED_REPLY
    def read(path,limit):
        with sftp.open(path,'rb') as stream: return stream.read(limit)
    def stats(path):
        kind,message=sftp._request(CMD_EXTENDED,'statvfs@openssh.com',path)
        if kind!=CMD_EXTENDED_REPLY: raise ValueError('statvfs не поддерживается')
        values=[message.get_int64() for unused in range(11)]
        return dict(zip(['bsize','frsize','blocks','bfree','bavail'],values[:5]))
    return linux_snapshot(read,stats)
