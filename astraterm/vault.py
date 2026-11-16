"""Password vault using library AES-GCM and PBKDF2; no secrets in SessionStore."""
from __future__ import unicode_literals
import base64
import hashlib
import json
import os
import threading
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from ._shared import VAULT_PARAMETERS as PARAMETERS
from .core import _atomic_json, _config_lock, _file_revision, _identifier, _single_line, _username

AAD = b'AstraTerm credential vault v1'


def _secret(value):
    if not isinstance(value, str) or len(value) > 65536 or any(c in value for c in ('\x00', '\r', '\n')):
        raise ValueError('Неподдерживаемый формат секрета.')
    return value


def validate_credentials(records):
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError('Недопустимое содержимое хранилища.')
    result = []; seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Недопустимая запись хранилища.')
        if not record.get('id'):raise ValueError('Недопустимая запись хранилища.')
        item = {'id': _identifier(record.get('id')), 'name': _single_line(record.get('name'), 'Профиль', 200),
                'username': _username(record.get('username', '')), 'password': _secret(record.get('password', ''))}
        if 'protocol' in record:
            if record['protocol'] not in ('ssh','rdp','both'):raise ValueError('Недопустимый тип учётных данных.')
            item['protocol']=record['protocol']
        if item['id'] in seen or not item['name']:
            raise ValueError('Повторяющийся или пустой профиль хранилища.')
        seen.add(item['id']); result.append(item)
    return result


def _decode(value, length=None):
    if not isinstance(value, str) or len(value) > PARAMETERS['maximum_bytes'] * 2:
        raise ValueError('Некорректный формат хранилища.')
    data = base64.b64decode(value.encode('ascii'), validate=True)
    if length is not None and len(data) != length:
        raise ValueError('Некорректный формат хранилища.')
    return data


def _b64(value):
    return base64.b64encode(value).decode('ascii')


def _validate_master(master, new=False):
    if not isinstance(master, str) or not master or len(master) > 4096 or '\x00' in master:
        raise ValueError('Введите мастер-пароль.')
    if new and len(master) < 10:
        raise ValueError('Мастер-пароль должен содержать не менее 10 символов.')


class CredentialVault(object):
    def __init__(self, path):
        self.path = os.path.abspath(str(path))
        self._key = None; self._salt = None; self._records = None
        self._revision = None  # A corrupt vault must not prevent unrelated sessions opening.
        self._guard = threading.RLock()
        self._epoch = 0

    @property
    def exists(self):
        return os.path.isfile(self.path)

    @property
    def unlocked(self):
        return self._key is not None

    def lock(self):
        with self._guard:
            self._epoch += 1
            self._key = self._salt = self._records = None

    def reload_if_changed(self):
        with self._guard:
            if self.unlocked and _file_revision(self.path) != self._revision:
                self.lock()

    def unlock(self, master, create=False):
        _validate_master(master)
        with self._guard:
            epoch = self._epoch
            revision = _file_revision(self.path)
            if revision is None:
                if not create:
                    raise ValueError('Хранилище ещё не создано.')
                _validate_master(master, new=True)
                salt = os.urandom(PARAMETERS['salt_bytes']); iterations = PARAMETERS['iterations']; records = []
            else:
                try:
                    if os.path.getsize(self.path) > PARAMETERS['maximum_bytes']:
                        raise ValueError()
                    with open(self.path, encoding='utf-8-sig') as stream:
                        document = json.load(stream)
                    if (document['version'] != PARAMETERS['version'] or document['cipher'] != PARAMETERS['cipher'] or
                            document['kdf'] != PARAMETERS['kdf'] or document['iterations'] != PARAMETERS['iterations']):
                        raise ValueError()
                    iterations = document['iterations']; salt = _decode(document['salt'], PARAMETERS['salt_bytes'])
                except Exception:
                    raise ValueError('Формат хранилища повреждён или не поддерживается.') from None
        # PBKDF2 runs in a worker in GTK. Locking during a slow unlock cancels it.
        key = hashlib.pbkdf2_hmac('sha256', master.encode('utf-8'), salt, iterations, dklen=32)
        if revision is not None:
            try:
                decryptor = Cipher(algorithms.AES(key), modes.GCM(_decode(document['nonce'], PARAMETERS['nonce_bytes']),
                                   _decode(document['tag'], PARAMETERS['tag_bytes'])), backend=default_backend()).decryptor()
                decryptor.authenticate_additional_data(AAD)
                raw = decryptor.update(_decode(document['ciphertext'])) + decryptor.finalize()
            except Exception:
                raise ValueError('Неверный мастер-пароль или повреждено хранилище.') from None
            try:
                records = validate_credentials(json.loads(raw.decode('utf-8'))['credentials'])
            except (KeyError, TypeError, ValueError):
                raise ValueError('Зашифрованный файл открыт, но содержит неподдерживаемые профили AstraTerm.') from None
        with self._guard:
            if self._epoch != epoch or _file_revision(self.path) != revision:
                raise ValueError('Хранилище изменилось или было заблокировано. Повторите разблокировку.')
            self._key, self._salt, self._records, self._revision = key, salt, records, revision
        return self.metadata()

    def change_master(self, current, master):
        _validate_master(master, new=True)
        with self._guard:
            epoch = self._epoch
        previous = CredentialVault(self.path)
        try:
            previous.unlock(current)
            self._replace_master(master, previous._revision, previous._records, epoch)
        finally:
            previous.lock()

    def reset_master(self, master):
        _validate_master(master, new=True)
        with self._guard:
            epoch = self._epoch
            revision = _file_revision(self.path)
        if revision is None:
            raise ValueError('Хранилище ещё не создано.')
        self._replace_master(master, revision, [], epoch)

    def _replace_master(self, master, revision, records, epoch):
        salt = os.urandom(PARAMETERS['salt_bytes'])
        key = hashlib.pbkdf2_hmac('sha256', master.encode('utf-8'), salt, PARAMETERS['iterations'], dklen=32)
        with self._guard:
            if self._epoch != epoch:
                raise ValueError('Состояние хранилища изменилось. Повторите операцию.')
            new_revision = self._write(records, key, salt, revision)
            self._epoch += 1
            self._key, self._salt, self._records, self._revision = key, salt, records, new_revision

    def metadata(self):
        with self._guard:
            return [dict({'id': item['id'], 'name': item['name'], 'username': item['username']}, **({'protocol':item['protocol']} if 'protocol' in item else {}))
                    for item in self._records or []]

    def get(self, identifier, protocol=None):
        with self._guard:
            if not self.unlocked:
                raise ValueError('Разблокируйте хранилище учётных данных.')
            record = next((item for item in self._records if item['id'] == identifier), None)
            if record is None:
                raise ValueError('Пароль этого профиля не импортирован. Импортируйте файл учётных данных.')
            if protocol and record.get('protocol','both') not in ('both',protocol):
                raise ValueError('Профиль учётных данных не подходит для этого протокола.')
            return dict(record)

    def merge(self, records, _remove=None):
        checked = validate_credentials(records)
        with self._guard:
            if not self.unlocked:
                raise ValueError('Разблокируйте хранилище учётных данных.')
            merged = {item['id']: dict(item) for item in self._records}
            if _remove is not None:merged.pop(_remove,None)
            merged.update((item['id'], item) for item in checked)
            updated = list(merged.values())
            self._revision = self._write(updated, self._key, self._salt, self._revision)
            self._records = updated
        return len(checked)

    def _write(self, records, key, salt, revision):
        payload = json.dumps({'credentials': records}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(payload) > PARAMETERS['maximum_bytes'] // 2:
            raise ValueError('Хранилище превышает допустимый размер.')
        nonce = os.urandom(PARAMETERS['nonce_bytes'])
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce), backend=default_backend()).encryptor()
        encryptor.authenticate_additional_data(AAD)
        ciphertext = encryptor.update(payload) + encryptor.finalize()
        document = {field: PARAMETERS[field] for field in ('version', 'cipher', 'kdf', 'iterations')}
        document.update(salt=_b64(salt), nonce=_b64(nonce), tag=_b64(encryptor.tag), ciphertext=_b64(ciphertext))
        with _config_lock(self.path):
            if _file_revision(self.path) != revision:
                raise ValueError('Хранилище изменено другим окном. Заблокируйте и откройте его снова.')
            _atomic_json(self.path, document)
            return _file_revision(self.path)

    def remove(self, identifier):
        self.get(identifier)
        self.merge([], _remove=identifier)
