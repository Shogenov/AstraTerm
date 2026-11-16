#!/usr/bin/env python3
"""Build verified platform artifacts; orchestration is compatible with Python 3.5."""
from __future__ import print_function, unicode_literals

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

try:
    from . import sync_shared
except (ImportError, SystemError):
    import sync_shared

ROOT = Path(__file__).resolve().parent.parent


def sha256(filename):
    digest = hashlib.sha256()
    with Path(filename).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(command, cwd=ROOT, environment=None, input_text=None, capture=False, reject_tracebacks=False):
    command = [str(value) for value in command]
    print('+ ' + ' '.join(shlex.quote(value) for value in command), flush=True)
    child = subprocess.Popen(command, cwd=str(cwd), env=environment,
                             stdin=subprocess.PIPE if input_text is not None else None,
                             stdout=subprocess.PIPE if capture or reject_tracebacks else None,
                             stderr=subprocess.STDOUT if capture or reject_tracebacks else None)
    output, unused = child.communicate(input_text.encode('utf-8') if input_text is not None else None)
    if reject_tracebacks and not capture and output:
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()
        else:
            sys.stdout.write(output.decode('utf-8', 'replace'))
    if child.returncode:
        if output:
            print(output.decode('utf-8', 'replace')[-3000:], file=sys.stderr)
        raise RuntimeError('Command failed ({}): {}'.format(child.returncode, command[0]))
    if reject_tracebacks and output and b'Traceback (most recent call last):' in output:
        raise RuntimeError('GUI callback raised an exception despite a successful process exit')
    return output.decode('utf-8', 'replace').strip() if capture else None


def write_json(filename, document):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def artifact(filename):
    filename = Path(filename)
    if not filename.is_file() or filename.stat().st_size == 0:
        raise RuntimeError('Expected artifact was not built: ' + str(filename))
    return {'name': filename.name, 'bytes': filename.stat().st_size, 'sha256': sha256(filename)}


def publish(source, destination):
    """Keep the previous artifact intact if the new copy or replacement fails."""
    destination = Path(destination)
    descriptor, name = tempfile.mkstemp(prefix='.publish-', dir=str(destination.parent))
    os.close(descriptor)
    try:
        shutil.copyfile(str(source), name)
        os.replace(name, str(destination))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def needed(command):
    executable = shutil.which(command)
    if not executable:
        raise RuntimeError('Required command not found: ' + command)
    return executable


def linux_archive(output):
    try:
        from . import package
    except (ImportError, SystemError):
        import package
    return package.build_archive(output)


def windows_archive(output):
    try:
        from . import package_windows
    except (ImportError, SystemError):
        import package_windows
    return package_windows.build_archive(output)


def combined_archive(output, version):
    try:
        from . import package, package_windows
    except (ImportError, SystemError):
        import package
        import package_windows
    selected = set(package.source_files()) | set(package_windows.source_files())
    with zipfile.ZipFile(str(output), 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in sorted(selected, key=lambda item: item.relative_to(ROOT).as_posix()):
            name = 'astraterm-{}/'.format(version) + filename.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100644 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, filename.read_bytes())
    return Path(output)


def build_windows(args, destination, version):
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    tests = 'not-run-source-only' if args.source_only else 'skipped' if args.skip_tests else 'passed'
    if not args.source_only:
        node = needed('node')
        major = int(run([node, '-p', 'process.versions.node.split(".")[0]'], capture=True))
        if major < 22:
            raise RuntimeError('Windows builds require Node.js 22 or newer')
        npm = needed('npm.cmd' if os.name == 'nt' else 'npm')
        app_root = ROOT / 'windows'
        modules = app_root / 'node_modules'
        marker = modules / '.astraterm-build-dependencies.sha256'
        lock = json.loads((app_root / 'package-lock.json').read_text(encoding='utf-8'))
        dependency_lock = {key: value for key, value in lock.get('packages', {}).items() if key != ''}
        signature = hashlib.sha256(json.dumps(dependency_lock, sort_keys=True).encode('utf-8')).hexdigest()
        installed = marker.read_text(encoding='ascii').strip() if marker.exists() else ''
        if args.install_dependencies or installed != signature or not (modules / 'electron/package.json').is_file():
            run([npm, 'ci', '--omit=optional', '--no-audit', '--no-fund'], cwd=app_root)
            marker.write_text(signature + '\n', encoding='ascii')
        if not args.skip_tests:
            run([npm, 'test'], cwd=app_root)
        # A new staging folder prevents yesterday's executable being accepted as this build.
        staging_root = ROOT / 'dist/staging'
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='windows-', dir=str(staging_root)) as directory:
            run([npm, 'run', 'dist', '--', '--config.directories.output=' + directory], cwd=app_root)
            for suffix in ('.exe', '.zip', '-Setup.exe'):
                name = 'AstraTerm-{}-Windows-x64{}'.format(version, suffix)
                source = Path(directory) / name
                artifact(source)
                publish(source, destination / name)
                files.append(destination / name)
        if args.smoke:
            environment = dict(os.environ, ASTRA_SMOKE_OUTPUT=str(destination / 'validation'))
            environment.pop('ELECTRON_RUN_AS_NODE', None)
            previous = destination / 'validation/report.json'
            if previous.exists():
                previous.unlink()
            run([str(files[0]), '--smoke-test'], environment=environment)
            report = json.loads((destination / 'validation/report.json').read_text(encoding='utf-8'))
            if report.get('result') != 'PASS' or report.get('packaged') is not True:
                raise RuntimeError('Packaged Windows GUI validation failed')
    files.append(windows_archive(destination / 'astraterm-{}-windows-source.zip'.format(version)))
    return {'platform': 'windows', 'version': version, 'status': 'built', 'tests': tests,
            'gui': 'passed' if args.smoke and not args.source_only else 'not-run', 'artifacts': [artifact(file) for file in files]}


def build_linux_local(args, destination, version):
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    tests = 'not-run-source-only' if args.source_only else 'skipped' if args.skip_tests else 'passed'
    if not args.source_only:
        environment = dict(os.environ, DISPLAY=args.display, LANG='C.UTF-8', LC_ALL='C.UTF-8')
        if not args.skip_tests:
            run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], environment=environment)
        try:
            from . import build_deb
        except (ImportError, SystemError):
            import build_deb
        files.append(build_deb.build_deb(destination / 'astraterm_{}_all.deb'.format(version)))
        if args.smoke:
            for name in ('smoke_gui.py', 'smoke_multiexec.py', 'smoke_groups.py', 'smoke_ui_helpers.py', 'smoke_design.py', 'smoke_tabs.py', 'smoke_imports.py', 'smoke_backup.py', 'smoke_status.py', 'smoke_session_actions.py', 'smoke_updates.py'):
                run([sys.executable, 'tests/' + name], environment=environment, reject_tracebacks=True)
    files.append(linux_archive(destination / 'astraterm-{}-source.tar.gz'.format(version)))
    return {'platform': 'linux', 'version': version, 'status': 'built', 'tests': tests,
            'gui': 'passed' if args.smoke and not args.source_only else 'not-run', 'artifacts': [artifact(file) for file in files]}


def ssh_arguments(args, scp=False):
    if not re.fullmatch(r'(?:[A-Za-z0-9_.-]+@)?(?:[A-Za-z0-9][A-Za-z0-9_.-]*|\[[0-9a-fA-F:]+\])', args.linux_host or ''):
        raise ValueError('Use --linux-host user@host or an SSH config alias without shell characters')
    if not 1 <= args.linux_port <= 65535:
        raise ValueError('Invalid Linux SSH port')
    command = [needed('scp' if scp else 'ssh'), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']
    if getattr(args, 'isolated_ssh', False):
        command += ['-F', 'NUL' if os.name == 'nt' else '/dev/null', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none']
    if args.known_hosts:
        filename = Path(args.known_hosts).resolve().as_posix()
        if '"' in filename or '\n' in filename:
            raise ValueError('Invalid known_hosts path')
        command += ['-o', 'UserKnownHostsFile="' + filename + '"']
    command += ['-P' if scp else '-p', str(args.linux_port)]
    if args.linux_key:
        command += ['-i', str(Path(args.linux_key).expanduser().resolve())]
    return command


def build_linux_remote(args, destination, version):
    destination.mkdir(parents=True, exist_ok=True)
    ssh, scp = ssh_arguments(args), ssh_arguments(args, scp=True)
    workspace = run(ssh + [args.linux_host, 'mktemp -d /tmp/astraterm-build.XXXXXXXX'], capture=True)
    if not re.fullmatch(r'/tmp/astraterm-build\.[A-Za-z0-9]+', workspace):
        raise RuntimeError('Remote builder did not return an isolated temporary directory')
    try:
        with tempfile.TemporaryDirectory(prefix='astraterm-upload-') as directory:
            archive = linux_archive(Path(directory) / 'source.tar.gz')
            run(scp + [archive.name, args.linux_host + ':' + workspace + '/source.tar.gz'], cwd=directory)
        worker = [args.linux_python, 'tools/build.py', 'linux', '--worker', '--output', workspace + '/payload', '--display', args.display]
        if args.skip_tests:
            worker.append('--skip-tests')
        if args.smoke:
            worker.append('--smoke')
        script = 'set -eu\ncd ' + shlex.quote(workspace) + '\ntar -xzf source.tar.gz\ncd ' + shlex.quote('astraterm-' + version) + '\n' + ' '.join(shlex.quote(value) for value in worker) + '\n'
        run(ssh + [args.linux_host, 'sh -s'], input_text=script)
        names = ['astraterm_{}_all.deb'.format(version), 'astraterm-{}-source.tar.gz'.format(version), 'build.json']
        for name in names:
            run(scp + [args.linux_host + ':' + workspace + '/payload/' + name, str(destination / name)])
        report = json.loads((destination / 'build.json').read_text(encoding='utf-8'))
        if report.get('version') != version or report.get('status') != 'built':
            raise RuntimeError('Remote result belongs to a different or failed release')
        if sorted(item['name'] for item in report['artifacts']) != sorted(names[:2]):
            raise RuntimeError('Unexpected remote artifact set')
        for item in report['artifacts']:
            if artifact(destination / item['name']) != item:
                raise RuntimeError('Downloaded Linux artifact failed its checksum: ' + item['name'])
        report['builder'] = 'remote-linux'
        return report
    finally:
        # workspace was validated against a fixed /tmp prefix above; no user checkout is removed.
        cleanup = 'set -eu\ncase ' + shlex.quote(workspace) + ' in /tmp/astraterm-build.*) rm -rf -- ' + shlex.quote(workspace) + ';; *) exit 2;; esac\n'
        try:
            run(ssh + [args.linux_host, 'sh -s'], input_text=cleanup)
        except (RuntimeError, OSError):
            print('Remote temporary build directory could not be cleaned: ' + workspace, file=sys.stderr)


def parse_args(argv=None):
    try:
        from .release import deployment_config
    except (ImportError, SystemError):
        from release import deployment_config
    deployment = deployment_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=('windows', 'linux', 'all'))
    parser.add_argument('--version', help='Set the shared release version before building, e.g. 0.10.1')
    parser.add_argument('--linux-host', default=os.environ.get('ASTRATERM_LINUX_HOST', ''))
    parser.add_argument('--linux-port', type=int, default=22)
    parser.add_argument('--linux-key', default='')
    parser.add_argument('--known-hosts', default='', help='Optional separate OpenSSH known_hosts file')
    parser.add_argument('--linux-python', default='/usr/bin/python3.5')
    parser.add_argument('--display', default=':0', help='Linux display for optional GUI validation')
    parser.add_argument('--skip-tests', action='store_true')
    parser.add_argument('--smoke', action='store_true', help='Run platform GUI checks; Linux needs an accessible display')
    parser.add_argument('--install-dependencies', action='store_true', help='Run npm ci even when the dependency lock matches')
    parser.add_argument('--source-only', action='store_true', help='Create source archives without binary build or tests')
    parser.add_argument('--output', help='Release directory; default: dist/releases/<version>')
    parser.add_argument('--plan', action='store_true', help='Show the plan without changing files')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--isolated-ssh', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--deploy', action='store_true', help='Publish a tested release and website over FTPS')
    parser.add_argument('--deploy-host', default=os.environ.get('ASTRATERM_DEPLOY_HOST', deployment.get('host','')))
    parser.add_argument('--deploy-user', default=os.environ.get('ASTRATERM_DEPLOY_USER', deployment.get('user','')))
    parser.add_argument('--deploy-root', default=os.environ.get('ASTRATERM_DEPLOY_ROOT', deployment.get('root','')))
    parser.add_argument('--signing-key', default='')
    parser.add_argument('--release-notes', default='')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    targets = ('windows', 'linux') if args.target == 'all' else (args.target,)
    version = sync_shared.release_version(args.version) if args.version is not None else sync_shared.product()['version']
    if args.deploy and (args.source_only or args.skip_tests or args.worker):
        raise ValueError('Deployment requires tested native artifacts; source-only/skip-tests/worker cannot deploy')
    if args.deploy and not all((args.deploy_host, args.deploy_user, args.deploy_root)):
        raise ValueError('Deployment needs --deploy-host, --deploy-user and --deploy-root (or ASTRATERM_DEPLOY_* variables)')
    destination = Path(os.path.abspath(args.output)) if args.output else ROOT / 'dist/releases' / version
    if args.worker and (args.target != 'linux' or sys.platform != 'linux'):
        raise ValueError('Worker mode is only for the native Linux build')
    if not args.source_only:
        if 'windows' in targets and sys.platform != 'win32':
            raise ValueError('Build Windows binaries on Windows. Use --source-only for archives on another OS.')
        if 'linux' in targets and sys.platform != 'linux' and not args.linux_host:
            raise ValueError('Provide --linux-host or ASTRATERM_LINUX_HOST to build .deb on a Linux machine')
        if args.linux_host:
            ssh_arguments(args)
    for target in targets:
        if not (ROOT / ('windows' if target == 'windows' else 'astraterm')).is_dir():
            raise ValueError('This source distribution does not contain the ' + target + ' implementation')
    if args.plan:
        print(json.dumps({'version': version, 'targets': targets, 'output': str(destination), 'source_only': args.source_only,
                          'linux_builder': 'remote-ssh' if args.linux_host else 'native-linux', 'deploy': args.deploy}, indent=2))
        return 0
    if args.version is not None:
        sync_shared.set_version(args.version, root=ROOT)
    sync_shared.synchronize(root=ROOT)
    destination.mkdir(parents=True, exist_ok=True)
    summary = {'version': version, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'status': 'building', 'platforms': []}
    try:
        for target in targets:
            target_dir = destination if args.worker else destination / target
            if target == 'windows':
                result = build_windows(args, target_dir, version)
            elif args.source_only or (sys.platform == 'linux' and not args.linux_host):
                result = build_linux_local(args, target_dir, version)
            else:
                result = build_linux_remote(args, target_dir, version)
            write_json(target_dir / 'build.json', result)
            sums = '\n'.join(item['sha256'] + '  ' + item['name'] for item in result['artifacts']) + '\n'
            (target_dir / 'SHA256SUMS.txt').write_text(sums, encoding='ascii')
            summary['platforms'].append(result)
        if args.target == 'all':
            source = combined_archive(destination / 'astraterm-{}-all-source.zip'.format(version), version)
            summary['combined_source'] = artifact(source)
        summary['status'] = 'built'
        if args.deploy:
            try:
                from . import release
            except (ImportError, SystemError):
                import release
            deploy_args = ['--release-dir', str(destination), '--platforms'] + list(targets)
            deploy_args += ['--host', args.deploy_host, '--user', args.deploy_user, '--root', args.deploy_root,
                            '--site', str(ROOT / 'website/production/dist'), '--notes', args.release_notes]
            if args.signing_key:
                deploy_args += ['--signing-key', args.signing_key]
            summary['deployment'] = 'publishing'
            release.main(deploy_args)
            summary['deployment'] = 'published'
        print('BUILD COMPLETE: ' + str(destination))
    except Exception as error:
        summary['status'] = 'failed'
        if summary.get('deployment') == 'publishing':
            summary['deployment'] = 'failed'
        summary['error'] = str(error)
        raise
    finally:
        write_json(destination / 'build-summary.json', summary)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print('BUILD FAILED: ' + str(error), file=sys.stderr)
        sys.exit(1)
