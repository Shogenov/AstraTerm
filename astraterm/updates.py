"""Signed update feed and bounded HTTPS downloads, compatible with Python 3.5."""
import base64
import hashlib
import json
import os
import re
import ssl
import tempfile
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from ._shared import UPDATE_PARAMETERS, VERSION


def version_tuple(value):
    if not isinstance(value, str) or not re.match(r'^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$', value):
        raise ValueError('Некорректная версия обновления')
    return tuple(int(part) for part in value.split('.'))


def release_url(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('Некорректная ссылка обновления')
    url = urlsplit(value)
    origin = urlsplit(UPDATE_PARAMETERS['origin'])
    if (url.scheme != 'https' or url.netloc != origin.netloc or url.username or
            url.password or url.query or url.fragment or '%' in url.path or
            '\\' in value or not re.match(r'^/releases/[0-9]+\.[0-9]+\.[0-9]+/[A-Za-z0-9_.-]+$', url.path)):
        raise ValueError('Обновление должно находиться на официальном сайте')
    return value


def read_manifest(contents, platform='linux', current=VERSION, public_key=None):
    if len(contents) > UPDATE_PARAMETERS['manifest_max_bytes']:
        raise ValueError('Список обновлений слишком большой')
    try:
        envelope = json.loads(contents.decode('utf-8'))
        payload = base64.b64decode(envelope['payload'].encode('ascii'), validate=True)
        signature = base64.b64decode(envelope['signature'].encode('ascii'), validate=True)
        key = serialization.load_pem_public_key((public_key or UPDATE_PARAMETERS['public_key']).encode('ascii'), backend=default_backend())
        algorithm = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
        key.verify(signature, payload, algorithm, hashes.SHA256())
        data = json.loads(payload.decode('utf-8'))
        if data['schema'] != 1 or data['product'] != 'AstraTerm':
            raise ValueError('Неверный продукт')
        item = data['releases'][platform]
        new_version = version_tuple(item['version'])
        version_tuple(current)
        expected = ('AstraTerm-{}-Windows-x64.exe' if platform == 'windows' else 'astraterm_{}_all.deb').format(item['version'])
        url = release_url(item['url'])
        if url != UPDATE_PARAMETERS['origin'] + '/releases/' + item['version'] + '/' + expected:
            raise ValueError('Имя пакета не соответствует версии')
        if (not isinstance(item['bytes'], int) or isinstance(item['bytes'], bool) or
                not 0 < item['bytes'] <= UPDATE_PARAMETERS['artifact_max_bytes'] or
                not isinstance(item['sha256'], str) or not re.match(r'^[0-9a-f]{64}$', item['sha256']) or
                not isinstance(item.get('notes', ''), str) or len(item.get('notes', '')) > 4000):
            raise ValueError('Неверные параметры пакета')
        result = {name: item[name] for name in ('version', 'url', 'bytes', 'sha256')}
        result['notes'] = item.get('notes', '')
        result['available'] = new_version > version_tuple(current)
        return result
    except Exception as exc:
        raise ValueError('Не удалось проверить подпись или формат списка обновлений') from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Перенаправление обновления запрещено')


def open_https(url):
    return build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect()).open(url, timeout=30)


def check(current=VERSION):
    with open_https(UPDATE_PARAMETERS['feed_url']) as response:
        return read_manifest(response.read(UPDATE_PARAMETERS['manifest_max_bytes'] + 1), current=current)


def download(item, directory, opener=open_https, progress=None):
    """Caller supplies a previously verified manifest entry; never a UI URL."""
    release_url(item['url'])
    if os.path.islink(directory):
        raise ValueError('Каталог обновлений не может быть ссылкой')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='download-', suffix='.deb', dir=directory)
    digest, count = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, 'wb') as target, opener(item['url']) as response:
            while True:
                block = response.read(1024 * 256)
                if not block:
                    break
                count += len(block)
                if count > item['bytes']:
                    raise ValueError('Размер обновления не совпадает')
                digest.update(block)
                target.write(block)
                if progress is not None:
                    progress(count, item['bytes'])
            target.flush()
            os.fsync(target.fileno())
        if count != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Контрольная сумма обновления не совпадает')
        return temporary
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
