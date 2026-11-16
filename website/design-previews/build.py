"""Build the three static design concepts; no hosting credentials or deployment."""
import html
import json
import shutil
import zipfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent.parent
DIST=HERE/'dist'
PRODUCT=json.loads((ROOT/'shared/product.json').read_text(encoding='utf-8'))
VERSION=PRODUCT['version']

PATHS={
 'arrow':'<path d="M4 12h15m-6-6 6 6-6 6"/>',
 'arrow-up':'<path d="M6 18 18 6M6 6h12v12"/>',
 'chevron':'<path d="m9 5 7 7-7 7"/>',
 'terminal':'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
 'folder':'<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11H3Z"/>',
 'split':'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>',
 'grid':'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
 'key':'<circle cx="8" cy="9" r="4"/><path d="m11 12 9 9m-5-5 3-3m0 6 3-3"/>',
 'switch':'<path d="M4 6h14m-4-4 4 4-4 4M20 18H6m4-4-4 4 4 4"/>',
 'multi':'<path d="M12 3v18M4 8l8-5 8 5M4 15l8 6 8-6M4 4v4h4m12-4v4h-4M4 20v-5h4m12 5v-5h-4"/>',
 'download':'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
 'windows':'<path d="m3 5 8-1v7H3Zm10-1 8-1v8h-8ZM3 13h8v7l-8-1Zm10 0h8v8l-8-1Z" fill="currentColor" stroke="none"/>',
 'linux':'<path d="m5 5 5 7-5 7m8 0h6"/><rect x="2" y="2" width="20" height="20" rx="4"/>',
 'monitor':'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>',
 'check':'<path d="m5 12 4 4L19 6"/>',
 'menu':'<path d="M4 7h16M4 12h16M4 17h16"/>',
}

def icon(name,cls='icon'):
    return f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{PATHS[name]}</svg>'

def brand():
    return '<a class="brand" href="#top" aria-label="AstraTerm — в начало"><svg class="brand-mark" viewBox="0 0 32 28" fill="none" aria-hidden="true"><path d="m3 5 8 8-8 8m13 0h12" stroke="currentColor" stroke-width="2.8"/></svg>AstraTerm</a>'

def nav():
    return f'''<div class="wrap"><header class="site-header">{brand()}
    <nav class="nav-links" aria-label="Основная навигация"><a href="#features">Возможности</a><a href="#multiexec">MultiExec</a><a href="#faq">Вопросы</a><a href="#download" class="button small">Скачать {icon('arrow-up')}</a></nav>
    <button class="menu-toggle" data-menu aria-expanded="false" aria-label="Открыть меню">{icon('menu')}</button></header></div>'''

def previewbar(selected):
    links=[]
    for index,(slug,name) in enumerate([('graphite','Graphite'),('paper','Paper'),('signal','Signal')],1):
        current=' aria-current="page"' if slug==selected else ''
        links.append(f'<a href="../{slug}/index.html"{current}><span class="number">0{index}</span><span class="variant-name">{name}</span></a>')
    return '<aside class="preview-bar" aria-label="Выбор концепции"><a class="back" href="../index.html" aria-label="К сравнению вариантов">←</a>'+''.join(links)+'</aside>'

def platformline():
    return f'<div class="platform-line"><span>{icon("windows")}Windows x64</span><span>{icon("linux")}Astra Linux 1.6</span><span>Версия {VERSION}</span></div>'

def workspace(interactive=False):
    toggles=''
    if interactive:
        toggles='''<div class="workspace-tabs" aria-label="Посмотреть режим интерфейса"><button data-workspace-tab="split" aria-pressed="true">Терминалы</button><button data-workspace-tab="sftp" aria-pressed="false">SFTP</button><button data-workspace-tab="rdp" aria-pressed="false">RDP</button></div>'''
    return f'''<div class="workspace" data-workspace data-mode="split">
      {toggles}<div class="ws-bar"><span class="ws-traffic"><i></i><i></i><i></i></span><span class="ws-brand">›_ &nbsp;AstraTerm</span><span class="ws-address">user@web-01:22</span>{icon('split')}</div>
      <div class="ws-body"><aside class="ws-sidebar"><div class="ws-sidebar-title">Подключения</div><div class="ws-folder">{icon('folder')} Production</div><div class="ws-host active">›_ &nbsp;web-01</div><div class="ws-host">›_ &nbsp;db-01</div><div class="ws-host">▣ &nbsp;win-01</div><div class="ws-folder">{icon('folder')} Development</div><div class="ws-host">›_ &nbsp;stage-01</div></aside>
      <div class="ws-main"><div class="ws-tabs"><span class="ws-tab"><i class="dot"></i>web-01 &nbsp; ×</span><span class="ws-tab">db-01 &nbsp; ×</span><span class="ws-tab">+</span></div>
      <div class="ws-terminals"><div class="ws-term"><div class="t-line prompt">ops@web-01:~$ hostname</div><div class="t-line t-white">web-01</div><div class="t-gap"></div><div class="t-line prompt">ops@web-01:~$ uptime</div><div class="t-line t-dim">up 18 days, 4:32</div><div class="t-line t-dim">load average: 0.12, 0.08, 0.06</div><div class="t-gap"></div><div class="t-line prompt">ops@web-01:~$ ls -la</div><div class="t-line t-blue">apps/ &nbsp; backups/ &nbsp; logs/</div><div class="t-line t-white">deploy.sh &nbsp; README.md</div><div class="t-gap"></div><div class="t-line prompt">ops@web-01:~$ <i class="t-cursor"></i></div></div>
      <div class="ws-term"><div class="t-line prompt">ops@db-01:~$ df -h /</div><div class="t-line t-dim">Filesystem &nbsp; Size &nbsp; Used</div><div class="t-line t-white">/dev/sda1 &nbsp; 100G &nbsp; 24G</div><div class="t-gap"></div><div class="t-line prompt">ops@db-01:~$ systemctl</div><div class="t-line t-dim">● postgresql.service</div><div class="t-line t-green">&nbsp; active (running)</div><div class="t-gap"></div><div class="t-line prompt">ops@db-01:~$ <i class="t-cursor"></i></div></div></div>
      <div class="ws-files"><div class="t-dim small-label">SFTP &nbsp; /home/ops</div><div class="file-row">{icon('folder')}apps <span class="t-dim">/</span></div><div class="file-row">{icon('folder')}backups <span class="t-dim">/</span></div><div class="file-row">{icon('folder')}logs <span class="t-dim">/</span></div><div class="file-row">{icon('terminal')}deploy.sh</div><div class="file-row">{icon('terminal')}README.md</div></div>
      <div class="ws-rdp"><div class="ws-rdp-screen"><div class="ws-rdp-label">{icon('monitor')}Удалённый рабочий стол<br><small>win-01 · RDP во вкладке</small></div></div></div>
      </div></div><div class="ws-status"><span>web-01</span><span>CPU 4%</span><span>1,8 / 8 ГиБ</span><span>↑ 0,1 МиБ/с</span><span>18 дней</span></div></div>'''

def multidemo():
    panes=[]
    for host,checked,osname in [('web-01',True,'Linux · SSH'),('db-01',False,'Linux · SSH'),('stage-01',True,'Linux · SSH')]:
        panes.append(f'''<article class="demo-pane" data-host="{host}"><label class="demo-host"><input type="checkbox" {'checked' if checked else ''} aria-label="Включить {host} в MultiExec"><span>{host}</span><span class="t-dim">/ SSH</span></label><div class="demo-body"><div class="demo-os">{osname} · демонстрация</div><div data-output><span class="demo-command">$ _</span><span class="demo-placeholder">ожидание команды</span></div></div></article>''')
    return f'''<div class="multi-demo" data-multiexec><div class="multi-toolbar"><span class="multi-title">{icon('multi')}MultiExec</span><div class="multi-controls"><button class="text-button" data-all>Отметить все</button><button class="text-button" data-none>Снять все</button></div><span class="demo-count" data-count></span></div><div class="demo-panes">{''.join(panes)}</div><div class="demo-send"><div class="code">$ hostname</div><button class="button" data-send>Отправить {icon('arrow')}</button></div><p class="sr-only" data-announcement role="status" aria-live="polite"></p></div>'''

def features():
    return f'''<div class="feature-list"><article class="feature-item">{icon('folder')}<h3>У каждого хоста<br>своё место.</h3><p>Соберите подключения в папки. Задайте общие SSH-настройки группы и сохраните индивидуальные параметры там, где это нужно.</p></article>
    <article class="feature-item">{icon('key')}<h3>Ключи и пароли.<br>Без суеты.</h3><p>Профили SSH-ключей и пароли SSH/RDP в одном списке. Ключи используют агент или кэш приложения, пароли — мастер-хранилище.</p></article>
    <article class="feature-item">{icon('switch')}<h3>Ваши подключения<br>уже с вами.</h3><p>Импортируйте сессии и профили из привычных инструментов. Перед импортом можно проверить параметры подключения.</p><div class="source-names"><span>MobaXterm</span><span>Termius</span><span>Ásbrú</span></div></article></div>'''

def downloads(layout='cards'):
    win=ROOT/f'dist/releases/{VERSION}/windows/AstraTerm-{VERSION}-Windows-x64.exe'
    linux=ROOT/f'dist/releases/{VERSION}/linux/astraterm_{VERSION}_all.deb'
    win_size=f'{win.stat().st_size/1024**2:.1f}'.replace('.',',')+' МБ' if win.exists() else 'EXE'
    lin_size=str(round(linux.stat().st_size/1024))+' КБ' if linux.exists() else 'DEB'
    return f'''<div class="download-options {layout}"><article class="os-card"><div class="os-title">{icon('windows','os-icon')}<div><h3>Windows</h3><div class="os-kind">x64 · portable EXE · {win_size}</div></div></div><p>PowerShell, SSH и RDP в одной рабочей среде. Приложение запускается без установки Node.js или Python.</p><a class="button" data-download href="../downloads/AstraTerm-{VERSION}-Windows-x64.exe">Скачать для Windows {icon('download')}</a></article>
    <article class="os-card"><div class="os-title">{icon('linux','os-icon')}<div><h3>Astra Linux</h3><div class="os-kind">SE 1.6 · пакет DEB · {lin_size}</div></div></div><p>Нативное приложение на GTK и VTE для Astra Linux 1.6. Полноценный терминал и системный OpenSSH.</p><a class="button ghost" data-download href="../downloads/astraterm_{VERSION}_all.deb">Скачать для Astra Linux {icon('download')}</a></article></div><p class="download-note">Версия {VERSION}. Возможности платформ различаются; ключевые функции SSH, SFTP, RDP и MultiExec доступны в обеих версиях.</p><p id="offline-note" class="offline-note" tabindex="-1" hidden>Это локальный макет сайта. Для проверки скачивания откройте его через сервер предпросмотра.</p>'''

def faq():
    return '''<div class="faq"><details><summary>Для каких систем предназначен AstraTerm?</summary><p>Для Windows x64 и Astra Linux SE 1.6. В Windows используется PowerShell и встроенный SSH-клиент, в Linux — GTK/VTE и системный OpenSSH. Набор дополнительных протоколов зависит от платформы.</p></details>
    <details><summary>Как работает MultiExec?</summary><p>Вы отмечаете терминалы, которые должны получать ввод. Команда отправляется только выбранным работающим получателям от выбранного источника. Новые и переподключённые панели автоматически не включаются в рассылку. RDP в MultiExec не участвует.</p></details>
    <details><summary>Нужно вводить пароль для SFTP отдельно?</summary><p>Файловая панель следует активному SSH-хосту и повторно использует доступную аутентификацию. В Windows SFTP работает в том же SSH-соединении; Linux переиспользует кэш соединений и выбранный ключ. Смена каталога командой cd в терминале не переключает файловую панель.</p></details>
    <details><summary>Можно перенести сессии из другого клиента?</summary><p>Есть импорт MobaXterm Sessions и экспортированных паролей, Termius CSV или OpenSSH config и Ásbrú YAML. Непереносимые параметры отмечаются в предпросмотре. Пароли можно сохранять под мастер-паролем; приватные ключи не копируются из чужих баз.</p></details></div>'''

def footer():
    return f'<div class="wrap"><footer class="site-footer">{brand()}<span>Терминалы. Подключения. Порядок.</span><span>AstraTerm · 2026</span></footer></div>'

def page(slug,title,body):
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta name="description" content="AstraTerm — SSH, SFTP, RDP и MultiExec для Windows и Astra Linux 1.6."><title>{title} — AstraTerm</title><link rel="icon" href="../assets/logo.svg" type="image/svg+xml"><link rel="stylesheet" href="../assets/base.css"><link rel="stylesheet" href="../assets/{slug}.css"><script defer src="../assets/site.js"></script></head><body class="{slug}" id="top"><a class="skip-link" href="#content">Перейти к содержимому</a>{nav()}<main id="content">{body}</main>{footer()}{previewbar(slug)}</body></html>'''

def graphite():
    body=f'''<section class="g-hero"><div class="wrap g-hero-grid"><div class="g-hero-copy"><div class="eyebrow"><i class="dot"></i> Рабочая среда системного администратора</div><h1>Вся инфраструктура.<br><span>В одном окне.</span></h1><p class="lead">SSH, файлы и удалённые рабочие столы. Соберите подключения в одном месте — и сосредоточьтесь на работе.</p><div class="actions"><a href="#download" class="button">Скачать AstraTerm {icon('arrow-up')}</a><a href="#features" class="button ghost">Посмотреть возможности</a></div>{platformline()}</div><div class="g-hero-visual">{workspace()}<div class="hero-proof"><span class="dot"></span>SSH / SFTP / RDP <span class="g-proof-rule"></span> Одна рабочая среда</div><div class="g-orbit-label">focus on what matters_</div></div></div></section>
    <div class="wrap"><div class="g-strip"><span>Сделано для повседневной работы</span><span>SSH-подключения</span><span>Файлы по SFTP</span><span>RDP во вкладке</span><span>Живой MultiExec</span></div></div>
    <section class="g-feature-section wrap" id="features"><div class="section-heading"><div><div class="eyebrow section-number">01 / Пространство для работы</div><h2>Меньше окон.<br>Больше контроля.</h2></div><p>Терминалы разных хостов рядом. Файлы — у активного подключения. Всё нужное остаётся перед глазами.</p></div><div class="g-workflow"><div class="g-workflow-text"><article><span>01</span><h3>Разделяйте пространство</h3><p>Объединяйте вкладки перетаскиванием, отделяйте панели и выносите их в отдельные окна.</p></article><article><span>02</span><h3>Оставайтесь в контексте</h3><p>SFTP рядом с терминалом, RDP внутри приложения, показатели активной SSH-машины внизу.</p></article><p class="demo-caption">Иллюстрация интерфейса с демонстрационными хостами.</p></div>{workspace(True)}</div></section>
    <section class="g-multi-section" id="multiexec"><div class="wrap"><div class="section-heading"><div><div class="eyebrow">02 / MultiExec</div><h2>Одна команда.<br>Нужным машинам.</h2></div><p>Вы определяете получателей. Отметьте хосты и попробуйте отправить hostname в этой демонстрации.</p></div>{multidemo()}<p class="demo-caption">Интерактивный пример. Данные вымышлены, подключения к серверам не выполняются.</p></div></section>
    <section class="g-tools wrap"><div class="eyebrow section-number">03 / Порядок в деталях</div>{features()}</section>
    <section class="g-download wrap" id="download"><div class="section-heading"><div><div class="eyebrow section-number">04 / Начните работу</div><h2>Ваша система.<br>Ваш AstraTerm.</h2></div><p>Один подход к работе с подключениями. Свой нативный стек для каждой платформы.</p></div>{downloads()}</section>
    <section class="g-faq wrap" id="faq"><div><div class="eyebrow section-number">Перед началом</div><h2>Есть вопросы?</h2></div>{faq()}</section>'''
    return page('graphite','Graphite · концепция 01',body)

def paper():
    body=f'''<section class="p-hero wrap"><div class="p-hero-kicker"><div class="eyebrow">Пространство для ваших подключений</div><span class="small-label">WINDOWS + ASTRA LINUX</span></div><div class="p-hero-grid"><h1>Работать<br><em>ясно.</em></h1><div class="p-hero-aside"><span class="p-side-mark">›_</span><p class="lead">Инфраструктура бывает сложной.<br>Рабочее пространство — не должно.</p><p class="p-intro-small">AstraTerm собирает терминалы, файлы и удалённые рабочие столы в одном спокойном интерфейсе.</p><a href="#download" class="button">Начать с AstraTerm {icon('arrow-up')}</a></div></div><div class="p-hero-bottom"><span>Терминалы. Файлы. Подключения.</span><span class="small-label">Релиз {VERSION} &nbsp; ↓</span></div></section>
    <section class="p-product"><div class="wrap p-product-inner"><div class="p-product-caption"><span class="eyebrow">Сосредоточьтесь на задаче</span><span class="small-label">01 — Рабочее пространство</span></div>{workspace()}<p class="demo-caption">Иллюстрация интерфейса · демонстрационные подключения.</p></div></section>
    <section class="p-feature-section wrap" id="features"><div class="p-section-intro"><span class="eyebrow">Меньше переключений</span><h2>Всё на своих местах.</h2><p class="lead">Удобство начинается там, где не нужно каждый раз собирать контекст заново.</p></div><div class="p-feature-rows"><article><span class="p-row-num">01</span><h3>Открыть.<br> Расположить.<br> Работать.</h3><div><p>Терминалы разных хостов можно разместить рядом, объединить вкладки или вынести подключение в отдельное окно.</p><span class="p-row-tag">Вкладки и разделение</span></div>{icon('split')}</article><article><span class="p-row-num">02</span><h3>Файлы там,<br> где вы работаете.</h3><div><p>Панель SFTP следует активному SSH-хосту. Просматривайте каталоги и редактируйте текстовые файлы рядом с терминалом.</p><span class="p-row-tag">SSH + SFTP</span></div>{icon('folder')}</article><article><span class="p-row-num">03</span><h3>Один интерфейс.<br> Разные системы.</h3><div><p>Локальные терминалы и SSH-сессии, удалённый рабочий стол RDP и показатели активной машины — в одном приложении.</p><span class="p-row-tag">Windows и Astra Linux 1.6</span></div>{icon('monitor')}</article></div></section>
    <section class="p-multi wrap" id="multiexec"><div class="p-multi-text"><span class="eyebrow">Контроль важнее количества</span><h2>Одновременно.<br><em>Избирательно.</em></h2><p class="lead">Для повторяющихся действий есть MultiExec. Выберите получателей — и вводите команду сразу в нужных терминалах.</p><p class="p-small">Попробуйте: измените галочки и отправьте hostname.</p></div><div>{multidemo()}<p class="demo-caption">Иллюстративные данные. Это демонстрация поведения MultiExec.</p></div></section>
    <section class="p-tools wrap"><div class="p-tools-heading"><div class="eyebrow">Сохраните привычное</div><h2>Порядок,<br>который остаётся.</h2></div>{features()}</section>
    <section class="p-download" id="download"><div class="wrap p-download-inner"><div><span class="eyebrow">Начните с вашей системы</span><h2>Место для<br>хорошей работы.</h2><p>Актуальная версия {VERSION}.<br>Выберите подходящую сборку.</p></div><div>{downloads('rows')}</div></div></section>
    <section class="p-faq wrap" id="faq"><div class="eyebrow">Ещё немного ясности</div><h2>Перед первым подключением.</h2>{faq()}</section>'''
    return page('paper','Paper · концепция 02',body)

def signal_network():
    return f'''<div class="signal-network"><svg class="network-lines" viewBox="0 0 600 560" fill="none" aria-hidden="true"><path d="M180 140 300 270 460 140M300 270v200" stroke="#97b5ff" stroke-width="1.2" stroke-dasharray="4 5"/><circle cx="300" cy="270" r="104" stroke="#85a6ff" opacity=".35"/><circle cx="300" cy="270" r="153" stroke="#85a6ff" opacity=".14"/></svg>
    <article class="net-window net-one"><div><i class="dot"></i>web-01 <span>SSH</span></div><pre><span>$ hostname</span>\nweb-01\n<span>$ uptime</span>\n18 days, 4:32</pre></article>
    <article class="net-window net-two"><div><i class="dot"></i>db-01 <span>SSH</span></div><pre><span>$ df -h /</span>\n/dev/sda1  24%\n<span>$ _</span></pre></article>
    <div class="network-core">{icon('terminal')}<span>AstraTerm</span><small>YOUR WORKSPACE</small></div>
    <article class="net-window net-three"><div><i class="dot"></i>stage-01 <span>SSH</span></div><pre><span>$ hostname</span>\nstage-01</pre></article>
    <span class="net-tag net-tag-one">ПОДКЛЮЧЕНИЯ</span><span class="net-tag net-tag-two">ОДНО ОКНО</span></div>'''

def signal():
    body=f'''<section class="s-hero"><div class="wrap"><div class="s-hero-label"><span class="eyebrow"><i class="dot"></i> Терминалы под вашим контролем</span><span class="small-label">ASTRATERM / {VERSION}</span></div><div class="s-hero-grid"><div><h1>МНОГО<br>ХОСТОВ.<br><span>ОДНО ОКНО.</span></h1><p class="lead">SSH. SFTP. RDP. MultiExec.<br>Рабочая среда для тех, кто держит инфраструктуру в порядке.</p><a class="button" href="#download">Забрать AstraTerm {icon('arrow-up')}</a>{platformline()}</div>{signal_network()}</div><div class="s-hero-foot"><span>Выбираете хосты вы.<br>Окна собирает AstraTerm.</span><a href="#multiexec">СМОТРЕТЬ MULTIEXEC {icon('arrow')}</a><span class="small-label">↓ ЛИСТАЙТЕ ДАЛЬШЕ</span></div></div></section>
    <section class="s-feature wrap" id="features"><div class="section-heading"><div><span class="eyebrow">01 / Соберите рабочую среду</span><h2>БОЛЬШЕ ДЕЛА.<br>МЕНЬШЕ ХАОСА.</h2></div><p>Расположите подключения так, как удобно вам. Не теряйте из виду соседний сервер, файлы и рабочий стол.</p></div><div class="s-workspace-row">{workspace(True)}<div class="s-features-side"><article><span>01</span><h3>Несколько терминалов рядом</h3><p>Разделяйте, объединяйте и выносите вкладки в отдельные окна.</p></article><article><span>02</span><h3>SFTP активного хоста</h3><p>Подключение к файлам использует доступную SSH-аутентификацию.</p></article><article><span>03</span><h3>RDP внутри приложения</h3><p>Удалённый рабочий стол занимает свою вкладку.</p></article></div></div><p class="demo-caption">Иллюстрация рабочего пространства. Адреса и данные демонстрационные.</p></section>
    <section class="s-multi" id="multiexec"><div class="wrap"><div class="s-multi-title"><div><span class="eyebrow">02 / MultiExec</span><h2>ОДНО ДЕЙСТВИЕ.<br><span>ВАШ ВЫБОР ХОСТОВ.</span></h2></div><p>Отметьте нужные терминалы.<br>Снимите галочки с остальных.<br>Отправьте одну команду.</p></div>{multidemo()}<p class="demo-caption">Попробуйте галочки в примере. Команды не выполняются на реальных машинах.</p></div></section>
    <section class="s-tools wrap"><div class="eyebrow">03 / Подготовлено для ежедневной работы</div>{features()}</section>
    <section class="s-download wrap" id="download"><div class="s-download-head"><span class="eyebrow">04 / Готовы подключиться?</span><h2>ВЫБЕРИТЕ СИСТЕМУ.<br><span>И НАЧНИТЕ.</span></h2></div>{downloads()}</section>
    <section class="s-faq wrap" id="faq"><div><span class="eyebrow">Без лишних догадок</span><h2>КОРОТКО<br>О ГЛАВНОМ.</h2></div>{faq()}</section>'''
    return page('signal','Signal · концепция 03',body)

def gallery():
    cards=[]
    data=[('graphite','01','Graphite','Графит. Мята. Фокус.','Тёмная технологичная подача — продолжение интерфейса AstraTerm.','Вся инфраструктура.<br><b>В одном окне.</b>'),('paper','02','Paper','Свет. Воздух. Ясность.','Светлая редакционная эстетика, спокойные детали и крупная типографика.','Работать<br><i>ясно.</i>'),('signal','03','Signal','Контраст. Энергия. Контроль.','Выразительная синяя концепция с лаймовым акцентом и сетевой метафорой.','МНОГО ХОСТОВ.<br><b>ОДНО ОКНО.</b>')]
    for slug,n,name,sub,desc,headline in data:
        cards.append(f'''<article class="concept"><a class="concept-cover cover-{slug}" href="{slug}/index.html" aria-label="Открыть концепцию {name}"><span class="cover-logo">›_ AstraTerm</span><span class="cover-headline">{headline}</span><span class="cover-button">{'Начать работу' if slug=='paper' else 'Скачать AstraTerm'} ↗</span><div class="cover-terminal"><div><i></i><i></i><i></i></div><p>$ hostname<br><span>web-01</span><br>$ _</p></div><span class="cover-open">Открыть концепцию ↗</span></a><div class="concept-meta"><span class="concept-number">{n}</span><h2>{name}</h2><span class="concept-arrow">↗</span></div><h3>{sub}</h3><p>{desc}</p><a class="gallery-link" href="{slug}/index.html">Посмотреть страницу целиком {icon('arrow')}</a></article>''')
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>AstraTerm — три концепции сайта</title><link rel="icon" href="assets/logo.svg"><link rel="stylesheet" href="assets/fonts.css"><link rel="stylesheet" href="assets/gallery.css"></head><body><header><a class="gallery-brand" href="index.html"><span>›_</span>AstraTerm</a><span>Концепции сайта / 2026</span><a href="#concepts">Выбрать направление ↓</a></header><main><div class="gallery-hero"><div class="gallery-eyebrow">Дизайн-предпросмотр · astraterminal.ru</div><h1>Один продукт.<br><span>Три характера.</span></h1><div class="gallery-intro"><p>Три разных взгляда на сайт AstraTerm.<br>Откройте страницы, попробуйте MultiExec<br>и выберите близкое вам направление.</p><div><span>01 — 03</span><small>Полноценные страницы<br>Десктоп + мобильная версия</small></div></div></div><div class="concepts" id="concepts">{''.join(cards)}</div><section class="gallery-choice"><span>Как смотреть</span><h2>Не только первый экран.</h2><p>В каждом варианте есть возможности приложения, интерактивный пример MultiExec, выбор платформы и ответы на вопросы. Переключатель внизу страницы позволяет быстро сравнить концепции.</p><div class="gallery-choice-bottom"><span>Основной сайт остаётся без изменений до выбора дизайна.</span><span>AstraTerm / {VERSION}</span></div></section></main><footer><span>AstraTerm · варианты для согласования</span><span>Graphite &nbsp; / &nbsp; Paper &nbsp; / &nbsp; Signal</span></footer></body></html>'''

def main():
    (DIST/'assets').mkdir(parents=True,exist_ok=True)
    for source in list((HERE/'styles').glob('*.css'))+list((HERE/'assets').glob('*')):
        if source.is_file():shutil.copyfile(source,DIST/'assets'/source.name)
    shutil.copyfile(ROOT/'windows/assets/astraterm.svg',DIST/'assets/logo.svg')
    for slug,render in [('graphite',graphite),('paper',paper),('signal',signal)]:
        (DIST/slug).mkdir(exist_ok=True);(DIST/slug/'index.html').write_text(render(),encoding='utf-8')
    (DIST/'index.html').write_text(gallery(),encoding='utf-8')
    if not (DIST/'assets/fonts.css').exists():(DIST/'assets/fonts.css').write_text('/* System font fallback. */\n',encoding='utf-8')
    package=ROOT/'artifacts/website/astraterm-design-previews.zip';package.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(package,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for file in DIST.rglob('*'):
            if file.is_file():archive.write(file,file.relative_to(DIST).as_posix())
    print('Built three AstraTerm concepts and comparison gallery.')

if __name__=='__main__':main()
