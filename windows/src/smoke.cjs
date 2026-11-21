'use strict';

// Runs only with --smoke-test, isolated application data and synthetic keys.
// This same test is shipped to exercise the actual portable executable.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createFixture } = require('./smoke-fixture.cjs');
const keyboard = require('./keyboard-shortcuts-contract.cjs');

module.exports = async function smoke({ window, store, vault, terminals, files, identities, emit, finish }) {
  const output = path.resolve(process.env.ASTRA_SMOKE_OUTPUT || path.join(__dirname, '..', '..', 'dist', 'windows', 'validation'));
  fs.mkdirSync(output, { recursive: true });
  const report = { platform: process.platform, electron: process.versions.electron, packaged: require('electron').app.isPackaged, checks: [], result: 'RUNNING' };
  let fixture;
  let stage = 'renderer startup';
  const errors = [];
  window.webContents.on('console-message', details => {
    if (details?.level === 'error') errors.push(String(details.message).slice(0, 500));
  });
  const run = code => window.webContents.executeJavaScript(code, true).catch(error => {
    throw new Error(errors.at(-1) || error.message);
  });
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(predicate, message, timeout = 20000) {
    const start = Date.now();
    while (!await predicate()) {
      if (Date.now() - start > timeout) throw new Error(message);
      await delay(60);
    }
  }
  async function check(name, work) { stage = name; await work(); report.checks.push(name); process.stdout.write('SMOKE_OK ' + name + '\n'); }
  const snapshot = id => run(`(()=>{const p=__astraTest.state.panes.get(${JSON.stringify(id)});if(!p)return '';const b=p.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join(String.fromCharCode(10))})()`);
  async function key(keyCode, character, modifiers = []) {
    window.webContents.sendInputEvent({ type: 'keyDown', keyCode, modifiers });
    if (character) window.webContents.sendInputEvent({ type: 'char', keyCode: character, modifiers });
    window.webContents.sendInputEvent({ type: 'keyUp', keyCode, modifiers });
    await delay(90);
  }
  async function renameWithF2(selector, identifier, names) {
    await run(`Array.from(document.querySelectorAll(${JSON.stringify(selector)})).find(row =>
      Object.values(row.dataset).includes(${JSON.stringify(identifier)})).click()`);
    await key(keyboard.rename.key);
    await until(() => run("Boolean(document.querySelector('dialog[open] input'))"), 'F2 rename dialog missing');
    assert.equal(await run("document.querySelector('dialog[open] input').value"), names[0]);
    await run(`(()=>{const d=document.querySelector('dialog[open]');d.querySelector('input').value=${JSON.stringify(names[1])};d.querySelector('.primary').click()})()`);
  }
  async function routed(source, character, expected, modifiers = []) {
    await run(`__astraTest.focusPane(${JSON.stringify(source)})`);
    await delay(100);
    await until(() => run(`document.activeElement===__astraTest.state.panes.get(${JSON.stringify(source)}).term.textarea&&__astraTest.state.active===${JSON.stringify(source)}`), 'Target terminal did not receive focus');
    await run('__astraTest.trace=[]');
    await key(character.toLowerCase(), character, modifiers);
    await until(async () => (await run('__astraTest.trace.length')) >= expected.length, 'Input did not reach expected panes');
    const trace = await run('__astraTest.trace');
    assert.deepEqual(trace.map(item => item.id).sort(), [...expected].sort());
    assert.ok(trace.every(item => item.data === character), 'Keystroke was changed or duplicated');
  }
  try {
    window.webContents.setBackgroundThrottling(false);
    await until(() => run('Boolean(window.__astraTest?.ready)'), 'Renderer did not initialize');
    window.show(); window.focus();
    const initial = await run('[...__astraTest.state.panes.keys()][0]');
    await check('local desktop URL and logging settings need no web server', async () => {
      assert.equal(await run('location.href'), require('./local-ui.cjs').entryURL);
      const logging = await run('astra.invoke("logging:settings")');
      assert.equal(logging.enabled, false);
      await run('__astraTest.action("settings")');
      await run(`(()=>{const dialog=document.querySelector('dialog[open]');dialog.querySelector('input[type=checkbox]').click();
        dialog.querySelector('[name=level]').value='debug';dialog.querySelector('.primary').click()})()`);
      await until(() => run('!document.querySelector("dialog[open]")'), 'Settings were not saved');
      assert.equal((await run('astra.invoke("logging:settings")')).level, 'debug');
      assert.ok(fs.existsSync(logging.filename));
      assert.match(fs.readFileSync(logging.filename, 'utf8'), /logging\.configured/);
      await run('astra.invoke("logging:save",{enabled:false,level:"debug"})');
    });
    await check('update download progress and verification render in the desktop window', async () => {
      const reportProgress = payload => window.webContents.send('astra:event', { type: 'update:progress', payload });
      reportProgress({ version: '99.0.0', percent: 25 });
      await until(() => run("document.querySelector('dialog[open] progress.update-progress')?.value === 25"), 'Update progress did not render');
      reportProgress({ version: '99.0.0', percent: 100 });
      await until(() => run("document.querySelector('dialog[open] [role=status]')?.textContent.includes('SHA-256')"), 'Update verification stage did not render');
      reportProgress(null);
      await until(() => run("!document.querySelector('dialog[open]')"), 'Update progress window did not close');
    });
    await check('local host status shows hostname, RAM and sampled CPU',async()=>{
      await until(()=>run('document.querySelector(".host-hostname")?.textContent.length>0&&document.querySelector(".host-memory")?.textContent.includes("ГиБ")'),'Local host stats missing');
      await until(()=>run('document.querySelector(".host-cpu")?.textContent.includes("%")'),'CPU delta was not sampled');
      assert.equal(await run('document.getElementById("host-stats").hidden'),false);
    });
    await check('sandboxed renderer and real PowerShell ConPTY', async () => {
      process.stdout.write('SMOKE_STEP renderer globals\n');
      assert.equal(await run('typeof require'), 'undefined');
      assert.equal(await run('typeof process'), 'undefined');
      process.stdout.write('SMOKE_STEP PowerShell snapshot\n');
      await until(async () => (await snapshot(initial)).includes('PS '), 'PowerShell prompt was not rendered');
      terminals.write(initial, "[Console]::WriteLine('ASTRA_WINDOWS_' + (6 * 7))\r");
      await until(async () => (await snapshot(initial)).includes('ASTRA_WINDOWS_42'), 'Actual PowerShell command output missing');
    });
    await check('Ctrl+0 resets terminal font and F2 renames sidebar items', async () => {
      const size = keyboard.font.default.windows;
      const currentFont = () => run('__astraTest.state.panes.values().next().value.term.options.fontSize');
      await run(`__astraTest.focusPane(${JSON.stringify(initial)})`);
      assert.equal(await currentFont(), size);
      await key(keyboard.font.increase, null, ['control']);
      assert.equal(await currentFont(), size + 1);
      await key(keyboard.font.reset, null, ['control']);
      assert.equal(await currentFont(), size);
      const group = await run(`astra.invoke('group:save',{record:{name:${JSON.stringify(keyboard.rename.group[0])},ssh:{port:2201}}})`);
      const session = await run(`astra.invoke('session:save',{record:{protocol:'ssh',host:'rename.example',name:${JSON.stringify(keyboard.rename.session[0])},group:${JSON.stringify(group.name)}}})`);
      const sessionRow = '.session-row[data-sidebar-id]';
      const groupRow = '.folder-row[data-sidebar-id]';
      await until(() => run(`Boolean(document.querySelector('[data-sidebar-id="session:${session.id}"]'))`), 'Saved test session missing');
      await renameWithF2(sessionRow, 'session:' + session.id, keyboard.rename.session);
      const savedSession = () => run(`__astraTest.state.store.sessions.find(item=>item.id===${JSON.stringify(session.id)})`);
      await until(async () => (await savedSession()).name === keyboard.rename.session[1], 'F2 did not rename session');
      assert.deepEqual(await savedSession(), { ...session, name: keyboard.rename.session[1] });
      await renameWithF2(groupRow, 'group:' + group.id, keyboard.rename.group);
      await until(async () => (await savedSession()).group === keyboard.rename.group[1], 'F2 did not move group contents');
      const savedGroup = await run(`__astraTest.state.store.groups.find(item=>item.id===${JSON.stringify(group.id)})`);
      assert.deepEqual(savedGroup, { ...group, name: keyboard.rename.group[1] });
      const { Menu } = require('electron');
      let capturedMenu;
      const popup = Menu.prototype.popup;
      Menu.prototype.popup = function () { capturedMenu = this; };
      try {
        const before = terminals.list().length;
        const selector = `[data-sidebar-id="session:${session.id}"]`;
        await run(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,button:2}))`);
        await until(() => Boolean(capturedMenu), 'Saved connection context menu did not open');
        capturedMenu.items.find(item => item.label === 'Дублировать').click();
        const name = keyboard.rename.session[1] + ' (1)';
        const copyInStore = () => run(`__astraTest.state.store.sessions.find(item=>item.name===${JSON.stringify(name)})`);
        await until(async () => Boolean(await copyInStore()), 'Saved connection copy did not appear');
        const copy = await copyInStore();
        assert.deepEqual(copy, { ...await savedSession(), id: copy.id, name });
        assert.equal(terminals.list().length, before);
        await until(() => run(`__astraTest.state.selectedSession===${JSON.stringify(copy.id)}`), 'Saved connection copy was not selected');
        await run(`astra.invoke('session:delete',{id:${JSON.stringify(copy.id)}})`);
      } finally {
        Menu.prototype.popup = popup;
      }
      await run(`astra.invoke('session:delete',{id:${JSON.stringify(session.id)}})`);
      await run(`astra.invoke('group:delete',{name:${JSON.stringify(savedGroup.name)}})`);
    });
    let identity;
    const passphrase = 'synthetic-key-generation-fixture';
    await check('credential manager generates encrypted OpenSSH key and offers its public file', async () => {
      await run("__astraTest.action('profiles')");
      await run(`Array.from(document.querySelectorAll('dialog[open] button')).find(b=>b.textContent==='Создать SSH-ключ…').click()`);
      await until(() => run("Boolean(document.querySelector('dialog[open] [name=algorithm]'))"), 'Key generation form missing');
      await run(`(()=>{
        const d=document.querySelector('dialog[open] [name=algorithm]').closest('dialog');
        d.querySelector('[name=name]').value='Isolated generated key';
        d.querySelector('[name=passphrase]').value=${JSON.stringify(passphrase)};
        d.querySelector('[name=repeat]').value=${JSON.stringify(passphrase)};
        d.querySelector('.primary').click();
      })()`);
      await until(() => run("Array.from(document.querySelectorAll('dialog[open] h2')).some(el=>el.textContent==='SSH-ключ создан')"), 'Generated key confirmation missing');
      identity = identities.list().profiles.find(p => p.name === 'Isolated generated key');
      assert.ok(identity);
      assert.equal(identities.publicKey(identity.id), fs.readFileSync(identity.key_file + '.pub', 'utf8').trim());
      await run("Array.from(document.querySelectorAll('dialog[open] h2')).find(el=>el.textContent==='SSH-ключ создан').closest('dialog').close()");
      await run("document.querySelector('dialog[open]').close()");
      await until(() => run("!document.querySelector('dialog[open]')"), 'Credential manager did not close');
    });
    fixture = await createFixture(path.join(path.dirname(identities.filename), 'fixture'), { keyFile: identity.key_file, passphrase });
    const statsFixture=require('./host-stats-contract.cjs');
    for(const [name,value]of Object.entries(statsFixture.files)){
      const filename=path.join(fixture.remoteRoot,name);fs.mkdirSync(path.dirname(filename),{recursive:true});fs.writeFileSync(filename,value);
    }
    // Answer only authentication dialogs for our generated loopback key/server.
    await run(`window.__smokeAuth={passphrase:0,host:0};window.__smokeUnsubscribe=astra.onEvent(({type,payload})=>{if(type==='prompt:request'){if(payload.kind==='passphrase')__smokeAuth.passphrase++;else if(payload.kind==='host-key')__smokeAuth.host++;else throw new Error('Unexpected authentication prompt')}});window.__smokeAnswer=setInterval(()=>{const d=[...document.querySelectorAll('dialog[open]')].at(-1);if(!d)return;const p=d.querySelector('input[type=password]');if(p){p.value=${JSON.stringify(fixture.passphrase)};d.querySelector('.primary').click()}else if(d.querySelector('.fingerprint'))d.querySelector('.primary').click()},80)`);
    const sshProfile = { ...fixture.profilebase, identity_profile: identity.id, name: 'SSH · first host' };
    let first; let second;
    await check('generated Ed25519 key unlocks once and authenticates real SSH', async () => {
      process.stdout.write('SMOKE_STEP first SSH\n');
      first = await run(`__astraTest.openSession(${JSON.stringify(sshProfile)}).then(record=>record.id)`);
      process.stdout.write('SMOKE_STEP second SSH\n');
      second = await run(`__astraTest.openSession(${JSON.stringify({ ...sshProfile, name: 'SSH · second host' })}).then(record=>record.id)`);
      process.stdout.write('SMOKE_STEP SSH counters\n');
      assert.deepEqual(await run('__smokeAuth'), { passphrase: 1, host: 1 });
      assert.equal(fixture.counters.authenticated, 2);
      await run('clearInterval(__smokeAnswer);__smokeUnsubscribe()');
    });
    await check('SFTP follows active SSH and shares authenticated transport', async () => {
      await until(async () => await run(`__astraTest.state.files.terminalId===${JSON.stringify(second)}&&__astraTest.state.files.entries.some(e=>e.name==='welcome.txt')`), 'Active SFTP directory missing');
      await run(`__astraTest.focusPane(${JSON.stringify(first)});__astraTest.syncFiles()`);
      await until(async () => await run(`__astraTest.state.files.terminalId===${JSON.stringify(first)}&&__astraTest.state.files.entries.some(e=>e.name==='welcome.txt')`), 'SFTP did not follow active host');
      const read = await files.readText({ mode: 'sftp', terminalId: first, path: '/welcome.txt' });
      assert.match(read.text, /AstraTerm/);
      await files.writeText({ mode: 'sftp', terminalId: first, path: '/welcome.txt', text: 'AstraTerm Windows SFTP verified\n', revision: read.revision });
      assert.equal((await files.readText({ mode: 'sftp', terminalId: first, path: '/welcome.txt' })).text, 'AstraTerm Windows SFTP verified\n');
      assert.equal(fixture.counters.authenticated, 2, 'SFTP must not authenticate a second connection');
    });
    await check('SSH last row stays inside the viewport above the status bar', async () => {
      const contract = require('./tab-contracts.cjs').terminal_viewport;
      const originalSize = window.getContentSize();
      const pane = `__astraTest.state.panes.get(${JSON.stringify(first)})`;
      try {
        for (const steps of contract.zoom_steps) {
          await run(`${pane}.term.options.fontSize=__astraTest.state.fontSize+${steps}`);
          for (const [width, height] of contract.sizes) {
            window.setContentSize(width, height);
            await until(() => run(`Math.abs(innerWidth-${width})<=2 && Math.abs(innerHeight-${height})<=2`), 'Terminal test window did not resize');
            const geometry = await run(`new Promise(resolve => setTimeout(() => {
              const { term, element } = ${pane};
              const screen = term.element.querySelector('.xterm-screen');
              const row = screen.querySelector('.xterm-rows').lastElementChild;
              resolve({rows:term.rows, cols:term.cols, rowBottom:row.getBoundingClientRect().bottom,
                rowHeight:row.getBoundingClientRect().height, screenBottom:screen.getBoundingClientRect().bottom,
                viewportBottom:term.element.getBoundingClientRect().bottom,
                paneBottom:element.getBoundingClientRect().bottom, footerTop:document.querySelector('footer').getBoundingClientRect().top});
            }, 120))`);
            const detail = JSON.stringify({height, steps, ...geometry});
            assert.ok(geometry.rowBottom <= geometry.viewportBottom + 1, 'Last row is clipped: ' + detail);
            assert.ok(geometry.screenBottom <= geometry.viewportBottom + 1, 'xterm screen exceeds viewport: ' + detail);
            assert.ok(geometry.paneBottom <= geometry.footerTop + 1, 'Terminal pane overlaps status bar: ' + detail);
            assert.ok(geometry.viewportBottom - geometry.screenBottom < geometry.rowHeight + 1, 'A complete row was left unused: ' + detail);
            await until(() => {
              const size = terminals.get(first).dimensions;
              return size.rows === geometry.rows && size.cols === geometry.cols;
            }, 'SSH PTY dimensions did not follow the viewport');
          }
        }
      } finally {
        await run(`${pane}.term.options.fontSize=__astraTest.state.fontSize`);
        window.setContentSize(...originalSize);
      }
    });
    await check('Linux SSH mark and independent side panel toggles preserve live SSH and SFTP', async () => {
      await run(`astra.invoke('session:save',{record:${JSON.stringify(sshProfile)}})`);
      assert.equal(await run(`[...document.querySelectorAll('.session-row')].some(row=>
        row.textContent.trim()==='SSH · first host' && !!row.querySelector('svg ellipse[fill="#f8bb45"]'))`), true);
      const readState = () => run(`({
        left:!document.getElementById('sidebar').hidden,right:!document.getElementById('files-panel').hidden,
        width:document.getElementById('workspace').clientWidth,
        leftControl:document.getElementById('toggle-sidebar').getAttribute('aria-pressed'),
        rightControl:document.getElementById('toggle-files').getAttribute('aria-pressed'),
        mode:__astraTest.state.files.mode,terminalId:__astraTest.state.files.terminalId,path:__astraTest.state.files.path,
      })`);
      const initial = await readState();
      const connection = terminals.get(first);
      const transport = connection.sshClient;
      let width = initial.width;
      for (const step of require('./tab-contracts.cjs').side_panels) {
        const id = step.toggle === 'left' ? 'toggle-sidebar' : 'toggle-files';
        await run(`document.getElementById(${JSON.stringify(id)}).click()`);
        const actual = await readState();
        assert.equal(actual.left, step.left);
        assert.equal(actual.right, step.right);
        assert.equal(actual.leftControl, String(step.left));
        assert.equal(actual.rightControl, String(step.right));
        assert.ok(step.grows ? actual.width > width : actual.width < width);
        assert.deepEqual([actual.mode, actual.terminalId, actual.path], [initial.mode, initial.terminalId, initial.path]);
        width = actual.width;
      }
      assert.equal(terminals.get(first), connection);
      assert.equal(connection.sshClient, transport);
      assert.equal(fixture.counters.authenticated, 2);
    });
    await check('SFTP metadata and native drag export hand a complete local file to the OS', async () => {
      fs.writeFileSync(path.join(fixture.remoteRoot, 'sort-small.bin'), 'a');
      fs.writeFileSync(path.join(fixture.remoteRoot, 'sort-large.bin'), 'a'.repeat(256));
      await run("__astraTest.loadFiles('/')");
      await until(() => run('Boolean(document.querySelector(".sftp-details .file-row.draggable-file"))'), 'SFTP drag row missing');
      const displayed = () => run(`[...document.querySelectorAll('.file-row .file-name')].map(row=>row.textContent).filter(name=>name.startsWith('sort-'))`);
      await run('document.querySelector(".file-head [data-sort=size]").click()');
      assert.deepEqual(await displayed(), ['sort-small.bin', 'sort-large.bin']);
      await run('document.querySelector(".file-head [data-sort=size]").click()');
      assert.deepEqual(await displayed(), ['sort-large.bin', 'sort-small.bin']);
      await run('document.querySelector(".file-head [data-sort=name]").click()');
      const metadata = await run(`(()=>{const e=__astraTest.state.files.entries.find(row=>row.name==='welcome.txt');
        return {permissions:e.permissions,mtime:e.mtime,created:e.created};})()`);
      assert.match(metadata.permissions, /^[rwxstST-]{9}$/);
      assert.ok(Number.isFinite(metadata.mtime));
      assert.equal(metadata.created, null);
      const position = await run(`(()=>{const r=[...document.querySelectorAll('.file-row')].find(row=>row.querySelector('.file-name').textContent==='welcome.txt').getBoundingClientRect();
        return {x:Math.round(r.left+48),y:Math.round(r.top+r.height/2)};})()`);
      let nativeFile;
      const native = window.webContents.startDrag;
      window.webContents.startDrag = item => { nativeFile = item.file; };
      const debuggerApi = window.webContents.debugger;
      debuggerApi.attach('1.3');
      const mouse = (type, x, down) => debuggerApi.sendCommand('Input.dispatchMouseEvent', {
        type, x, y: position.y, button: type === 'mouseMoved' ? 'none' : 'left', buttons: down ? 1 : 0, clickCount: 1,
      });
      try {
        await mouse('mousePressed', position.x, true);
        await mouse('mouseMoved', position.x - 18, true);
        await until(() => Boolean(nativeFile), 'SFTP file did not reach native drag API');
        assert.equal(path.basename(nativeFile), 'welcome.txt');
        assert.deepEqual(fs.readFileSync(nativeFile), fs.readFileSync(path.join(fixture.remoteRoot, 'welcome.txt')));
      } finally {
        await mouse('mouseReleased', position.x - 18, false);
        debuggerApi.detach();
        window.webContents.startDrag = native;
      }
    });
    await check('terminal and SFTP panel drops use independent directories', async () => {
      const fixtureDir = path.dirname(fixture.remoteRoot);
      const contract = require('./file-drop-contract.cjs');
      const localFiles = contract.files.map(name => path.join(fixtureDir, name));
      for (const source of localFiles) fs.writeFileSync(source, path.basename(source));
      for (const name of contract.directories) fs.mkdirSync(path.join(fixture.remoteRoot, name));
      await run(`__astraTest.loadFiles('/drop-a')`);
      await run(`(async()=>{__astraTest.focusPane(${JSON.stringify(second)});await __astraTest.syncFiles();await __astraTest.loadFiles('/drop-b')})()`);
      const shellDirectory = contract.directories[2];
      const pane = `__astraTest.state.panes.get(${JSON.stringify(first)})`;
      const generation = terminals.get(first).generation;
      const inputs = fixture.counters.received.length;
      async function drop(bounds, filenames) {
        const data = { items: [], files: filenames, dragOperationsMask: 1 };
        const cancel = bounds === "document.getElementById('cancel-drop')";
        if (cancel) {
          assert.equal(await run(bounds + ".hidden"), true);
          await window.webContents.debugger.sendCommand('Input.dispatchDragEvent', { type: 'dragEnter', x: 300, y: 120, data });
          await until(() => run(bounds + ".getBoundingClientRect().width >= 250"), 'Cancellation target did not appear');
        }
        const target = await run(`(()=>{const r=${bounds}.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
        for (const type of cancel ? ['dragOver', 'drop'] : ['dragEnter', 'dragOver', 'drop']) {
          await window.webContents.debugger.sendCommand('Input.dispatchDragEvent', { type, ...target, data });
        }
        if (cancel) await until(() => run(bounds + ".hidden"), 'Cancellation target remained visible after drop');
      }
      window.webContents.debugger.attach('1.3');
      try {
        await drop("document.getElementById('cancel-drop')", [localFiles[2]]);
        assert.equal(await run('__astraTest.state.files.busy'), 0);
        assert.ok(!fs.existsSync(path.join(fixture.remoteRoot, 'drop-b', path.basename(localFiles[2]))));
        await drop("document.getElementById('files-panel')", [localFiles[2]]);
        await until(() => fs.existsSync(path.join(fixture.remoteRoot, 'drop-b', path.basename(localFiles[2]))), 'SFTP panel drop did not upload');
        await until(() => run('__astraTest.state.files.busy===0'), 'Panel drop still running');
        await run('__astraTest.overview(true)');
        await drop(`${pane}.container`, [localFiles[0]]);
        await until(() => run("document.getElementById('toasts').textContent.includes('Терминал не сообщил текущий каталог')"), 'Unknown shell directory should report an error');
        assert.ok(!fs.existsSync(path.join(fixture.remoteRoot, 'drop-a', path.basename(localFiles[0]))));
        emit('terminal:data', { id: first, generation, data: '\x1b]7;file:///drop-a\x07\x1b]7;file:///' });
        emit('terminal:data', { id: first, generation, data: shellDirectory + '\x07' });
        await until(() => run(`${pane}.directoryReport?.value===${JSON.stringify('file:///' + shellDirectory)}`), 'Shell directory passed through the xterm OSC parser');
        await drop(`__astraTest.state.panes.get(${JSON.stringify(first)}).container`, localFiles.slice(0, 2));
        await until(() => localFiles.slice(0, 2).every(source => fs.existsSync(path.join(fixture.remoteRoot, shellDirectory, path.basename(source)))), 'Drop onto inactive terminal did not upload both files in its shell directory');
        assert.ok(localFiles.slice(0, 2).every(source => !fs.existsSync(path.join(fixture.remoteRoot, 'drop-a', path.basename(source)))));
        assert.ok(localFiles.slice(0, 2).every(source => !fs.existsSync(path.join(fixture.remoteRoot, 'drop-b', path.basename(source)))));
        assert.equal(fixture.counters.received.length, inputs, 'File drop must not reach SSH shell input');
        assert.equal(files._directories.get(first), '/drop-a');
        const synthetic = await run(`astra.uploadDropped({mode:'sftp',terminalId:${JSON.stringify(first)}},[new File(['test'],'synthetic.txt')]).then(()=>false,()=>true)`);
        assert.equal(synthetic, true, 'Renderer-created File must not authorize local disk access');
        await until(() => run('__astraTest.state.files.busy===0'), 'Terminal drop still running');
        const { dialog } = require('electron');
        const originalConfirm = dialog.showMessageBox;
        let questions = 0;
        dialog.showMessageBox = async () => { questions++; return { response: 0 }; };
        try {
          fs.writeFileSync(localFiles[0], 'replacement that must be cancelled');
          await drop(`__astraTest.state.panes.get(${JSON.stringify(first)}).container`, [localFiles[0]]);
          await until(() => questions === 1, 'Overwrite confirmation missing');
          await until(() => run('__astraTest.state.files.busy===0'), 'Cancelled drop still running');
          assert.equal(fs.readFileSync(path.join(fixture.remoteRoot, shellDirectory, path.basename(localFiles[0])), 'utf8'), path.basename(localFiles[0]));
        } finally { dialog.showMessageBox = originalConfirm; }
      } finally {
        window.webContents.debugger.detach();
        await run('__astraTest.overview(false)');
        await run(`(async()=>{__astraTest.focusPane(${JSON.stringify(first)});await __astraTest.syncFiles();await __astraTest.loadFiles('/')})()`);
      }
    });
    await check('SSH host status follows active terminal without authentication or shell input',async()=>{
      const inputCount=fixture.counters.received.length;
      await until(()=>run('document.querySelector(".host-hostname")?.textContent==="fixture-host"'),'SSH statistics did not follow active host');
      assert.equal(fixture.counters.authenticated,2);assert.equal(fixture.counters.received.length,inputCount);
      assert.equal(await run('document.querySelector(".host-memory").textContent'),'6 МиБ / 8 МиБ');
      fs.writeFileSync(path.join(output,'windows-host-stats.png'),(await window.webContents.capturePage()).toPNG());
    });
    await require('./smoke-session-actions.cjs')({run,until,delay,check,terminals,files,fixture,first,second});
    await run(`__astraTest.focusPane(${JSON.stringify(first)});__astraTest.syncFiles()`);
    await until(()=>run(`__astraTest.state.files.terminalId===${JSON.stringify(first)}&&__astraTest.state.files.entries.some(e=>e.name==='welcome.txt')`),'SFTP after saved-settings reconnect');
    await check('F2 renames a file and directory on the active SFTP host', async () => {
      const directory = keyboard.rename.directory;
      const file = keyboard.rename.file;
      fs.writeFileSync(path.join(fixture.remoteRoot, file[0]), 'F2 test');
      await run("void __astraTest.fileAction('mkdir')");
      await until(() => run("Boolean(document.querySelector('dialog[open] input'))"), 'Create directory dialog missing');
      await run(`(()=>{const d=document.querySelector('dialog[open]');d.querySelector('input').value=${JSON.stringify(directory[0])};d.querySelector('.primary').click()})()`);
      for (const names of [directory, file]) {
        const listed = name => run(`__astraTest.state.files.entries.some(item=>item.name===${JSON.stringify(name)}) && !__astraTest.state.files.busy`);
        await until(() => listed(names[0]), 'SFTP rename source not listed');
        await renameWithF2('.file-row[data-path]', '/' + names[0], names);
        await until(() => listed(names[1]), 'Renamed SFTP item not listed');
        assert.equal(fs.existsSync(path.join(fixture.remoteRoot, names[0])), false);
        assert.ok(fs.existsSync(path.join(fixture.remoteRoot, names[1])));
      }
    });
    await check('SFTP context menu applies octal permissions to the selected file', async () => {
      const { Menu } = require('electron');
      const popup = Menu.prototype.popup;
      const filename = path.join(fixture.remoteRoot, 'welcome.txt');
      let menu;
      Menu.prototype.popup = function () { menu = this; };
      try {
        await run(`document.querySelector('[data-path="/welcome.txt"]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,button:2}))`);
        await until(() => Boolean(menu), 'SFTP context menu missing');
        menu.items.find(item => item.label === 'Права…').click();
        await until(() => run(`Boolean(document.querySelector('dialog[open] input[aria-label="Восьмеричный режим"]'))`), 'Permissions dialog missing');
        await run(`(()=>{const field=document.querySelector('dialog[open] input[aria-label="Восьмеричный режим"]');field.value="444";field.dispatchEvent(new Event("input"));document.querySelector('dialog[open] button.primary').click()})()`);
        await until(() => (fs.statSync(filename).mode & 0o777) === 0o444, 'SFTP chmod did not change the selected file');
        await until(() => run(`__astraTest.state.files.entries.some(entry=>entry.path==="/welcome.txt"&&entry.permissions==="r--r--r--")`), 'Permissions column did not refresh');
      } finally {
        Menu.prototype.popup = popup;
        fs.chmodSync(filename, 0o666);
        await run('__astraTest.loadFiles("/")');
      }
    });
    await check('editor save preserves text typed during an in-flight SFTP write', async () => {
      await until(() => !files.busy, 'Previous file operation still running');
      await run("__astraTest.editFile({name:'welcome.txt',path:'/welcome.txt'})");
      fixture.counters.writeDelayMs = 350;
      await run("(()=>{const d=document.querySelector('dialog[open]');const t=d.querySelector('textarea');t.value='saved revision';t.dispatchEvent(new Event('input'));d.querySelector('.primary').click()})()");
      await until(() => files.busy, 'Editor did not start saving');
      await run("(()=>{const d=document.querySelector('dialog[open]');const t=d.querySelector('textarea');t.value='newer unsaved text';t.dispatchEvent(new Event('input'));d.close()})()");
      assert.equal(await run("Boolean(document.querySelector('dialog[open].editor-dialog'))"), true, 'Editor must remain open during save');
      await until(() => !files.busy, 'Editor save did not finish');
      await delay(100);
      assert.equal(fs.readFileSync(path.join(fixture.remoteRoot, 'welcome.txt'), 'utf8'), 'saved revision');
      assert.equal(await run('__astraTest.state.dirty.size'), 1, 'Newer text must remain dirty');
      fixture.counters.writeDelayMs = 0;
      await run("document.querySelector('dialog[open] .primary').click()");
      await until(() => fs.readFileSync(path.join(fixture.remoteRoot, 'welcome.txt'), 'utf8') === 'newer unsaved text', 'Latest editor contents were not saved');
      await until(() => run('__astraTest.state.dirty.size===0'), 'Saved editor remained dirty');
      await run("document.querySelector('dialog[open]').close()");
    });
    await check('group dialog and imported protocol/encoding remain intact', async () => {
      await run("__astraTest.action('new-group')");
      await run("(()=>{const d=document.querySelector('dialog[open]');d.querySelector('[name=name]').value='Windows lab';d.querySelector('.primary').click()})()");
      await until(() => run("!document.querySelector('dialog[open]')&&__astraTest.state.store.groups.some(group=>group.name==='Windows lab')"), 'Group dialog did not close after save');
      await run("document.querySelector('.folder-row[title=\"Windows lab\"]').click()");
      const sidebar = path.join(await run('__astraTest.state.bootstrap.appData'), 'sidebar.json');
      const folderId = await run("__astraTest.state.store.groups.find(group=>group.name==='Windows lab').id");
      await until(() => fs.existsSync(sidebar) && JSON.parse(fs.readFileSync(sidebar)).collapsed.includes(folderId), 'Collapsed folder was not written to disk');
      assert.equal(await run("document.querySelector('.folder-row[title=\"Windows lab\"]').getAttribute('aria-expanded')"), 'false');
      const imported = await run(`astra.invoke('session:save',{record:{...${JSON.stringify(sshProfile)},identity_profile:'',protocol:'mosh',encoding:'CP866',name:'Imported Linux session',group:'Windows lab'}})`);
      await run(`__astraTest.action('edit-session',{id:${JSON.stringify(imported.id)}})`);
      assert.equal(await run("document.querySelector('dialog[open] [name=protocol]').value"), 'mosh');
      assert.equal(await run("document.querySelector('dialog[open] [name=encoding]').value"), 'CP866');
      await run("document.querySelector('dialog[open] .primary').click()");
      await until(() => run("!document.querySelector('dialog[open]')"), 'Imported session editor did not save');
      const preserved = await run(`__astraTest.state.store.sessions.find(record=>record.id===${JSON.stringify(imported.id)})`);
      assert.equal(preserved.protocol, 'mosh'); assert.equal(preserved.encoding, 'CP866');
    });
    await check('multiple tunnel profiles keep separate IDs and captured host', async () => {
      await run("__astraTest.action('tunnels')");
      for (const name of ['First tunnel', 'Second tunnel']) {
        await run("[...document.querySelector('dialog[open] .dialog-footer').querySelectorAll('button')].find(b=>b.textContent==='Новый туннель').click()");
        await run(`(()=>{const d=[...document.querySelectorAll('dialog[open]')].at(-1);d.querySelector('[name=name]').value=${JSON.stringify(name)};d.querySelector('.primary').click()})()`);
        await until(async () => await run(`__astraTest.state.store.tunnels.some(t=>t.name===${JSON.stringify(name)})`), 'Tunnel form did not save');
        await until(() => run("document.querySelectorAll('dialog[open]').length===1"), 'Tunnel editor did not close');
      }
      const tunnels = await run('__astraTest.state.store.tunnels');
      assert.equal(tunnels.length, 2); assert.notEqual(tunnels[0].id, tunnels[1].id);
      assert.ok(tunnels.every(tunnel => tunnel.host === sshProfile.host && tunnel.port === sshProfile.port));
      await run("document.querySelector('dialog[open]').close()");
      const mismatch = await run("__astraTest.action('file-delete',{binding:{mode:'sftp',terminalId:'outdated',path:'/'},entry:{path:'/welcome.txt'}}).then(()=>false,()=>true)");
      assert.equal(mismatch, true, 'A stale context menu must not mutate a different host');
    });
    await check('MultiExec real keyboard lower/uppercase and unchecked source', async () => {
      await run(`(()=>{const t=__astraTest;const write=t.router.write;t.trace=[];t.router.write=(id,data)=>{t.trace.push({id,data});write(id,data)};t.multi(true);t.router.clear();t.router.select(${JSON.stringify(first)},true);t.router.select(${JSON.stringify(second)},true);t.renderLayout()})()`);
      await routed(first, 'x', [first, second]);
      await routed(first, 'A', [first, second], ['shift']);
      await routed(first, 'я', [first, second]);
      await routed(initial, 'z', [initial]);
      await key('Backspace');
    });
    await check('terminal replies and macros remain local; native paste is per recipient', async () => {
      await run(`__astraTest.trace=[];__astraTest.router.input(${JSON.stringify(first)},'local macro',false)`);
      assert.deepEqual((await run('__astraTest.trace')).map(item => item.id), [first]);
      await run(`__astraTest.trace=[];__astraTest.state.panes.get(${JSON.stringify(first)}).term.write('\x1b[6n')`);
      await until(async () => (await run('__astraTest.trace.length')) > 0, 'Terminal query response missing');
      assert.deepEqual((await run('__astraTest.trace')).map(item => item.id), [first]);
      await run(`new Promise(resolve=>__astraTest.state.panes.get(${JSON.stringify(first)}).term.write(String.fromCharCode(27)+'[?2004h',resolve))`);
      await run(`__astraTest.trace=[];__astraTest.pasteFor(${JSON.stringify(first)},'Привет 🌍')`);
      const pasted = await run('__astraTest.trace');
      assert.deepEqual(pasted.map(item => item.id).sort(), [first, second].sort());
      assert.equal(pasted.find(item => item.id === first).data, '\x1b[200~Привет 🌍\x1b[201~');
      assert.equal(pasted.find(item => item.id === second).data, 'Привет 🌍');
      await run(`new Promise(resolve=>__astraTest.state.panes.get(${JSON.stringify(first)}).term.write(String.fromCharCode(27)+'[?2004l',resolve))`);
    });
    await check('form paste is isolated from terminal broadcast', async () => {
      await run(`__astraTest.trace=[];document.getElementById('quick').focus()`);
      await delay(100);
      assert.ok((await run('__astraTest.trace')).every(item => item.id === initial && /^\x1b\[[IO]$/.test(item.data)), 'Focus reports must stay local');
      await run('__astraTest.trace=[]');
      await key('v', null, ['control', 'shift']);
      assert.equal((await run('__astraTest.trace')).length, 0);
    });
    await check('select all, clear all, pause and reconnect selection reset', async () => {
      await run("document.getElementById('select-all').click()");
      await until(async () => await run('__astraTest.router.selected.size===3'), 'Select all failed');
      await run("document.getElementById('clear-all').click()");
      await until(async () => await run('__astraTest.router.selected.size===0'), 'Clear all failed');
      await run(`__astraTest.router.select(${JSON.stringify(first)},true);__astraTest.multi(false)`);
      assert.equal(await run('__astraTest.router.selected.size'), 1);
      const generation=terminals.get(first).generation;
      await run(`astra.invoke('terminal:reconnect',{id:${JSON.stringify(first)}})`);
      await until(async () => await run(`__astraTest.state.panes.get(${JSON.stringify(first)}).generation===${generation+1}`), 'Reconnect generation missing');
      assert.equal(await run(`__astraTest.router.selected.has(${JSON.stringify(first)})`), false);
    });
    await check('split existing hosts preserves terminal, process and scrollback', async () => {
      const before = terminals.get(initial).process;
      await run(`__astraTest.overview(false);__astraTest.focusPane(${JSON.stringify(first)});void __astraTest.split('horizontal')`);
      await until(() => run("Boolean(document.querySelector('dialog[open] select'))"), 'Split chooser missing');
      await run(`(()=>{const d=document.querySelector('dialog[open]');d.querySelector('select').value=${JSON.stringify('pane:' + second)};d.querySelector('.primary').click()})()`);
      await until(async () => await run(`__astraTest.state.groups.some(g=>g.panes.includes(${JSON.stringify(first)})&&g.panes.includes(${JSON.stringify(second)}))`), 'Existing hosts were not joined');
      assert.equal(terminals.get(initial).process, before);
      assert.ok((await snapshot(initial)).includes('ASTRA_WINDOWS_42'));
      assert.equal(terminals.list().length, 3);
    });
    await check('tab wheel scrolls only the list when it overflows', async () => {
      assert.deepEqual(await run(`(()=>{
        const tabs=document.getElementById('tabs');
        const active=__astraTest.state.activeGroup;
        const wheel=(deltaX,deltaY)=>tabs.dispatchEvent(new WheelEvent('wheel',{deltaX,deltaY,cancelable:true}));
        tabs.style.width='180px';tabs.scrollLeft=0;
        wheel(0,120);
        const moved=tabs.scrollLeft>0;
        wheel(-120,0);
        const returned=tabs.scrollLeft===0;
        tabs.style.width='2000px';wheel(0,120);
        const fits=tabs.scrollLeft===0;
        tabs.style.width='';
        return [moved,returned,fits,active===__astraTest.state.activeGroup];
      })()`), [true,true,true,true]);
    });
    await check('notification dismiss button and offline component notices', async () => {
      assert.deepEqual(await run(`(async()=>{
        const toasts=document.getElementById('toasts');
        __astraTest.showError(new Error('Synthetic notification'));
        const notice=toasts.lastElementChild;
        notice.querySelector('[aria-label="Закрыть уведомление"]').click();
        await __astraTest.action('about');
        const dialog=document.querySelector('dialog[open]');
        const present=dialog.querySelector('.component-notices').textContent.includes('LGPL');
        dialog.querySelector('.primary').click();
        return [!notice.isConnected,present];
      })()`), [true,true]);
    });
    await check('two middle clicks select text without terminal input', async () => {
      const selection = require('./tab-contracts.cjs').rangeSelection;
      const inputs = fixture.counters.received.length;
      await run("__astraTest.selectGroup(__astraTest.state.groups.find(group=>group.panes.includes(" + JSON.stringify(first) + ")).id)");
      await delay(350);
      const points = await run(`(async()=>{
        const term=__astraTest.state.panes.get(__astraTest.state.active).term;
        await new Promise(done=>term.write("\\x1b[?1049h\\x1b[3;1H"+${JSON.stringify(selection.text)},done));
        const buffer=term.buffer.active;
        const bounds=term.element.querySelector('.xterm-screen').getBoundingClientRect();
        return ${JSON.stringify(selection.columns)}.map(col=>({
          x:Math.round(bounds.left+(col+.1)*bounds.width/term.cols),
          y:Math.round(bounds.top+(buffer.baseY+buffer.cursorY-buffer.viewportY+.5)*bounds.height/term.rows)
        }));
      })()`);
      for (const point of points) {
        for (const type of ['mouseDown','mouseUp'])
          window.webContents.sendInputEvent({type,button:'middle',clickCount:1,...point});
        await delay(75);
      }
      assert.equal(await run('__astraTest.state.panes.get(__astraTest.state.active).term.getSelection()'), selection.expected);
      assert.equal(fixture.counters.received.length, inputs);
      await run("new Promise(done=>__astraTest.state.panes.get(__astraTest.state.active).term.write('\\x1b[?1049l',done))");
    });
    await require('./smoke-tabs.cjs')({ window, terminals, run, until, delay, check });
    await run(`__astraTest.overview(true);__astraTest.multi(true);__astraTest.router.select(${JSON.stringify(first)},true);__astraTest.router.select(${JSON.stringify(second)},true);__astraTest.focusPane(${JSON.stringify(first)});__astraTest.renderLayout()`);
    await delay(400);
    fs.writeFileSync(path.join(output, 'windows-preview.png'), (await window.webContents.capturePage()).toPNG());
    await check('PowerShell natural exit and transport cleanup', async () => {
      await run(`__astraTest.focusPane(${JSON.stringify(initial)})`);
      await delay(150);
      terminals.write(initial, 'Write-Output ASTRA_PENDING_MUST_CANCEL');
      await delay(150);
      await key('c', null, ['control']);
      await delay(250);
      terminals.write(initial, 'exit\r');
      await until(() => terminals.get(initial)?.status === 'exited', 'PowerShell did not exit');
      assert.equal(terminals.write(initial, 'ignored'), false);
      for (const record of terminals.list()) terminals.close(record.id);
    });
    await require('./smoke-tab-management.cjs')({ window, terminals, files, run, until, delay, check });
    await require('./smoke-imports.cjs')({ window, terminals, files, run, until, delay, check, output });
    await require('./smoke-backup.cjs')({ window, store, vault, identities, run, until, check });
    files.dispose();
    await fixture.close(); fixture = null;
    report.result = 'PASS';
  } catch (error) {
    report.result = 'FAIL'; report.stage = stage; report.error = error.message;
    process.stderr.write('SMOKE_FAILED ' + stage + ': ' + error.message + '\n');
    try { fs.writeFileSync(path.join(output, 'windows-failure.png'), (await window.webContents.capturePage()).toPNG()); } catch {}
  } finally {
    if (fixture) await fixture.close().catch(() => {});
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    process.stdout.write('ASTRATERM_WINDOWS_SMOKE_' + report.result + '\n');
    await finish(report.result === 'PASS' ? 0 : 1);
  }
};
