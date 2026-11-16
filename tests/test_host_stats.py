import json
import struct
import unittest
from pathlib import Path
from astraterm.host_stats import linux_snapshot, Sampler, users_from_utmp

FIXTURE=json.loads((Path(__file__).resolve().parent.parent/'shared/contracts/host-stats.json').read_text(encoding='utf-8'))

class HostStatsTest(unittest.TestCase):
    def snapshot(self,files=None):
        files=dict(FIXTURE['files'] if files is None else files)
        records=[]
        for name in FIXTURE['utmp_users']:
            record=bytearray(384);struct.pack_into('<h',record,0,7);record[44:44+len(name)]=name.encode();records.append(record)
        files['/run/utmp']=b''.join(records)
        def read(path,limit):
            value=files[path];return (value.encode() if isinstance(value,str) else value)[:limit]
        return linux_snapshot(read,lambda path:FIXTURE['statvfs'])

    def test_proc_snapshot_and_mounts(self):
        result=self.snapshot()
        for key,value in FIXTURE['expected'].items():self.assertEqual(result[key],value,key)
        self.assertEqual([d['path'] for d in result['disks']],FIXTURE['mounts'])
        self.assertTrue(all(d['percent']==66.7 and d['used']==614400 for d in result['disks']))

    def test_old_kernel_memory_and_optional_missing_data(self):
        files=dict(FIXTURE['files']);files['/proc/meminfo']=files['/proc/meminfo'].replace('MemAvailable: 2048 kB\n','')
        files['/proc/uptime']='nan';files.pop('/proc/net/dev')
        result=self.snapshot(files)
        self.assertEqual(result['memory_used'],FIXTURE['fallback_memory_used']);self.assertIsNone(result['uptime']);self.assertIsNone(result['rx'])
        self.assertIsNone(users_from_utmp(b'bad ABI'));self.assertEqual(users_from_utmp(b''),[])
        with self.assertRaises(ValueError):linux_snapshot(lambda *args:b'',lambda p:{})

    def test_rates_resets_and_host_isolation(self):
        sampler=Sampler();first=self.snapshot();self.assertIsNone(sampler.sample('a',first,0)['cpu'])
        later=dict(first,cpu_total=721,cpu_idle=480,rx=3024,tx=6048)
        result=sampler.sample('a',later,2);self.assertEqual(result['cpu'],70);self.assertEqual(result['download'],1000);self.assertEqual(result['upload'],2000)
        self.assertIsNone(sampler.sample('b',later,2)['cpu'])
        self.assertIsNone(sampler.sample('a',dict(later,interfaces=['eth1'],rx=10000),4)['download'])
        self.assertIsNone(sampler.sample('a',first,6)['download'])
        self.assertIsNone(sampler.sample('a',later,60)['cpu'])

if __name__=='__main__':unittest.main()
