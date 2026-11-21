"""Encrypted, portable snapshots of AstraTerm's saved profile."""
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ._shared import BACKUP_PARAMETERS as P, VAULT_PARAMETERS as V
from .core import _config_lock, _normalize_document
from .profile_location import MARKER, PROFILES, active_profile
from .ssh_profiles import ProfileStore


DOCUMENTS = ('sessions.json', 'ssh-profiles.json', 'credentials.vault.json',
             'sidebar.json', 'logging.json', 'known-hosts.json')
EXTENSION = P['extension']
MAX_DOCUMENT = 8 * 1024 * 1024
MAX_PAYLOAD = P['maximum_payload_bytes']
MAX_ARCHIVE = MAX_PAYLOAD * 4 // 3 + 1024
MAX_KEY = P['maximum_key_bytes']
HEADER = {'format': 'AstraTerm backup', 'version': P['version'], 'iterations': P['iterations']}
AAD = b'AstraTerm profile backup v1'
KEY_REF = 'astraterm-backup-key:'
KEY_PARTS = ('private', 'public', 'certificate')
KEY_SUFFIXES = ('', '.pub', '-cert.pub')
KEY_HEADER = re.compile(br'^(?:-----BEGIN (?:OPENSSH |RSA |DSA |EC |ENCRYPTED )?PRIVATE KEY-----|PuTTY-User-Key-File-[23]:)')
INVALID = 'Формат резервной копии повреждён или не поддерживается.'


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _read(filename, limit, optional=False):
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        descriptor = os.open(filename, flags)
    except FileNotFoundError:
        if optional:
            return None
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Недопустимый тип или размер файла резервной копии.')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Файл резервной копии превышает допустимый размер.')
        return data


def _write_new(filename, data):
    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        os.unlink(filename)
        raise


def _b64(data):
    return base64.b64encode(data).decode('ascii')


def _decode(value, limit, exact=None):
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
        raise ValueError(INVALID)
    data = base64.b64decode(value.encode('ascii'), validate=True)
    if len(data) > limit or (exact is not None and len(data) != exact) or _b64(data) != value:
        raise ValueError(INVALID)
    return data


def _password_bytes(password, fresh=False):
    if not isinstance(password, str) or not password or len(password) > 4096 or '\0' in password:
        raise ValueError('Введите пароль резервной копии.')
    if fresh and len(password) < 10:
        raise ValueError('Пароль резервной копии должен содержать не менее 10 символов.')
    return password.encode('utf-8')


def _key_holders(documents):
    sessions = documents['sessions.json']
    return ([record for name in ('sessions', 'tunnels') for record in sessions[name]] +
            [record['ssh'] for record in sessions['groups']] +
            documents['ssh-profiles.json']['profiles'])


def _normalize_documents(documents):
    if not isinstance(documents, dict) or set(documents).difference(DOCUMENTS):
        raise ValueError(INVALID)
    checked = {name: value for name, value in documents.items() if isinstance(value, dict)}
    if len(checked) != len(documents):
        raise ValueError(INVALID)
    checked['sessions.json'] = _normalize_document(checked.get('sessions.json', {}))
    identities = ProfileStore._document(checked.get('ssh-profiles.json', {}), resolve_paths=False)
    checked['ssh-profiles.json'] = dict(identities, schema=1)
    if 'credentials.vault.json' in checked:
        vault = checked['credentials.vault.json']
        if any(vault.get(name) != V[name] for name in ('version', 'cipher', 'kdf', 'iterations')):
            raise ValueError('Формат хранилища учётных данных не поддерживается.')
        for name in ('salt', 'nonce', 'tag'):
            length = V[name + '_bytes']
            _decode(vault.get(name), length, length)
        if not _decode(vault.get('ciphertext'), MAX_DOCUMENT):
            raise ValueError(INVALID)
        checked['credentials.vault.json'] = {name: vault[name] for name in
                                             ('version', 'cipher', 'kdf', 'iterations', 'salt', 'nonce', 'tag', 'ciphertext')}
    if 'sidebar.json' in checked:
        collapsed = checked['sidebar.json'].get('collapsed')
        if not isinstance(collapsed, list):
            raise ValueError(INVALID)
        groups = {group['id'] for group in checked['sessions.json']['groups']}
        checked['sidebar.json'] = {'collapsed': [item for item in collapsed if isinstance(item, str) and item in groups]}
    if 'logging.json' in checked:
        log = checked['logging.json']
        if not isinstance(log.get('enabled'), bool) or log.get('level') not in ('error', 'warning', 'info', 'debug'):
            raise ValueError(INVALID)
        checked['logging.json'] = {'enabled': log['enabled'], 'level': log['level']}
    if 'known-hosts.json' in checked:
        trust = checked['known-hosts.json']
        if trust.get('schema') != 1 or not isinstance(trust.get('hosts'), dict):
            raise ValueError(INVALID)
    return checked


def _key_identifier(parts):
    return hashlib.sha256('\n'.join(parts[name] for name in KEY_PARTS).encode('ascii')).hexdigest()


def capture_profile(directory):
    documents = {}
    for name in DOCUMENTS:
        raw = _read(os.path.join(directory, name), MAX_DOCUMENT, optional=True)
        if raw is not None:
            documents[name] = json.loads(raw.decode('utf-8-sig'))
    documents = _normalize_documents(documents)
    keys, by_path, missing = {}, {}, []
    size = len(_json_bytes(documents))
    for record in _key_holders(documents):
        filename = record.get('key_file')
        if not filename:
            continue
        if filename in by_path:
            record['key_file'] = by_path[filename]
            continue
        try:
            local = os.path.realpath(os.path.expanduser(filename))
            data = _read(local, MAX_KEY)
            if not KEY_HEADER.match(data[:128]):
                raise ValueError()
            public = _read(local + '.pub', MAX_KEY, optional=True)
            certificate = _read(local + '-cert.pub', MAX_KEY, optional=True)
            parts = dict(zip(KEY_PARTS, map(_b64, (data, public or b'', certificate or b''))))
        except (OSError, ValueError):
            missing.append(filename)
            by_path[filename] = filename
            continue
        identifier = _key_identifier(parts)
        if identifier not in keys:
            size += sum(len(part) for part in parts.values())
            if len(keys) >= P['maximum_keys'] or size > MAX_PAYLOAD:
                raise ValueError('Резервная копия превышает допустимый размер или число SSH-ключей.')
            keys[identifier] = parts
        record['key_file'] = by_path[filename] = KEY_REF + identifier
    payload = {'documents': documents, 'keys': keys, 'missing': missing}
    raw = _read(os.path.join(directory, 'appearance.json'), MAX_DOCUMENT, optional=True)
    if raw is not None:
        appearance = json.loads(raw.decode('utf-8-sig'))
        if isinstance(appearance, dict) and appearance.get('theme') in ('dark', 'light'):
            payload['theme'] = appearance['theme']
    return payload


def _validate_payload(payload):
    try:
        documents = _normalize_documents(payload['documents'])
        keys, missing = payload['keys'], payload['missing']
        if (not isinstance(keys, dict) or len(keys) > P['maximum_keys'] or not isinstance(missing, list) or
                any(not isinstance(item, str) or not item or len(item) > 4096 or
                    any(ord(char) < 32 for char in item) for item in missing)):
            raise ValueError()
        for identifier, parts in keys.items():
            if (not isinstance(parts, dict) or set(parts) != set(KEY_PARTS) or
                    not KEY_HEADER.match(_decode(parts['private'], MAX_KEY)[:128])):
                raise ValueError()
            for name in KEY_PARTS[1:]:
                _decode(parts[name], MAX_KEY)
            if _key_identifier(parts) != identifier:
                raise ValueError()
        references = {item['key_file'][len(KEY_REF):] for item in _key_holders(documents)
                      if item.get('key_file', '').startswith(KEY_REF)}
        if set(keys) != references:
            raise ValueError()
        checked = {'documents': documents, 'keys': keys, 'missing': missing}
        if 'theme' in payload:
            if payload['theme'] not in ('dark', 'light'):
                raise ValueError()
            checked['theme'] = payload['theme']
        return checked
    except (KeyError, ValueError, TypeError):
        raise ValueError(INVALID) from None


def write_backup(filename, payload, password, root):
    directory = os.path.realpath(root)
    filename = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(filename))),
                            os.path.basename(filename))
    try:
        inside = os.path.commonpath((directory, filename)) == directory
    except ValueError:
        inside = False  # A separate drive is outside the profile.
    if inside:
        raise ValueError('Выберите файл вне каталога настроек AstraTerm.')
    password = _password_bytes(password, fresh=True)
    raw = _json_bytes(payload)
    if len(raw) > MAX_PAYLOAD:
        raise ValueError('Резервная копия превышает допустимый размер.')
    salt, nonce = os.urandom(32), os.urandom(12)
    key = hashlib.pbkdf2_hmac('sha256', password, salt, P['iterations'], dklen=32)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce), backend=default_backend()).encryptor()
    encryptor.authenticate_additional_data(AAD)
    ciphertext = encryptor.update(raw) + encryptor.finalize()
    envelope = dict(HEADER, salt=_b64(salt), nonce=_b64(nonce), tag=_b64(encryptor.tag),
                    ciphertext=_b64(ciphertext))
    try:
        _write_new(filename, _json_bytes(envelope))
    except FileExistsError:
        raise ValueError('Файл уже существует. Выберите новое имя для резервной копии.') from None


def read_backup(filename, password):
    password = _password_bytes(password)
    raw = _read(filename, MAX_ARCHIVE)
    try:
        envelope = json.loads(raw.decode('utf-8'))
        if any(envelope[name] != value for name, value in HEADER.items()):
            raise ValueError()
        salt = _decode(envelope['salt'], 32, 32)
        nonce = _decode(envelope['nonce'], 12, 12)
        tag = _decode(envelope['tag'], 16, 16)
        ciphertext = _decode(envelope['ciphertext'], MAX_PAYLOAD)
    except (KeyError, TypeError, ValueError):
        raise ValueError(INVALID) from None
    key = hashlib.pbkdf2_hmac('sha256', password, salt, P['iterations'], dklen=32)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend()).decryptor()
        decryptor.authenticate_additional_data(AAD)
        plain = decryptor.update(ciphertext) + decryptor.finalize()
    except Exception:
        raise ValueError('Неверный пароль резервной копии или файл повреждён.') from None
    try:
        return _validate_payload(json.loads(plain.decode('utf-8')))
    except (ValueError, RecursionError):
        raise ValueError(INVALID) from None


def restore_profile(root, previous, payload):
    """Build a complete profile, then atomically select it for the next launch."""
    payload = _validate_payload(payload)
    os.makedirs(root, mode=0o700, exist_ok=True)
    parent = os.path.join(root, PROFILES)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.islink(parent):
        raise ValueError('Недопустимый каталог восстановления AstraTerm.')
    identifier = uuid.uuid4().hex
    directory = os.path.join(parent, identifier)
    os.mkdir(directory, 0o700)
    try:
        documents, keys = payload['documents'], payload['keys']
        for identifier_key, parts in keys.items():
            folder = os.path.join(directory, 'keys', identifier_key)
            os.makedirs(folder, mode=0o700)
            for name, suffix in zip(KEY_PARTS, KEY_SUFFIXES):
                if parts[name]:
                    _write_new(os.path.join(folder, 'identity' + suffix), _decode(parts[name], MAX_KEY))
        for record in _key_holders(documents):
            reference = record.get('key_file', '')
            if reference.startswith(KEY_REF):
                record['key_file'] = os.path.join(directory, 'keys', reference[len(KEY_REF):], 'identity')
        for name, document in documents.items():
            data = _json_bytes(document)
            if len(data) > MAX_DOCUMENT:
                raise ValueError('Восстановленные настройки превышают допустимый размер.')
            _write_new(os.path.join(directory, name), data)
        if 'theme' in payload:
            _write_new(os.path.join(directory, 'appearance.json'), _json_bytes({'theme': payload['theme']}))

        marker = os.path.join(root, MARKER)
        with _config_lock(marker):
            if active_profile(root) != previous:
                raise ValueError('Активный профиль изменился. Перезапустите AstraTerm и повторите восстановление.')
            descriptor, temporary = tempfile.mkstemp(prefix='.restore-', dir=root)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(_json_bytes({'directory': identifier}))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, marker)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except Exception:
        shutil.rmtree(directory)
        raise
    return directory
