#!/usr/bin/env python3
"""Sign release metadata and deploy static files over certificate-checked FTPS.

Signing keys stay outside the project. Passwords come from a prompt or CI secrets.
"""
from __future__ import print_function
import argparse
import base64
import ftplib
import getpass
import hashlib
import json
import os
from pathlib import Path
import posixpath
import ssl
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent


def key_path():
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share')))
    return base / 'AstraTermBuild' / 'release-signing.pem'


def deployment_config():
    filename = key_path().parent / 'deploy.json'
    if not filename.exists():
        return {}
    data = json.loads(filename.read_text(encoding='utf-8'))
    return {name:data[name] for name in ('host','user','root') if isinstance(data.get(name), str)}


def initialize_key(filename):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    filename = Path(filename).resolve()
    try:
        filename.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValueError('Signing key must be outside the source project')
    if filename.exists():
        raise ValueError('Signing key already exists; refusing to overwrite')
    filename.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072, backend=default_backend())
    with filename.open('xb') as stream:
        os.chmod(str(filename), 0o600)
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    if os.name == 'nt':
        import subprocess
        subprocess.check_call(['icacls', str(filename), '/inheritance:r', '/grant:r', getpass.getuser() + ':F'], stdout=subprocess.DEVNULL)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')
    product_path = ROOT / 'shared/product.json'
    product = json.loads(product_path.read_text(encoding='utf-8'))
    product['update_parameters'] = {'origin': 'https://astraterminal.ru',
        'feed_url': 'https://astraterminal.ru/releases/latest.json', 'public_key': public,
        'manifest_max_bytes': 131072, 'artifact_max_bytes': 536870912}
    product_path.write_text(json.dumps(product, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Release signing initialized; private key remains outside source and hosting.')


def sha256(filename):
    digest = hashlib.sha256()
    with Path(filename).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def sign_payload(payload, filename):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    product = json.loads((ROOT / 'shared/product.json').read_text(encoding='utf-8'))
    key = serialization.load_pem_private_key(Path(filename).read_bytes(), password=None, backend=default_backend())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')
    if public != product['update_parameters']['public_key']:
        raise ValueError('Signing key does not match the public key embedded in both applications')
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    signature = key.sign(raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    return json.dumps({'payload': base64.b64encode(raw).decode('ascii'), 'signature': base64.b64encode(signature).decode('ascii')}, indent=2).encode('utf-8') + b'\n'


def prepare(release_dir, platforms, signing_key, notes='', previous=None):
    product = json.loads((ROOT / 'shared/product.json').read_text(encoding='utf-8'))
    version = product['version']
    release_dir = Path(release_dir)
    releases = dict(previous.get('releases', {})) if previous else {}
    files = []
    for platform in platforms:
        report = json.loads((release_dir / platform / 'build.json').read_text(encoding='utf-8'))
        if report.get('version') != version or report.get('tests') != 'passed' or report.get('status') != 'built':
            raise ValueError('Deployment requires a native release with passing unit tests: ' + platform)
        def release_artifact(name):
            filename = release_dir / platform / name
            expected = next((item for item in report['artifacts'] if item['name'] == name), None)
            digest, size = sha256(filename), filename.stat().st_size
            if not expected or digest != expected['sha256'] or size != expected['bytes']:
                raise ValueError('Artifact differs from its build report: ' + name)
            relative = 'releases/' + version + '/' + name
            files.append((filename, relative))
            return {'url': product['update_parameters']['origin'] + '/' + relative,
                    'bytes': size, 'sha256': digest}

        name = ('AstraTerm-{}-Windows-x64.exe' if platform == 'windows' else 'astraterm_{}_all.deb').format(version)
        item = release_artifact(name)
        item.update(version=version, notes=notes[:4000])
        if platform == 'windows':
            item['installer'] = release_artifact('AstraTerm-{}-Windows-x64-Setup.exe'.format(version))
        releases[platform] = item
    payload = {'schema': 1, 'product': 'AstraTerm', 'published_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'releases': releases}
    envelope = sign_payload(payload, signing_key)
    return files, payload, envelope


class FTPS(object):
    def __init__(self, host, user, password, root):
        if not root.startswith('/') or '..' in root.split('/') or root == '/':
            raise ValueError('An explicit absolute web root is required')
        self.root = root.rstrip('/')
        self.ftp = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=60)
        self.ftp.connect(host, 21)
        self.ftp.login(user, password)
        self.ftp.prot_p()
        self.ftp.cwd(self.root)
        self.backup = '/.astraterm-deploy-backups/' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]

    def mkdirs(self, absolute):
        part = ''
        for segment in absolute.split('/'):
            if not segment:
                continue
            part += '/' + segment
            try:
                self.ftp.mkd(part)
            except ftplib.error_perm:
                self.ftp.cwd(part)  # Existing directory, or fail rather than swallowing an access error.

    def read(self, relative, maximum=131072):
        blocks = []
        count = [0]
        def consume(block):
            count[0] += len(block)
            if count[0] > maximum:
                raise ValueError('Remote metadata exceeds the limit')
            blocks.append(block)
        try:
            self.ftp.retrbinary('RETR ' + self.root + '/' + relative, consume)
        except ftplib.error_perm as error:
            if str(error).startswith('550'):
                return None
            raise
        return b''.join(blocks)

    def put(self, local, relative, replace=True):
        if relative.startswith('/') or '..' in relative.split('/') or any(c in relative for c in '\r\n\\'):
            raise ValueError('Invalid deployment path')
        destination = self.root + '/' + relative
        self.mkdirs(posixpath.dirname(destination))
        self.ftp.voidcmd('TYPE I')
        try:
            exists = self.ftp.size(destination) is not None
        except ftplib.error_perm:
            exists = False
        if exists and not replace:
            digest = hashlib.sha256()
            self.ftp.retrbinary('RETR ' + destination, digest.update)
            if digest.hexdigest() != sha256(local):
                raise ValueError('Immutable release path contains different bytes: ' + relative)
            return  # Safe retry after an interrupted publication.
        staged = destination + '.upload-' + uuid.uuid4().hex
        with Path(local).open('rb') as stream:
            self.ftp.storbinary('STOR ' + staged, stream, blocksize=256 * 1024)
        self.ftp.voidcmd('TYPE I')
        if self.ftp.size(staged) != Path(local).stat().st_size:
            raise ValueError('Remote file size mismatch')
        # Move the original outside public webroot; no remote recursive delete.
        if exists:
            saved = self.backup + '/' + relative
            self.mkdirs(posixpath.dirname(saved))
            self.ftp.rename(destination, saved)
            try:
                self.ftp.rename(staged, destination)
            except Exception:
                self.ftp.rename(saved, destination)
                raise
        else:
            self.ftp.rename(staged, destination)

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            self.ftp.close()


def main(argv=None):
    config = deployment_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init-key', action='store_true')
    parser.add_argument('--signing-key', default=str(key_path()))
    parser.add_argument('--release-dir')
    parser.add_argument('--platforms', nargs='+', choices=['windows', 'linux'], default=['windows', 'linux'])
    parser.add_argument('--site', help='Static production directory')
    parser.add_argument('--host', default=os.environ.get('ASTRATERM_DEPLOY_HOST', config.get('host','')))
    parser.add_argument('--user', default=os.environ.get('ASTRATERM_DEPLOY_USER', config.get('user','')))
    parser.add_argument('--root', default=os.environ.get('ASTRATERM_DEPLOY_ROOT', config.get('root','')))
    parser.add_argument('--notes', default='')
    args = parser.parse_args(argv)
    if args.init_key:
        initialize_key(args.signing_key)
        return
    if not args.host or not args.user or not args.root or not args.release_dir:
        raise ValueError('Provide deployment host, user, root and release directory')
    password = os.environ.pop('ASTRATERM_DEPLOY_PASSWORD', None) or getpass.getpass('FTPS password (not stored): ')
    client = FTPS(args.host, args.user, password, args.root)
    password = None
    try:
        previous = client.read('releases/latest.json')
        if previous:
            # Verify the previous feed before merging an untouched platform.
            import sys
            sys.path.insert(0, str(ROOT))
            from astraterm.updates import read_manifest, version_tuple
            envelope = json.loads(previous.decode('utf-8'))
            payload = json.loads(base64.b64decode(envelope['payload']).decode('utf-8'))
            current = json.loads((ROOT / 'shared/product.json').read_text(encoding='utf-8'))['version']
            known_platforms = sorted(set(payload['releases']).intersection(('windows', 'linux')))
            if not known_platforms:
                raise ValueError('No platform found in the previous release feed')
            for platform in known_platforms:
                published = read_manifest(previous, platform=platform, current=current)
                if platform in args.platforms and version_tuple(published['version']) > version_tuple(current):
                    raise ValueError('A newer release is already published for ' + platform)
            previous = payload
        files, payload, envelope = prepare(args.release_dir, args.platforms, args.signing_key, args.notes, previous)
        if args.site:
            try:
                from . import website_build
            except (ImportError, SystemError):
                import website_build
            website_build.build(Path(args.site), payload)
        publish_site(client, args.site, files, payload, envelope)
    finally:
        client.close()


def publish_site(client, site, files, payload, envelope):
    import tempfile
    for local, relative in files:
        client.put(local, relative, replace=False)
        print('Uploaded release: ' + relative)
    if site:
        # Publish dependencies before their entry points.
        entry_points = ('index.html', 'index.php')
        for filename in sorted(Path(site).rglob('*')):
            if filename.is_file() and filename.name not in entry_points:
                client.put(filename, filename.relative_to(site).as_posix())
        for name in entry_points:
            for filename in sorted(Path(site).rglob(name)):
                if filename.parent != Path(site):
                    client.put(filename, filename.relative_to(site).as_posix())
    with tempfile.TemporaryDirectory(prefix='astraterm-release-metadata-') as directory:
        envelope_file = Path(directory) / 'latest.json'
        envelope_file.write_bytes(envelope)
        public_file = Path(directory) / 'downloads.json'
        public_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        client.put(public_file, 'releases/downloads.json')
        client.put(envelope_file, 'releases/latest.json')  # Commit update availability after all binaries exist.
    if site:
        client.put(Path(site) / 'index.html', 'index.html')
    print('Deployment complete. Previous files retained outside public webroot.')


if __name__ == '__main__':
    main()
