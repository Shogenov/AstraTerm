# -*- coding: utf-8 -*-
"""SFTP packets over OpenSSH when Paramiko cannot negotiate or use a key.

OpenSSH owns SSH, host verification and authentication. This fallback handles
direct connections and preserves the selected SSH identity.
"""
from __future__ import unicode_literals

import base64
import os
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time

import paramiko

from .core import validate_session
from .ssh_profiles import (_AskpassBroker, _public_identity, _read_identities,
                           _verify_terminal_config)


class NativeSSHError(paramiko.SSHException):
    """A safe diagnostic which does not include captured OpenSSH output."""


def _config_path(value):
    """Quote one pathname for an OpenSSH configuration operand, not a shell."""
    path = os.path.abspath(os.path.expanduser(value))
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise NativeSSHError("Недопустимый путь файла ключей серверов.")
    return '"' + path.replace('\\', '\\\\').replace('"', '\\"') + '"'


class _PromptBroker(_AskpassBroker):
    """The existing private askpass transport with a bounded prompt request."""
    def __init__(self, runtime, callback):
        self._callback = callback
        self._active = None
        self._active_lock = threading.Lock()
        _AskpassBroker.__init__(self, runtime, "")

    def _read(self, stream, count):
        result = bytearray()
        deadline = time.monotonic() + 3
        while len(result) < count and not self._stop.is_set():
            if time.monotonic() >= deadline:
                raise socket.timeout()
            try:
                part = stream.recv(count - len(result))
            except socket.timeout:
                continue
            if not part:
                raise EOFError()
            result.extend(part)
        if len(result) != count:
            raise EOFError()
        return bytes(result)

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
                client.settimeout(0.2)
                with self._active_lock:
                    self._active = client
                if hasattr(socket, "SO_PEERCRED"):
                    unused_pid, uid, unused_gid = struct.unpack(
                        "3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                length = struct.unpack(">I", self._read(client, 4))[0]
                if not 0 < length <= 16384:
                    continue
                prompt = self._read(client, length).decode("utf-8", "replace")
                attempts += 1
                response = self._callback(prompt)
                if self._stop.is_set() or response is None:
                    continue
                if (not isinstance(response, str) or len(response) > 65536 or
                        any(char in response for char in ("\x00", "\r", "\n"))):
                    continue
                client.sendall(response.encode("utf-8") + b"\n")
            except (OSError, EOFError, ValueError, struct.error):
                pass
            except Exception:
                # GTK/cancellation failures must neither accept a key nor reveal
                # a secret or raw prompt through background-thread tracebacks.
                pass
            finally:
                with self._active_lock:
                    self._active = None
                client.close()

    def close(self):
        self._stop.set()
        self._stream.close()
        with self._active_lock:
            if self._active is not None:
                try:
                    self._active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._active.close()
        if threading.current_thread() is not self._thread:
            self._thread.join(0.3)
        self.secret = b""
        shutil.rmtree(self.directory, ignore_errors=True)


class _NativeChannel(object):
    """Socket-like SFTP channel with cancellable, bounded blocking operations."""
    def __init__(self, owner, stream):
        self._owner = owner
        self._stream = stream
        self._stream.setblocking(False)
        self._timeout = 120.0

    @property
    def closed(self):
        return self._owner.closed

    def get_name(self):
        return "openssh-sftp"

    def settimeout(self, seconds):
        self._timeout = None if seconds is None else float(seconds)

    def gettimeout(self):
        return self._timeout

    def _ready(self, writing=False):
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            self._owner.check_cancelled()
            if self.closed:
                raise NativeSSHError("Подключение SFTP закрыто.")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                self._owner.raise_error(timed_out=True)
            wait = 0.2 if remaining is None else min(0.2, remaining)
            try:
                readable, writable, unused = select.select(
                    [] if writing else [self._stream], [self._stream] if writing else [], [], wait)
            except (OSError, ValueError):
                self._owner.check_cancelled()
                self._owner.raise_error()
            if readable or writable:
                return
            if self._owner.process is not None and self._owner.process.poll() is not None:
                self._owner.raise_error()

    def send(self, data):
        while True:
            self._ready(writing=True)
            try:
                count = self._stream.send(data)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                self._owner.raise_error()
            if count == 0:
                self._owner.raise_error()
            return count

    def recv(self, count):
        while True:
            self._ready()
            try:
                part = self._stream.recv(count)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                self._owner.raise_error()
            if not part:
                self._owner.raise_error()
            return part

    def close(self):
        self._owner.close()

    def _close_socket(self):
        try:
            self._stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._stream.close()


class _NativeClient(object):
    def __init__(self, cancelled):
        self._cancelled = cancelled
        self._lock = threading.RLock()
        self.closed = False
        self.process = None
        self.channel = None
        self.broker = None
        self.directory = None
        self.authenticated = False
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = None
        self.host_rejected = False
        self._secret = None
        self._policy = None
        self._secret_attempts = 0
        self._selected = False
        self._public_key_only = False
        self._passphrase_prompt = None

    def check_cancelled(self):
        if self._cancelled is not None and self._cancelled():
            self.close()
            raise NativeSSHError("Подключение SFTP отменено.")

    def get_transport(self):
        return self

    def is_active(self):
        return bool(not self.closed and self.process is not None and self.process.poll() is None)

    def is_authenticated(self):
        return bool(self.authenticated and self.is_active())

    def set_keepalive(self, unused_seconds):
        # OpenSSH owns keepalives through the explicit ServerAlive options.
        pass

    def _drain_stderr(self):
        try:
            descriptor = self.process.stderr.fileno()
            while not self.closed:
                readable, unused_write, unused_error = select.select([descriptor], [], [], 0.2)
                if not readable:
                    continue
                part = os.read(descriptor, 4096)
                if not part:
                    break
                with self._stderr_lock:
                    self._stderr.extend(part)
                    del self._stderr[:-16384]
        except (OSError, ValueError):
            pass

    def raise_error(self, timed_out=False):
        if self._stderr_thread is not None:
            self._stderr_thread.join(0.15)
        with self._stderr_lock:
            message = bytes(self._stderr).decode("utf-8", "replace").lower()
        if ("remote host identification has changed" in message or
                "offending " in message or "host key has changed" in message):
            raise NativeSSHError("Ключ SSH-сервера изменился. Подключение остановлено. "
                                 "Проверьте новый отпечаток у администратора перед обновлением known_hosts.")
        if ("permission denied" in message or "authentication failed" in message or
                "too many authentication failures" in message):
            raise paramiko.AuthenticationException("Сервер отклонил аутентификацию SSH.")
        if self.host_rejected or "host key verification failed" in message:
            raise NativeSSHError("Ключ SSH-сервера не подтверждён. Проверьте отпечаток сервера и known_hosts.")
        if timed_out:
            raise NativeSSHError("Превышено время ожидания SFTP через OpenSSH.")
        if "could not resolve hostname" in message:
            raise NativeSSHError("OpenSSH не удалось определить адрес сервера SFTP.")
        if "connection refused" in message:
            raise NativeSSHError("Сервер отклонил подключение SFTP.")
        raise NativeSSHError("Не удалось открыть SFTP через системный OpenSSH.")

    def _answer(self, prompt):
        self.check_cancelled()
        lower = prompt.lower()
        if "are you sure you want to continue connecting" in lower:
            callback = getattr(self._policy, "confirm_openssh_host_key", None)
            accepted = bool(callback is not None and callback(prompt))
            self.check_cancelled()
            self.host_rejected = not accepted
            return "yes" if accepted else "no"
        passphrase_prompt = lower.startswith("enter passphrase for key ")
        password_prompt = not self._public_key_only and "password:" in lower
        if self._public_key_only:
            passphrase_prompt = prompt.strip() == self._passphrase_prompt
        if (not self._selected and self._secret is not None and self._secret_attempts < 2 and
                (passphrase_prompt or password_prompt)):
            self._secret_attempts += 1
            return self._secret
        return None

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.authenticated = False
            process = self.process
            channel, broker, directory = self.channel, self.broker, self.directory
            self._secret = None
            self._policy = None
        # No client lock is held while a broker callback is finishing. Closing
        # its active IPC first also releases an owned askpass helper promptly.
        try:
            if channel is not None:
                channel._close_socket()
            if broker is not None:
                broker.close()
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                if (self._stderr_thread is not None and
                        threading.current_thread() is not self._stderr_thread):
                    self._stderr_thread.join(0.3)
                if process.stderr is not None:
                    process.stderr.close()
        finally:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)


def _check_selected_config(client, argv, environment, public_file):
    """A selected agent identity must not inherit additional configured keys."""
    client.check_cancelled()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment,
                               start_new_session=True, close_fds=True)
    try:
        deadline = time.monotonic() + 15
        while True:
            client.check_cancelled()
            if time.monotonic() > deadline:
                raise NativeSSHError("Превышено время проверки настроек OpenSSH.")
            try:
                output, unused_error = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0 or len(output) > 1024 * 1024:
            raise NativeSSHError("Не удалось проверить настройки OpenSSH для SFTP.")
        _verify_terminal_config(output, public_file)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
        else:
            # Python 3.5 cannot communicate() again after its pipes were
            # already closed by a successful communicate().
            process.stdout.close()
            process.stderr.close()


def connect_openssh_sftp(profile, secret, policy, known_hosts,
                         register_client=None, cancelled=None, public_key_only=False):
    """Open a directly connected SFTP client with native SSH trust and auth.

    ``policy.confirm_openssh_host_key(prompt)`` may explicitly confirm a new
    server fingerprint. Changed known keys always remain a native hard failure.
    """
    original = dict(profile)
    profile = validate_session(profile)
    if profile["protocol"] != "ssh" or profile.get("jump_host"):
        raise NativeSSHError("Резервный SFTP через OpenSSH доступен для прямого SSH-подключения.")
    if secret is not None and (not isinstance(secret, str) or len(secret) > 65536 or
                               any(char in secret for char in ("\x00", "\r", "\n"))):
        raise NativeSSHError("Неподдерживаемый формат пароля SSH.")
    client = _NativeClient(cancelled)
    child = None
    try:
        if register_client is not None:
            register_client(client)
        client.check_cancelled()
        if client.closed:
            raise NativeSSHError("Подключение SFTP отменено.")
        client.directory = tempfile.mkdtemp(prefix="astraterm-sftp-")
        os.chmod(client.directory, 0o700)
        environment = dict(os.environ)
        if original.get("_ssh_agent_sock"):
            environment["SSH_AUTH_SOCK"] = original["_ssh_agent_sock"]
        environment.update({"LC_ALL": "C", "LANG": "C", "SSH_ASKPASS_REQUIRE": "force",
                            "DISPLAY": environment.get("DISPLAY") or ":0"})
        client._secret, client._policy = secret, policy
        client._public_key_only = public_key_only
        client.broker = _PromptBroker(client.directory, client._answer)
        helper = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "scripts", "astraterm-askpass")
        if not os.path.isfile(helper) or not os.access(helper, os.X_OK):
            raise NativeSSHError("Не установлен помощник ввода SSH для SFTP.")
        environment.update({"SSH_ASKPASS": helper,
                            "ASTRATERM_ASKPASS_SOCKET": client.broker.path,
                            "ASTRATERM_ASKPASS_REQUEST": "1"})
        options = ["-T", "-o", "ConnectTimeout=12", "-o", "ServerAliveInterval=30",
                   "-o", "ServerAliveCountMax=2", "-o", "ClearAllForwardings=yes",
                   "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
                   "-o", "ControlMaster=no", "-o", "ControlPath=none",
                   "-o", "StrictHostKeyChecking=ask", "-o", "NumberOfPasswordPrompts=1",
                   "-o", "UserKnownHostsFile=" + _config_path(known_hosts),
                   "-p", str(profile["port"])]
        if public_key_only:
            options += ["-o", "PreferredAuthentications=publickey",
                        "-o", "PasswordAuthentication=no", "-o", "ChallengeResponseAuthentication=no"]
        if profile.get("username"):
            options += ["-l", profile["username"]]
        if profile.get("compression"):
            options += ["-C"]
        blob = original.get("_agent_key_blob")
        required = bool(original.get("_identity_required") or
                        profile.get("identity_profile") not in (None, "", "system"))
        if required and not blob:
            raise NativeSSHError("Разблокируйте ключ выбранного SSH-профиля перед подключением SFTP.")
        public_file = None
        if blob:
            client._selected = True
            try:
                raw = base64.b64decode(blob.encode("ascii"), validate=True)
                length = struct.unpack(">I", raw[:4])[0]
                kind = raw[4:4 + length].decode("ascii")
                identity = _public_identity(kind + " " + blob)
            except (ValueError, UnicodeError, struct.error):
                identity = None
            if identity is None:
                raise NativeSSHError("Некорректная публичная идентичность SSH-профиля.")
            loaded = _read_identities(environment.get("SSH_AUTH_SOCK")) or []
            if not any(item["blob"] == blob for item in loaded):
                raise NativeSSHError("Ключ SSH-профиля не разблокирован в ssh-agent.")
            client.check_cancelled()
            public_file = os.path.join(client.directory, "identity.pub")
            with open(public_file, "wb") as stream:
                stream.write((identity["type"] + " " + blob + "\n").encode("ascii"))
            os.chmod(public_file, 0o600)
            options += ["-i", public_file, "-o", "IdentitiesOnly=yes", "-o",
                        "IdentityAgent=SSH_AUTH_SOCK", "-o", "PubkeyAuthentication=yes",
                        "-o", "PasswordAuthentication=no", "-o", "ChallengeResponseAuthentication=no"]
        elif profile.get("key_file"):
            key_path = os.path.expanduser(profile["key_file"])
            client._passphrase_prompt = "Enter passphrase for key '{}':".format(key_path)
            options += ["-i", key_path]
        if public_file is not None:
            _check_selected_config(client, ["ssh", "-G"] + options + ["--", profile["host"]],
                                   environment, public_file)
        client.check_cancelled()
        stream, child = socket.socketpair()
        client.channel = _NativeChannel(client, stream)
        with client._lock:
            client.check_cancelled()
            if client.closed:
                raise NativeSSHError("Подключение SFTP отменено.")
            client.process = subprocess.Popen(
                ["ssh"] + options + ["-s", "--", profile["host"], "sftp"],
                stdin=child, stdout=child, stderr=subprocess.PIPE, env=environment,
                start_new_session=True, close_fds=True)
        child.close()
        child = None
        client._stderr_thread = threading.Thread(target=client._drain_stderr)
        client._stderr_thread.daemon = True
        client._stderr_thread.start()
        sftp = paramiko.SFTPClient(client.channel)
        client.channel.settimeout(20)
        client.authenticated = True
        home = sftp.normalize(".")
        client.check_cancelled()
        # The caller separately closes stale completed results. A cached live
        # connection must not retain the generation of its opening request.
        client._cancelled = None
        client._secret = None
        return client, sftp, home, []
    except OSError:
        client.close()
        raise NativeSSHError("Не удалось запустить системный OpenSSH для SFTP.") from None
    except Exception:
        client.close()
        raise
    finally:
        if child is not None:
            child.close()
