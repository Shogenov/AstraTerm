'use strict';

const { SFTP_CHMOD_SCRIPT } = require('../shared.cjs');

function parseMode(text) {
  if (typeof text !== 'string' || ![3, 4].includes(text.length) || /[^0-7]/.test(text)) {
    throw new Error('Введите три или четыре восьмеричные цифры, например 644.');
  }
  return Number.parseInt(text, 8);
}

function permissionRevision(stats) {
  if (!stats?.isFile() && !stats?.isDirectory()) throw new Error('Права доступны для обычных файлов и папок.');
  return [stats.mode, stats.uid, stats.gid, stats.size, stats.mtime];
}

const quote = value => "'" + String(value).replaceAll("'", "'\"'\"'") + "'";

function execute(client, command, password) {
  return new Promise((resolve, reject) => {
    let stream;
    let finished = false;
    let output = '';
    const timer = setTimeout(() => finish(new Error('Превышено время ожидания sudo.')), 20000);
    function finish(error, status) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (error) {
        stream?.close();
        reject(error);
      } else resolve({ status, output });
    }
    client.exec(command, (error, channel) => {
      if (finished) { channel?.close(); return; }
      if (error) { finish(new Error('Сервер не разрешил открыть канал sudo.')); return; }
      stream = channel;
      const collect = data => {
        output += data.toString('utf8');
        if (output.length > 16384) finish(new Error('Сервер вернул слишком большой ответ sudo.'));
      };
      channel.on('data', collect);
      channel.stderr.on('data', collect);
      channel.once('error', () => finish(new Error('Соединение sudo прервано.')));
      channel.once('close', status => finish(null, status));
      channel.end(password === null ? undefined : password + '\n');
    });
  });
}

async function sudoChmod(client, filename, mode, revision, askPassword, check) {
  if (!revision.every(Number.isSafeInteger)) throw new Error('Сервер не сообщил свойства объекта для проверки sudo.');
  const tail = ['/usr/bin/python3', '-IS', '-c', SFTP_CHMOD_SCRIPT, filename, mode.toString(8).padStart(4, '0'), ...revision].map(quote).join(' ');
  check();
  let result = await execute(client, 'LC_ALL=C sudo -n -- ' + tail, null);
  if (result.status !== 0 && result.output.includes('sudo: a password is required')) {
    const password = await askPassword();
    if (password === null) return false;
    if (typeof password !== 'string' || password.length > 4096 || /[\x00\r\n]/.test(password)) {
      throw new Error('Недопустимый пароль sudo.');
    }
    check();
    result = await execute(client, "LC_ALL=C sudo -S -p '' -- " + tail, password);
  }
  if (result.status === 55) throw new Error('Объект изменился. Откройте права заново.');
  if (result.status !== 0) throw new Error('Не удалось применить sudo. Проверьте пароль, разрешение на /usr/bin/python3 и доступ к пути.');
  return true;
}
module.exports = { parseMode, permissionRevision, sudoChmod };
