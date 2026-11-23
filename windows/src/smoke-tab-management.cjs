'use strict';
const assert = require('node:assert/strict');
const { BrowserWindow, Menu } = require('electron');
const { separate: contract, floating } = require('./tab-contracts.cjs');

module.exports = async function checkTabManagement({ window, terminals, files, run, until, delay, check }) {
  const groupFor = id => run(`__astraTest.state.groups.find(g=>g.panes.includes(${JSON.stringify(id)})).id`);
  const drag = (source, target) => run(`(()=>{
    const source=${source}, target=${target}, transfer=new DataTransfer();
    source.dispatchEvent(new DragEvent('dragstart',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    target.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    target.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    source.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:transfer}));
  })()`);
  let ids, target;
  await check('reverse pane/tab drag preserves live PTYs, history and MultiExec', async () => {
    await run('__astraTest.overview(false);for(const id of [...__astraTest.state.panes.keys()])__astraTest.removePane(id)');
    ids = {};
    for (const name of [...contract.panes, 'survivor']) {
      ids[name] = await run(`__astraTest.openSession({protocol:'local',name:${JSON.stringify(name)}}).then(r=>r.id)`);
    }
    await until(() => Object.values(ids).every(id => terminals.get(id)?.status === 'running'), 'Management PTYs ready');
    const original = Object.fromEntries(Object.values(ids).map(id => [id, terminals.get(id).process]));
    const generations = await run('Object.fromEntries([...__astraTest.state.panes].map(([id,p])=>[id,p.generation]))');
    for (const [name,id] of Object.entries(ids)) terminals.write(id, `[Console]::WriteLine('DETACH_KEEP_' + '${name}')\r`);
    const history = `Object.entries(${JSON.stringify(ids)}).every(([name,id])=>{const b=__astraTest.state.panes.get(id).term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join(String.fromCharCode(10)).includes('DETACH_KEEP_'+name)})`;
    await until(() => run(history), 'Initial management output');
    target = await groupFor(ids.a);
    for (const name of ['b','c']) await run(`__astraTest.mergeTabs(${JSON.stringify(await groupFor(ids[name]))},${JSON.stringify(target)})`);
    await run(`__astraTest.router.select(${JSON.stringify(ids.b)},true);__astraTest.focusPane(${JSON.stringify(ids.b)});__astraTest.renderLayout()`);
    // Keep JS object references inside the renderer, which IPC cannot serialize.
    await run('window.__managementTerms=new Map([...__astraTest.state.panes].map(([id,p])=>[id,p.term]));void 0');
    await drag(`__astraTest.state.panes.get(${JSON.stringify(ids[contract.detached])}).heading`, 'document.querySelector(".tab-new")');
    assert.deepEqual(await run(`__astraTest.state.groups.find(g=>g.id===${JSON.stringify(target)}).panes`), contract.remaining.map(name=>ids[name]));
    assert.equal(await run('__astraTest.state.groups.length'),3);
    assert.equal(await run('__astraTest.state.active'),ids.b);
    await drag(`document.querySelector('[data-group-id="'+${JSON.stringify(target)}+'"]')`, 'document.querySelector(".tab-new")');
    assert.equal(await run('__astraTest.state.groups.length'),4);
    assert.equal(await run('__astraTest.state.groups.every(g=>g.panes.length===1)'),true);
    assert.equal(await run(history),true);
    assert.equal(await run('[...__astraTest.state.panes].every(([id,p])=>p.term===window.__managementTerms.get(id))'),true);
    assert.deepEqual(await run('Object.fromEntries([...__astraTest.state.panes].map(([id,p])=>[id,p.generation]))'),generations);
    assert.deepEqual(await run('[...__astraTest.router.selected]'),[ids.b]);
    for (const id of Object.values(ids)) assert.equal(terminals.get(id).process,original[id]);
    await run('delete window.__managementTerms');
  });
  await check('floating tab preserves xterm, PTY, history and MultiExec selection and returns on native close',async()=>{
    const group=await groupFor(ids.b);const record=terminals.get(ids.b),process=record.process,generation=record.generation;
    await run(`window.__floatingTerm=__astraTest.state.panes.get(${JSON.stringify(ids.b)}).term;void 0`);
    assert.equal(await run(`__astraTest.floatWindow(${JSON.stringify(group)})`),true);
    assert.equal(await run(`__astraTest.state.panes.get(${JSON.stringify(ids.b)}).element.ownerDocument===__astraTest.state.groups.find(g=>g.id===${JSON.stringify(group)}).floating.window.document`),true);
    terminals.write(ids.b,'Write-Output ASTRA_FLOATING_PTY\r');
    await until(()=>run(`__astraTest.state.panes.get(${JSON.stringify(ids.b)}).term.buffer.active.getLine(__astraTest.state.panes.get(${JSON.stringify(ids.b)}).term.buffer.active.baseY)?.translateToString().includes('ASTRA_FLOATING') || Array.from({length:__floatingTerm.buffer.active.length},(_,i)=>__floatingTerm.buffer.active.getLine(i).translateToString()).join('').includes('ASTRA_FLOATING_PTY')`),'Live floating output');
    const {BrowserWindow}=require('electron');const child=BrowserWindow.getAllWindows().find(item=>item!==window);
    assert(child);assert.equal(child.webContents.getLastWebPreferences().sandbox,true);
    await run(`__astraTest.focusPane(${JSON.stringify(ids.survivor)})`);
    await run(`__astraTest.router.select(${JSON.stringify(ids.a)},true);__astraTest.router.enabled=true`);
    child.focus();await run(`__astraTest.focusPane(${JSON.stringify(ids.b)})`);await delay(300);
    for(const character of "Write-Output ('ASTRA_FLOAT_'+'KEYBOARD_OK')")child.webContents.sendInputEvent({type:'char',keyCode:character});
    child.webContents.sendInputEvent({type:'keyDown',keyCode:'Return'});child.webContents.sendInputEvent({type:'keyUp',keyCode:'Return'});
    await until(()=>run("Array.from({length:__floatingTerm.buffer.active.length},(_,i)=>__floatingTerm.buffer.active.getLine(i).translateToString()).join('').includes('ASTRA_FLOAT_KEYBOARD_OK')"),'Native keyboard input reaches the floating PTY');
    assert.equal(await run('__astraTest.state.active'),ids.survivor);
    assert.equal(await run(`(()=>{const term=__astraTest.state.panes.get(${JSON.stringify(ids.a)}).term;return Array.from({length:term.buffer.active.length},(_,i)=>term.buffer.active.getLine(i).translateToString()).join('').includes('ASTRA_FLOAT_KEYBOARD_OK')})()`),true);
    await run(`__astraTest.router.enabled=false;__astraTest.router.select(${JSON.stringify(ids.a)},false)`);
    child.close();await until(()=>run(`!__astraTest.state.groups.find(g=>g.id===${JSON.stringify(group)}).floating`),'Native window close docks the tab');
    await until(()=>child.isDestroyed(),'Returned presentation window destroyed');
    assert.equal(terminals.get(ids.b).process,process);assert.equal(terminals.get(ids.b).generation,generation);
    assert.equal(await run(`__astraTest.state.panes.get(${JSON.stringify(ids.b)}).term===__floatingTerm`),true);
    assert.equal(await run(`__astraTest.router.selected.has(${JSON.stringify(ids.b)})`),true);
    assert.equal(await run(`__astraTest.state.panes.get(${JSON.stringify(ids.b)}).element.ownerDocument===document`),true);
    await run('delete window.__floatingTerm');
  });
  await check('native tab context menu binds clicked tab and confirms bulk close once', async () => {
    let captured;
    const originalPopup = Menu.prototype.popup;
    Menu.prototype.popup = function () { captured = this; };
    const menu = async (id, expected) => {
      captured = null;
      await run(`document.querySelector('[data-group-id="'+${JSON.stringify(id)}+'"] a, [data-group-id="'+${JSON.stringify(id)}+'"]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,button:2}))`);
      await until(()=>Boolean(captured), 'Native context menu IPC');
      const items=captured.items.filter(item=>item.type!=='separator');
      assert.deepEqual(items.map(item=>item.label),expected);
      return items;
    };
    const answer = async accept => {
      await until(()=>run('Boolean(document.querySelector("dialog[open]"))'),'Close confirmation');
      await run(accept?'document.querySelector("dialog[open] .primary").click()':'document.querySelector("dialog[open]").close()');
      await until(()=>run('!document.querySelector("dialog[open]")'),'Confirmation dismissed');
      await delay(150);
    };
    const regroup = async () => {
      for (const name of ['b','c']) await run(`__astraTest.mergeTabs(${JSON.stringify(await groupFor(ids[name]))},${JSON.stringify(target)})`);
      await run(`__astraTest.focusPane(${JSON.stringify(ids.survivor)})`);
    };
    try {
      await run(`__astraTest.focusPane(${JSON.stringify(ids.survivor)})`);
      await menu(target,contract.menu_plain);
      await regroup();
      const beforeIds=terminals.list().map(record=>record.id),generations=terminals.list().map(record=>[record.id,record.generation]);
      const duplicateItems=await menu(target,contract.menu_merged);
      duplicateItems.find(item=>item.label==='Дублировать вкладку').click();
      await until(()=>terminals.list().length===beforeIds.length+3&&terminals.list().every(r=>r.status==='running'),'Merged tab duplicate');
      const copies=terminals.list().filter(r=>!beforeIds.includes(r.id));
      assert.equal(await run(`__astraTest.state.groups.find(g=>g.panes.includes(${JSON.stringify(copies[0].id)})).panes.length`),3);
      assert.ok(copies.every(r=>!beforeIds.includes(r.id)));
      for(const [id,generation]of generations)assert.equal(terminals.get(id).generation,generation);
      assert.equal(await run(`${JSON.stringify(copies.map(r=>r.id))}.some(id=>__astraTest.router.selected.has(id))`),false);
      for(const copy of copies) {
        await until(()=>!files.busy, 'File listing before closing duplicated tab');
        await run(`__astraTest.closePane(${JSON.stringify(copy.id)},false)`);
      }
      await run(`__astraTest.focusPane(${JSON.stringify(ids.survivor)})`);
      (await menu(target,contract.menu_merged)).find(item=>item.label==='Разъединить все').click();
      await until(()=>run('__astraTest.state.groups.length===4'),'Separate all menu action');
      assert.equal(await run('__astraTest.state.active'),ids.survivor);
      await regroup();
      (await menu(target,contract.menu_merged))[4].click(); await answer(false);
      assert.equal(await run('__astraTest.state.groups.length'),2);
      (await menu(target,contract.menu_merged))[3].click(); await answer(false);
      assert.equal(await run('__astraTest.state.groups.length'),2);
      const stale = await groupFor(ids.survivor);
      (await menu(target,contract.menu_merged))[4].click(); await answer(true);
      await until(()=>run('__astraTest.state.groups.length===1'),'Close others preserves clicked tab');
      assert.ok(!terminals.get(ids.survivor) && terminals.get(ids.a));
      assert.equal(await run(`__astraTest.tabAction('close-all',${JSON.stringify(stale)})`),false);
      (await menu(target,contract.menu_merged))[2].click(); await answer(true);
      await until(()=>run('__astraTest.state.groups.length===0'),'Close current merged tab');
      assert.equal(terminals.list().length,0);
      for (let i=0;i<2;i++) await run("__astraTest.openSession({protocol:'local',name:'Bulk '+"+i+"})");
      await until(()=>terminals.list().every(record=>record.status==='running'),'Final local terminals ready');
      const last=await run('__astraTest.state.groups[0].id');
      (await menu(last,contract.menu_plain))[3].click(); await answer(true);
      await until(()=>run('__astraTest.state.groups.length===0'),'Close all tabs');
      assert.equal(terminals.list().length,0);
    } finally { Menu.prototype.popup=originalPopup; }
  });
  await check('four equal terminal panes float, resize and return with live PTYs', async () => {
    const scenario = floating.merged;
    const panes = [];
    for (const name of scenario.panes) {
      const id = await run(`__astraTest.openSession({protocol:'local',name:${JSON.stringify(name)}}).then(record=>record.id)`);
      panes.push(id);
    }
    await until(() => panes.every(id => terminals.get(id)?.status === 'running'), 'Four local PTYs ready');
    const original = panes.map(id => [terminals.get(id).process, terminals.get(id).generation]);
    const tab = id => `document.querySelector('[data-group-id="${id}"]')`;
    const merged = await groupFor(panes[0]);
    const ids = JSON.stringify(panes);
    await run(`window.__mergedTerms=${ids}.map(id=>__astraTest.state.panes.get(id).term);void 0`);
    for (const pane of panes.slice(1)) await drag(tab(await groupFor(pane)), tab(merged));
    const group = `__astraTest.state.groups.find(item=>item.id===${JSON.stringify(merged)})`;
    assert.deepEqual(await run(`${group}.panes`), panes);
    await run(`__astraTest.router.select(${JSON.stringify(panes[1])},true)`);
    terminals.write(panes[1], "[Console]::WriteLine('MERGED_' + 'KEEP')\r");
    const history = "Array.from({length:__mergedTerms[1].buffer.active.length},(_,i)=>__mergedTerms[1].buffer.active.getLine(i).translateToString()).join('').includes('MERGED_KEEP')";
    await until(() => run(history), 'Merged terminal output');

    const assertLayout = async detached => {
      const layout = await run(`(()=>{
        const container = ${detached ? `${group}.floating.container` : 'document.querySelector("#workspace .panel-group")'};
        const bounds=container.getBoundingClientRect();
        return {bounds:bounds.toJSON(),panes:${ids}.map(id=>{
          const pane=__astraTest.state.panes.get(id);
          return {bounds:pane.element.getBoundingClientRect().toJSON(),
            attached:pane.element.parentNode===container&&pane.term.element.isConnected&&
              pane.term.element.ownerDocument===container.ownerDocument, columns:pane.term.cols};
        })};
      })()`);
      const widths = layout.panes.map(pane => pane.bounds.width);
      assert(Math.max(...widths) - Math.min(...widths) <= scenario.width_tolerance);
      let right = layout.bounds.left;
      for (const pane of layout.panes) {
        assert(pane.attached && pane.columns > 10 && pane.bounds.width > 0);
        assert(pane.bounds.left >= right - 1 && pane.bounds.left <= right + 4);
        assert.equal(pane.bounds.top, layout.bounds.top);
        assert.equal(pane.bounds.bottom, layout.bounds.bottom);
        right = pane.bounds.right;
      }
      assert.equal(right, layout.bounds.right);
    };

    await assertLayout(false);
    await run(`${tab(merged)}.querySelector('button[title="В отдельное окно"]').click()`);
    await until(() => run(`Boolean(${group}.floating?.container)`), 'Merged tab opens in separate window');
    const child = BrowserWindow.getAllWindows().find(item => item !== window && !item.isDestroyed());
    assert(child);
    try {
      for (const size of scenario.sizes) {
        child.setContentSize(...size);
        await delay(200);
        await assertLayout(true);
      }
      assert.equal(await run(`${group}.floating.window.document.querySelector(".floating-title").textContent`), 'd +3');
      assert.equal(await run(history), true);
    } finally {
      if (!child.isDestroyed()) child.close();
      await until(() => child.isDestroyed(), 'Merged tab returned to main window');
    }
    await assertLayout(false);
    assert.equal(await run(`${ids}.every((id,index)=>__astraTest.state.panes.get(id).term===__mergedTerms[index])`), true);
    assert.equal(await run(history), true);
    assert.equal(await run(`__astraTest.router.selected.has(${JSON.stringify(panes[1])})`), true);
    for (const [index, id] of panes.entries()) {
      assert.equal(terminals.get(id).process, original[index][0]);
      assert.equal(terminals.get(id).generation, original[index][1]);
    }
    await run(`delete window.__mergedTerms;for(const id of ${ids})__astraTest.removePane(id)`);
  });
};
