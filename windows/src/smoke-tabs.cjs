'use strict';
const assert = require('node:assert/strict');
const contracts = require('./tab-contracts.cjs');

module.exports = async function checkTabs({ window, terminals, run, until, delay, check }) {
  await check('leftmost menu and tab drop preserve processes, history and recipients', async () => {
    assert.equal(await run("document.querySelector('.titlebar').firstElementChild.id"), 'menu');
    const results = [];
    for (const scenario of contracts.merges) {
      const ids = {};
      for (const name of [...scenario.target, ...scenario.source]) {
        ids[name] = await run(`__astraTest.openSession({protocol:'local',name:${JSON.stringify('Tab ' + name)}}).then(record=>record.id)`);
      }
      await until(() => [...Object.values(ids)].every(id => terminals.get(id)?.status === 'running'), 'Tab fixture processes not ready');
      const before = Object.fromEntries(Object.values(ids).map(id => [id, terminals.get(id).process]));
      for (const name of Object.keys(ids)) {
        terminals.write(ids[name], `[Console]::WriteLine('TAB_KEEP_' + '${name}')\r`);
      }
      await until(() => run(`Object.entries(${JSON.stringify(ids)}).every(([name,id])=>{const b=__astraTest.state.panes.get(id).term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join(String.fromCharCode(10)).includes('TAB_KEEP_'+name)})`), 'Initial tab output missing');
      const groupFor = id => run(`__astraTest.state.groups.find(group=>group.panes.includes(${JSON.stringify(id)})).id`);
      const target = await groupFor(ids[scenario.target[0]]);
      const source = await groupFor(ids[scenario.source[0]]);
      for (const name of scenario.target.slice(1)) await run(`__astraTest.mergeTabs(${JSON.stringify(await groupFor(ids[name]))},${JSON.stringify(target)})`);
      for (const name of scenario.source.slice(1)) await run(`__astraTest.mergeTabs(${JSON.stringify(await groupFor(ids[name]))},${JSON.stringify(source)})`);
      await run(`__astraTest.router.select(${JSON.stringify(ids[scenario.source[0]])},true)`);
      const selected = await run('[...__astraTest.router.selected].sort()');
      // Browser DragEvents exercise the actual header listeners/DataTransfer.
      // No backend move/reconnect call is used by a tab drop.
      const dropped = await run(`(()=>{
        const source=document.querySelector('[data-group-id="'+${JSON.stringify(source)}+'"]');
        const target=document.querySelector('[data-group-id="'+${JSON.stringify(target)}+'"]');
        const transfer=new DataTransfer();
        source.dispatchEvent(new DragEvent('dragstart',{bubbles:true,cancelable:true,dataTransfer:transfer}));
        __astraTest.renderLayout();
        if(!source.isConnected||!target.isConnected)throw Error('Status rendering replaced an active drag');
        target.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:transfer}));
        const highlighted=target.classList.contains('drop-target');
        target.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
        source.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:transfer}));
        return highlighted;
      })()`);
      assert.equal(dropped, true);
      assert.deepEqual(await run(`__astraTest.state.groups.find(group=>group.id===${JSON.stringify(target)}).panes`), scenario.expected.map(name => ids[name]));
      assert.equal(await run(`__astraTest.state.groups.some(group=>group.id===${JSON.stringify(source)})`), false);
      assert.deepEqual(await run('[...__astraTest.router.selected].sort()'), selected);
      for (const id of Object.values(ids)) assert.equal(terminals.get(id).process, before[id]);
      assert.equal(await run(`__astraTest.state.active===${JSON.stringify(ids[scenario.source.at(-1)])}`), true);
      for (const rejected of contracts.rejected) {
        const pair = rejected === 'same-tab' ? [target,target] : rejected === 'closed-source' ? [source,target] : [target,source];
        assert.equal(await run(`__astraTest.mergeTabs(...${JSON.stringify(pair)})`), false);
      }
      results.push({ target, ids: Object.values(ids) });
    }
    // Keep the fixture tabs for the following real middle-button check.
    window.__tabSmokeGroups = results;
  });
  await check('middle button closes an inactive split tab without confirmation', async () => {
    const records = window.__tabSmokeGroups;
    assert.equal(contracts.middle_close.confirm, false);
    await run(`void __astraTest.tabAction('close-current', ${JSON.stringify(records[0].target)})`);
    await until(() => run('Boolean(document.querySelector("dialog[open]"))'), 'Closing from the tab menu must still ask');
    await run('document.querySelector("dialog[open]").close()');
    await until(() => run('!document.querySelector("dialog[open]")'), 'Cancel ordinary close');
    assert.equal(await run(`__astraTest.state.groups.some(group=>group.id===${JSON.stringify(records[0].target)})`), true);
    for (const { target, ids } of records) {
      const survivor = await run(`__astraTest.state.groups.find(group=>group.id!==${JSON.stringify(target)}).active`);
      await run(`__astraTest.focusPane(${JSON.stringify(survivor)})`);
      await delay(150);
      const point = await run(`(()=>{const tab=document.querySelector('[data-group-id="'+${JSON.stringify(target)}+'"]');tab.scrollIntoView({inline:'nearest'});const r=(${ids.length}>2?tab.querySelector('button'):tab).getBoundingClientRect();return{x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()`);
      window.webContents.sendInputEvent({ type: 'mouseDown', button: 'middle', clickCount: 1, ...point });
      window.webContents.sendInputEvent({ type: 'mouseUp', button: 'middle', clickCount: 1, ...point });
      await until(() => run(`!__astraTest.state.groups.some(group=>group.id===${JSON.stringify(target)})`), 'Middle close requested a warning or did not close');
      assert.equal(await run('Boolean(document.querySelector("dialog[open]"))'), false);
      assert.ok(ids.every(id => !terminals.get(id)));
      assert.equal(await run(`__astraTest.state.active===${JSON.stringify(survivor)}`), true);
    }
    delete window.__tabSmokeGroups;
  });
};
