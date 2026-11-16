"""Storage and safe process arguments for AstraTerm (Python 3.5+).

This module deliberately has no GUI dependencies. Passwords and private key
contents never belong in the session database or in process arguments.
"""
from __future__ import unicode_literals

import codecs
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import tempfile
import uuid
from ._shared import PROTOCOLS, DEFAULT_PORTS, GROUP_SSH_FIELDS, SESSION_SCHEMA_VERSION


MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 10000


def _text(value, field, maximum=4096):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Поле «{}» должно быть строкой".format(field))
    if "\x00" in value or len(value) > maximum:
        raise ValueError("Недопустимое значение поля «{}»".format(field))
    return value


def _single_line(value, field, maximum=4096):
    value = _text(value, field, maximum)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Поле «{}» не должно содержать управляющие символы".format(field))
    return value.strip()


def _port(value, field="Порт", allow_zero=False):
    if isinstance(value, bool):
        raise ValueError("{} должен быть числом от 1 до 65535".format(field))
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("{} должен быть числом от 1 до 65535".format(field))
    if isinstance(value, float) and value != result:
        raise ValueError("{} должен быть целым числом".format(field))
    if result < (0 if allow_zero else 1) or result > 65535:
        raise ValueError("{} должен быть числом от 1 до 65535".format(field))
    return result


def _boolean(value, field):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError("Поле «{}» должно быть логическим значением".format(field))


def _identifier(value=None):
    value = value or uuid.uuid4().hex
    if not isinstance(value, str) or not re.match(r"^[a-zA-Z0-9_-]{1,80}$", value):
        raise ValueError("Недопустимый идентификатор записи")
    return value


def validate_host(value, field="Сервер", required=True, allow_wildcard=False):
    value = _single_line(value, field, 255)
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value and not required:
        return ""
    if value == "*" and allow_wildcard:
        return value
    if (not value or value.startswith("-") or
            not re.match(r"^[A-Za-z0-9_.:%-]+$", value)):
        raise ValueError("Недопустимое имя в поле «{}»".format(field))
    if ":" in value:
        address = value.split("%", 1)[0]
        try:
            if ipaddress.ip_address(address).version != 6:
                raise ValueError()
        except ValueError:
            raise ValueError("В поле «{}» укажите имя сервера или IPv6-адрес без номера порта".format(field))
    return value


def _username(value):
    value = _single_line(value, "Пользователь", 255)
    if value and (value.startswith("-") or
                  not re.match(r"^[A-Za-z0-9_.@\\-]+$", value)):
        raise ValueError("Недопустимое имя пользователя")
    return value


def validate_jump_host(value):
    value = _single_line(value, "SSH-шлюз", 2048)
    if not value:
        return ""
    for entry in value.split(","):
        if not entry:
            raise ValueError("Пустой SSH-шлюз в цепочке")
        target = entry
        if "@" in target:
            user, target = target.rsplit("@", 1)
            if not _username(user) or "@" in user:
                raise ValueError("Недопустимый пользователь SSH-шлюза")
        if target.startswith("["):
            match = re.match(r"^\[([^\]]+)\](?::([0-9]+))?$", target)
            if not match:
                raise ValueError("IPv6-адрес SSH-шлюза должен быть в квадратных скобках")
            validate_host(match.group(1), "SSH-шлюз")
            if match.group(2):
                _port(match.group(2), "Порт SSH-шлюза")
        elif ":" in target:
            host, port = target.rsplit(":", 1)
            if ":" in host:
                raise ValueError("IPv6-адрес SSH-шлюза должен быть в квадратных скобках")
            validate_host(host, "SSH-шлюз")
            _port(port, "Порт SSH-шлюза")
        else:
            validate_host(target, "SSH-шлюз")
    return value


def default_session(protocol="ssh"):
    if protocol not in PROTOCOLS:
        raise ValueError("Неизвестный протокол: {}".format(protocol))
    return {
        "id": uuid.uuid4().hex, "name": "", "group": "", "inherit_group_ssh": True, "protocol": protocol,
        "host": "", "port": DEFAULT_PORTS[protocol], "username": "",
        "key_file": "", "identity_profile": "", "credential_profile": "", "credential_label": "",
        "import_warning": "", "x11": False, "compression": True,
        "jump_host": "", "remote_command": "", "encoding": "UTF-8",
        "startup_directory": "", "serial_device": "/dev/ttyUSB0", "baud": 115200,
        "rdp_domain": "", "rdp_resolution": "1280x800",
    }


def validate_session(session):
    if not isinstance(session, dict):
        raise ValueError("Сессия должна быть объектом")
    protocol = session.get("protocol", "ssh")
    result = default_session(protocol)
    # Whitelisting intentionally discards passwords and unknown imported fields.
    for name in result:
        if name in session:
            result[name] = session[name]
    result["id"] = _identifier(result["id"])
    result["name"] = _single_line(result["name"], "Название", 200)
    result["group"] = normalize_group_name(result["group"], allow_empty=True)
    result["inherit_group_ssh"] = _boolean(result["inherit_group_ssh"], "Настройки SSH группы")
    remote = protocol not in ("local", "serial")
    result["host"] = validate_host(result["host"], required=remote)
    result["port"] = _port(result["port"], allow_zero=not remote)
    result["username"] = _username(result["username"])
    result["key_file"] = _single_line(result["key_file"], "Файл ключа")
    identity = _single_line(result["identity_profile"], "Профиль SSH", 80)
    result["identity_profile"] = _identifier(identity) if identity else ""
    credential = _single_line(result["credential_profile"], "Учётные данные", 80)
    result["credential_profile"] = _identifier(credential) if credential else ""
    result["credential_label"] = _single_line(result["credential_label"], "Название учётных данных", 200)
    result["import_warning"] = _single_line(result["import_warning"], "Ограничения импорта", 2000)
    result["x11"] = _boolean(result["x11"], "X11")
    result["compression"] = _boolean(result["compression"], "Сжатие")
    result["jump_host"] = validate_jump_host(result["jump_host"])
    result["remote_command"] = _text(result["remote_command"], "Удалённая команда", 65536)
    result["startup_directory"] = _single_line(result["startup_directory"], "Начальный каталог")
    result["serial_device"] = _single_line(result["serial_device"], "Последовательный порт")
    if protocol == "serial" and not result["serial_device"].startswith("/dev/"):
        raise ValueError("Укажите последовательное устройство в /dev/")
    try:
        baud = int(result["baud"])
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Недопустимая скорость последовательного порта")
    if baud < 50 or baud > 4000000:
        raise ValueError("Скорость последовательного порта должна быть от 50 до 4000000")
    result["baud"] = baud
    result["encoding"] = _single_line(result["encoding"], "Кодировка", 100) or "UTF-8"
    try:
        codecs.lookup(result["encoding"])
    except LookupError:
        raise ValueError("Неизвестная кодировка: {}".format(result["encoding"]))
    result["rdp_domain"] = _single_line(result["rdp_domain"], "Домен RDP", 255)
    result["rdp_resolution"] = _single_line(result["rdp_resolution"], "Разрешение RDP", 40)
    if not re.match(r"^[0-9]{3,5}x[0-9]{3,5}$", result["rdp_resolution"]):
        raise ValueError("Разрешение RDP указывается в формате 1280x800")
    if not result["name"]:
        result["name"] = {"local": "Локальный терминал", "serial": result["serial_device"]}.get(
            protocol, result["host"])
    return result


def _ssh_options(session, tunnel=False):
    args = ["ssh", "-o", "StrictHostKeyChecking=ask", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-p", str(session["port"])]
    if session.get("username"):
        args += ["-l", session["username"]]
    if session.get("key_file"):
        args += ["-i", os.path.expanduser(session["key_file"])]
        if session.get("identity_profile") not in (None, "", "system"):
            args += ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=SSH_AUTH_SOCK",
                     "-o", "PubkeyAuthentication=yes"]
    if session.get("jump_host"):
        args += ["-J", session["jump_host"]]
    if session.get("compression"):
        args += ["-C"]
    if not tunnel and session.get("x11"):
        args += ["-X"]
    return args


def _bracket_host(host):
    return "[{}]".format(host) if ":" in host else host


def session_argv(session):
    """Return process arguments, never a shell command string.

    SSH remote_command is an explicitly requested command interpreted by the
    remote login shell; it is never interpreted by a local shell.
    """
    session = validate_session(session)
    protocol = session["protocol"]
    host = session["host"]
    if protocol == "local":
        return ["/bin/bash", "-l"]
    if protocol == "ssh":
        args = _ssh_options(session) + ["-tt", "--", host]
        if session["remote_command"]:
            args.append(session["remote_command"])
        return args
    if protocol == "telnet":
        args = ["telnet"]
        if session["username"]:
            args += ["-l", session["username"]]
        return args + [host, str(session["port"])]
    if protocol == "mosh":
        ssh_args = _ssh_options(session)
        # --ssh takes one shell-like string; quote every individual argument.
        ssh_command = " ".join(shlex.quote(arg) for arg in ssh_args)
        target = (session["username"] + "@" if session["username"] else "") + _bracket_host(host)
        return ["mosh", "--ssh=" + ssh_command, "--", target]
    if protocol == "serial":
        if shutil.which("picocom"):
            return ["picocom", "--baud", str(session["baud"]), session["serial_device"]]
        return ["screen", session["serial_device"], str(session["baud"])]
    if protocol == "rdp":
        executable = "xfreerdp" if shutil.which("xfreerdp") else "xfreerdp3"
        args = [executable, "/v:{}:{}".format(_bracket_host(host), session["port"]),
                "/size:" + session["rdp_resolution"], "/dynamic-resolution", "+clipboard"]
        if session["username"]:
            args.append("/u:" + session["username"])
        if session["rdp_domain"]:
            args.append("/d:" + session["rdp_domain"])
        return args
    if protocol == "vnc":
        return ["vncviewer", "{}::{}".format(_bracket_host(host), session["port"])]
    raise ValueError("Неизвестный протокол")


def validate_tunnel(tunnel):
    if not isinstance(tunnel, dict):
        raise ValueError("Туннель должен быть объектом")
    kind = tunnel.get("type", "local")
    if kind not in ("local", "remote", "dynamic"):
        raise ValueError("Неизвестный тип туннеля")
    result = {
        "id": _identifier(tunnel.get("id")), "name": _single_line(tunnel.get("name", ""), "Название", 200),
        "type": kind, "host": validate_host(tunnel.get("host", "")),
        "port": _port(tunnel.get("port", 22)),
        "username": _username(tunnel.get("username", tunnel.get("user", ""))),
        "key_file": _single_line(tunnel.get("key_file", ""), "Файл ключа"),
        "jump_host": validate_jump_host(tunnel.get("jump_host", "")),
        "bind_host": validate_host(tunnel.get("bind_host", "127.0.0.1"), "Адрес прослушивания", allow_wildcard=True),
        "listen_port": _port(tunnel.get("listen_port", 8080), "Порт прослушивания"),
        "destination_host": "", "destination_port": 0,
    }
    if kind != "dynamic":
        result["destination_host"] = validate_host(tunnel.get("destination_host", "127.0.0.1"), "Сервер назначения")
        result["destination_port"] = _port(tunnel.get("destination_port", 80), "Порт назначения")
    if not result["name"]:
        result["name"] = "{} :{} → {}".format(kind, result["listen_port"], result["host"])
    return result


def tunnel_argv(tunnel):
    tunnel = validate_tunnel(tunnel)
    args = _ssh_options(tunnel, tunnel=True) + ["-N", "-T", "-o", "ExitOnForwardFailure=yes"]
    bind = "{}:{}".format(_bracket_host(tunnel["bind_host"]), tunnel["listen_port"])
    if tunnel["type"] == "dynamic":
        args += ["-D", bind]
    else:
        target = "{}:{}:{}".format(bind, _bracket_host(tunnel["destination_host"]), tunnel["destination_port"])
        args += ["-L" if tunnel["type"] == "local" else "-R", target]
    return args + ["--", tunnel["host"]]


def _normalize_macro(macro):
    if not isinstance(macro, dict):
        raise ValueError("Макрос должен быть объектом")
    return {"id": _identifier(macro.get("id")),
            "name": _single_line(macro.get("name", "Макрос"), "Название макроса", 200) or "Макрос",
            "text": _text(macro.get("text", macro.get("command", "")), "Текст макроса", 65536),
            "append_newline": _boolean(macro.get("append_newline", True), "Перевод строки")}


def normalize_group_name(value, allow_empty=False):
    """Normalize logical folder names; these are never filesystem paths."""
    value = _single_line(value, "Группа", 200)
    if not value and allow_empty:
        return ""
    parts = [part.strip() for part in value.split("/")]
    if not value or any(not part or part in (".", "..") for part in parts):
        raise ValueError("Укажите непустые названия папок через / без . и ..")
    return "/".join(parts)


def _group_id(name):
    return uuid.uuid5(uuid.NAMESPACE_URL, "astraterm-group:" + name).hex


def validate_group(group):
    if not isinstance(group, dict):
        raise ValueError("Группа должна быть объектом")
    name = normalize_group_name(group.get("name", ""))
    settings = group.get("ssh", {})
    if not isinstance(settings, dict):
        raise ValueError("Настройки SSH группы должны быть объектом")
    candidate = default_session("ssh")
    candidate["host"] = "group.invalid"
    candidate.update({key: settings[key] for key in GROUP_SSH_FIELDS if key in settings})
    checked = validate_session(candidate)
    return {"id": _identifier(group.get("id")), "name": name,
            "ssh": {key: checked[key] for key in GROUP_SSH_FIELDS if key in settings}}


def _group_prefixes(name):
    parts = name.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)] if name else []


def _in_group(name, parent, recursive=True):
    return name == parent or (recursive and name.startswith(parent + "/"))


def _complete_groups(groups, sessions):
    """Infer missing folders without replacing any explicit SSH defaults."""
    by_name = {group["name"]: group for group in groups}
    used_ids = {group["id"] for group in groups}
    if len(used_ids) != len(groups):
        raise ValueError("Повторяющиеся идентификаторы групп")
    for name in list(by_name) + [session["group"] for session in sessions if session["group"]]:
        for prefix in _group_prefixes(name):
            if prefix not in by_name:
                identifier = _group_id(prefix)
                if identifier in used_ids:
                    identifier = uuid.uuid4().hex
                by_name[prefix] = {"id": identifier, "name": prefix, "ssh": {}}
                used_ids.add(identifier)
    result = sorted(by_name.values(), key=lambda group: group["name"])
    if len(result) > MAX_ITEMS:
        raise ValueError("Слишком много групп")
    identifiers = [group["id"] for group in result]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Повторяющиеся идентификаторы групп")
    return result


def _normalize_document(document):
    if isinstance(document, list):
        document = {"sessions": document}
    if not isinstance(document, dict):
        raise ValueError("Файл сессий должен содержать JSON-объект")
    version = document.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
        raise ValueError("Неподдерживаемая версия формата: {}".format(version))
    result = {"schema_version": SESSION_SCHEMA_VERSION}
    for name, normalizer in (("sessions", validate_session), ("macros", _normalize_macro), ("tunnels", validate_tunnel)):
        values = document.get(name, [])
        if not isinstance(values, list) or len(values) > MAX_ITEMS:
            raise ValueError("Недопустимый список «{}»".format(name))
        if name == "sessions" and version == 1:
            values = [dict(value, inherit_group_ssh=value.get("inherit_group_ssh", False))
                      if isinstance(value, dict) else value for value in values]
        result[name] = [normalizer(value) for value in values]
        identifiers = [value["id"] for value in result[name]]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Повторяющиеся идентификаторы в списке «{}»".format(name))
    groups = document.get("groups", [])
    if not isinstance(groups, list) or len(groups) > MAX_ITEMS:
        raise ValueError("Недопустимый список групп")
    groups = [validate_group(group) for group in groups]
    if len({group["name"] for group in groups}) != len(groups):
        raise ValueError("Повторяющиеся названия групп")
    result["groups"] = _complete_groups(groups, result["sessions"])
    return result


def _read_document(path):
    return _document_and_revision(path)[0]


def _file_bytes(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("Файл сессий слишком большой (максимум 8 МиБ)")
    return data


def _document_and_revision(path):
    # Read the content and its revision from the same open file. Atomic rename
    # by another window can then only produce a stale (detectable) snapshot.
    data = _file_bytes(path)
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("Некорректный JSON: {}".format(exc))
    return _normalize_document(document), hashlib.sha256(data).hexdigest()


def _file_revision(path):
    try:
        return hashlib.sha256(_file_bytes(path)).hexdigest()
    except FileNotFoundError:
        return None


@contextmanager
def _config_lock(path):
    """Serialize revision check + atomic replace across application windows."""
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    lock_path = os.path.join(parent, "." + os.path.basename(path) + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            # Windows byte-range locks require a byte even in an empty file.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\x00")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _atomic_json(path, document):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".astraterm-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SessionStore(object):
    def __init__(self, path=None):
        if path is None:
            from .profile_location import active_profile, profile_root
            path = os.path.join(active_profile(profile_root()), "sessions.json")
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        self.sessions = []
        self.macros = []
        self.tunnels = []
        self.groups = []
        self._revision = None
        self.load()

    def load(self):
        try:
            document, revision = _document_and_revision(self.path)
        except FileNotFoundError:
            self.sessions, self.macros, self.tunnels, self.groups = [], [], [], []
            self._revision = None
            return self.sessions
        self.sessions = document["sessions"]
        self.macros = document["macros"]
        self.tunnels = document["tunnels"]
        self.groups = document["groups"]
        self._revision = revision
        return self.sessions

    def _document(self):
        return _normalize_document({"schema_version": SESSION_SCHEMA_VERSION, "sessions": self.sessions,
                                    "macros": self.macros, "tunnels": self.tunnels,
                                    "groups": self.groups})

    def save(self):
        document = self._document()
        with _config_lock(self.path):
            if _file_revision(self.path) != self._revision:
                raise ValueError("Настройки изменены другим окном AstraTerm. "
                                 "Перезапустите приложение или перечитайте настройки перед сохранением.")
            _atomic_json(self.path, document)
            self._revision = _file_revision(self.path)
        self.sessions, self.macros, self.tunnels = document["sessions"], document["macros"], document["tunnels"]
        self.groups = document["groups"]

    def upsert(self, session):
        session = validate_session(session)
        old = list(self.sessions)
        for index, existing in enumerate(self.sessions):
            if existing["id"] == session["id"]:
                self.sessions[index] = session
                break
        else:
            self.sessions.append(session)
        try:
            self.save()
        except Exception:
            self.sessions = old
            raise
        return session

    def delete(self, session_id):
        old = list(self.sessions)
        self.sessions = [session for session in self.sessions if session["id"] != session_id]
        try:
            self.save()
        except Exception:
            self.sessions = old
            raise
        return len(old) != len(self.sessions)

    def duplicate(self, session_id):
        source = next((item for item in self.sessions if item["id"] == session_id), None)
        if source is None:
            raise ValueError("Подключение не найдено")
        names = {item["name"] for item in self.sessions if item["group"] == source["group"]}
        base = re.sub(r" \([1-9][0-9]*\)$", "", source["name"]).rstrip()
        encoded = base.encode("utf-16-le")
        for number in range(1, len(names) + 2):
            suffix = " ({})".format(number)
            # Keep names portable to the Windows validator's UTF-16 limit.
            stem = encoded[:2 * (200 - len(suffix))].decode("utf-16-le", errors="ignore")
            name = stem.rstrip() + suffix
            if name not in names:
                return self.upsert(dict(source, id="", name=name))
        raise ValueError("Не удалось подобрать имя подключения")

    def import_file(self, path):
        try:
            raw = json.loads(_file_bytes(path).decode("utf-8"))
        except ValueError as exc:
            raise ValueError("Некорректный JSON: {}".format(exc))
        return self.import_document(raw)

    def import_document(self, raw):
        incoming = _normalize_document(raw)
        explicit_groups = ({normalize_group_name(item.get("name", "")) for item in raw.get("groups", [])}
                           if isinstance(raw, dict) else set())
        old = (self.sessions, self.macros, self.tunnels, self.groups)
        merged_state = {}
        for name in ("sessions", "macros", "tunnels"):
            merged = list(getattr(self, name))
            by_id = {value["id"]: index for index, value in enumerate(merged)}
            for value in incoming[name]:
                if value["id"] in by_id:
                    merged[by_id[value["id"]]] = value
                else:
                    by_id[value["id"]] = len(merged)
                    merged.append(value)
            merged_state[name] = merged
        groups = list(self.groups)
        by_id = {group["id"]: index for index, group in enumerate(groups)}
        by_name = {group["name"]: index for index, group in enumerate(groups)}
        for group in incoming["groups"]:
            if group["name"] not in explicit_groups:
                if group["name"] in by_name:
                    continue  # An inferred folder must not erase saved defaults.
                if group["id"] in by_id:
                    # A renamed inferred folder may still have the deterministic
                    # ID of this name. Legacy import carries no rename intent.
                    group = dict(group, id=uuid.uuid4().hex)
            index = by_id.get(group["id"], by_name.get(group["name"]))
            if index is None:
                index = len(groups)
                groups.append(group)
            else:
                previous = groups[index]
                if previous["name"] != group["name"]:
                    merged_state["sessions"], groups = self._renamed_group_data(
                        previous["name"], group["name"], merged_state["sessions"], groups)
                groups[index] = dict(group, id=previous["id"])
            by_id = {value["id"]: position for position, value in enumerate(groups)}
            by_name = {value["name"]: position for position, value in enumerate(groups)}
        self.sessions, self.macros, self.tunnels = (merged_state["sessions"], merged_state["macros"],
                                                  merged_state["tunnels"])
        self.groups = groups
        try:
            self.save()
        except Exception:
            self.sessions, self.macros, self.tunnels, self.groups = old
            raise
        return len(incoming["sessions"])

    def group_ssh_settings(self, group_name):
        group_name = normalize_group_name(group_name, allow_empty=True)
        by_name = {group["name"]: group for group in self.groups}
        result = {}
        for name in _group_prefixes(group_name):
            result.update(by_name.get(name, {}).get("ssh", {}))
        return dict(result)

    def resolve_session(self, session):
        result = validate_session(session)
        if result["protocol"] in ("ssh", "mosh") and result["inherit_group_ssh"]:
            result.update(self.group_ssh_settings(result["group"]))
        return validate_session(result)

    @staticmethod
    def _renamed_group_data(old_name, new_name, sessions, groups):
        if old_name == new_name:
            if not any(group["name"] == old_name for group in groups):
                raise ValueError("Группа не найдена")
            return list(sessions), list(groups)
        if _in_group(new_name, old_name):
            raise ValueError("Нельзя переместить группу внутрь самой себя")
        affected = [group for group in groups if _in_group(group["name"], old_name)]
        if not affected:
            raise ValueError("Группа не найдена")
        destinations = {new_name + group["name"][len(old_name):] for group in affected}
        untouched = {group["name"] for group in groups if not _in_group(group["name"], old_name)}
        if destinations & untouched:
            raise ValueError("В месте назначения уже есть группа с таким названием")
        moved_groups = [dict(group, name=new_name + group["name"][len(old_name):])
                        if _in_group(group["name"], old_name) else group for group in groups]
        moved_sessions = [dict(session, group=new_name + session["group"][len(old_name):])
                          if _in_group(session["group"], old_name) else session for session in sessions]
        return moved_sessions, moved_groups

    def _save_group_change(self, sessions, groups):
        old = self.sessions, self.groups
        self.sessions, self.groups = sessions, groups
        try:
            self.save()
        except Exception:
            self.sessions, self.groups = old
            raise

    def upsert_group(self, group, apply_to_sessions=False):
        provided_id = isinstance(group, dict) and bool(group.get("id"))
        group = validate_group(group)
        apply_to_sessions = _boolean(apply_to_sessions, "Применить настройки группы")
        sessions, groups = list(self.sessions), list(self.groups)
        previous = next((item for item in groups if item["id"] == group["id"]), None)
        if previous is not None and previous["name"] != group["name"]:
            sessions, groups = self._renamed_group_data(previous["name"], group["name"], sessions, groups)
        same_name = next((item for item in groups if item["name"] == group["name"]), None)
        if same_name is not None and (not provided_id or same_name["id"] != group["id"]):
            raise ValueError("Группа с таким названием уже существует")
        groups = [item for item in groups if item["id"] != group["id"]] + [group]
        if apply_to_sessions:
            sessions = [dict(session, inherit_group_ssh=True)
                        if session["protocol"] in ("ssh", "mosh") and _in_group(session["group"], group["name"])
                        else session for session in sessions]
        self._save_group_change(sessions, groups)
        return next(item for item in self.groups if item["id"] == group["id"])

    def rename_group(self, old_name, new_name):
        old_name, new_name = normalize_group_name(old_name), normalize_group_name(new_name)
        sessions, groups = self._renamed_group_data(old_name, new_name, self.sessions, self.groups)
        self._save_group_change(sessions, groups)
        return next(group for group in self.groups if group["name"] == new_name)

    def delete_group(self, name, move_to_parent=True):
        name = normalize_group_name(name)
        move_to_parent = _boolean(move_to_parent, "Переместить подключения в родительскую группу")
        if not any(group["name"] == name for group in self.groups):
            return False
        destination = name.rsplit("/", 1)[0] if move_to_parent and "/" in name else ""
        sessions = []
        for session in self.sessions:
            if _in_group(session["group"], name):
                # Deleting a folder must not silently change credentials of its
                # preserved connections when they move to a different parent.
                value = self.resolve_session(session)
                value["group"] = destination
                if value["protocol"] in ("ssh", "mosh"):
                    value["inherit_group_ssh"] = False
                sessions.append(value)
            else:
                sessions.append(session)
        groups = [group for group in self.groups if not _in_group(group["name"], name)]
        self._save_group_change(sessions, groups)
        return True

    def set_group_inheritance(self, name, enabled, recursive=True):
        name = normalize_group_name(name)
        enabled = _boolean(enabled, "Настройки SSH группы")
        recursive = _boolean(recursive, "Вложенные группы")
        if not any(group["name"] == name for group in self.groups):
            raise ValueError("Группа не найдена")
        sessions = [dict(session, inherit_group_ssh=enabled)
                    if session["protocol"] in ("ssh", "mosh") and _in_group(session["group"], name, recursive)
                    else session for session in self.sessions]
        self._save_group_change(sessions, list(self.groups))

    def export_file(self, path):
        path = os.path.abspath(os.path.expanduser(str(path)))
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(self.path)):
            self.save()
        else:
            _atomic_json(path, self._document())


def parse_ssh_config(path=None):
    """Import simple, explicit OpenSSH Host blocks without executing directives.

    Wildcard blocks supply defaults to matching explicit aliases. Include,
    Match and connection policies absent from our profile model are rejected
    rather than silently bypassed by changing an alias to its HostName.
    """
    path = path or os.path.expanduser("~/.ssh/config")
    if not os.path.exists(path):
        return []
    if os.path.getsize(path) > MAX_CONFIG_BYTES:
        raise ValueError("Файл SSH config слишком большой")
    blocks = []
    patterns, options = ["*"], {}
    aliases = []
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            lexer = shlex.shlex(line, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            lexer.wordchars += "/.-@:%+~"
            try:
                parts = list(lexer)
            except ValueError as exc:
                raise ValueError("Некорректная строка SSH config: {}".format(exc))
            if not parts:
                continue
            # OpenSSH accepts both Keyword value and Keyword=value.
            first = parts[0].split("=", 1)
            if len(first) == 2:
                parts = [first[0], first[1]] + parts[1:]
            elif len(parts) > 1 and parts[1] == "=":
                parts = [parts[0]] + parts[2:]
            keyword = parts[0].lower()
            if keyword == "host":
                blocks.append((patterns, options))
                patterns, options = parts[1:], {}
                for alias in patterns:
                    if not any(char in alias for char in "*?!") and alias not in aliases:
                        aliases.append(alias)
            elif keyword in ("match", "include"):
                raise ValueError("Импорт SSH config не поддерживает директиву {}. "
                                 "Используйте файл с простыми блоками Host без Include/Match.".format(parts[0]))
            elif len(parts) > 1:
                if keyword == "identityfile":
                    options["__identity_files__"] = options.get("__identity_files__", 0) + 1
                if keyword not in options:
                    options[keyword] = " ".join(parts[1:])
    blocks.append((patterns, options))
    import fnmatch
    result = []
    unsupported = {
        "proxycommand", "proxyusefdpass", "certificatefile", "identityagent", "hostkeyalias",
        "pkcs11provider", "securitykeyprovider", "identitiesonly", "userknownhostsfile",
        "globalknownhostsfile", "knownhostscommand", "canonicalizehostname", "canonicaldomains",
        "bindaddress", "bindinterface", "hostbasedauthentication", "gssapiauthentication",
        "gssapikeyexchange", "kexalgorithms", "hostkeyalgorithms", "ciphers", "macs",
        "preferredauthentications", "stricthostkeychecking", "checkhostip", "forwardagent",
        "remotecommand", "requesttty", "localforward", "remoteforward", "dynamicforward",
        "localcommand", "permitlocalcommand", "controlmaster", "controlpath", "controlpersist",
    }
    for alias in aliases:
        options = {}
        identity_count = 0
        for patterns, values in blocks:
            positive = any(fnmatch.fnmatchcase(alias, pattern) for pattern in patterns if not pattern.startswith("!"))
            excluded = any(fnmatch.fnmatchcase(alias, pattern[1:]) for pattern in patterns if pattern.startswith("!"))
            if positive and not excluded:
                identity_count += values.get("__identity_files__", 0)
                for key, value in values.items():
                    if key not in options:
                        options[key] = value
        unsupported_keys = sorted(unsupported.intersection(options))
        if identity_count > 1:
            unsupported_keys.append("IdentityFile (несколько ключей)")
        if unsupported_keys:
            raise ValueError("Сессия «{}»: импорт SSH config не поддерживает {}. "
                             "Используйте исходный SSH alias в локальном терминале; "
                             "автоматический импорт отменён.".format(alias, ", ".join(unsupported_keys)))
        session = default_session("ssh")
        session.update({"name": alias, "group": "SSH config", "host": options.get("hostname", alias),
                        "username": options.get("user", ""), "port": options.get("port", 22),
                        "key_file": options.get("identityfile", ""), "jump_host": options.get("proxyjump", ""),
                        "x11": options.get("forwardx11", "no").lower() == "yes",
                        "compression": options.get("compression", "yes").lower() == "yes"})
        # OpenSSH performs its usual expansion when connecting through alias.
        # Imported values that depend on expansion cannot safely be copied.
        if any("%" in options.get(key, "") for key in ("hostname", "user", "identityfile", "proxyjump")):
            session.update({"host": alias, "username": "", "key_file": "", "jump_host": ""})
        result.append(validate_session(session))
    return result


def detect_dependencies():
    names = ("ssh", "sftp", "ssh-keygen", "ssh-copy-id", "telnet", "mosh", "picocom", "screen",
             "xfreerdp", "xfreerdp3", "vncviewer", "script", "xauth", "ping", "traceroute", "tracepath",
             "dig", "nslookup", "nc")
    return {name: shutil.which(name) for name in names}


def tcp_probe(host, port, timeout=5):
    """Bounded DNS+TCP diagnostic suitable for running in a worker thread."""
    host = validate_host(host)
    port = _port(port)
    connection = socket.create_connection((host, port), timeout=timeout)
    try:
        return connection.getpeername()
    finally:
        connection.close()
