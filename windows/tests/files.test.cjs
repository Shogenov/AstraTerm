'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const os = require('node:os');
const { EventEmitter } = require('node:events');
const { FilesService, MAX_TEXT_BYTES } = require('../src/backend/files.cjs');
const { permissions, parseBirthTimes } = require('../src/backend/file-metadata.cjs');
const { SftpDrag } = require('../src/backend/sftp-drag.cjs');
const metadataContract = require('../../shared/contracts/file-metadata.json');
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-files-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const records = new Map();
  const service = new FilesService({ terminals: { get: id => records.get(id) } });
  t.after(() => service.dispose());
  return { directory, service, records };
}
test('an editor or file dialog cannot target a replacement connection with the same terminal id',async t=>{
 const {service,records}=fixture(t);let opened=0;
 records.set('reused',{id:'reused',profile:{protocol:'ssh'},generation:2,status:'running',sshClient:{sftp(){opened++;}}});
 await assert.rejects(service.readText({mode:'sftp',terminalId:'reused',generation:1,path:'/same.txt'}),/изменилось/);
 await assert.rejects(service.writeText({mode:'sftp',terminalId:'reused',generation:1,path:'/same.txt',text:'wrong destination',revision:null}),/изменилось/);
 assert.equal(opened,0);
});
// A filesystem-backed SFTP fake exercises the same callback protocol used by
// ssh2, including unsupported posix-rename and failed replacement rollback.
class FakeSftp {
  constructor(directory) { this.directory = directory; this.closed = false; this.renames = []; this.failReplacement = false; this.posix = false; }
  local(remote) { return path.join(this.directory, remote.replace(/^\/+/, '')); }
  end() { this.closed = true; }
  realpath(remote, callback) { callback(null, remote === '.' ? '/' : remote); }
  lstat(remote, callback) { fs.lstat(this.local(remote), callback); }
  readdir(remote, callback) {
    fsp.readdir(this.local(remote)).then(async names => Promise.all(names.map(async filename => ({ filename, attrs: await fsp.lstat(this.local(path.posix.join(remote, filename))) })))).then(data => callback(null, data), callback);
  }
  readFile(remote, callback) { fs.readFile(this.local(remote), callback); }
  open(remote, flags, callback) { fs.open(this.local(remote), flags, callback); }
  read(handle, buffer, offset, length, position, callback) { fs.read(handle, buffer, offset, length, position, callback); }
  close(handle, callback) { fs.close(handle, callback); }
  writeFile(remote, bytes, options, callback) { fs.writeFile(this.local(remote), bytes, options, callback); }
  mkdir(remote, options, callback) { fs.mkdir(this.local(remote), options, callback); }
  rmdir(remote, callback) { fs.rmdir(this.local(remote), callback); }
  unlink(remote, callback) { fs.unlink(this.local(remote), callback); }
  chmod(remote, mode, callback) { fs.chmod(this.local(remote), mode, callback); }
  chown(remote, uid, gid, callback) { fs.chown(this.local(remote), uid, gid, callback); }
  rename(source, target, callback) {
    this.renames.push([source, target]);
    if (this.failReplacement && source.endsWith('.tmp')) { callback(new Error('synthetic replacement failure')); return; }
    fs.rename(this.local(source), this.local(target), callback);
  }
  ext_openssh_rename(source, target, callback) {
    if (this.posix) { this.rename(source, target, callback); return; }
    const error = new Error('Unsupported'); error.code = 8; callback(error);
  }
  fastPut(source, remote, options, callback) {
    fsp.copyFile(source, this.local(remote)).then(() => fsp.chmod(this.local(remote), options.mode)).then(() => callback(null), callback);
  }
  fastGet(remote, target, callback) { fs.copyFile(this.local(remote), target, callback); }
}
function connect(records, identifier, directory) {
  const sftp = new FakeSftp(directory), sshClient = new EventEmitter();
  let opens = 0;
  sshClient.sftp = callback => { opens += 1; callback(null, sftp); };
  const record = { id: identifier, sshClient, generation: 1, status: 'running', profile: { protocol: 'ssh' } };
  records.set(identifier, record);
  return { sftp, sshClient, record, opens: () => opens };
}
test('shared permissions and birth times preserve missing metadata', () => {
  for (const [mode, expected] of metadataContract.permissions) assert.equal(permissions(mode), expected);
  const parsed = Object.fromEntries([...parseBirthTimes(Buffer.from(metadataContract.birth_records))]
    .map(([filename, timestamp]) => [filename, timestamp / 1000]));
  assert.deepEqual(parsed, metadataContract.birth_times);
});
test('SFTP drag exports a copy, retains its filename and rejects a replaced connection', async t => {
  const { directory, service, records } = fixture(t);
  const { record } = connect(records, 'drag-source', directory);
  const data = Buffer.from([0, 1, 2, 127, 255]);
  fs.writeFileSync(path.join(directory, 'remote.bin'), data);
  const drag = new SftpDrag(service);
  t.after(() => drag.dispose());
  const check = () => { if (record.generation !== 1) throw Error('SSH-подключение изменилось'); };
  const request = { mode: 'sftp', terminalId: 'drag-source', generation: 1, path: '/remote.bin' };
  const token = await drag.prepare(request, check);
  let local;
  assert.equal(drag.start(token, file => { local = file; }), true);
  assert.equal(path.basename(local), 'remote.bin');
  assert.deepEqual(fs.readFileSync(local), data);
  assert.deepEqual(fs.readFileSync(path.join(directory, 'remote.bin')), data);
  record.generation++;
  assert.throws(() => drag.start(token, () => assert.fail()), /изменилось/);
  await drag.dispose();
  assert.equal(fs.existsSync(local), false);
});
test('local editor saves atomically and detects external changes', async t => {
  const { directory, service } = fixture(t), filename = path.join(directory, 'text.txt');
  fs.writeFileSync(filename, 'Привет\n');
  const first = await service.readText({ path: filename });
  assert.equal(first.text, 'Привет\n');
  const saved = await service.writeText({ path: filename, text: 'Updated\n', revision: first.revision });
  assert.equal(fs.readFileSync(filename, 'utf8'), 'Updated\n'); assert.notEqual(saved.revision, first.revision);
  fs.writeFileSync(filename, 'External\n');
  await assert.rejects(service.writeText({ path: filename, text: 'Lost update', revision: saved.revision }), /изменён/);
  assert.equal(fs.readFileSync(filename, 'utf8'), 'External\n');
  assert.deepEqual(fs.readdirSync(directory), ['text.txt']);
});
test('editor enforces explicit revision, new-file intent, binary and size limits', async t => {
  const { directory, service } = fixture(t), filename = path.join(directory, 'new.txt');
  await assert.rejects(service.writeText({ path: filename, text: 'new' }), /перечитайте/);
  await service.writeText({ path: filename, text: 'new', revision: null });
  await assert.rejects(service.writeText({ path: filename, text: 'replace', revision: null }), /изменён/);
  fs.writeFileSync(filename, Buffer.from([65, 0, 66]));
  await assert.rejects(service.readText({ path: filename }), /Двоичный/);
  fs.writeFileSync(filename, Buffer.alloc(MAX_TEXT_BYTES + 1, 65));
  await assert.rejects(service.readText({ path: filename }), /2 МиБ/);
});
test('overlapping mutations are refused so a second write cannot race a revision check', async t => {
  const { directory, service } = fixture(t), filename = path.join(directory, 'text.txt');
  const first = service.writeText({ path: filename, text: 'first', revision: null });
  assert.equal(service.busy, true);
  await assert.rejects(service.writeText({ path: filename, text: 'second', revision: null }), /Дождитесь/);
  await first;
  assert.equal(fs.readFileSync(filename, 'utf8'), 'first');
  assert.equal(service.busy, false);
});
test('list/rename and single delete preserve directories and never recurse', async t => {
  const { directory, service } = fixture(t);
  await service.mkdir({ path: path.join(directory, 'folder') });
  fs.writeFileSync(path.join(directory, 'folder', 'keep.txt'), 'safe');
  fs.writeFileSync(path.join(directory, 'a.txt'), 'a'); fs.writeFileSync(path.join(directory, 'b.txt'), 'b');
  const listing = await service.list({ path: directory });
  assert.equal(listing.entries[0].name, 'folder');
  await assert.rejects(service.rename({ path: path.join(directory, 'a.txt'), targetPath: path.join(directory, 'b.txt') }), /существует/);
  await assert.rejects(service.remove({ path: path.join(directory, 'folder') }));
  assert.equal(fs.readFileSync(path.join(directory, 'folder', 'keep.txt'), 'utf8'), 'safe');
  await service.rename({ path: path.join(directory, 'a.txt'), targetPath: path.join(directory, 'renamed.txt') });
  assert.ok(fs.existsSync(path.join(directory, 'renamed.txt')));
});
test('SFTP reuses the ready terminal transport and keeps directories per terminal', async t => {
  const { directory, service, records } = fixture(t);
  fs.mkdirSync(path.join(directory, 'folder')); fs.writeFileSync(path.join(directory, 'a.txt'), 'a');
  const first = connect(records, 'one', directory);
  const second = connect(records, 'two', directory);
  const listing = await service.list({ mode: 'sftp', terminalId: 'one', path: '/folder' });
  assert.equal(listing.path, '/folder');
  await service.list({ mode: 'sftp', terminalId: 'one' });
  assert.equal(first.opens(), 1);
  assert.equal((await service.list({ mode: 'sftp', terminalId: 'two' })).path, '/');
  assert.equal(second.opens(), 1);
  first.record.status = 'exited'; first.sshClient.emit('close');
  assert.equal(first.sftp.closed, true);
  await assert.rejects(service.list({ mode: 'sftp', terminalId: 'one' }), /активное SSH/);
});
test('late SFTP callback from a previous connection cannot attach to a replacement', async t => {
  const { directory, service, records } = fixture(t), old = connect(records, 'one', directory);
  let delayed;
  old.sshClient.sftp = callback => { delayed = callback; };
  const result = service.list({ mode: 'sftp', terminalId: 'one' });
  // _binding creates the pending callback before its first await.
  assert.equal(typeof delayed, 'function');
  connect(records, 'one', directory);
  delayed(null, old.sftp);
  await assert.rejects(result, /изменилось/);
  assert.equal(old.sftp.closed, true);
  assert.equal((await service.list({ mode: 'sftp', terminalId: 'one' })).path, '/');
});
test('remote editor fallback replaces safely and rolls back a failed replacement', async t => {
  const { directory, service, records } = fixture(t), remote = connect(records, 'one', directory);
  const filename = path.join(directory, 'edit.txt'); fs.writeFileSync(filename, 'original');
  const args = { mode: 'sftp', terminalId: 'one', path: '/edit.txt' };
  const opened = await service.readText(args);
  const saved = await service.writeText({ ...args, revision: opened.revision, text: 'updated' });
  assert.equal(fs.readFileSync(filename, 'utf8'), 'updated'); assert.ok(saved.revision);
  assert.ok(remote.sftp.renames.some(([, target]) => target.endsWith('.bak')));
  remote.sftp.failReplacement = true;
  await assert.rejects(service.writeText({ ...args, revision: saved.revision, text: 'failed' }), /replacement failure/);
  assert.equal(fs.readFileSync(filename, 'utf8'), 'updated');
  assert.deepEqual(fs.readdirSync(directory), ['edit.txt']);
});
test('remote upload/download preserve bytes and require explicit replacement', async t => {
  const { directory, service, records } = fixture(t), localDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-transfer-'));
  t.after(() => fs.rmSync(localDirectory, { recursive: true, force: true }));
  connect(records, 'one', directory);
  const source = path.join(localDirectory, 'source.bin'), downloaded = path.join(localDirectory, 'copy.bin');
  const bytes = Buffer.alloc(100003); for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251;
  fs.writeFileSync(source, bytes);
  const args = { mode: 'sftp', terminalId: 'one' };
  await service.upload({ ...args, localPath: source, targetPath: '/uploaded.bin' });
  await assert.rejects(service.upload({ ...args, localPath: source, targetPath: '/uploaded.bin' }), /Подтвердите/);
  await service.upload({ ...args, localPath: source, targetPath: '/uploaded.bin', overwrite: true });
  await service.download({ ...args, remotePath: '/uploaded.bin', localPath: downloaded });
  assert.deepEqual(fs.readFileSync(downloaded), bytes);
  await assert.rejects(service.download({ ...args, remotePath: '/uploaded.bin', localPath: downloaded }), /Подтвердите/);
  assert.deepEqual(fs.readdirSync(directory), ['uploaded.bin']);
});
test('writes reject symbolic links while unlink only removes the link', { skip: process.platform === 'win32' ? 'Windows symlink privilege is not assumed' : false }, async t => {
  const { directory, service } = fixture(t), target = path.join(directory, 'target.txt'), link = path.join(directory, 'link.txt');
  fs.writeFileSync(target, 'original'); fs.symlinkSync(target, link);
  await assert.rejects(service.writeText({ path: link, text: 'bad', revision: null }), /ссылку/);
  await service.remove({ path: link });
  assert.equal(fs.readFileSync(target, 'utf8'), 'original');
});

test('actual ssh2 SFTP subsystem supports bounded reads, atomic editor and binary transfers on the terminal connection', { timeout: 15000 }, async t => {
  const { Server, Client, utils } = require('ssh2');
  const { generateKeyPairSync } = require('node:crypto');
  const { directory, service, records } = fixture(t);
  const localDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraterm-real-sftp-'));
  t.after(() => fs.rmSync(localDirectory, { recursive: true, force: true }));
  const key = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
  const { STATUS_CODE: S, OPEN_MODE: F } = utils.sftp;
  const peers = new Set(); let subsystems = 0;
  const attrs = stat => ({ size: stat.size, mode: stat.mode, uid: stat.uid, gid: stat.gid, atime: Math.floor(stat.atimeMs / 1000), mtime: Math.floor(stat.mtimeMs / 1000) });
  const server = new Server({ hostKeys: [key] }, peer => {
    peers.add(peer); peer.on('error', () => {}); peer.on('close', () => peers.delete(peer));
    peer.on('authentication', context => context.accept());
    peer.on('ready', () => peer.on('session', accept => accept().on('sftp', acceptSftp => {
      subsystems += 1;
      const channel = acceptSftp(), handles = new Map(); let nextHandle = 0;
      const local = remote => {
        const resolved = path.resolve(directory, `.${remote}`);
        if (resolved !== directory && !resolved.startsWith(`${directory}${path.sep}`)) throw new Error('Fixture path escaped');
        return resolved;
      };
      const reply = (request, error) => channel.status(request, error ? error.code === 'ENOENT' ? S.NO_SUCH_FILE : S.FAILURE : S.OK);
      const makeHandle = value => { const handle = Buffer.alloc(4); handle.writeUInt32BE(++nextHandle); handles.set(handle.toString('hex'), value); return handle; };
      const entry = handle => handles.get(handle.toString('hex'));
      channel.on('REALPATH', (request, remote) => channel.name(request, [{ filename: remote === '.' ? '/' : remote, longname: remote, attrs: {} }]));
      channel.on('LSTAT', (request, remote) => fs.lstat(local(remote), (error, stat) => error ? reply(request, error) : channel.attrs(request, attrs(stat))));
      channel.on('STAT', (request, remote) => fs.stat(local(remote), (error, stat) => error ? reply(request, error) : channel.attrs(request, attrs(stat))));
      channel.on('FSTAT', (request, handle) => fs.fstat(entry(handle).fd, (error, stat) => error ? reply(request, error) : channel.attrs(request, attrs(stat))));
      channel.on('OPENDIR', (request, remote) => channel.handle(request, makeHandle({ directory: local(remote), read: false })));
      channel.on('READDIR', (request, handle) => {
        const item = entry(handle);
        if (item.read) { channel.status(request, S.EOF); return; }
        item.read = true;
        fsp.readdir(item.directory).then(async names => Promise.all(names.map(async filename => ({ filename, longname: filename, attrs: attrs(await fsp.lstat(path.join(item.directory, filename))) })))).then(data => data.length ? channel.name(request, data) : channel.status(request, S.EOF), error => reply(request, error));
      });
      channel.on('OPEN', (request, remote, flags, attributes) => {
        let native = flags & F.WRITE ? flags & F.READ ? fs.constants.O_RDWR : fs.constants.O_WRONLY : fs.constants.O_RDONLY;
        for (const [remoteFlag, localFlag] of [[F.CREAT, fs.constants.O_CREAT], [F.TRUNC, fs.constants.O_TRUNC], [F.EXCL, fs.constants.O_EXCL], [F.APPEND, fs.constants.O_APPEND]]) if (flags & remoteFlag) native |= localFlag;
        fs.open(local(remote), native, attributes.mode || 0o600, (error, fd) => error ? reply(request, error) : channel.handle(request, makeHandle({ fd })));
      });
      channel.on('CLOSE', (request, handle) => {
        const item = entry(handle); handles.delete(handle.toString('hex'));
        if (item.fd !== undefined) fs.close(item.fd, error => reply(request, error)); else reply(request);
      });
      channel.on('READ', (request, handle, offset, length) => {
        const bytes = Buffer.alloc(length);
        fs.read(entry(handle).fd, bytes, 0, length, offset, (error, count) => error ? reply(request, error) : count ? channel.data(request, bytes.subarray(0, count)) : channel.status(request, S.EOF));
      });
      channel.on('WRITE', (request, handle, offset, bytes) => fs.write(entry(handle).fd, bytes, 0, bytes.length, offset, error => reply(request, error)));
      channel.on('SETSTAT', (request, remote, attributes) => {
        if (attributes.mode !== undefined) fs.chmod(local(remote), attributes.mode, error => reply(request, error)); else reply(request);
      });
      channel.on('FSETSTAT', (request, handle, attributes) => {
        if (attributes.mode !== undefined) fs.fchmod(entry(handle).fd, attributes.mode, error => reply(request, error)); else reply(request);
      });
      channel.on('RENAME', (request, source, target) => fs.rename(local(source), local(target), error => reply(request, error)));
      channel.on('REMOVE', (request, remote) => fs.unlink(local(remote), error => reply(request, error)));
      channel.on('MKDIR', (request, remote) => fs.mkdir(local(remote), error => reply(request, error)));
      channel.on('RMDIR', (request, remote) => fs.rmdir(local(remote), error => reply(request, error)));
      channel.on('EXTENDED', request => channel.status(request, S.OP_UNSUPPORTED));
      channel.on('close', () => { for (const item of handles.values()) if (item.fd !== undefined) fs.close(item.fd, () => {}); handles.clear(); });
    })));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { for (const peer of peers) peer.end(); await new Promise(resolve => server.close(resolve)); });
  const client = new Client(); client.on('error', () => {});
  t.after(() => client.end());
  await new Promise((resolve, reject) => { client.once('ready', resolve); client.once('error', reject); client.connect({ host: '127.0.0.1', port: server.address().port, username: 'fixture', hostVerifier: () => true }); });
  records.set('real', { id: 'real', sshClient: client, generation: 1, status: 'running', profile: { protocol: 'ssh' } });
  const request = { mode: 'sftp', terminalId: 'real' };
  await service.writeText({ ...request, path: '/edit.txt', text: 'Привет\n', revision: null });
  const read = await service.readText({ ...request, path: '/edit.txt' }); assert.equal(read.text, 'Привет\n');
  await service.writeText({ ...request, path: '/edit.txt', text: 'Updated\n', revision: read.revision });
  assert.equal(fs.readFileSync(path.join(directory, 'edit.txt'), 'utf8'), 'Updated\n');
  const source = path.join(localDirectory, 'binary.dat'), target = path.join(localDirectory, 'download.dat');
  const bytes = Buffer.alloc(200017, 0xa5); fs.writeFileSync(source, bytes);
  await service.upload({ ...request, localPath: source, targetPath: '/uploaded.bin' });
  await service.download({ ...request, remotePath: '/uploaded.bin', localPath: target });
  assert.deepEqual(fs.readFileSync(target), bytes);
  assert.equal((await service.list({ ...request, path: '/' })).entries.length, 2);
  assert.equal(subsystems, 1, 'file panel must reuse one subsystem on the existing SSH transport');
  assert.deepEqual(fs.readdirSync(directory).sort(), ['edit.txt', 'uploaded.bin']);
});
