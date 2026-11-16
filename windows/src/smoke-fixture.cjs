'use strict';

// This synthetic loopback server is used only by the explicit --smoke-test
// runner. It never reads user SSH keys, known_hosts, or Windows ssh-agent.
const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const crypto = require('node:crypto');
const { Server, utils } = require('ssh2');

async function createFixture(directory, options = {}) {
  const base = path.resolve(directory);
  const remoteRoot = path.join(base, 'remote');
  await fsp.mkdir(path.join(remoteRoot, 'logs', 'subdir'), { recursive: true });
  await fsp.writeFile(path.join(remoteRoot, 'welcome.txt'), 'AstraTerm synthetic SFTP fixture\nПривет, Windows!\n');
  await fsp.writeFile(path.join(remoteRoot, 'logs', 'session.log'), 'Synthetic log\n');
  const passphrase = options.passphrase || 'synthetic-' + crypto.randomBytes(12).toString('hex');
  const keyFile = options.keyFile || path.join(base, 'id_smoke_rsa');
  if (!options.keyFile) {
    const pair = crypto.generateKeyPairSync('rsa', { modulusLength: 2048, publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs1', format: 'pem', cipher: 'aes-256-cbc', passphrase } });
    const key = utils.parseKey(pair.privateKey, passphrase);
    if (key instanceof Error) throw new Error('Synthetic SSH key generation failed');
    await fsp.writeFile(keyFile, pair.privateKey, { mode: 0o600 });
    await fsp.writeFile(keyFile + '.pub', key.type + ' ' + key.getPublicSSH().toString('base64') + ' synthetic-astraterm\n');
  }
  const parsedUser = utils.parseKey(await fsp.readFile(keyFile + '.pub'));
  if (parsedUser instanceof Error) throw new Error('Synthetic SSH public key is invalid');
  const hostKey = utils.generateKeyPairSync('ed25519').private;
  const counters = { authenticated: 0, rejected: 0, subsystems: 0, received: [], ptys: [], windows: [], handshakes: [], operations: [] };
  const clients = new Set();
  let nextClient = 0;
  let closed = false;
  const { STATUS_CODE: S, OPEN_MODE: F } = utils.sftp;

  const server = new Server({ hostKeys: [hostKey] }, client => {
    const clientId = ++nextClient;
    client.fixtureId = clientId;
    clients.add(client);
    client.on('error', () => {});
    client.on('close', () => clients.delete(client));
    client.on('handshake', negotiated => counters.handshakes.push({ clientId, ...negotiated }));
    client.on('authentication', context => {
      if (options.password !== undefined) {
        if (context.username === 'smoke-user' && context.method === 'password' && context.password === options.password)context.accept();
        else {counters.rejected++;context.reject(['password']);}
        return;
      }
      if (context.username !== 'smoke-user' || context.method !== 'publickey' || !context.key.data.equals(parsedUser.getPublicSSH()) || (context.signature && parsedUser.verify(context.blob, context.signature, context.hashAlgo) !== true)) {
        counters.rejected++;
        context.reject(['publickey']);
        return;
      }
      context.accept();
    });
    client.on('ready', () => {
      counters.authenticated++;
      client.on('session', accept => {
        const session = accept();
        session.on('pty', (acceptPty, reject, info) => { counters.ptys.push({ clientId, ...info }); acceptPty(); });
        session.on('window-change', (acceptWindow, reject, info) => { counters.windows.push({ clientId, ...info }); acceptWindow?.(); });
        session.on('shell', acceptShell => {
          const shell = acceptShell();
          shell.write(options.banner || `\x1b[32mSynthetic SSH ${clientId} ready\x1b[0m\r\nsmoke@fixture:~$ `);
          shell.on('error', () => {});
          shell.on('data', data => { counters.received.push({ clientId, channel: 'shell', data: data.toString('utf8'), bytes: Buffer.from(data) }); shell.write(data); });
        });
        session.on('sftp', acceptSftp => {
          counters.subsystems++;
          const channel = acceptSftp();
          const handles = new Map();
          let nextHandle = 0;
          let ended = false;
          channel.on('error', () => {});
          const send = fn => { if (!ended && !channel.destroyed) { try { fn(); } catch {} } };
          const reply = (request, error) => send(() => channel.status(request, error ? error.code === 'ENOENT' ? S.NO_SUCH_FILE : error.code === 'EACCES' || error.code === 'EPERM' ? S.PERMISSION_DENIED : S.FAILURE : S.OK));
          const attrs = stat => ({ size: stat.size, mode: stat.mode, uid: stat.uid || 0, gid: stat.gid || 0, atime: Math.floor(stat.atimeMs / 1000), mtime: Math.floor(stat.mtimeMs / 1000) });
          const normalize = remote => {
            if (typeof remote !== 'string' || remote.includes('\0') || remote.includes('\\') || remote.length > 32767) throw new Error('Invalid fixture path');
            return path.posix.resolve('/', remote);
          };
          const local = remote => {
            const filename = path.resolve(remoteRoot, '.' + normalize(remote));
            if (filename !== remoteRoot && !filename.startsWith(remoteRoot + path.sep)) throw new Error('Fixture path escaped');
            let ancestor = filename;
            while (ancestor !== remoteRoot) {
              try { if (fs.lstatSync(ancestor).isSymbolicLink()) throw new Error('Fixture symlinks are unsupported'); }
              catch (error) { if (error.code !== 'ENOENT') throw error; }
              ancestor = path.dirname(ancestor);
            }
            return filename;
          };
          const entry = handle => {
            const item = Buffer.isBuffer(handle) && handles.get(handle.toString('hex'));
            if (!item) throw new Error('Unknown fixture handle');
            return item;
          };
          const makeHandle = value => { const handle = Buffer.alloc(4); handle.writeUInt32BE(++nextHandle); handles.set(handle.toString('hex'), value); return handle; };
          const on = (name, handler) => channel.on(name, (request, ...args) => {
            counters.operations.push({ clientId, name });
            try { handler(request, ...args); } catch (error) { reply(request, error); }
          });
          on('REALPATH', (request, remote) => { const filename = normalize(remote); local(filename); send(() => channel.name(request, [{ filename, longname: filename, attrs: {} }])); });
          for (const method of ['LSTAT', 'STAT']) on(method, (request, remote) => fs.lstat(local(remote), (error, stat) => error ? reply(request, error) : send(() => channel.attrs(request, attrs(stat)))));
          on('FSTAT', (request, handle) => fs.fstat(entry(handle).fd, (error, stat) => error ? reply(request, error) : send(() => channel.attrs(request, attrs(stat)))));
          on('OPENDIR', (request, remote) => {
            const filename = local(remote);
            fs.stat(filename, (error, stat) => {
              if (error || !stat.isDirectory()) return reply(request, error || new Error('Not a directory'));
              send(() => channel.handle(request, makeHandle({ directory: filename, read: false })));
            });
          });
          on('READDIR', (request, handle) => {
            const item = entry(handle);
            if (item.read) { send(() => channel.status(request, S.EOF)); return; }
            item.read = true;
            fsp.readdir(item.directory).then(names => Promise.all(names.map(async filename => ({ filename, longname: filename, attrs: attrs(await fsp.lstat(path.join(item.directory, filename))) })))).then(entries => send(() => entries.length ? channel.name(request, entries) : channel.status(request, S.EOF)), error => reply(request, error));
          });
          on('OPEN', (request, remote, flags, attributes) => {
            let native = flags & F.WRITE ? flags & F.READ ? fs.constants.O_RDWR : fs.constants.O_WRONLY : fs.constants.O_RDONLY;
            for (const [remoteFlag, localFlag] of [[F.CREAT, fs.constants.O_CREAT], [F.TRUNC, fs.constants.O_TRUNC], [F.EXCL, fs.constants.O_EXCL], [F.APPEND, fs.constants.O_APPEND]]) if (flags & remoteFlag) native |= localFlag;
            fs.open(local(remote), native, attributes.mode || 0o600, (error, fd) => {
              if (error) return reply(request, error);
              if (ended) { fs.close(fd, () => {}); return; }
              send(() => channel.handle(request, makeHandle({ fd })));
            });
          });
          on('CLOSE', (request, handle) => {
            const item = entry(handle); handles.delete(handle.toString('hex'));
            if (item.fd !== undefined) fs.close(item.fd, error => reply(request, error)); else reply(request);
          });
          on('READ', (request, handle, offset, length) => {
            if (!Number.isSafeInteger(offset) || offset < 0 || length < 0 || length > 1024 * 1024) throw new Error('Invalid fixture read');
            const bytes = Buffer.alloc(length);
            fs.read(entry(handle).fd, bytes, 0, length, offset, (error, count) => error ? reply(request, error) : send(() => count ? channel.data(request, bytes.subarray(0, count)) : channel.status(request, S.EOF)));
          });
          on('WRITE', (request, handle, offset, bytes) => {
            const descriptor = entry(handle).fd;
            if (!Number.isSafeInteger(offset) || offset < 0 || bytes.length > 1024 * 1024) throw new Error('Invalid fixture write');
            const write = count => fs.write(descriptor, bytes, count, bytes.length - count, offset + count, (error, written) => {
              if (error || !written) return reply(request, error || new Error('Fixture write failed'));
              if (count + written < bytes.length) write(count + written); else reply(request);
            });
            if (!bytes.length) reply(request);
            else if (counters.writeDelayMs) setTimeout(() => { if (!ended) write(0); }, counters.writeDelayMs);
            else write(0);
          });
          on('SETSTAT', (request, remote, attributes) => {
            const filename = local(remote);
            if (attributes.mode !== undefined) fs.chmod(filename, attributes.mode, error => reply(request, error)); else reply(request);
          });
          on('FSETSTAT', (request, handle, attributes) => {
            if (attributes.mode !== undefined) fs.fchmod(entry(handle).fd, attributes.mode, error => reply(request, error)); else reply(request);
          });
          on('RENAME', (request, source, target) => fs.rename(local(source), local(target), error => reply(request, error)));
          on('REMOVE', (request, remote) => fs.unlink(local(remote), error => reply(request, error)));
          on('MKDIR', (request, remote) => fs.mkdir(local(remote), error => reply(request, error)));
          on('RMDIR', (request, remote) => fs.rmdir(local(remote), error => reply(request, error)));
          for (const name of ['EXTENDED', 'READLINK', 'SYMLINK']) on(name, request => send(() => channel.status(request, S.OP_UNSUPPORTED)));
          channel.on('close', () => { ended = true; for (const item of handles.values()) if (item.fd !== undefined) fs.close(item.fd, () => {}); handles.clear(); });
        });
      });
    });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return {
    profilebase: { protocol: 'ssh', host: '127.0.0.1', port: server.address().port, username: 'smoke-user', name: 'Synthetic SSH' },
    keyFile, passphrase, counters, clients, remoteRoot,
    async close() {
      if (closed) return;
      closed = true;
      for (const client of clients) { try { client.end(); client.destroy(); } catch {} }
      await new Promise(resolve => server.close(resolve));
    }
  };
}

module.exports = { createFixture };
