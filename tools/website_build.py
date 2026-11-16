#!/usr/bin/env python3
"""Render the user's retained template with real, isolated application captures."""
from __future__ import print_function
import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'website/production'


def build(destination=None, payload=None):
    destination = Path(destination or SOURCE / 'dist')
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'assets').mkdir(exist_ok=True)
    product = json.loads((ROOT / 'shared/product.json').read_text(encoding='utf-8'))
    if payload is None:
        version = product['version']
        payload = {'releases':{}}
        for platform in ('windows', 'linux'):
            name = ('AstraTerm-{}-Windows-x64.exe' if platform == 'windows' else 'astraterm_{}_all.deb').format(version)
            item = {'version':version, 'url':'https://astraterminal.ru/releases/' + version + '/' + name, 'bytes':0, 'sha256':''}
            if platform == 'windows':
                item['installer'] = {'url':item['url'][:-4] + '-Setup.exe', 'bytes':0, 'sha256':''}
            payload['releases'][platform] = item
    source = (SOURCE / 'template.html').read_text(encoding='utf-8')
    styles = re.search(r'<style>(.*?)</style>', source, re.S).group(1)
    source = re.sub(r'<style>.*?</style>', '<link rel="stylesheet" href="assets/main.css">\n<link rel="stylesheet" href="assets/screenshots.css">', source, count=1, flags=re.S)
    source = re.sub(r'<script>.*?</script>', '<script src="assets/site.js" defer></script>', source, count=1, flags=re.S)
    hero = '''<figure class="mock-wrap real-preview">
      <div class="screenshot-switch" role="group" aria-label="Платформа на скриншоте">
        <button type="button" data-shot="windows" aria-pressed="true">Windows</button>
        <button type="button" data-shot="linux" aria-pressed="false">Astra Linux 1.6</button>
      </div>
      <a class="app-window real-window" href="assets/windows-app.png" target="_blank" rel="noopener" aria-label="Открыть скриншот AstraTerm в полном размере" data-shot-link>
        <img src="assets/windows-app.png" alt="Рабочий AstraTerm в Windows: три тестовых SSH-терминала, выбранные получатели MultiExec и SFTP" width="1440" height="900" data-shot-image fetchpriority="high">
      </a>
      <figcaption>Настоящее приложение · только тестовые данные <a href="assets/windows-app.png" target="_blank" rel="noopener" data-shot-link>Рассмотреть ↗</a></figcaption>
    </figure>'''
    source, count = re.subn(r'<div class="mock-wrap"[^>]*>.*?<div class="hero-caption">.*?</div>\s*</div>', hero, source, count=1, flags=re.S)
    if count != 1:
        raise ValueError('Retained template hero no longer matches; review the template change')
    workflow = '''<figure class="linux-preview"><a href="assets/linux-app.png" target="_blank" rel="noopener" aria-label="Открыть скриншот AstraTerm для Astra Linux"><img src="assets/linux-app.png" alt="Рабочий AstraTerm в Astra Linux 1.6: четыре тестовых PTY, MultiExec и файловая панель" width="1024" height="690" loading="lazy"></a><figcaption>Astra Linux 1.6 · GTK / VTE · демонстрационные данные</figcaption></figure>'''
    source, count = re.subn(r'<div class="big-terminal"[^>]*>.*?</pre></div>', workflow, source, count=1, flags=re.S)
    if count != 1:
        raise ValueError('Retained template workflow no longer matches')
    source = source.replace('Ссылки на релизы появятся здесь после их настройки.', 'Готовые сборки для Windows x64 и Astra Linux SE 1.6.')
    windows_installer = payload['releases'].get('windows', {}).get('installer')
    windows_description = ('Установка для текущего пользователя Windows x64. AstraTerm запускается из меню «Пуск».'
                           if windows_installer else 'Portable EXE для Windows x64. Запускается без установки Node.js и Python.')
    source = source.replace('Версия AstraTerm для рабочих станций на Windows.', windows_description)
    source = source.replace('<h3>Linux</h3><p>Версия AstraTerm для Linux-дистрибутивов.</p>', '<h3>Astra Linux</h3><p>Пакет DEB для Astra Linux SE 1.6. Нативный интерфейс GTK и терминал VTE.</p>')
    source = source.replace('Единый подход к работе независимо от основной операционной системы.', 'SSH, SFTP, встроенный RDP и MultiExec доступны на обеих платформах.')
    release_lines = []
    for platform, label in (('windows', 'Windows'), ('linux', 'Astra Linux')):
        item = payload['releases'].get(platform)
        if not item:
            raise ValueError('The production homepage requires both platform downloads')
        origin = product['update_parameters']['origin']
        setup = windows_installer if platform == 'windows' else None
        path = html.escape((setup or item)['url'].replace(origin, '', 1), quote=True)
        portable = html.escape(item['url'].replace(origin, '', 1), quote=True)
        source = re.sub(r'<button class="download-button" data-download="' + platform + r'"[^>]*>(.*?)</button>',
                        lambda m: ('<a class="download-button" href="' + path + '" download>' +
                                   ('Скачать установщик' if setup else m.group(1)) + '</a>' +
                                   (' <a href="' + portable + '" download>Portable EXE</a>' if setup else '')), source)
        digest = html.escape((setup or item)['sha256'])
        release_lines.append('<span>' + label + ' <strong>' + html.escape(item['version']) + '</strong>' +
                             (' · SHA-256 <code>' + digest + '</code>' if digest else '') + '</span>')
    source = re.sub(r'<p class="release-info">.*?</p>', '<div class="release-info">' + ''.join(release_lines) + '<a href="releases/downloads.json">Все данные релиза и контрольные суммы ↗</a></div>', source, count=1, flags=re.S)
    source = source.replace('<a href="#top">Наверх ↑</a>', '<a href="alternatives/index.html">Другие варианты дизайна</a><a href="#top">Наверх ↑</a>')
    source = source.replace('<meta name="theme-color"', '<link rel="canonical" href="https://astraterminal.ru/">\n  <meta name="theme-color"', 1)
    (destination / 'assets/main.css').write_text(styles, encoding='utf-8')
    for filename in (SOURCE / 'assets').glob('*'):
        if filename.is_file():
            shutil.copyfile(str(filename), str(destination / 'assets' / filename.name))
    for required in ('windows-app.png', 'linux-app.png', 'screenshots.css', 'site.js'):
        if not (destination / 'assets' / required).is_file():
            raise ValueError('Missing real production asset: ' + required)
    # Hosting caches static files. Use the same fresh URL in HTML and in the platform switcher.
    script = destination / 'assets/site.js'
    script_source = script.read_text(encoding='utf-8')
    for name in ('windows-app.png', 'linux-app.png'):
        url = 'assets/' + name
        revision = hashlib.sha256((destination / url).read_bytes()).hexdigest()[:12]
        versioned = url + '?v=' + revision
        source = source.replace(url, versioned)
        script_source = script_source.replace(url, versioned)
    script.write_text(script_source, encoding='utf-8')
    script_revision = hashlib.sha256(script.read_bytes()).hexdigest()[:12]
    source = source.replace('assets/site.js', 'assets/site.js?v=' + script_revision)
    (destination / 'index.html').write_text(source, encoding='utf-8')
    admin = destination / 'admin'
    admin.mkdir(exist_ok=True)
    for filename in ('index.php', 'statistics.php', 'dashboard.css'):
        shutil.copyfile(str(SOURCE / 'admin' / filename), str(admin / filename))
    alternative = ROOT / 'website/design-previews/dist'
    if alternative.is_dir():
        copy_tree(alternative, destination / 'alternatives')
        for filename in (destination / 'alternatives').rglob('*.html'):
            content = filename.read_text(encoding='utf-8')
            content = content.replace('Основной сайт остаётся без изменений до выбора дизайна.', 'Альтернативные концепции. Основной сайт: astraterminal.ru.')
            # Historical design alternatives point to the current production downloads.
            content = re.sub(r'\.\./downloads/[^"\s]+\.exe', payload['releases']['windows']['url'], content)
            content = re.sub(r'\.\./downloads/[^"\s]+\.deb', payload['releases']['linux']['url'], content)
            filename.write_text(content, encoding='utf-8')
    (destination / '.htaccess').write_text('''Options -Indexes
# HTTPS redirect belongs to the hosting front proxy, not Apache behind nginx.
<IfModule mod_headers.c>
Header always set X-Content-Type-Options "nosniff"
Header always set Referrer-Policy "strict-origin-when-cross-origin"
Header always set X-Frame-Options "DENY"
Header set Cache-Control "no-cache"
</IfModule>
''', encoding='ascii')
    print('Production site built from retained user template; alternatives preserved.')
    return destination


def copy_tree(source, destination):
    for filename in source.rglob('*'):
        target = destination / filename.relative_to(source)
        if filename.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(filename), str(target))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--metadata', help='Previously verified public release payload')
    args = parser.parse_args()
    build(args.output, json.loads(Path(args.metadata).read_text(encoding='utf-8')) if args.metadata else None)
