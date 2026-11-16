#!/usr/bin/python3.5
# -*- coding: utf-8 -*-
"""Isolated Ed25519-only host / encrypted RSA agent SFTP regression on Astra.

DISPLAY=:0 /usr/bin/python3.5 tests/smoke_sftp_ed25519.py

The fixture owns its loopback sshd, agent, encrypted synthetic identity and all
trust files. No application window, real private key, authorized_keys or user's
known_hosts is opened or modified. Password authentication is disabled.
"""
from __future__ import print_function, unicode_literals

import hashlib
import logging
import os
import posixpath
import pwd
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko

from astraterm import files as files_module
from astraterm.core import default_session
from astraterm.ssh_profiles import SSHAgent
from smoke_auth_profiles import (make_key, start_sshd, stop_owned_agent,
                                 stop_owned_process, write_private)


def make_ed25519(path):
    """Use the installed OpenSSH for a host-key type old Paramiko cannot load."""
    process = subprocess.Popen(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    unused, failure = process.communicate(timeout=15)
    if process.returncode:
        raise RuntimeError("Could not generate synthetic Ed25519 host key: " +
                           failure.decode("utf-8", "replace")[-500:])
    with open(path + ".pub", "r") as stream:
        fields = stream.read().split()
    assert fields[0] == "ssh-ed25519" and len(fields) >= 2
    return fields[0], fields[1]


@contextmanager
def isolated_trust(known):
    """Route both system/user Paramiko trust reads to this fixture only."""
    original_client = paramiko.SSHClient
    original_expanduser = os.path.expanduser

    class FixtureSSHClient(original_client):
        def load_system_host_keys(self, filename=None):
            self._system_host_keys.load(known)

        def load_host_keys(self, filename):
            self._host_keys.load(known)

    def expanduser(path):
        if path == "~/.ssh/known_hosts":
            return known
        return original_expanduser(path)

    with patch.object(paramiko, "SSHClient", FixtureSSHClient), \
            patch.object(os.path, "expanduser", expanduser):
        yield


def legacy_failure(port):
    """Show the pre-fix negotiation failure before user authentication starts."""
    connection = socket.create_connection(("127.0.0.1", port), timeout=8)
    transport = paramiko.Transport(connection)
    try:
        options = transport.get_security_options()
        # Modern developer machines may have native Ed25519. The narrowed
        # baseline represents exactly the older Astra Paramiko capability.
        if "ssh-ed25519" in options.key_types:
            options.key_types = ("ssh-rsa",)
        ready = threading.Event()
        try:
            # Paramiko 2.0 has no timeout argument on start_client.
            transport.start_client(event=ready)
            assert ready.wait(12), "Legacy SSH negotiation did not finish"
            error = transport.get_exception()
            if error is not None:
                raise error
        except paramiko.SSHException as error:
            assert "no acceptable host key" in str(error).lower(), str(error)
        else:
            raise AssertionError("Ed25519-only server unexpectedly negotiated an RSA-only client")
    finally:
        transport.close()
        connection.close()
    print("PASS original no acceptable host key reproduced before authentication", flush=True)


def connect(profile, known, policy, cancelled=None, register_client=None):
    with isolated_trust(known):
        return files_module._connect_sftp_chain(profile, "", [], lambda endpoint: policy,
                                                cancelled=cancelled, register_client=register_client)


def close_result(result):
    if result is None:
        return
    client, sftp, unused_home, gateways = result
    try:
        sftp.close()
    finally:
        files_module._close_clients(gateways + [client])


def check_public_key_authentication(username):
    """A compatible server host key can still leave old Paramiko unable to log in."""
    with tempfile.TemporaryDirectory(prefix=".astraterm-sftp-auth-", dir=os.path.expanduser("~")) as directory:
        identity = os.path.join(directory, "identity")
        kind, blob = make_ed25519(identity)
        public = types.SimpleNamespace(get_name=lambda: kind, get_base64=lambda: blob)
        host_path = os.path.join(directory, "host_rsa")
        host_key = make_key(host_path)
        sshd, port, remote, config, unused_log = start_sshd(directory, public, host_path, username)
        known = os.path.join(directory, "known_hosts")
        write_private(known, ("[127.0.0.1]:%d %s %s\n" % (
            port, host_key.get_name(), host_key.get_base64())).encode("ascii"))
        profile = default_session("ssh")
        profile.update(host="127.0.0.1", port=port, username=username, key_file=identity, identity_profile="system")
        # Represent the legacy key-format gap even when run with recent Paramiko.
        legacy_identity = None if hasattr(paramiko, "Ed25519Key") else identity
        legacy_errors = []
        try:
            with isolated_trust(known):
                old_connect = paramiko.SSHClient.connect

                def attempt(client, **arguments):
                    arguments.update(allow_agent=False, look_for_keys=False,
                                     key_filename=legacy_identity)
                    try:
                        return old_connect(client, **arguments)
                    except paramiko.BadAuthenticationType as failure:
                        legacy_errors.append(failure)
                        raise

                with patch.object(paramiko.SSHClient, "connect", attempt):
                    for secret in ("fixture-unusable-password", ""):
                        result = files_module._connect_sftp_chain(profile, secret, [],
                                                                 lambda endpoint: paramiko.RejectPolicy())
                        try:
                            assert result[2] == remote and set(result[1].listdir(".")) == {"A", "B"}
                        finally:
                            close_result(result)
                    assert len(legacy_errors) == 1
                    assert legacy_errors[0].allowed_types == ["publickey"]
            print("PASS original BadAuthenticationType(publickey) / same key via OpenSSH / SFTP directory listed", flush=True)
        finally:
            stop_owned_process(sshd, config)


def check_transfers(profile, known, directory, remote):
    class NeverUnknown(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            raise AssertionError("A trusted Ed25519 key was treated as unknown")

        def confirm_openssh_host_key(self, prompt):
            raise AssertionError("Native transport asked about a trusted Ed25519 key")

    stale_focus = [False]
    result = connect(profile, known, NeverUnknown(), cancelled=lambda: stale_focus[0])
    client, sftp, home, gateways = result
    uploaded = None
    try:
        # The connection now belongs to the cache. A later focus-generation
        # change must not retain a startup-only cancellation predicate.
        stale_focus[0] = True
        assert not gateways and home == remote, (home, remote)
        assert client.get_transport().is_authenticated()
        assert "A" in sftp.listdir(home) and "B" in sftp.listdir(home)
        initial = os.path.join(directory, "initial.txt")
        sftp.get(posixpath.join(home, "A", "only-A.txt"), initial)
        with open(initial, "rb") as stream:
            assert stream.read() == b"synthetic fixture\n"
        payload = ("Astra SFTP / зашифрованный RSA / Ed25519\n".encode("utf-8") +
                   b"\x00" + bytes(range(256))) * 32
        source = os.path.join(directory, "upload.bin")
        destination = os.path.join(directory, "download.bin")
        write_private(source, payload)
        uploaded = posixpath.join(home, "ed25519-agent.bin")
        sftp.put(source, uploaded)
        sftp.get(uploaded, destination)
        with open(destination, "rb") as stream:
            assert hashlib.sha256(stream.read()).digest() == hashlib.sha256(payload).digest()
        assert "ed25519-agent.bin" in sftp.listdir(home)
    finally:
        if uploaded is not None:
            try:
                sftp.remove(uploaded)
            except IOError:
                pass
        close_result(result)
    print("PASS trusted Ed25519 + encrypted RSA agent, cached lifecycle, list/get/put and binary hash", flush=True)


def check_wrong_host_key(profile, known, stored_type):
    class DoNotAsk(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            raise AssertionError("Changed host key was incorrectly offered as a new key")

        def confirm_openssh_host_key(self, prompt):
            raise AssertionError("Native transport offered a changed host key as a new key")

    result = None
    try:
        result = connect(profile, known, DoNotAsk())
    except paramiko.BadHostKeyException:
        pass
    except Exception as error:
        assert "Ключ SSH-сервера изменился" in str(error), str(error)
    else:
        raise AssertionError("Incorrect stored Ed25519 host key was not rejected")
    finally:
        close_result(result)
    print("PASS stored %s vs current Ed25519 host key rejected before authentication" % stored_type,
          flush=True)


def check_selected_identity(profile, known, wrong_blob, wrong_public_path):
    """The adapter must not try the other authorized key left in the agent."""
    wrong_profile = dict(profile)
    wrong_profile["_agent_key_blob"] = wrong_blob
    wrong_profile["key_file"] = wrong_public_path
    result = None
    try:
        result = connect(wrong_profile, known, paramiko.RejectPolicy())
    except Exception as error:
        assert files_module._authentication_error(error), str(error)
    else:
        raise AssertionError("Wrong selected identity silently fell back to the authorized agent key")
    finally:
        close_result(result)
    print("PASS selected unauthorized agent key fails while an authorized other key remains loaded", flush=True)


def check_unknown_host_key(profile, known):
    class DeniedUnknown(paramiko.MissingHostKeyPolicy):
        def __init__(self):
            self.called = False

        def missing_host_key(self, client, hostname, key):
            self.called = True
            assert key.get_name() == "ssh-ed25519"
            raise paramiko.SSHException("Synthetic policy refuses unknown host key")

        def confirm_openssh_host_key(self, prompt):
            self.called = True
            assert "ED25519" in prompt.upper(), prompt
            return False

    policy = DeniedUnknown()
    result = None
    try:
        result = connect(profile, known, policy)
    except Exception:
        # Either Paramiko's explicit rejection or native OpenSSH rejecting the
        # denied confirmation is valid; the callback below must have run.
        pass
    else:
        raise AssertionError("Unknown Ed25519 host key was silently accepted")
    finally:
        close_result(result)
    assert policy.called, "Missing-host-key policy was bypassed"
    with open(known, "rb") as stream:
        assert not stream.read(), "Unknown host was persisted despite rejection"
    print("PASS unknown Ed25519 host key requires policy and is not silently trusted", flush=True)


def check_accept_unknown_host_key(profile, known, host_blob):
    class AcceptedUnknown(paramiko.MissingHostKeyPolicy):
        def __init__(self):
            self.prompts = []

        def confirm_openssh_host_key(self, prompt):
            assert "ED25519" in prompt.upper(), prompt
            self.prompts.append(prompt)
            return True

    policy = AcceptedUnknown()
    result = None
    try:
        result = connect(profile, known, policy)
        assert "A" in result[1].listdir(result[2])
        assert len(policy.prompts) == 1, "A new host needs exactly one explicit approval"
    finally:
        close_result(result)
    with open(known, "rb") as stream:
        assert host_blob.encode("ascii") in stream.read(), "Approved Ed25519 key was not saved"
    result = None
    try:
        result = connect(profile, known, policy)
        assert "B" in result[1].listdir(result[2])
        assert len(policy.prompts) == 1, "The saved approved key caused another confirmation"
    finally:
        close_result(result)
    print("PASS approved new Ed25519 key saved only to fixture trust file and reused without prompt", flush=True)


def check_cancelled_confirmation(profile, known):
    prompted, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    outcomes, clients = [], []

    class WaitingUnknown(paramiko.MissingHostKeyPolicy):
        def confirm_openssh_host_key(self, prompt):
            assert "ED25519" in prompt.upper(), prompt
            prompted.set()
            assert release.wait(8), "Fixture did not release host-key confirmation"
            # Even an approval arriving after cancellation must be ignored.
            return True

    def attempt():
        try:
            result = connect(profile, known, WaitingUnknown(), cancelled=cancelled.is_set,
                             register_client=clients.append)
        except Exception as error:
            outcomes.append(error)
        else:
            close_result(result)
            outcomes.append(None)

    worker = threading.Thread(target=attempt)
    worker.daemon = True
    worker.start()
    try:
        assert prompted.wait(12), "Native host-key confirmation did not start"
        cancelled.set()
        release.set()
        worker.join(10)
        assert not worker.is_alive(), "Cancelled SFTP startup did not finish"
        assert len(outcomes) == 1 and outcomes[0] is not None, "Cancelled startup returned a live connection"
        with open(known, "rb") as stream:
            assert not stream.read(), "Late approval persisted a key after cancellation"
        native = [client for client in clients if hasattr(client, "process")]
        assert len(native) == 1, "Expected one owned native client during cancellation"
        client = native[0]
        assert client.closed and not client.is_active()
        assert client.process is not None and client.process.poll() is not None
        # Cancellation can start cleanup on the broker thread. The connection
        # worker may finish just before that owned thread removes its directory.
        deadline = time.monotonic() + 3
        while client.directory is not None and os.path.exists(client.directory) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.directory is not None and not os.path.exists(client.directory)
    finally:
        cancelled.set()
        release.set()
        files_module._close_clients(clients)
        worker.join(10)
    print("PASS cancelled host confirmation ignores late approval, stops owned SSH and removes temporary state",
          flush=True)


def main():
    username = pwd.getpwuid(os.getuid()).pw_name
    secret = "synthetic-" + uuid.uuid4().hex
    previous_socket = os.environ.pop("SSH_AUTH_SOCK", None)
    sshd = agent = None
    sshd_config = ""
    # Expected baseline negotiation failure should not flood the console with a
    # transport-thread traceback. Assertions retain its precise error message.
    transport_log = logging.getLogger("paramiko.transport")
    old_level = transport_log.level
    transport_log.setLevel(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix=".astraterm-ed25519-", dir=os.path.expanduser("~")) as directory:
        try:
            private_path = os.path.join(directory, "identity.pem")
            public = make_key(private_path, secret)
            host_path = os.path.join(directory, "host_ed25519")
            host_type, host_blob = make_ed25519(host_path)
            wrong_type, wrong_blob = make_ed25519(os.path.join(directory, "wrong_ed25519"))
            sshd, port, remote, sshd_config, sshd_log = start_sshd(directory, public, host_path, username)
            legacy_failure(port)
            check_public_key_authentication(username)
            agent = SSHAgent(runtime_dir=os.path.join(directory, "agent"))
            other_path = os.path.join(directory, "unrelated_identity.pem")
            other_public = make_key(other_path, secret)
            unrelated = agent.unlock(other_path, secret)
            assert unrelated["blob"] == other_public.get_base64()
            identity = agent.unlock(private_path, secret)
            os.environ.update(agent.environment())
            assert identity["blob"] == public.get_base64()
            assert len(agent.identities()) == 2, "Fixture must include an unrelated earlier agent key"
            profile = default_session("ssh")
            profile.update({"name": "Isolated Ed25519 SFTP", "host": "127.0.0.1", "port": port,
                            "username": username, "identity_profile": "synthetic-encrypted-rsa",
                            "key_file": agent.public_identity_file(private_path),
                            "_agent_key_blob": identity["blob"], "_identity_required": True})
            known = os.path.join(directory, "known_hosts")
            wrong = os.path.join(directory, "known_hosts_wrong")
            wrong_algorithm = os.path.join(directory, "known_hosts_wrong_rsa")
            empty = os.path.join(directory, "known_hosts_empty")
            endpoint = "[127.0.0.1]:%d" % port
            write_private(known, ("%s %s %s\n" % (endpoint, host_type, host_blob)).encode("ascii"))
            write_private(wrong, ("%s %s %s\n" % (endpoint, wrong_type, wrong_blob)).encode("ascii"))
            write_private(wrong_algorithm, ("%s %s %s\n" % (endpoint, other_public.get_name(),
                                                           other_public.get_base64())).encode("ascii"))
            write_private(empty, b"")
            check_transfers(profile, known, directory, remote)
            check_selected_identity(profile, known, unrelated["blob"], agent.public_identity_file(other_path))
            assert agent.loaded_identity(private_path) is not None
            check_wrong_host_key(profile, wrong, "Ed25519")
            check_wrong_host_key(profile, wrong_algorithm, "RSA")
            check_unknown_host_key(profile, empty)
            check_cancelled_confirmation(profile, empty)
            check_accept_unknown_host_key(profile, empty, host_blob)
            print("SFTP_ED25519_SMOKE_OK", flush=True)
        except Exception as error:
            print("FIXTURE TRACE: " + traceback.format_exc().replace(secret, "[REDACTED]"), flush=True)
            if sshd is not None and os.path.isfile(sshd_log):
                with open(sshd_log, "r") as stream:
                    print("ISOLATED SSHD: " + stream.read()[-1200:].replace(secret, "[REDACTED]"), flush=True)
            raise RuntimeError(str(error).replace(secret, "[REDACTED]")) from None
        finally:
            stop_owned_process(sshd, sshd_config)
            stop_owned_agent(agent)
            transport_log.setLevel(old_level)
            if previous_socket is None:
                os.environ.pop("SSH_AUTH_SOCK", None)
            else:
                os.environ["SSH_AUTH_SOCK"] = previous_socket


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
