#!/usr/bin/env python3
"""Development-only end-to-end build check using an isolated SSH/Xephyr fixture.

Not shipped. Uses ASTRA_VM_HOST/USER/PASSWORD/FINGERPRINT, a pinned development
connection and temporary synthetic SSH keys; never edits user authorized_keys.
"""
import argparse
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from vm import PinnedKey

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = r'''
import json, os, pathlib, pwd, shutil, socket, subprocess, sys, time
root=pathlib.Path(sys.argv[1]); children=[]; logs=[]
try:
    subprocess.check_call(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/'hostkey')])
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]; sock.close()
    config='\n'.join(['Port '+str(port),'ListenAddress 127.0.0.1','HostKey '+str(root/'hostkey'),
      'PidFile '+str(root/'sshd.pid'),'AuthorizedKeysFile '+str(root/'authorized_keys'),
      'PasswordAuthentication no','ChallengeResponseAuthentication no','UsePAM no','StrictModes no',
      'UsePrivilegeSeparation no','AllowUsers '+pwd.getpwuid(os.geteuid()).pw_name,'Subsystem sftp internal-sftp'])+'\n'
    # Interpret only the literal newline separator used above.
    config=config.replace('\\n','\n')
    (root/'sshd_config').write_text(config)
    log=(root/'sshd.log').open('wb');logs.append(log)
    daemon=subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(root/'sshd_config')],stdout=log,stderr=log);children.append(daemon)
    for attempt in range(100):
        if daemon.poll() is not None: raise RuntimeError((root/'sshd.log').read_text())
        try:
            probe=socket.create_connection(('127.0.0.1',port),.1);probe.close();break
        except OSError: time.sleep(.1)
    else: raise RuntimeError('Fixture sshd did not start')
    display=next(number for number in range(99,200) if not os.path.exists('/tmp/.X11-unix/X'+str(number)))
    environment=dict(os.environ,DISPLAY=':0');environment.setdefault('XAUTHORITY',os.path.expanduser('~/.Xauthority'))
    log=(root/'xephyr.log').open('wb');logs.append(log)
    display_command=(['Xvfb',':'+str(display),'-screen','0','1360x900x24','-nolisten','tcp','-ac'] if shutil.which('Xvfb') else
                     ['Xephyr',':'+str(display),'-screen','1360x900','-nolisten','tcp','-ac'])
    nested=subprocess.Popen(display_command,env=environment,stdout=log,stderr=log);children.append(nested)
    for attempt in range(100):
        if nested.poll() is not None: raise RuntimeError((root/'xephyr.log').read_text())
        if os.path.exists('/tmp/.X11-unix/X'+str(display)): break
        time.sleep(.1)
    else: raise RuntimeError('Nested test display did not start')
    print(json.dumps({'port':port,'display':':'+str(display)}),flush=True)
    sys.stdin.buffer.read(1)
finally:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill();child.wait()
    for log in logs: log.close()
    assert str(root).startswith('/tmp/astraterm-build-verification-')
    shutil.rmtree(str(root))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('linux', 'all'), default='all')
    parser.add_argument('--credentials-stdin', action='store_true', help='Read session-only connection JSON from stdin')
    parser.add_argument('--password-prompt', action='store_true', help='Read VM password without echo in a terminal')
    parser.add_argument('--host', default='')
    parser.add_argument('--user', default='')
    parser.add_argument('--fingerprint', default='')
    parser.add_argument('--website-capture', action='store_true', help='Use demo-only status values in design screenshots')
    parser.add_argument('--smoke-script', choices=sorted(path.name for path in (ROOT / 'tests').glob('smoke_*.py')),
                        help='Run one isolated Astra GUI check instead of building artifacts')
    args = parser.parse_args()
    values = json.loads(sys.stdin.readline()) if args.credentials_stdin else {key: os.environ.get('ASTRA_VM_' + key, '') for key in ('HOST', 'USER', 'PASSWORD', 'FINGERPRINT')}
    if args.password_prompt:
        import getpass
        values = {'HOST':args.host, 'USER':args.user, 'FINGERPRINT':args.fingerprint, 'PASSWORD':getpass.getpass('VM password (not stored): ')}
    if not all(values.values()):
        raise RuntimeError('Set the four ASTRA_VM_* development environment variables')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedKey(values['FINGERPRINT']))
    client.connect(values['HOST'], username=values['USER'], password=values['PASSWORD'], look_for_keys=False, allow_agent=False, timeout=15)
    client.get_transport().set_keepalive(15)
    control_in = None
    server = None
    try:
        stdin, stdout, stderr = client.exec_command("mktemp -d /tmp/astraterm-build-verification-XXXXXXXX")
        remote = stdout.read().decode().strip()
        if not remote.startswith('/tmp/astraterm-build-verification-'):
            raise RuntimeError('Invalid fixture directory')
        with tempfile.TemporaryDirectory(prefix='astra build verification ') as directory:
            local = Path(directory)
            private = ed25519.Ed25519PrivateKey.generate()
            (local / 'id_fixture').write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
            if os.name == 'nt':
                import ctypes
                account = ctypes.create_unicode_buffer(256)
                length = ctypes.c_ulong(len(account))
                if not ctypes.windll.advapi32.GetUserNameW(account, ctypes.byref(length)):
                    raise RuntimeError('Cannot identify the Windows build account')
                subprocess.check_call(['icacls', str(local / 'id_fixture'), '/inheritance:r', '/grant:r', account.value+':F'], stdin=subprocess.DEVNULL)
            public = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            with client.open_sftp() as sftp:
                with sftp.open(remote + '/supervisor.py', 'w') as file:
                    file.write(SUPERVISOR)
                with sftp.open(remote + '/authorized_keys', 'wb') as file:
                    file.write(public + b' isolated-build-verification\n')
            command = '/usr/bin/python3.5 -u ' + shlex.quote(remote + '/supervisor.py') + ' ' + shlex.quote(remote)
            control_in, control_out, control_error = client.exec_command(command)
            line = control_out.readline()
            if not line:
                raise RuntimeError(control_error.read().decode('utf-8', 'replace'))
            fixture = json.loads(line)
            with client.open_sftp() as sftp:
                host_public = sftp.open(remote + '/hostkey.pub').read().decode().split()
            transport = client.get_transport()

            class Forward(socketserver.BaseRequestHandler):
                def handle(self):
                    channel = transport.open_channel('direct-tcpip', ('127.0.0.1', fixture['port']), self.client_address)
                    try:
                        while True:
                            ready, unused, unused_errors = select.select([self.request, channel], [], [], 1)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (channel if source is self.request else self.request).sendall(data)
                    except (OSError, EOFError):
                        pass
                    finally:
                        channel.close()

            class Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True
            server = Server(('127.0.0.1', 0), Forward)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]
            known = local / 'known hosts'
            known.write_text('[127.0.0.1]:{} {} {}\n'.format(port, *host_public[:2]), encoding='utf-8')
            # Windows OpenSSH private files inherit the caller's protected TEMP ACL.
            command = [sys.executable, str(ROOT / 'tools/build.py'), args.target,
                       '--linux-host', values['USER'] + '@127.0.0.1', '--linux-port', str(port),
                       '--linux-key', str(local / 'id_fixture'), '--known-hosts', str(known), '--smoke', '--display', fixture['display'], '--isolated-ssh']
            environment = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
            environment.pop('ASTRA_VM_PASSWORD', None)
            if args.smoke_script:
                from package import build_archive
                archive = build_archive(local / 'source.tar.gz')
                with client.open_sftp() as sftp:
                    sftp.put(str(archive), remote + '/source.tar.gz')
                from sync_shared import product
                shell = ('set -eu\ncd ' + shlex.quote(remote) + '\ntar -xzf source.tar.gz\ncd ' +
                         shlex.quote('astraterm-' + product()['version']) + '\nDISPLAY=' +
                         shlex.quote(fixture['display']) + ' LANG=C.UTF-8 LC_ALL=C.UTF-8 /usr/bin/python3.5 tests/' +
                         args.smoke_script)
                if args.smoke_script == 'smoke_design.py':
                    shell = shell.replace(' LANG=C.UTF-8', ' ASTRA_DESIGN_SCREENSHOT=' +
                                          shlex.quote(remote + '/design.png') + ' LANG=C.UTF-8')
                    if args.website_capture:
                        shell = shell.replace(' LANG=C.UTF-8', ' ASTRA_WEBSITE_SHOT=1 LANG=C.UTF-8')
                if args.smoke_script == 'smoke_status.py':
                    shell = shell.replace(' LANG=C.UTF-8', ' ASTRA_STATUS_SCREENSHOT=' +
                                          shlex.quote(remote + '/status.png') + ' LANG=C.UTF-8')
                test_in, test_out, test_errors = client.exec_command('sh -s')
                test_in.write(shell); test_in.flush(); test_in.channel.shutdown_write()
                output = test_out.read().decode('utf-8', 'replace')
                errors = test_errors.read().decode('utf-8', 'replace')
                print(output); print(errors)
                code = test_out.channel.recv_exit_status()
                if 'Traceback (most recent call last):' in output + errors:
                    code = 1
                if args.smoke_script == 'smoke_design.py' and not code:
                    previews = ROOT / 'artifacts/native-smoke' / product()['version']
                    previews.mkdir(parents=True, exist_ok=True)
                    with client.open_sftp() as sftp:
                        for name in ('design.png', 'design-multiexec.png'):
                            sftp.get(remote + '/' + name, str(previews / name))
                if args.smoke_script == 'smoke_status.py' and not code:
                    previews = ROOT / 'artifacts/native-smoke' / product()['version']
                    previews.mkdir(parents=True, exist_ok=True)
                    with client.open_sftp() as sftp:
                        sftp.get(remote + '/status.png', str(previews / 'status.png'))
            else:
                code = subprocess.call(command, cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL)
                if not code:
                    from sync_shared import product
                    summary = json.loads((ROOT / 'dist/releases' / product()['version'] / 'build-summary.json').read_text(encoding='utf-8'))
                    if summary['status'] != 'built':
                        raise RuntimeError('Native build did not produce a successful release summary')
            if code:
                raise RuntimeError('Build verification failed with exit code ' + str(code))
            print('NATIVE_GUI_CHECK_PASS ' + args.smoke_script if args.smoke_script else 'NATIVE_BUILD_VERIFICATION_PASS ' + args.target)
    finally:
        if server:
            server.shutdown(); server.server_close()
        if control_in:
            try:
                control_in.write('x'); control_in.flush(); control_in.channel.shutdown_write()
                # Let the fixture supervisor reap exactly its own child processes.
                control_out.channel.settimeout(15)
                control_out.read()
                control_error.read()
            except (OSError, EOFError):
                pass
        client.close()


if __name__ == '__main__':
    main()
