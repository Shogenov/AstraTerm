'use strict';

const { app, BrowserWindow, ipcMain, dialog, Menu, clipboard, shell, protocol } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { entryURL, floatingURL, installLocalUI } = require('./local-ui.cjs');
const { Store, defaultSession } = require('./backend/store.cjs');
const { TerminalManager, safeError } = require('./backend/terminal.cjs');
const { RdpManager } = require('./backend/rdp.cjs');
const { FloatingWindows } = require('./floating-windows.cjs');
const { IdentityManager } = require('./backend/identities.cjs');
const { HostTrustStore } = require('./backend/host-trust.cjs');
const { FilesService } = require('./backend/files.cjs');
const { SftpDrag } = require('./backend/sftp-drag.cjs');
const { DiagnosticLog } = require('./backend/diagnostic-log.cjs');
const { HostStats } = require('./backend/host-stats.cjs');
const { TunnelManager } = require('./backend/tunnels.cjs');
const { CredentialVault } = require('./backend/vault.cjs');
const { parseImport } = require('./backend/importers.cjs');
const { mobaConfigPaths, readMobaCredentials, planMobaLinks } = require('./backend/mobaxterm.cjs');
const { UpdateController } = require('./update-ui.cjs');
const { BackupDialogs } = require('./backup-ui.cjs');
const { activeProfile } = require('./backend/backup.cjs');

// DirectComposition paints above foreign native child HWNDs even with HWND_TOP.
// Keep GPU acceleration; use a redirection surface so the embedded RDP child is visible.
if (process.platform === 'win32') app.commandLine.appendSwitch('disable-direct-composition');
protocol.registerSchemesAsPrivileged([{ scheme: 'astraterm', privileges: { standard: true, secure: true } }]);

const IS_RDP_TEST = process.argv.includes('--rdp-test');
const IS_WEBSITE = process.argv.includes('--website-capture');
const IS_SMOKE = process.argv.includes('--smoke-test') || IS_RDP_TEST || IS_WEBSITE;
const appDirectory = IS_SMOKE ? fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-electron-smoke-')) : path.join(app.getPath('appData'), 'AstraTerm');
const documentsDirectory = IS_SMOKE ? path.join(appDirectory, 'Documents') : app.getPath('documents');
fs.mkdirSync(appDirectory, { recursive: true, mode: 0o700 });
app.setPath('userData', appDirectory);

let window;
let profileDirectory;
let diagnosticLog;
let sidebarPath;
let backups;
let store;
let terminals;
let rdp;
let floating;
let files;
let sftpDrag;
let hostStats;
let identities;
let trust;
let tunnels;
let vault;
let vaultUnlock;
let updates;
let collapsedFolders = new Set();
let folderWrites = Promise.resolve();
let loggingWrites = Promise.resolve();
const imports = new Map();
let allowClose = false;
let installingUpdate = false;
let confirmingClose = false;
let finishing = false;
let dirtyCount = 0;
const prompts = new Map();
const recorders = new Map();

function emit(type, payload) {
  if (type === 'terminal:status') {
    if (payload.status === 'error') diagnosticLog.write('warning', 'terminal.error');
    if (payload.status === 'running') diagnosticLog.write('info', 'terminal.running');
  } else if (type === 'terminal:exit') diagnosticLog.write('info', 'terminal.exit');
  if (type === 'terminal:data' && recorders.has(payload.id)) {
    const recorder = recorders.get(payload.id);
    if (recorder.writableLength > 8 * 1024 * 1024) stopRecording(payload.id, 'Запись остановлена: диск не успевает сохранять вывод.');
    else recorder.write(payload.data, 'utf8');
  }
  if (type === 'terminal:exit' || (type === 'terminal:status' && ['closed', 'error'].includes(payload.status))) {
    files?.closeTerminal(payload.id);
    void tunnels?.sessionClosed(payload.id);
    stopRecording(payload.id);
  }
  if (window && !window.isDestroyed() && !window.webContents.isDestroyed()) window.webContents.send('astra:event', { type, payload });
}

function prompt(request) {
  if (finishing || !window || window.isDestroyed()) return Promise.resolve(null);
  if (prompts.size >= 64) return Promise.reject(new Error('Слишком много ожидающих запросов'));
  const id = crypto.randomUUID();
  return new Promise(resolve => { prompts.set(id, resolve); emit('prompt:request', { ...request, id }); });
}

function stateUpdate() {
  const result = { store: store.snapshot(), identities: identities.list(), vault: vaultState() };
  emit('state:update', result);
  return result;
}

function vaultState() { return { exists: vault.exists, unlocked: vault.unlocked, profiles: vault.metadata() }; }
async function ensureVault(create = false) {
  vault.reloadIfChanged();
  if (vault.unlocked) return;
  if (vaultUnlock) return vaultUnlock;
  vaultUnlock = (async () => {
    const fresh = !vault.exists;
    if (fresh && !create) throw new Error('Сначала импортируйте файл паролей и создайте хранилище.');
    const master = await prompt({ kind:'master-password', title:fresh?'Создать хранилище AstraTerm':'Разблокировать хранилище AstraTerm', label:'Мастер-пароль AstraTerm', secret:true,
      message:fresh?'Создайте мастер-пароль, не менее 10 символов. Он защищает импортированные пароли.':'Введите мастер-пароль. Разблокировка действует до закрытия или блокировки AstraTerm.' });
    if (master === null || finishing) throw new Error('Разблокировка отменена.');
    if (fresh) {
      const repeated = await prompt({ kind:'master-password-confirm',title:'Повторите мастер-пароль',secret:true,message:'Повторите мастер-пароль нового хранилища.' });
      if (repeated === null || finishing) throw new Error('Создание хранилища отменено.');
      if (master !== repeated) throw new Error('Мастер-пароли не совпадают.');
    }
    await vault.unlock(master,{create});
    if (finishing) { vault.lock(); throw new Error('Приложение закрывается.'); }
    stateUpdate();
  })();
  try { return await vaultUnlock; } finally { vaultUnlock=null; }
}

async function prepareImport() {
  const selected = await dialog.showOpenDialog(window,{title:'Импорт AstraTerm / MobaXterm / Termius / Ásbrú',properties:['openFile','multiSelections'],
    filters:[{name:'Подключения и пароли',extensions:['mxtsessions','moba','txt','csv','json','yaml','yml','conf','config']},{name:'Все файлы',extensions:['*']}]});
  if (selected.canceled || !selected.filePaths.length) return null;
  const plan=parseImport(selected.filePaths,{documents:documentsDirectory});
  return { id: rememberImport({ plan }), ...plan.preview(vault.metadata()) };
}

function rememberImport(pending) {
  const id = crypto.randomUUID();
  while(imports.size>=4)discardImport(imports.keys().next().value);
  const timer=setTimeout(()=>discardImport(id),300000);timer.unref();
  imports.set(id,{...pending,timer});
  return id;
}

async function prepareMobaImport() {
  const candidates = IS_SMOKE ? [path.join(appDirectory, 'MobaXterm.ini')] :
    mobaConfigPaths({ appData: app.getPath('appData'), documents: documentsDirectory });
  const selected = await dialog.showOpenDialog(window, { title: 'Выберите конфигурацию с паролями MobaXterm',
    defaultPath: candidates.find(filename => fs.existsSync(filename)) || documentsDirectory,
    properties: ['openFile'], filters: [{ name: 'Конфигурация MobaXterm', extensions: ['ini'] }] });
  if (selected.canceled || !selected.filePaths.length) return null;
  const [filename] = selected.filePaths;
  const master = await prompt({ kind: 'mobaxterm-master-password', title: 'Пароли из MobaXterm',
    label: 'Мастер-пароль MobaXterm', message: 'Введите мастер-пароль MobaXterm.', secret: true });
  if (master === null || finishing) return null;
  const source = await readMobaCredentials(filename, master);
  try { await ensureVault(true); }
  catch (error) {
    if (/^Разблокировка отменена\.|^Создание хранилища отменено\./.test(error.message)) return null;
    throw Error('Пароли MobaXterm прочитаны. Не удалось открыть хранилище AstraTerm. ' + safeError(error));
  }
  store.load();
  const links = planMobaLinks(store, source, vault);
  const id = rememberImport({ ...source, kind: 'mobaxterm', storeRevision: store._revision, vaultRevision: vault.revision });
  return { id, sessions: links.previewRows, credentials: links.records.length, conflicts: links.conflicts,
    stale: links.stale, total: source.plan.credentials.size, unsupported: source.unsupported, empty: source.empty };
}

async function applyMobaImport(id, pending) {
  await ensureVault(true);
  if (imports.get(id) !== pending || finishing) throw Error('Предпросмотр импорта устарел. Откройте импорт снова.');
  store.load();
  if (store._revision !== pending.storeRevision || vault.revision !== pending.vaultRevision) {
    throw Error('Сессии или хранилище изменились. Откройте импорт снова.');
  }
  const links = planMobaLinks(store, pending, vault);
  if (links.records.length) {
    try { vault.merge(links.records); }
    catch (error) { throw Error('Не удалось записать пароли в хранилище AstraTerm. ' + safeError(error)); }
  }
  if (links.sessions.length) {
    try { store.importDocument({ version: 2, sessions: links.sessions }); }
    catch (error) {
      if (links.records.length) throw Error('Пароли сохранены, но привязки не записались. Повторите импорт.');
      throw error;
    }
  }
  discardImport(id);
  stateUpdate();
  return { imported: links.matched, credentials: links.records.length };
}
function discardImport(id) { const pending=imports.get(id);if(pending){clearTimeout(pending.timer);pending.plan.credentials.clear();imports.delete(id);}return pending; }
async function applyImport(args) {
  const pending=imports.get(args.id);if(!pending)throw new Error('Предпросмотр импорта устарел. Выберите файл снова.');
  if(pending.busy)throw new Error('Импорт уже выполняется.');pending.busy=true;
  const plan=pending.plan;
  try {
    if (pending.kind === 'mobaxterm') return await applyMobaImport(args.id, pending);
    if (args.keyProfile && !identities.list().profiles.some(item=>item.id===args.keyProfile))throw new Error('Профиль ключа недоступен.');
    for (const session of plan.sessions) if (session.protocol==='ssh') {
      if (args.disableX11 === true)session.x11=false;
      if (args.keyProfile) {session.identity_profile=args.keyProfile;session.key_file='';}
    }
    const document=plan.document(store);
    if (plan.credentials.size) {
      await ensureVault(true);
      if(imports.get(args.id)!==pending)throw new Error('Импорт отменён или предпросмотр устарел.');
      vault.merge([...plan.credentials.values()]);
    }
    let result;
    try {result=store.importDocument(document);}catch(error){
      if(plan.credentials.size)throw new Error('Пароли сохранены в хранилище, но подключения сохранить не удалось. Повторите импорт после перечитывания базы.');
      throw error;
    }
    const count=plan.credentials.size;discardImport(args.id);
    stateUpdate();return {imported:result.imported,credentials:count};
  } finally {pending.busy=false;}
}

function stopRecording(id, message) {
  const recorder = recorders.get(id);
  if (!recorder) return false;
  recorders.delete(id);
  recorder.end();
  emit('terminal:recording', { id, active: false, ...(message ? { message } : {}) });
  return true;
}

async function confirm(message, detail = '', accept = 'Продолжить') {
  if (!window || window.isDestroyed()) return false;
  const answer = await dialog.showMessageBox(window, { type: 'question', title: 'AstraTerm', message, detail,
    buttons: ['Отмена', accept], defaultId: 0, cancelId: 0, noLink: true });
  return answer.response === 1;
}

function requireIdleFiles() {
  if (files.busy) throw new Error('Дождитесь завершения файловой операции.');
}

function captureBinding(request) {
  if ((request.mode || 'local') !== 'sftp') return () => {};
  const record = terminals.get(request.terminalId);
  if (!record || !record.sshClient || record.status !== 'running') throw new Error('Для SFTP нужно активное SSH-подключение.');
  const client = record.sshClient;
  const generation = record.generation;
  if(request.generation!==undefined&&request.generation!==generation)throw Error('SSH-подключение изменилось; повторите файловую операцию.');
  return () => {
    const current = terminals.get(request.terminalId);
    if (!current || current.sshClient !== client || current.generation !== generation || current.status !== 'running') {
      throw new Error('SSH-подключение изменилось; операция отменена.');
    }
  };
}

async function uploadFiles(endpoint, sources, check, results = []) {
  check();
  const listing = await files.list(endpoint);
  const names = sources.map(source => path.basename(source));
  for (const source of sources) {
    if (!path.isAbsolute(source) || !(await fs.promises.lstat(source)).isFile()) {
      throw new Error('Можно загружать только обычные локальные файлы.');
    }
  }
  const existing = new Set(listing.entries.map(entry => entry.name));
  for (let index = 0; index < sources.length; index++) {
    check();
    const name = names[index];
    const targetPath = path.posix.join(listing.path, name);
    const overwrite = existing.has(name);
    if (overwrite && !await confirm('Заменить существующий файл?', targetPath, 'Заменить')) continue;
    check();
    results.push(await files.upload({ ...endpoint, localPath: sources[index], targetPath, overwrite }));
    existing.add(name);
  }
  return results;
}

function assertAppSender(event) {
  if (!window || window.isDestroyed() || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== entryURL) {
    throw new Error('Доступ запрещён.');
  }
  if (finishing || installingUpdate) throw new Error('Приложение закрывается или устанавливает обновление.');
}

async function writeAtomic(filename, contents) {
  const temporary = path.join(path.dirname(filename), `.astraterm-${crypto.randomUUID()}.tmp`);
  try {
    await fs.promises.writeFile(temporary, contents, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    for (let attempt = 0; ; attempt++) {
      try {
        await fs.promises.rename(temporary, filename);
        break;
      } catch (error) {
        // A reader or antivirus can briefly prevent replacement on Windows.
        if (process.platform !== 'win32' || attempt >= 5 ||
            !['EACCES', 'EBUSY', 'EPERM'].includes(error.code)) throw error;
        await new Promise(resolve => setTimeout(resolve, 25 * (attempt + 1)));
      }
    }
  } finally { await fs.promises.unlink(temporary).catch(() => {}); }
}

async function finish(exitCode = 0) {
  if (finishing) return;
  finishing = true;
  vault?.lock();
  for (const id of imports.keys())discardImport(id);
  allowClose = true;
  for (const resolve of prompts.values()) resolve(null);
  prompts.clear();
  const recordingFinished = [...recorders.values()].map(recorder => new Promise(resolve => {
    if (recorder.closed) return resolve();
    recorder.once('close', resolve);
    recorder.end();
  }));
  recorders.clear();
  files?.dispose();
  hostStats?.dispose();
  await tunnels?.dispose();
  const terminalsFinished=terminals?.dispose();
  rdp?.dispose();
  floating?.dispose();
  identities?.dispose();
  trust?.dispose();
  await Promise.all([...recordingFinished,terminalsFinished,folderWrites.catch(() => {}),loggingWrites.catch(() => {}),sftpDrag?.dispose()]);
  if (window && !window.isDestroyed()) window.destroy();
  if (IS_SMOKE) app.exit(Number.isInteger(exitCode) ? exitCode : 0);
  else app.quit();
}

async function requestClose() {
  if (allowClose || finishing || confirmingClose || installingUpdate) return;
  confirmingClose = true;
  try {
    if (files?.busy) {
      await dialog.showMessageBox(window, { type: 'info', title: 'AstraTerm', message: 'Дождитесь завершения файловой операции.' });
      return;
    }
    const active = [...(terminals?.list()||[]),...(rdp?.list()||[])].filter(record => ['connecting', 'running'].includes(record.status)).length;
    const detail = [active ? `Будут закрыты активные терминалы: ${active}.` : '', dirtyCount ? `Несохранённых редакторов: ${dirtyCount}. Изменения будут потеряны.` : ''].filter(Boolean).join('\n');
    if ((active || dirtyCount) && !await confirm('Закрыть AstraTerm?', detail, 'Закрыть')) return;
    if (files?.busy) throw new Error('Дождитесь завершения файловой операции.');
    await finish();
  } catch (error) {
    if (window && !window.isDestroyed()) await dialog.showMessageBox(window, { type: 'error', title: 'AstraTerm', message: safeError(error) });
  } finally { confirmingClose = false; }
}

function action(name, payload = {}) { emit('action', { name, payload }); }
function item(label, name, payload, extra = {}) { return { label, click: () => action(name, payload), ...extra }; }

function popupMenu(method, args) {
  let template;
  if (method === 'ui:sessionMenu') {
    if (args.id) {
      template = [item('Открыть', 'open-session', { id: args.id }), item('Изменить…', 'edit-session', { id: args.id }),
        item('Дублировать', 'duplicate-session', { id: args.id }),
        item('Переместить в папку…', 'move-session', { id: args.id }), { type: 'separator' }, item('Удалить подключение…', 'delete-session', { id: args.id })];
    } else {
      const name = args.group || '';
      template = [item('Новое подключение…', 'new-session', { group: name }), item('Новая папка…', 'new-group', { parent: name })];
      if (name) template.push({ type: 'separator' }, item('Общие SSH-настройки / переименовать…', 'edit-group', { name }), item('Удалить папку…', 'delete-group', { name }));
    }
  } else if (method === 'ui:tabMenu') {
    if (typeof args.id !== 'string' || !args.id || args.id.length > 128) throw new Error('Вкладка недоступна.');
    const payload = { id: args.id };
    template = [item('Дублировать вкладку', 'tab-duplicate', payload), item('Переподключиться', 'tab-reconnect', payload), {type:'separator'}, item('Закрыть текущую вкладку', 'tab-close-current', payload),
      item('Закрыть все', 'tab-close-all', payload), item('Закрыть все кроме текущей', 'tab-close-others', payload)];
    template.push({ type: 'separator' }, item(args.floating ? 'Вернуть во вкладки' : 'В отдельное окно', args.floating ? 'tab-dock-window' : 'tab-float-window', payload));
    if (args.merged === true) template.push({ type: 'separator' }, item('Разъединить все', 'tab-separate-all', payload));
  } else if (method === 'ui:terminalMenu') {
    const payload = { id: args.id };
    template = [item('Копировать', 'copy', payload, { enabled: !!args.hasSelection }), item('Вставить', 'paste', payload), item('Выделить всё', 'select-all', payload),
      { type: 'separator' }, item('Поиск…', 'find', payload), item('Сохранить вывод…', 'save-output', payload),
      item(recorders.has(args.id) ? 'Остановить запись' : 'Записывать вывод…', 'record', payload),
      { type: 'separator' }, item('Переподключить', 'reconnect', payload)];
  } else if (method === 'ui:fileMenu') {
    const payload = { entry: args.entry || null, binding: { mode: args.mode, terminalId: args.terminalId || null, path: args.path, generation: args.generation } };
    template = [item('Новая папка…', 'file-mkdir', payload)];
    if (args.entry) template.push(item('Открыть', 'file-edit', payload), item('Скачать…', 'file-download', payload, { enabled: !args.entry.isDirectory }),
      item('Переименовать…', 'file-rename', payload), { type: 'separator' }, item('Удалить…', 'file-delete', payload));
  } else {
    template = [
      { label: 'Файл', submenu: [item('Новое подключение…', 'new-session'), item('Новая папка…', 'new-group'), item('Локальный PowerShell', 'new-local'),
        { type: 'separator' }, item('Импорт подключений…', 'import'), item('Пароли из MobaXterm…', 'import-mobaxterm'), item('Экспорт подключений…', 'export'),
        { type: 'separator' }, item('Создать резервную копию…', 'backup-create'), item('Восстановить из резервной копии…', 'backup-restore'),
        { type: 'separator' }, { label: 'Выход', click: () => void requestClose() }] },
      { label: 'Вид', submenu: [item('Общий вид', 'overview'), item('MultiExec', 'multiexec'), { type: 'separator' }, item('Панель подключений', 'toggle-sidebar'), item('Файловая панель', 'toggle-files'),
        { type: 'separator' }, item('Увеличить шрифт', 'zoom-in'), item('Уменьшить шрифт', 'zoom-out')] },
      { label: 'Инструменты', submenu: [item('Настройки…', 'settings'), item('Хранилище учётных данных…', 'vault'), item('SSH-туннели…', 'tunnels'), item('Макросы…', 'macros'),
        { type: 'separator' }, item('Сохранить вывод…', 'save-output'), item('Запись терминала…', 'record'), item('Поиск в терминале…', 'find'), item('Переподключить', 'reconnect')] },
      { label: 'Справка', submenu: [item('Сочетания клавиш', 'help'), item('Проверить обновления…', 'updates'), item('О программе', 'about')] },
    ];
  }
  Menu.buildFromTemplate(template).popup({ window });
  return true;
}

function latestProfile(profile) {
  return store.snapshot().sessions.find(item=>item.id===profile.id) || profile;
}
async function prepareProfile(input) {
  const profile = store.resolveSession(input);
  if(profile.import_warning?.includes('заблокировано'))throw new Error(profile.import_warning);
  if(profile.credential_profile){await ensureVault();const credential=vault.get(profile.credential_profile,profile.protocol);profile.username=credential.username||profile.username;if(!profile.identity_profile)profile.identity_profile='system';}
  if (profile.x11) throw new Error('X11 forwarding в Windows-версии пока не реализован. Отключите X11 в настройках подключения или группы.');
  return profile;
}
async function openSession(args) {
  const original=args.duplicateId&&(terminals.get(args.duplicateId)||rdp.get(args.duplicateId));
  if(args.duplicateId&&!original)throw Error('Исходная вкладка закрыта.');
  const profile = await prepareProfile(original?latestProfile(original.profile):args.id || args.profile || defaultSession('local'));
  if(original&&profile.protocol!==original.profile.protocol)throw Error('Протокол подключения изменён. Откройте новую вкладку из списка подключений.');
  if (profile.protocol === 'rdp') {
    if (typeof args.openToken === 'string' && args.openToken.length <= 160) profile._uiOpenToken=args.openToken;
    return rdp.create(profile);
  }
  if (!['local', 'ssh'].includes(profile.protocol)) throw new Error(`Протокол ${profile.protocol} пока не перенесён на Windows.`);
  if (typeof args.openToken === 'string' && args.openToken.length <= 160) profile._uiOpenToken = args.openToken;
  return terminals.create(profile, args.dimensions || {});
}

async function call(method, args) {
  switch (method) {
    case 'host:stats': return hostStats.read(args.id);
    case 'bootstrap': return { store: store.snapshot(), identities: identities.list(), vault: vaultState(), platform: process.platform, home: os.homedir(), version: app.getVersion(), appData: profileDirectory, terminals: [...terminals.list(),...rdp.list()], collapsedFolders: [...collapsedFolders], smoke: IS_SMOKE };
    case 'backup:create': return backups.run(true);
    case 'backup:restore': return backups.run(false);
    case 'sidebar:collapse': {
      if (typeof args.collapsed !== 'boolean' || !store.snapshot().groups.some(group => group.id === args.id)) throw Error('Папка не найдена.');
      if (args.collapsed) collapsedFolders.add(args.id);
      else collapsedFolders.delete(args.id);
      const snapshot = JSON.stringify({ collapsed: [...collapsedFolders] });
      folderWrites = folderWrites.catch(() => {}).then(() => writeAtomic(sidebarPath, snapshot));
      await folderWrites;
      return true;
    }
    case 'import:preview': return prepareImport();
    case 'import:mobaxterm': return prepareMobaImport();
    case 'import:cancel': discardImport(args.id); return true;
    case 'import:apply': return applyImport(args);
    case 'vault:status': vault.reloadIfChanged(); return vaultState();
    case 'vault:unlock': await ensureVault(true); if(!vault.exists)vault.merge([]); stateUpdate(); return vaultState();
    case 'vault:lock': vault.lock(); stateUpdate(); return vaultState();
    case 'vault:change-master':
      await vault.changeMaster(args.current, args.master);
      stateUpdate();
      return vaultState();
    case 'vault:reset-master':
      await vault.resetMaster(args.master);
      stateUpdate();
      return vaultState();
    case 'vault:save': {
      await ensureVault(true);const input=args.record||{},old=input.id?vault.get(input.id):null;
      const record={id:old?.id||crypto.randomUUID(),name:input.name,username:input.username,protocol:input.protocol,password:input.password===undefined?old?.password||'':input.password};
      if(!['ssh','rdp','both'].includes(record.protocol))throw Error('Выберите SSH или RDP.');
      if(store.snapshot().sessions.some(s=>s.credential_profile===record.id&&!['both',s.protocol].includes(record.protocol)))throw Error('Этот профиль используется подключениями другого протокола.');
      vault.merge([record]);stateUpdate();return vaultState();
    }
    case 'vault:delete':
      await ensureVault();if(store.snapshot().sessions.some(s=>s.credential_profile===args.id))throw Error('Профиль используется в подключениях. Сначала измените их учётные данные.');
      vault.remove(args.id);stateUpdate();return vaultState();
    case 'prompt:respond': {
      if (typeof args.id !== 'string') throw new Error('Неверный запрос подтверждения.');
      const resolve = prompts.get(args.id);
      if (!resolve) return false;
      if (args.answer !== null && typeof args.answer !== 'boolean' && (typeof args.answer !== 'string' || args.answer.length > 65536)) throw new Error('Неверный ответ.');
      prompts.delete(args.id); resolve(args.answer); return true;
    }
    case 'session:save': { const result = store.upsertSession(args.record); stateUpdate(); return result; }
    case 'session:duplicate': { const result = store.duplicateSession(args.id); stateUpdate(); return result; }
    case 'session:rename': {
      const current = store.snapshot().sessions.find(item => item.id === args.id);
      if (!current) throw new Error('Подключение не найдено');
      const result = store.upsertSession({ ...current, name: args.name });
      stateUpdate();
      return result;
    }
    case 'session:delete': { const result = store.deleteSession(args.id); stateUpdate(); return result; }
    case 'session:open': return openSession(args);
    case 'terminal:write': return terminals.write(args.id, args.data);
    case 'terminal:binary': return terminals.writeBinary(args.id, args.data);
    case 'terminal:resize': return terminals.resize(args.id, { cols: args.cols, rows: args.rows });
    case 'ui:floatPrepare': return floating.prepare(args);
    case 'ui:floatReady': return floating.ready(args.token);
    case 'ui:floatReturn': return floating.dock(args.token);
    case 'rdp:bounds': return rdp.bounds(args.id,args);
    case 'rdp:focus': return rdp.focus(args.id);
    case 'terminal:close': {
      requireIdleFiles();
      files.closeTerminal(args.id); await tunnels.sessionClosed(args.id); stopRecording(args.id);
      return rdp.get(args.id)?rdp.close(args.id):terminals.close(args.id);
    }
    case 'terminal:reconnect': {
      requireIdleFiles();const record=rdp.get(args.id)||terminals.get(args.id);
      if(!record)throw Error('Терминал закрыт.');
      const generation=record.generation,profile=await prepareProfile(latestProfile(record.profile));
      if(profile.protocol!==record.profile.protocol)throw Error('Протокол подключения изменён. Откройте новую вкладку из списка подключений.');
      if((rdp.get(args.id)||terminals.get(args.id))!==record||record.generation!==generation)throw Error('Подключение уже изменилось.');
      requireIdleFiles();files.closeTerminal(args.id); await tunnels.sessionClosed(args.id); stopRecording(args.id);
      return rdp.get(args.id)?rdp.reconnect(args.id,profile):terminals.reconnect(args.id,profile);
    }
    case 'terminal:saveOutput': {
      if (typeof args.text !== 'string' || Buffer.byteLength(args.text, 'utf8') > 64 * 1024 * 1024) throw new Error('Недопустимый размер вывода.');
      const selected = await dialog.showSaveDialog(window, { title: 'Сохранить вывод терминала', defaultPath: 'AstraTerm-output.txt', filters: [{ name: 'Текст', extensions: ['txt', 'log'] }] });
      if (selected.canceled || !selected.filePath) return null;
      await writeAtomic(selected.filePath, args.text); return selected.filePath;
    }
    case 'terminal:record': {
      if (stopRecording(args.id)) return { active: false };
      const record = terminals.get(args.id);
      if (!record || record.status !== 'running') throw new Error('Для записи нужен работающий терминал.');
      const generation = record.generation;
      const selected = await dialog.showSaveDialog(window, { title: 'Записывать вывод терминала', defaultPath: 'AstraTerm-session.log' });
      if (selected.canceled || !selected.filePath) return { active: false };
      if (terminals.get(args.id) !== record || generation !== record.generation || record.status !== 'running') throw new Error('Терминал уже изменился.');
      if (recorders.has(args.id)) return { active: true };
      const recorder = fs.createWriteStream(selected.filePath, { flags: 'w', encoding: 'utf8', mode: 0o600 });
      recorders.set(args.id, recorder);
      recorder.on('error', () => stopRecording(args.id, 'Не удалось сохранить журнал терминала.'));
      emit('terminal:recording', { id: args.id, active: true }); return { active: true };
    }
    case 'profile:list': return identities.list();
    case 'profile:generate': {
      const profile = await identities.generate(args);
      stateUpdate();
      return profile;
    }
    case 'profile:copyPublic': {
      clipboard.writeText(identities.publicKey(args.id));
      return true;
    }
    case 'profile:save': identities.upsert(args.record, { makeDefault: args.makeDefault === true }); stateUpdate(); return identities.list();
    case 'profile:delete': {
      const snapshot=store.snapshot();
      if(snapshot.sessions.some(s=>s.identity_profile===args.id)||snapshot.groups.some(g=>g.ssh?.identity_profile===args.id))throw Error('SSH-профиль используется в подключениях или папках. Сначала измените их учётные данные.');
      identities.remove(args.id); stateUpdate(); return identities.list();
    }
    case 'profile:default': identities.setDefault(args.id); stateUpdate(); return identities.list();
    case 'profile:unlock': await identities.ensure({ identity_profile: args.id }); stateUpdate(); return identities.list();
    case 'group:save': { const result = store.upsertGroup(args.record, args.applyToSessions === true); stateUpdate(); return result; }
    case 'group:rename': {
      const current = store.snapshot().groups.find(item => item.id === args.id);
      if (!current) throw new Error('Папка не найдена');
      const name = typeof args.name === 'string' ? args.name.trim() : '';
      if (!name || name.includes('/') || name.includes('\\')) throw new Error('Укажите имя папки без разделителей пути');
      const parent = current.name.slice(0, current.name.lastIndexOf('/') + 1);
      const result = store.upsertGroup({ ...current, name: parent + name });
      stateUpdate();
      return result;
    }
    case 'group:delete': { const result = store.deleteGroup(args.name); stateUpdate(); return result; }
    case 'store:import': {
      const selected = await dialog.showOpenDialog(window, { title: 'Импорт подключений AstraTerm', properties: ['openFile'], filters: [{ name: 'AstraTerm JSON', extensions: ['json'] }] });
      if (selected.canceled || !selected.filePaths[0]) return null;
      const stat = await fs.promises.stat(selected.filePaths[0]);
      if (!stat.isFile() || stat.size > 8 * 1024 * 1024) throw new Error('Файл подключений превышает 8 МиБ.');
      const bytes = await fs.promises.readFile(selected.filePaths[0]);
      if (bytes.length > 8 * 1024 * 1024) throw new Error('Файл подключений превышает 8 МиБ.');
      let document;
      try { document = JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, '')); } catch (_) { throw new Error('Файл не содержит корректный JSON.'); }
      const result = store.importDocument(document); stateUpdate(); return result;
    }
    case 'store:export': {
      const selected = await dialog.showSaveDialog(window, { title: 'Экспорт подключений AstraTerm', defaultPath: 'astraterm-sessions.json', filters: [{ name: 'AstraTerm JSON', extensions: ['json'] }] });
      if (selected.canceled || !selected.filePath) return null;
      const relative = path.relative(appDirectory, path.resolve(selected.filePath));
      if (relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))) throw new Error('Для экспорта выберите файл вне служебного каталога AstraTerm.');
      await writeAtomic(selected.filePath, JSON.stringify(store.exportDocument(), null, 2) + '\n'); return selected.filePath;
    }
    case 'files:list': return files.list(args);
    case 'files:drag-prepare':
      if (args.mode !== 'sftp' || !Number.isInteger(args.generation)) throw Error('Выберите файл в активной SFTP-сессии.');
      return sftpDrag.prepare(args, captureBinding(args));
    case 'files:drag-start':
      return sftpDrag.start(args.token, file => window.webContents.startDrag({
        file, icon: path.join(__dirname, '..', 'assets', 'astraterm.png'),
      }));
    case 'files:mkdir': return files.mkdir(args);
    case 'files:rename': return files.rename(args);
    case 'files:remove': { const check = captureBinding(args); if (!await confirm('Удалить файл или папку?', args.path + '\nНепустые каталоги не удаляются.', 'Удалить')) return null; check(); return files.remove(args); }
    case 'files:readText': return files.readText(args);
    case 'files:writeText': return files.writeText(args);
    case 'files:upload': {
      if (args.mode !== 'sftp') throw new Error('Для загрузки включите SFTP.');
      const check = captureBinding(args);
      const selected = await dialog.showOpenDialog(window, { title: 'Загрузить файл по SFTP', properties: ['openFile'] });
      if (selected.canceled || !selected.filePaths[0]) return null;
      const results = await uploadFiles(args, selected.filePaths, check);
      return results[0] || null;
    }
    case 'files:download': {
      if (args.mode !== 'sftp') throw new Error('Для скачивания включите SFTP.');
      const check = captureBinding(args);
      const name = path.win32.basename(args.name || path.posix.basename(args.path || 'download'));
      const selected = await dialog.showSaveDialog(window, { title: 'Скачать файл по SFTP', defaultPath: path.join(app.getPath('downloads'), name) });
      if (selected.canceled || !selected.filePath) return null;
      check();
      return files.download({ mode: 'sftp', terminalId: args.terminalId, remotePath: args.path, localPath: selected.filePath, overwrite: true });
    }
    case 'files:up': return args.mode === 'sftp' ? path.posix.dirname(String(args.path || '/')) : path.dirname(String(args.path || os.homedir()));
    case 'dialog:pickKey': {
      const selected = await dialog.showOpenDialog(window, { title: 'Выбрать приватный SSH-ключ', defaultPath: path.join(os.homedir(), '.ssh'), properties: ['openFile'] });
      return selected.canceled ? null : selected.filePaths[0] || null;
    }
    case 'dialog:pickFolder': {
      const selected = await dialog.showOpenDialog(window, { title: 'Выбрать каталог', properties: ['openDirectory'] });
      return selected.canceled ? null : selected.filePaths[0] || null;
    }
    case 'clipboard:read': return clipboard.readText();
    case 'clipboard:write': if (typeof args.text !== 'string' || args.text.length > 16 * 1024 * 1024) throw new Error('Слишком большой текст буфера.'); await clipboard.writeText(args.text); return true;
    case 'macro:save': { const result = store.upsertMacro(args.record); stateUpdate(); return result; }
    case 'macro:delete': { const result = store.deleteMacro(args.id); stateUpdate(); return result; }
    case 'tunnel:save': {
      const current = terminals.get(args.terminalId);
      const profile = current?.profile || {};
      const result = store.upsertTunnel({ host: profile.host, port: profile.port, username: profile.username, key_file: profile.key_file, jump_host: profile.jump_host, ...args.record });
      stateUpdate(); return result;
    }
    case 'tunnel:delete': { const result = store.deleteTunnel(args.id); stateUpdate(); return result; }
    case 'tunnel:start': return tunnels.start({ ...args.record, terminalId: args.terminalId });
    case 'tunnel:stop': return tunnels.stop(args.id);
    case 'tunnel:list': return tunnels.list();
    case 'app:updates': void updates.check(); return true;
    case 'logging:settings': return { ...diagnosticLog.settings, filename: diagnosticLog.filename };
    case 'logging:save': {
      const next = diagnosticLog.validate(args);
      loggingWrites = loggingWrites.catch(() => {}).then(async () => {
        await writeAtomic(diagnosticLog.settingsPath, JSON.stringify(next));
        diagnosticLog.settings = next;
        diagnosticLog.write('info', 'logging.configured');
      });
      await loggingWrites;
      return true;
    }
    case 'app:dirty': dirtyCount = Number.isSafeInteger(args.count) && args.count >= 0 ? Math.min(1000, args.count) : 0; return true;
    case 'app:diagnostics': return { version: app.getVersion(), platform: process.platform, architecture: process.arch, windows: os.release(), electron: process.versions.electron, node: process.versions.node,
      terminal: 'xterm.js / Windows ConPTY', ssh: 'ssh2 / проверка ключа сервера', sftp: 'в активном SSH-соединении', protocols: ['local', 'ssh', 'rdp (встроенный ActiveX)'], unsupported: ['X11', 'Mosh', 'Telnet', 'Serial', 'VNC'], appData: appDirectory };
    case 'app:componentNotices': {
      const notices = app.isPackaged ? path.join(process.resourcesPath, 'THIRD_PARTY_NOTICES.md')
        : path.join(__dirname, '../../docs/THIRD_PARTY_NOTICES.md');
      const assets = path.join(app.getAppPath(), 'assets');
      const paths = [notices, path.join(assets, 'CONPTY-LICENSE.txt'), path.join(assets, 'NSIS-COPYING.txt')];
      const [summary, conpty, nsis] = await Promise.all(paths.map(file => fs.promises.readFile(file, 'utf8')));
      return [summary, 'ConPTY / OpenConsole\n\n' + conpty, 'NSIS\n\n' + nsis].join('\n\n');
    }
    case 'external:open': {
      if (typeof args.url !== 'string' || args.url.length > 4096) throw new Error('Недопустимая ссылка.');
      let url;
      try { url = new URL(args.url); } catch (_) { throw new Error('Недопустимая ссылка.'); }
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Разрешены только ссылки HTTP и HTTPS.');
      await shell.openExternal(url.href); return true;
    }
    case 'ui:menu': case 'ui:sessionMenu': case 'ui:terminalMenu': case 'ui:fileMenu': case 'ui:tabMenu': return popupMenu(method, args);
    case 'window:minimize': window.minimize(); return true;
    case 'window:maximize': if (window.isMaximized()) window.unmaximize(); else window.maximize(); return window.isMaximized();
    case 'window:close': void requestClose(); return true;
    default: throw new Error('Неизвестное действие приложения.');
  }
}

async function createWindow() {
  profileDirectory = activeProfile(appDirectory);
  sidebarPath = path.join(profileDirectory, 'sidebar.json');
  diagnosticLog = new DiagnosticLog(profileDirectory);
  diagnosticLog.write('info', 'app.start');
  installLocalUI(protocol, path.dirname(__dirname));
  store = new Store(profileDirectory);
  try {
    const sidebar = JSON.parse(fs.readFileSync(sidebarPath, 'utf8'));
    const known = new Set(store.snapshot().groups.map(group => group.id));
    if (Array.isArray(sidebar?.collapsed)) collapsedFolders = new Set(sidebar.collapsed.filter(id => known.has(id)));
  } catch { /* The session list opens expanded when sidebar preferences are unavailable. */ }
  vault = new CredentialVault(path.join(profileDirectory,'credentials.vault.json'));
  identities = new IdentityManager({ directory: profileDirectory, prompt, documents: documentsDirectory,
    ...(IS_SMOKE ? { sshDirectory: path.join(appDirectory, 'ssh'), agentPath: `\\\\.\\pipe\\astraterm-smoke-${crypto.randomUUID()}` } : {}) });
  trust = new HostTrustStore({ directory: profileDirectory, prompt, ...(IS_SMOKE ? { knownHostsPath: path.join(appDirectory, 'known_hosts') } : {}) });
  terminals = new TerminalManager({ emit, prompt, identityManager: identities, trustStore: trust,
    credentialProvider:async profile=>{await ensureVault();const credential=vault.get(profile.credential_profile,profile.protocol||'ssh');
      if(credential.username&&credential.username!==profile.username)throw new Error('Пользователь профиля изменился. Откройте сохранённое подключение заново.');
      return credential.password;} });
  files = new FilesService({ terminals });
  sftpDrag = new SftpDrag(files);
  hostStats = new HostStats(files,terminals);
  tunnels = new TunnelManager({ terminals, emit });
  window = new BrowserWindow({ width: 1360, height: 840, minWidth: 900, minHeight: 600, frame: false,
    title: 'AstraTerm', icon: path.join(__dirname, '..', 'assets', 'astraterm.ico'), backgroundColor: '#171b20', show: false, autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false, webviewTag: false, spellcheck: false, backgroundThrottling: false } });
  Menu.setApplicationMenu(null);
  rdp=new RdpManager({window,emit,prompt,credentialProvider:async profile=>{
    await ensureVault();const credential=vault.get(profile.credential_profile,'rdp');
    if(credential.username&&credential.username!==profile.username)throw new Error('Пользователь профиля изменился.');
    return credential.password;
  }});
  floating=new FloatingWindows({owner:window,url:floatingURL,terminals,rdp,emit});
  updates=new UpdateController({app,window,directory:appDirectory,dialog,finish,isClosing:()=>finishing||window.isDestroyed(),showProgress:value=>emit('update:progress',value),canInstall:async()=>{
    if(files.busy||dirtyCount){await dialog.showMessageBox(window,{type:'info',title:'Обновления AstraTerm',message:'Завершите файловые операции и сохраните открытые редакторы перед обновлением.'});return false;}installingUpdate=true;return true;
  },endInstall:()=>{installingUpdate=false;}});
  for(const event of ['focus','blur','show','restore','resize','minimize'])window.on(event,()=>rdp.refresh(window));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.webContents.session.setPermissionCheckHandler(() => false);
  backups = new BackupDialogs({ window, dialog, root: appDirectory, directory: profileDirectory,
    documents: documentsDirectory, prompt, confirm, isClosing: () => finishing || window.isDestroyed(),
    flushSettings: () => Promise.all([folderWrites, loggingWrites]) });
  window.webContents.session.on('will-download', event => event.preventDefault());
  window.on('close', event => { if (!allowClose) { event.preventDefault(); void requestClose(); } });
  window.on('maximize', () => emit('window:state', { maximized: true }));
  window.on('unmaximize', () => emit('window:state', { maximized: false }));
  window.once('ready-to-show', () => { if (!IS_SMOKE) window.show(); });
  ipcMain.handle('astra:call', async (event, request) => {
    try { assertAppSender(event); }
    catch (error) { diagnosticLog.write('warning', 'ipc.sender-denied'); throw error; }
    if (!request || typeof request !== 'object' || typeof request.method !== 'string' || request.method.length > 80
      || !request.args || typeof request.args !== 'object' || Array.isArray(request.args)) throw new Error('Некорректный запрос приложения.');
    const category = request.method.split(':', 1)[0];
    const operation = ['session','terminal','files','import','vault','logging','app','group','profile','sidebar','backup'].includes(category) ? category : 'other';
    if (!/^terminal:(?:write|resize)$/.test(request.method)) diagnosticLog.write('debug', 'ipc.' + operation);
    try { return await call(request.method, request.args); }
    catch (error) {
      diagnosticLog.write('error', 'ipc.' + operation + '.error');
      if (IS_SMOKE) process.stderr.write('SMOKE_IPC_ERROR ' + request.method + ': ' + safeError(error) + '\n');
      throw new Error(safeError(error));
    }
  });
  ipcMain.handle('astra:upload-dropped', async (event, endpoint, paths) => {
    assertAppSender(event);
    if (endpoint?.mode !== 'sftp' || !Number.isInteger(endpoint.generation) || !Array.isArray(paths) ||
        !paths.length || paths.length > 100 || paths.some(source => typeof source !== 'string' || !path.isAbsolute(source) || sftpDrag.owns(source))) {
      throw new Error('Некорректная загрузка по SFTP.');
    }
    const results = [];
    try { await uploadFiles(endpoint, paths, captureBinding(endpoint), results); }
    catch (error) { return { results, error: safeError(error) }; }
    return { results };
  });
  if (IS_SMOKE) window.webContents.once('did-finish-load', () => {
    Promise.resolve().then(() => require(IS_WEBSITE?'./website-capture.cjs':IS_RDP_TEST?'./smoke-rdp.cjs':'./smoke.cjs')({ window, store, terminals, rdp, vault, files, identities, trust, tunnels, prompt, emit, finish }))
      .catch(() => { process.stderr.write('ASTRATERM_WINDOWS_SMOKE_FAILED\n'); void finish(1); });
  });
  await window.loadURL(entryURL);
  if(!IS_SMOKE){const timer=setTimeout(()=>void updates.check(true),15000);timer.unref();}
}

if (!IS_SMOKE && !app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window && !window.isDestroyed()) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } });
  app.on('before-quit', event => { if (!allowClose) { event.preventDefault(); void requestClose(); } });
  app.on('window-all-closed', () => { if (!finishing) void finish(); });
  app.whenReady().then(createWindow).catch(async () => {
    if (!IS_SMOKE) await dialog.showMessageBox({ type: 'error', title: 'AstraTerm', message: 'Не удалось запустить AstraTerm. Проверьте доступ к каталогу настроек приложения.' });
    if (IS_SMOKE) process.stderr.write('ASTRATERM_WINDOWS_START_FAILED\n');
    await finish(1);
  });
}
