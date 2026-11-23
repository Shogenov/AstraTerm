'use strict';
const fs = require('node:fs');

const COLORS = {
  dark: { accent: '#82d9bd', terminal_background: '#101316', terminal_foreground: '#d9e1e8', terminal_cursor: '#82d9bd' },
  light: { accent: '#246752', terminal_background: '#ffffff', terminal_foreground: '#21303a', terminal_cursor: '#246752' },
};
const PANELS = ['sidebar', 'files'];

function normalizeAppearance(value) {
  const colors = {};
  for (const [theme, defaults] of Object.entries(COLORS)) {
    const selected = Object.fromEntries(Object.keys(defaults).flatMap(key => {
      const color = value?.colors?.[theme]?.[key];
      return typeof color === 'string' && /^#[0-9a-f]{6}$/i.test(color) && color.length === 7
        ? [[key, color.toLowerCase()]] : [];
    }));
    if (Object.keys(selected).length) colors[theme] = selected;
  }
  return {
    theme: value?.theme === 'light' ? 'light' : 'dark',
    terminal_clipboard: value?.terminal_clipboard === true,
    colors,
    panels: Object.fromEntries(PANELS.filter(key => typeof value?.panels?.[key] === 'boolean')
      .map(key => [key, value.panels[key]])),
  };
}

function readAppearance(filename) {
  try { return normalizeAppearance(JSON.parse(fs.readFileSync(filename, 'utf8').replace(/^\uFEFF/, ''))); }
  catch { return normalizeAppearance(); }
}

module.exports = { COLORS, PANELS, normalizeAppearance, readAppearance };
