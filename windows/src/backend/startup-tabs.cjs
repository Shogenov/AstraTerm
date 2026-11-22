'use strict';
const fs = require('node:fs');

const quickFields = ['protocol', 'name', 'host', 'port', 'username', 'startup_directory',
  'encoding', 'serial_device', 'baud', 'rdp_domain', 'rdp_resolution'];

function reference(profile, saved) {
  if (saved.has(profile.id)) return { session: profile.id };
  return { profile: Object.fromEntries(quickFields.filter(key => Object.hasOwn(profile, key)).map(key => [key, profile[key]])) };
}

function readWorkspace(filename) {
  try {
    const document = JSON.parse(fs.readFileSync(filename, 'utf8'));
    if (document?.remember !== true) return { remember: false, tabs: [], active: 0 };
    const tabs = (Array.isArray(document.tabs) ? document.tabs : []).map(tab => ({
      orientation: tab?.orientation === 'vertical' ? 'vertical' : 'horizontal',
      active: Number.isSafeInteger(tab?.active) ? tab.active : 0,
      panes: (Array.isArray(tab?.panes) ? tab.panes : []).map(pane => {
        if (typeof pane?.session === 'string') return { session: pane.session };
        if (pane?.profile && typeof pane.profile.protocol === 'string') return reference(pane.profile, new Set());
        return null;
      })
    }));
    return { remember: true, tabs, active: Number.isSafeInteger(document.active) ? document.active : 0 };
  } catch {
    return { remember: false, tabs: [], active: 0 };
  }
}

function captureWorkspace(layout, getProfile, saved) {
  if (!Array.isArray(layout?.tabs)) throw Error('Не удалось определить открытые вкладки.');
  const tabs = [];
  let active = 0;
  for (const [index, tab] of layout.tabs.entries()) {
    if (!Array.isArray(tab?.panes)) continue;
    const panes = tab.panes.map(id => {
      const profile = typeof id === 'string' ? getProfile(id) : null;
      return profile ? reference(profile, saved) : null;
    });
    if (!panes.some(Boolean)) continue;
    if (layout.active === index) active = tabs.length;
    tabs.push({ panes, active: Number.isSafeInteger(tab.active) ? tab.active : 0,
      orientation: tab.orientation === 'vertical' ? 'vertical' : 'horizontal' });
  }
  return { remember: true, tabs, active };
}

module.exports = { readWorkspace, captureWorkspace };
