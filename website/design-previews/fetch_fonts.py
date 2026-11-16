"""Fetch public presentation fonts once; visitors then use local assets only."""
import hashlib
import re
import urllib.request
from pathlib import Path

folder=Path(__file__).resolve().parent/'assets'
url='https://fonts.googleapis.com/css2?family=Onest:wght@400..800&family=IBM+Plex+Mono:wght@400;500&family=Prata&display=swap'
request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36'})
try:
    css=urllib.request.urlopen(request,timeout=20).read().decode('utf-8')
    replacements={}
    for remote in set(re.findall(r'url\((https://[^)]+)\)',css)):
        name='font-'+hashlib.sha256(remote.encode()).hexdigest()[:14]+('.woff2' if '.woff2' in remote else '.ttf')
        target=folder/name
        if not target.exists():target.write_bytes(urllib.request.urlopen(remote,timeout=20).read())
        replacements[remote]=name
    for remote,name in replacements.items():css=css.replace(remote,name)
    (folder/'fonts.css').write_text(css,encoding='utf-8')
    print('Downloaded {} local font assets.'.format(len(replacements)))
except Exception:
    if not (folder/'fonts.css').exists():
        (folder/'fonts.css').write_text('/* Offline-safe system font fallback. */\n',encoding='utf-8')
    print('Using system font fallback.')

# Redistributed font files retain their original license notices.
for family in ('onest', 'ibmplexmono', 'prata'):
    target = folder / ('LICENSE-' + family + '.txt')
    if target.exists():
        continue
    remote = 'https://raw.githubusercontent.com/google/fonts/main/ofl/' + family + '/OFL.txt'
    target.write_bytes(urllib.request.urlopen(remote, timeout=20).read())
