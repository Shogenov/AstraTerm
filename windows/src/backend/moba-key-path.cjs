'use strict';

const { win32 } = require('node:path');

const MOBA_KEY_PATH_WARNING = 'Путь ключа MobaXterm необходимо выбрать на этом компьютере.';
const isMobaKeyPath = value => /^_(?:MyDocuments|CurrentDrive)_(?:[\\/]|$)/i.test(value);

function resolveMobaDocumentsKey(value, documents) {
  const match = /^_MyDocuments_[\\/](.+)$/i.exec(value);
  if (!match || !documents || !win32.isAbsolute(documents) || win32.parse(documents).root.length < 2) return null;
  const relative = match[1];
  if (win32.isAbsolute(relative) || relative.includes(':')) return null;
  const filename = win32.resolve(documents, relative);
  const inside = win32.relative(documents, filename);
  if (!inside || inside === '..' || inside.startsWith('..\\') || win32.isAbsolute(inside)) return null;
  return filename;
}

module.exports = { isMobaKeyPath, resolveMobaDocumentsKey, MOBA_KEY_PATH_WARNING };
