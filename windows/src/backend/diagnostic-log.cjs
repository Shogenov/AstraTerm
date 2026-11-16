'use strict';

const fs = require('node:fs');
const path = require('node:path');

const LEVELS = ['error', 'warning', 'info', 'debug'];
const MAX_LOG_BYTES = 1024 * 1024;

class DiagnosticLog {
  constructor(directory) {
    this.settingsPath = path.join(directory, 'logging.json');
    this.filename = path.join(directory, 'astraterm.log');
    this.settings = { enabled: false, level: 'info' };
    try {
      const stored = JSON.parse(fs.readFileSync(this.settingsPath, 'utf8'));
      if (typeof stored.enabled === 'boolean' && LEVELS.includes(stored.level)) this.settings = stored;
    } catch { /* Logging defaults to off when there are no valid preferences. */ }
  }

  validate({ enabled, level }) {
    if (typeof enabled !== 'boolean' || !LEVELS.includes(level)) throw Error('Некорректные настройки журнала.');
    return { enabled, level };
  }

  write(level, event) {
    if (!this.settings.enabled || !LEVELS.includes(level) || LEVELS.indexOf(level) > LEVELS.indexOf(this.settings.level) ||
        !/^[a-z][a-z0-9.:-]{0,79}$/.test(event)) return;
    try {
      const size = fs.statSync(this.filename, { throwIfNoEntry: false })?.size || 0;
      if (size >= MAX_LOG_BYTES) fs.renameSync(this.filename, this.filename + '.1');
      const entry = { time: new Date().toISOString(), level, event };
      fs.appendFileSync(this.filename, JSON.stringify(entry) + '\n', { mode: 0o600 });
    } catch { /* A write error in the optional log cannot interrupt a command. */ }
  }
}

module.exports = { DiagnosticLog, LEVELS };
