#!/usr/bin/env python3
"""Import the current Windows user's MobaXterm credentials into AstraTerm."""
import argparse
import base64
import configparser
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB8
except ImportError:
    from cryptography.hazmat.primitives.ciphers.modes import CFB8

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astraterm.core import MAX_CONFIG_BYTES, SessionStore, _atomic_json, _file_revision, default_session, validate_session
from astraterm.importers import ImportPlan, _decode, credential_id, moba_sessions, stable_id
from astraterm.vault import CredentialVault

DPAPI_HEADER = bytes.fromhex('01000000d08c9ddf0115d1118c7a00c04fc297eb')
ENDPOINT = re.compile(r'(?:(ssh|rdp|vnc|telnet)([0-9]*):)?(.+)@(.+)', re.I)


class DataBlob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    @classmethod
    def from_bytes(cls, raw):
        return cls(len(raw), (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw))


def verified_digest(config, master):
    entropy = DataBlob.from_bytes(config.get('Misc', 'SessionP').encode('mbcs'))
    digests = [hashlib.sha512(master.encode('utf-8')).digest()]
    try:
        digests.append(hashlib.sha512(master.encode('mbcs')).digest())
    except UnicodeEncodeError:
        pass
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    for unused, value in config.items('Sesspass'):
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            continue  # The section also contains ordinary settings.
        if len(raw) < 60:
            continue
        blob = DataBlob.from_bytes(raw if raw.startswith(DPAPI_HEADER) else DPAPI_HEADER + raw)
        output = DataBlob()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob), None, ctypes.byref(entropy),
                                                       None, None, 1, ctypes.byref(output)):
            continue
        try:
            clear = ctypes.string_at(output.data, output.size)
            for digest in digests:
                if hmac.compare_digest(clear, base64.b64encode(digest)) or hmac.compare_digest(clear, digest):
                    return digest
        finally:
            ctypes.memset(output.data, 0, output.size)
            free(output.data)
    raise ValueError('Не удалось подтвердить мастер-пароль в профиле MobaXterm текущего пользователя Windows.')


def decode_password(value, key):
    if not value:
        return ''
    if value.startswith('_@'):
        # MobaXterm 25.x stores a per-record IV in its ASCII prefix.
        if len(value) < 20 or not re.fullmatch(r'[A-Za-z0-9_@+/=]+', value[2:]):
            raise ValueError('Неподдерживаемая запись MobaXterm.')
        if len(value) == 20:
            return ''
        iv = value[2:18].encode('ascii')
        encoded = value[20:].replace('@', '+').replace('_', '/')
    else:
        cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        iv = cipher.update(b'\x00' * 16) + cipher.finalize()
        encoded = value
    body = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True)
    if not body or base64.b64encode(body).decode('ascii').rstrip('=') != encoded.rstrip('='):
        raise ValueError('Неподдерживаемая запись MobaXterm.')
    decryptor = Cipher(algorithms.AES(key), CFB8(iv)).decryptor()
    clear = (decryptor.update(body) + decryptor.finalize()).rstrip(b'\x00')
    try:
        return clear.decode('utf-8')
    except UnicodeDecodeError:
        if value.startswith('_@'):
            raise
        return clear.decode('mbcs')


def read_source(path, master):
    with path.open('rb') as stream:
        text = _decode(stream.read(MAX_CONFIG_BYTES + 1))
    config = configparser.RawConfigParser(delimiters=('=',))
    config.optionxform = str
    try:
        config.read_string(text)
        key = verified_digest(config, master)[:32]
    except configparser.Error:
        raise ValueError('Не удалось прочитать настройки шифрования MobaXterm.') from None
    plan = ImportPlan()
    moba_sessions(text, plan)
    endpoints = {}
    empty = 0
    for name, value in config.items('Credentials') if config.has_section('Credentials') else ():
        username, separator, encoded = value.partition(':')
        if not separator:
            raise ValueError('Неизвестный формат именованного профиля MobaXterm.')
        password = decode_password(encoded, key)
        if not password:
            empty += 1
            continue
        plan.secret({'id': credential_id(name), 'name': name, 'username': username,
                     'password': password})
    for name, value in config.items('Passwords') if config.has_section('Passwords') else ():
        if not value:
            empty += 1
            continue
        match = ENDPOINT.fullmatch(name)
        if not match:
            raise ValueError('Неизвестный адресный профиль MobaXterm.')
        protocol, port, username, host = match.groups()
        protocol = (protocol or 'ssh').lower()
        port = int(port or default_session(protocol)['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Некорректный порт профиля MobaXterm.')
        host = host.lower()
        identifier = stable_id('mobaxterm', 'endpoint', '{}:{}:{}@{}'.format(protocol, port, username, host))
        name = '{}{}:{}@{}'.format(protocol, port, username, host)
        password = decode_password(value, key)
        if not password:
            empty += 1
            continue
        record = {'id': identifier, 'name': name, 'username': username, 'password': password}
        if protocol in ('ssh', 'rdp'):
            record['protocol'] = protocol
        plan.secret(record)
        endpoints[identifier] = (protocol, port, username, host)
    return plan, endpoints, empty


def save_migration(store, vault, vault_revision, master, records, updates, reset):
    directory = Path(store.path).parent
    directory.mkdir(parents=True, exist_ok=True)
    backup_root = directory / 'backups'
    backup_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mobaxterm-import-', dir=str(directory)) as temporary:
        stage = Path(temporary)
        staged_vault = CredentialVault(stage / 'credentials.vault.json')
        if not reset and vault_revision is not None:
            shutil.copyfile(vault.path, staged_vault.path)
        try:
            staged_vault.unlock(master, create=True)
            staged_vault.merge(records)
        finally:
            staged_vault.lock()
        if updates:
            document = store._document()
            by_id = {session['id']: validate_session(session) for session in updates}
            document['sessions'] = [by_id.get(session['id'], session) for session in document['sessions']]
            _atomic_json(str(stage / 'sessions.json'), document)

        with ExitStack() as held_locks:
            # Electron claims these exact lock files with exclusive creation.
            for filename in (directory / '.sessions.json.lock', directory / 'credentials.vault.json.lock'):
                descriptor = os.open(str(filename), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                held_locks.callback(filename.unlink)
                held_locks.callback(os.close, descriptor)
            if _file_revision(store.path) != store._revision or _file_revision(vault.path) != vault_revision:
                raise ValueError('Настройки AstraTerm изменились во время импорта. Повторите операцию.')
            backup = Path(tempfile.mkdtemp(prefix='mobaxterm-import-', dir=str(backup_root)))
            for filename in (Path(store.path), Path(vault.path)):
                if filename.is_file():
                    shutil.copyfile(str(filename), str(backup / filename.name))
            if updates:
                os.replace(str(stage / 'sessions.json'), store.path)
            try:
                os.replace(staged_vault.path, vault.path)
            except OSError:
                if updates:
                    original = backup / 'sessions.json'
                    if original.is_file():
                        shutil.copyfile(str(original), str(stage / 'rollback.json'))
                        os.replace(str(stage / 'rollback.json'), store.path)
                    else:
                        os.unlink(store.path)
                raise
        return backup


def migrate(source, directory, moba_master, astra_master, apply=False, reset=False):
    from astraterm.moba_migration import plan_credential_migration
    plan, endpoints, empty = read_source(source, moba_master)
    store = SessionStore(directory / 'sessions.json')
    vault = CredentialVault(directory / 'credentials.vault.json')
    vault_revision = _file_revision(vault.path)
    original_sessions = store.sessions
    try:
        metadata = [] if reset else vault.unlock(astra_master, create=True)
        if reset:
            store.sessions = [dict(session, credential_profile='', credential_label='') for session in original_sessions]
        existing = {item['id']: vault.get(item['id']) for item in metadata}
        records, updates, counts = plan_credential_migration(store, plan.sessions, plan.credentials, endpoints, existing)
        if reset:
            if not records:
                raise ValueError('В MobaXterm нет подтверждённых паролей для создания нового хранилища.')
            imported = {session['id']: session for session in updates}
            final_sessions = [imported.get(session['id'], session) for session in store.sessions]
            updates = [after for before, after in zip(original_sessions, final_sessions) if before != after]
            counts['cleared'] = sum(bool(before.get('credential_profile')) and not after.get('credential_profile')
                                    for before, after in zip(original_sessions, final_sessions))
            store.sessions = original_sessions
        counts['profiles_to_save'] = len(records)
        counts['sessions_to_update'] = len(updates)
        print('MobaXterm: {} профилей с паролями, {} пустых.'.format(len(plan.credentials), empty), flush=True)
        print('Привязки: ' + ', '.join('{}={}'.format(name, count) for name, count in sorted(counts.items())), flush=True)
        if not apply or not (records or updates):
            return counts
        backup = save_migration(store, vault, vault_revision, astra_master, records, updates, reset)
        print('Перенос сохранён. Резервная копия: {}'.format(backup), flush=True)
        return counts
    finally:
        vault.lock()


def main():
    from astraterm.profile_location import active_profile

    if os.name != 'nt':
        raise ValueError('Для переноса локального хранилища MobaXterm запустите скрипт в Windows.')
    roaming = Path(os.environ.get('APPDATA') or Path.home() / 'AppData/Roaming')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=roaming / 'MobaXterm/MobaXterm.ini')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--apply', action='store_true', help='Save profiles and session links')
    parser.add_argument('--reset-vault', action='store_true', help='Replace the old vault after backing it up')
    args = parser.parse_args()
    moba_master = getpass.getpass('MobaXterm master password: ')
    astra_master = getpass.getpass('New AstraTerm master password: ' if args.reset_vault else 'AstraTerm master password: ')
    if args.reset_vault and astra_master != getpass.getpass('Confirm new AstraTerm master password: '):
        raise ValueError('Мастер-пароли AstraTerm не совпадают.')
    directory = args.data_dir or Path(active_profile(str(roaming / 'AstraTerm')))
    migrate(args.source, directory, moba_master, astra_master, apply=args.apply, reset=args.reset_vault)


if __name__ == '__main__':
    try:
        main()
    except (UnicodeError, OSError):
        sys.exit('Не удалось прочитать или сохранить файлы импорта.')
    except ValueError as error:
        sys.exit(str(error))
