'use strict';

const UNKNOWN_DIRECTORY = 'Терминал не сообщил текущий каталог.';
const REMOTE_HOSTNAME = '/proc/sys/kernel/hostname';
const HOST = String.raw`(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}|\[[0-9A-Fa-f:.]+\])`;
const TITLE = new RegExp(String.raw`^(?:\([^()]+\))?([A-Za-z0-9_.-]+)@(${HOST}):\s?(.*)$`);
const OSC7 = new RegExp(String.raw`^file://(${HOST})?(/[^?#]*)$`);
const CONTROL = /[\x00-\x1f\x7f]/;

function directoryReport(kind, value) {
  if (typeof value !== 'string' || !value || value.length > 32767 || CONTROL.test(value)) return null;
  let host, user, path;
  if (kind === 'osc7') {
    const match = OSC7.exec(value);
    if (!match) return null;
    host = match[1] || '';
    if (host.toLowerCase() === 'localhost') host = '';
    user = '';
    try { path = decodeURIComponent(match[2]); }
    catch { return null; }
  } else if (kind === 'title') {
    const match = TITLE.exec(value);
    if (!match) return null;
    [, user, host, path] = match;
  } else return null;
  if (CONTROL.test(path) || !(path.startsWith('/') || path.startsWith('~/') || path === '~')) return null;
  return { host, user, path };
}

function matchesHost(reported, expected) {
  const clean = value => value.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
  reported = clean(reported);
  expected = clean(expected);
  return reported === expected || (!reported.includes('.') && expected.startsWith(reported + '.'));
}

async function resolveTerminalDirectory(report, { host, user, readHome, readHostname }) {
  if (report.host && !matchesHost(report.host, host) && !matchesHost(report.host, await readHostname())) {
    throw new Error('Текущий каталог относится к другому SSH-хосту или имя сервера не подтверждено.');
  }
  if (!report.path.startsWith('~')) return report.path;
  if (report.user !== user) throw new Error('Терминал сообщил домашний каталог другого пользователя. Нужен абсолютный путь из оболочки.');
  const home = await readHome();
  if (!home?.startsWith('/')) throw new Error('Текущий каталог терминала недоступен в SFTP.');
  return home.replace(/\/+$/, '') + '/' + report.path.slice(2);
}

module.exports = { directoryReport, resolveTerminalDirectory, REMOTE_HOSTNAME, UNKNOWN_DIRECTORY };
