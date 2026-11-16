'use strict';

const MAX_STAT_OUTPUT = 2 * 1024 * 1024;

function permissions(mode) {
  if (!Number.isInteger(mode)) return null;
  const letters = Array.from('rwxrwxrwx', (letter, index) => mode & (1 << (8 - index)) ? letter : '-');
  for (const [index, bit, letter] of [[2, 0o4000, 's'], [5, 0o2000, 's'], [8, 0o1000, 't']]) {
    if (mode & bit) letters[index] = letters[index] === 'x' ? letter : letter.toUpperCase();
  }
  return letters.join('');
}

function parseBirthTimes(data) {
  const fields = data.toString('utf8').split('\0');
  const times = new Map();
  for (let i = 0; i + 2 < fields.length; i += 2) {
    const time = Number(fields[i]);
    if (/^[0-9]+$/.test(fields[i]) && time > 0 && time <= 253402300799 && fields[i + 1].startsWith('/')) {
      times.set(fields[i + 1], time * 1000);
    }
  }
  return times;
}

function remoteBirthTimes(client, directory) {
  if (typeof client.exec !== 'function' || !directory.startsWith('/') || /[\r\n]/.test(directory)) return Promise.resolve(new Map());
  // SFTP v3 has mtime only. Ask the same SSH endpoint for GNU stat birth times.
  const command = "sh -c 'IFS= read -r directory || exit; find -H \"$directory\" -mindepth 1 -maxdepth 1 -exec stat --printf=\"%W\\0%n\\0\" -- {} + 2>/dev/null'";
  return new Promise(resolve => {
    let stream;
    let done = false;
    let size = 0;
    const blocks = [];
    const complete = data => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(data);
      if (stream) stream.close();
    };
    const timer = setTimeout(() => complete(new Map()), 1500);
    const receive = (error, channel) => {
      if (error) { complete(new Map()); return; }
      if (done) { channel.close(); return; }
      stream = channel;
      stream.stderr?.resume();
      stream.on('data', block => {
        size += block.length;
        if (size > MAX_STAT_OUTPUT) complete(new Map());
        else blocks.push(block);
      });
      stream.once('error', () => complete(new Map()));
      stream.once('close', () => {
        stream = null;
        if (!done) complete(parseBirthTimes(Buffer.concat(blocks)));
      });
      stream.end(directory + '\n');
    };
    try { client.exec(command, receive); } catch { complete(new Map()); }
  });
}

module.exports = { permissions, parseBirthTimes, remoteBirthTimes };
