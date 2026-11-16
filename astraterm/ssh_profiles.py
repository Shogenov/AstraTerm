"""Per-user SSH identities and a shared OpenSSH agent (Python 3.5+).

Only public metadata is persisted.  Private keys stay in their original files;
passphrases travel to OpenSSH through a short-lived private askpass socket.
This module has no GTK dependency.  Agent and key operations belong in workers.
"""
from __future__ import unicode_literals

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import uuid

from .core import (_atomic_json, _config_lock, _file_revision, _ssh_options,
                   _port, _username, validate_host, validate_session)


MAX_PROFILES = 1000
MAX_METADATA_BYTES = 1024 * 1024
MAX_AGENT_PACKET = 1024 * 1024
_KEY_TYPES = ("ssh-", "ecdsa-", "sk-")


class SSHProfileError(ValueError):
    """A safe user-facing error which never contains passphrases/CLI output."""


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SSHProfileError("Укажите файл SSH-ключа.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SSHProfileError("Недопустимый путь к SSH-ключу.")
    return os.path.abspath(os.path.expanduser(value))


def _profile(value, resolve_path=True):
    if not isinstance(value, dict):
        raise SSHProfileError("Некорректный SSH-профиль.")
    identifier = value.get("id") or uuid.uuid4().hex
    name = value.get("name", "")
    fingerprint = value.get("fingerprint", "")
    if (not isinstance(identifier, str) or identifier == "system" or
            not re.match(r"^[A-Za-z0-9_-]{1,80}$", identifier)):
        raise SSHProfileError("Некорректный идентификатор SSH-профиля.")
    if (not isinstance(name, str) or len(name) > 200 or
            any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise SSHProfileError("Некорректное название SSH-профиля.")
    if (not isinstance(fingerprint, str) or
            (fingerprint and not re.match(r"^SHA256:[A-Za-z0-9+/]{43}$", fingerprint))):
        raise SSHProfileError("Некорректный отпечаток SSH-ключа.")
    key_file = value.get("key_file", "")
    resolved_key = _path(key_file)
    key_file = resolved_key if resolve_path else key_file
    return {"id": identifier, "name": name.strip() or os.path.basename(key_file),
            "key_file": key_file, "fingerprint": fingerprint}


def discover_keys(directory=None):
    """List candidate filenames; never read private-key contents for discovery.

    Arbitrary private-key filenames are supported.  Selection is validated by
    OpenSSH when the user unlocks it, rather than by parsing private material.
    """
    directory = os.path.abspath(os.path.expanduser(directory or "~/.ssh"))
    ignored = frozenset(("config", "known_hosts", "known_hosts2", "authorized_keys",
                         "authorized_keys2", "environment", "rc"))
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    result = []
    for name in names[:10000]:
        if (name in ignored or name.startswith(".") or name.endswith(
                (".pub", ".old", ".bak", ".lock", ".swp", "~"))):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.R_OK):
            result.append(path)
    return result


class ProfileStore(object):
    """Separate local metadata; session import/export never includes this file."""
    def __init__(self, path=None):
        if path is None:
            from .profile_location import active_profile, profile_root
            path = os.path.join(active_profile(profile_root()), "ssh-profiles.json")
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        self.profiles = []
        self.default_id = ""
        self.configured = False
        self._revision = None
        self.load()

    @staticmethod
    def _document(value, resolve_paths=True):
        if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
            raise SSHProfileError("Неподдерживаемый формат SSH-профилей.")
        profiles = value.get("profiles", [])
        configured = value.get("configured", False)
        default = value.get("default_id", "")
        if (not isinstance(profiles, list) or len(profiles) > MAX_PROFILES or
                not isinstance(configured, bool) or not isinstance(default, str)):
            raise SSHProfileError("Некорректный список SSH-профилей.")
        profiles = [_profile(item, resolve_paths) for item in profiles]
        identifiers = [item["id"] for item in profiles]
        if len(set(identifiers)) != len(identifiers) or (default and default not in identifiers):
            raise SSHProfileError("Некорректный основной SSH-профиль.")
        return {"schema_version": 1, "profiles": profiles,
                "default_id": default, "configured": configured}

    def load(self):
        try:
            with open(self.path, "rb") as stream:
                data = stream.read(MAX_METADATA_BYTES + 1)
        except FileNotFoundError:
            self.profiles, self.default_id, self.configured = [], "", False
            self._revision = None
            return self.profiles
        if len(data) > MAX_METADATA_BYTES:
            raise SSHProfileError("Файл SSH-профилей слишком большой.")
        try:
            document = self._document(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeError):
            raise SSHProfileError("Не удалось прочитать SSH-профили.") from None
        self.profiles = document["profiles"]
        self.default_id, self.configured = document["default_id"], document["configured"]
        self._revision = hashlib.sha256(data).hexdigest()
        return self.profiles

    def save(self):
        document = self._document({"schema_version": 1, "profiles": self.profiles,
                                   "default_id": self.default_id, "configured": self.configured})
        with _config_lock(self.path):
            if _file_revision(self.path) != self._revision:
                raise SSHProfileError("SSH-профили изменены другим окном. Перечитайте настройки.")
            _atomic_json(self.path, document)
            self._revision = _file_revision(self.path)
        self.profiles = document["profiles"]

    def get(self, profile_id=None):
        identifier = self.default_id if profile_id is None else profile_id
        return next((dict(item) for item in self.profiles if item["id"] == identifier), None)

    def _change(self, callback):
        previous = (list(self.profiles), self.default_id, self.configured)
        try:
            result = callback()
            self.save()
            return result
        except Exception:
            self.profiles, self.default_id, self.configured = previous
            raise

    def upsert(self, profile, make_default=False, default_if_first=True):
        profile = _profile(profile)
        def change():
            first_profile = not self.profiles
            self.profiles = list(self.profiles)
            for index, existing in enumerate(self.profiles):
                if existing["id"] == profile["id"]:
                    if (existing["key_file"] != profile["key_file"] and
                            existing.get("fingerprint") == profile["fingerprint"]):
                        profile["fingerprint"] = ""
                    self.profiles[index] = profile
                    break
            else:
                self.profiles.append(profile)
            self.configured = True
            if make_default or (default_if_first and not self.default_id and first_profile):
                self.default_id = profile["id"]
            return dict(profile)
        return self._change(change)

    def delete(self, profile_id):
        def change():
            original = len(self.profiles)
            self.profiles = [item for item in self.profiles if item["id"] != profile_id]
            if self.default_id == profile_id:
                self.default_id = self.profiles[0]["id"] if self.profiles else ""
            return len(self.profiles) != original
        return self._change(change)

    def set_default(self, profile_id):
        if profile_id and self.get(profile_id) is None:
            raise SSHProfileError("SSH-профиль не найден.")
        def change():
            self.default_id, self.configured = profile_id, True
        self._change(change)

    def skip_setup(self):
        def change():
            self.configured = True
        self._change(change)


def _public_identity(value):
    """Validate a public descriptor, with bounded decoding and type agreement."""
    if not isinstance(value, str) or len(value) > MAX_AGENT_PACKET:
        return None
    parts = value.strip().split()
    if len(parts) < 2 or not parts[0].startswith(_KEY_TYPES):
        return None
    try:
        blob = base64.b64decode(parts[1].encode("ascii"), validate=True)
        if len(blob) < 8 or len(blob) > MAX_AGENT_PACKET:
            return None
        length = struct.unpack(">I", blob[:4])[0]
        kind = blob[4:4 + length].decode("ascii")
        if length > 200 or kind != parts[0]:
            return None
    except (ValueError, UnicodeError, struct.error):
        return None
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return {"type": kind, "blob": base64.b64encode(blob).decode("ascii"),
            "fingerprint": "SHA256:" + digest, "comment": " ".join(parts[2:])}


def public_key_blob(path):
    """Read a public companion only; never decrypt/read the private key."""
    path = _path(path)
    candidate = path if path.endswith(".pub") else path + ".pub"
    try:
        with open(candidate, "rb") as stream:
            data = stream.read(MAX_AGENT_PACKET + 1)
        identity = _public_identity(data.decode("utf-8"))
    except (OSError, UnicodeError):
        identity = None
    return identity["blob"] if identity else ""


def read_public_key(key_file):
    filename = _path(key_file) + ".pub"
    if not os.path.isfile(filename):
        raise SSHProfileError("Файл публичного SSH-ключа .pub не найден.")
    with open(filename, "rb") as stream:
        text = stream.read(MAX_AGENT_PACKET + 1).decode("utf-8").strip()
    if "\n" in text or "\r" in text or not _public_identity(text):
        raise SSHProfileError("Некорректный файл публичного SSH-ключа.")
    return text


def generate_profile(store, name, filename, algorithm, passphrase, directory=None):
    if (not isinstance(filename, str) or not re.fullmatch(r"id_[A-Za-z0-9_-]{1,75}", filename) or
            algorithm not in ("ed25519", "rsa")):
        raise SSHProfileError("Укажите тип ключа и имя файла вида id_astraterm.")
    if (not isinstance(passphrase, str) or len(passphrase.encode("utf-8")) > 1024 or
            any(char in passphrase for char in "\x00\r\n")):
        raise SSHProfileError("Недопустимая парольная фраза SSH-ключа.")
    if not isinstance(name, str) or not name.strip() or len(name.encode("utf-16-le")) > 320:
        raise SSHProfileError("Укажите название SSH-профиля длиной до 160 символов.")
    directory = os.path.expanduser(directory or "~/.ssh")
    destination = os.path.join(directory, filename)
    profile = _profile({"name": name, "key_file": destination})
    os.makedirs(directory, mode=0o700, exist_ok=True)
    info = os.stat(directory)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SSHProfileError("Каталог ~/.ssh должен принадлежать вам и быть закрыт для записи другими пользователями.")
    linked = []
    saved = False
    try:
        with tempfile.TemporaryDirectory(prefix=".astraterm-key-", dir=directory) as staging:
            source = os.path.join(staging, "key")
            command = ["/usr/bin/ssh-keygen", "-q", "-t", algorithm, "-o", "-a", "32",
                       "-C", filename, "-f", source]
            if algorithm == "rsa":
                command += ["-b", "3072"]
            if any(os.path.lexists(destination + suffix) for suffix in ("", ".pub")):
                raise FileExistsError()
            environment = dict(os.environ)
            # OpenSSH 7.4 prefers askpass when DISPLAY is set without a tty.
            for key in ("DISPLAY", "WAYLAND_DISPLAY", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
                environment.pop(key, None)
            subprocess.run(command, input=((passphrase + "\n") * 2).encode("utf-8"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
                           start_new_session=True, check=True, timeout=30)
            public = read_public_key(source)
            profile["fingerprint"] = _public_identity(public)["fingerprint"]
            for suffix in ("", ".pub"):
                target = destination + suffix
                os.link(source + suffix, target)
                linked.append(target)
            result = store.upsert(profile, default_if_first=False)
            saved = True
            return result
    except FileExistsError:
        raise SSHProfileError("Такой ключ или файл .pub уже существует. Выберите другое имя файла.") from None
    except (OSError, subprocess.SubprocessError):
        raise SSHProfileError("Не удалось создать SSH-ключ в ~/.ssh.") from None
    finally:
        if not saved:
            for target in reversed(linked):
                try:
                    os.unlink(target)
                except FileNotFoundError:
                    pass


def _verify_terminal_config(output, public_file):
    """Validate resolved OpenSSH policy, without altering or echoing it."""
    expected = os.path.normcase(_path(public_file))
    try:
        if len(output) > MAX_AGENT_PACKET:
            raise ValueError()
        text = output.decode("utf-8") if isinstance(output, bytes) else output
        identities, routes, certificates, aliases = [], [], [], []
        endpoint = {}
        for line in text.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            keyword, value = parts[0].lower(), parts[1].strip()
            if keyword == "identityfile":
                identities.append(os.path.normcase(_path(value)))
            elif keyword in ("proxyjump", "proxycommand") and value.lower() not in ("none", ""):
                routes.append(value)
            elif keyword == "certificatefile" and value.lower() not in ("none", ""):
                certificates.append(value)
            elif keyword == "hostkeyalias" and value.lower() not in ("none", ""):
                aliases.append(value)
            elif keyword in ("hostname", "port", "user"):
                endpoint[keyword] = value
    except (ValueError, TypeError, UnicodeError):
        raise SSHProfileError("Не удалось проверить настройки OpenSSH для выбранного ключа.") from None
    if routes:
        raise SSHProfileError("Профиль SSH-ключа пока не поддерживает ProxyJump/ProxyCommand. "
                              "Для этого подключения выберите «Системные ключи / свой файл», "
                              "чтобы сохранить настройки шлюза OpenSSH.")
    if not identities or any(value != expected for value in identities) or certificates:
        raise SSHProfileError("Настройки OpenSSH добавляют другие ключи или сертификаты. "
                              "Для этого подключения выберите «Системные ключи / свой файл» "
                              "или настройте единственный ключ выбранного профиля.")
    if aliases:
        raise SSHProfileError("Профиль SSH-ключа пока не поддерживает HostKeyAlias для SFTP. "
                              "Для этого подключения выберите «Системные ключи / свой файл».")
    try:
        result = {"host": validate_host(endpoint.get("hostname", "")),
                  "port": _port(endpoint.get("port", "")),
                  "username": _username(endpoint.get("user", ""))}
        if not result["username"]:
            raise ValueError()
    except ValueError:
        raise SSHProfileError("Не удалось определить сервер и пользователя OpenSSH для SFTP.") from None
    return result


def verify_terminal_identity(profile, public_file):
    """Fail closed when user/system config conflicts with a named key profile.

    ``ssh -G`` resolves the normal OpenSSH configuration without establishing a
    connection.  We preserve that configuration; in particular no mandatory
    proxy route is silently discarded in order to enforce a selected identity.
    Returns the validated resolved host/port/username for SFTP; the caller must
    retain the original terminal hostname so its OpenSSH Host rules still apply.
    Run this function in a worker, before opening an SSH or Mosh terminal.
    """
    session = validate_session(profile)
    if session["protocol"] not in ("ssh", "mosh"):
        return True
    options = _ssh_options(session)
    try:
        output = SSHAgent._run([options[0], "-G"] + options[1:] + ["--", session["host"]],
                               dict(os.environ), timeout=15)
    except SSHProfileError:
        raise SSHProfileError("Не удалось проверить настройки OpenSSH для выбранного ключа.") from None
    return _verify_terminal_config(output, public_file)


def _private_directory(path):
    if not os.path.exists(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or
            (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise SSHProfileError("Каталог SSH-агента должен принадлежать текущему пользователю.")
    os.chmod(path, 0o700)
    return path


def _default_runtime():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        try:
            info = os.stat(runtime)
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
                return os.path.join(runtime, "astraterm")
        except (OSError, AttributeError):
            pass
    return os.path.expanduser("~/.cache/astraterm/run")


def _recv_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        part = stream.recv(count - len(data))
        if not part:
            raise SSHProfileError("SSH-агент завершил соединение.")
        data.extend(part)
    return bytes(data)


def _read_identities(path):
    """A read-only SSH agent protocol request; None means unavailable."""
    if not path or not hasattr(socket, "AF_UNIX"):
        return None
    try:
        info = os.stat(path)
        if not stat.S_ISSOCK(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            return None
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stream.settimeout(3)
            stream.connect(path)
            stream.sendall(b"\x00\x00\x00\x01\x0b")
            size = struct.unpack(">I", _recv_exact(stream, 4))[0]
            if size < 5 or size > MAX_AGENT_PACKET:
                return None
            packet = _recv_exact(stream, size)
            if packet[0] != 12:
                return None
            count, offset = struct.unpack(">I", packet[1:5])[0], 5
            if count > 4096:
                return None
            result = []
            for unused in range(count):
                fields = []
                for unused_field in range(2):
                    length = struct.unpack(">I", packet[offset:offset + 4])[0]
                    offset += 4
                    if length > MAX_AGENT_PACKET or offset + length > len(packet):
                        return None
                    fields.append(packet[offset:offset + length])
                    offset += length
                blob, comment = fields
                kind_length = struct.unpack(">I", blob[:4])[0]
                kind = blob[4:4 + kind_length].decode("ascii")
                identity = _public_identity(kind + " " + base64.b64encode(blob).decode("ascii"))
                if identity is not None:
                    identity["comment"] = comment.decode("utf-8", "replace")
                    result.append(identity)
            return result if offset == len(packet) else None
        finally:
            stream.close()
    except (OSError, ValueError, UnicodeError, struct.error, SSHProfileError):
        return None


class _AskpassBroker(object):
    """Short-lived private IPC; the passphrase never appears in the environment."""
    def __init__(self, runtime_dir, passphrase):
        if (not isinstance(passphrase, str) or len(passphrase) > 65536 or
                any(char in passphrase for char in ("\x00", "\r", "\n"))):
            raise SSHProfileError("Неподдерживаемый формат passphrase.")
        self.directory = tempfile.mkdtemp(prefix="ask-", dir=runtime_dir)
        os.chmod(self.directory, 0o700)
        self.path = os.path.join(self.directory, "socket")
        self.secret = passphrase.encode("utf-8")
        self._stop = threading.Event()
        self._stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._stream.bind(self.path)
            os.chmod(self.path, 0o600)
            self._stream.listen(4)
            self._stream.settimeout(0.2)
        except Exception:
            self._stream.close()
            shutil.rmtree(self.directory)
            raise SSHProfileError("Не удалось подготовить ввод passphrase.") from None
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    def _serve(self):
        attempts = 0
        while not self._stop.is_set() and attempts < 6:
            try:
                client, unused = self._stream.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.settimeout(3)
                if hasattr(socket, "SO_PEERCRED"):
                    pid, uid, gid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                attempts += 1
                client.sendall(self.secret + b"\n")
            except OSError:
                pass
            finally:
                client.close()

    def close(self):
        self._stop.set()
        self._stream.close()
        self._thread.join(1)
        self.secret = b""
        shutil.rmtree(self.directory)


class SSHAgent(object):
    def __init__(self, runtime_dir=None, helper_path=None):
        self.runtime_dir = os.path.abspath(os.path.expanduser(runtime_dir or _default_runtime()))
        self.helper_path = helper_path or os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                                     "scripts", "astraterm-askpass")
        self.socket_path = None
        self.managed_pid = None
        self._lock = threading.RLock()

    def ensure(self):
        if os.name != "posix":
            raise SSHProfileError("SSH-агент доступен в Linux.")
        with self._lock:
            for candidate in (self.socket_path, os.environ.get("SSH_AUTH_SOCK")):
                if _read_identities(candidate) is not None:
                    self.socket_path = candidate
                    return candidate
            _private_directory(self.runtime_dir)
            path = os.path.join(self.runtime_dir, "agent.sock")
            if len(path.encode("utf-8")) > 100:
                raise SSHProfileError("Путь каталога SSH-агента слишком длинный.")
            with _config_lock(os.path.join(self.runtime_dir, "agent.json")):
                if _read_identities(path) is None:
                    try:
                        info = os.lstat(path)
                    except FileNotFoundError:
                        pass
                    else:
                        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                            raise SSHProfileError("Недопустимый файл сокета SSH-агента.")
                        os.unlink(path)  # Only our stale, user-owned socket.
                    output = self._run(["ssh-agent", "-s", "-a", path], os.environ.copy(), 10)
                    match = re.search(br"SSH_AGENT_PID=([0-9]+)", output)
                    self.managed_pid = int(match.group(1)) if match else None
                    if _read_identities(path) is None:
                        raise SSHProfileError("Не удалось запустить SSH-агент.")
                    _atomic_json(os.path.join(self.runtime_dir, "agent.json"),
                                 {"socket": path, "pid": self.managed_pid})
                self.socket_path = path
                return path

    def environment(self):
        return {"SSH_AUTH_SOCK": self.ensure()}

    def identities(self):
        result = _read_identities(self.ensure())
        if result is None:
            raise SSHProfileError("SSH-агент недоступен.")
        return result

    def loaded_identity(self, key_path, fingerprint=""):
        blob = public_key_blob(key_path)
        for identity in self.identities():
            if ((blob and identity["blob"] == blob) or
                    (not blob and fingerprint and identity["fingerprint"] == fingerprint)):
                return identity
        return None

    def key_loaded(self, key_path, fingerprint=""):
        return self.loaded_identity(key_path, fingerprint) is not None

    def public_identity_file(self, key_path, fingerprint=""):
        identity = self.loaded_identity(key_path, fingerprint)
        if identity is None:
            raise SSHProfileError("Сначала разблокируйте SSH-ключ.")
        _private_directory(self.runtime_dir)
        name = hashlib.sha256(identity["blob"].encode("ascii")).hexdigest() + ".pub"
        path = os.path.join(self.runtime_dir, name)
        data = (identity["type"] + " " + identity["blob"] + "\n").encode("ascii")
        descriptor, temporary = tempfile.mkstemp(prefix=".identity-", dir=self.runtime_dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path

    @staticmethod
    def _run(argv, environment, timeout=35):
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=environment, close_fds=True,
                                       start_new_session=True)
            try:
                output, unused_error = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise SSHProfileError("Превышено время ожидания SSH-операции.") from None
            if process.returncode:
                raise SSHProfileError("Не удалось разблокировать SSH-ключ. Проверьте ключ и passphrase.")
            return output
        except OSError:
            raise SSHProfileError("Не найдены совместимые инструменты OpenSSH.") from None

    def unlock(self, key_path, passphrase=""):
        key_path = _path(key_path)
        try:
            info = os.stat(key_path)
        except OSError:
            raise SSHProfileError("Файл SSH-ключа недоступен.") from None
        if (not stat.S_ISREG(info.st_mode) or not os.access(key_path, os.R_OK) or
                (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise SSHProfileError("SSH-ключ должен принадлежать текущему пользователю.")
        if not os.path.isfile(self.helper_path) or not os.access(self.helper_path, os.X_OK):
            raise SSHProfileError("Не установлен помощник разблокировки SSH-ключа.")
        with self._lock:
            environment = dict(os.environ)
            environment.update(self.environment())
            _private_directory(self.runtime_dir)
            broker = _AskpassBroker(self.runtime_dir, passphrase)
            environment["SSH_ASKPASS"] = os.path.abspath(self.helper_path)
            environment["ASTRATERM_ASKPASS_SOCKET"] = broker.path  # IPC address, never a secret.
            environment["DISPLAY"] = environment.get("DISPLAY") or ":0"
            environment["SSH_ASKPASS_REQUIRE"] = "force"  # Old OpenSSH uses no-TTY + DISPLAY.
            try:
                # Derive public identity even for encrypted PEM keys without a
                # .pub companion, using the same private askpass IPC.
                output = self._run(["ssh-keygen", "-y", "-f", key_path], environment)
                try:
                    identity = _public_identity(output.decode("utf-8"))
                except UnicodeError:
                    identity = None
                if identity is None:
                    raise SSHProfileError("Формат SSH-ключа не поддерживается OpenSSH.")
                self._run(["ssh-add", key_path], environment)
                loaded = self.loaded_identity(key_path, identity["fingerprint"])
                if loaded is None:
                    raise SSHProfileError("SSH-агент не подтвердил загрузку выбранного ключа.")
                return loaded
            finally:
                broker.close()
