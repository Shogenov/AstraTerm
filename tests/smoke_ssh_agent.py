#!/usr/bin/python3.5
"""Real OpenSSH agent/askpass regression using an isolated synthetic key only.

Uses no user private keys or inherited agent. Run with /usr/bin/python3.5.
The generated passphrase exists only in memory and private askpass IPC.
"""
from __future__ import print_function, unicode_literals

import os
import binascii
import base64
import signal
import stat
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from astraterm.ssh_profiles import ProfileStore, SSHAgent, SSHProfileError, _read_identities


def main():
    original_socket = os.environ.pop("SSH_AUTH_SOCK", None)
    agent = None
    try:
        with tempfile.TemporaryDirectory(prefix="astraterm-agent-smoke-") as directory:
            secret = "AstraTerm synthetic regression " + binascii.hexlify(os.urandom(16)).decode("ascii")
            path = os.path.join(directory, "encrypted-rsa")
            # Use the cryptography serializer directly, then verify the fixture
            # is genuinely encrypted before testing OpenSSH's askpass boundary.
            generated = rsa.generate_private_key(public_exponent=65537, key_size=2048,
                                                 backend=default_backend())
            encrypted = generated.private_bytes(serialization.Encoding.PEM,
                                                serialization.PrivateFormat.TraditionalOpenSSL,
                                                serialization.BestAvailableEncryption(secret.encode("utf-8")))
            assert b"ENCRYPTED" in encrypted, "Synthetic fixture is not encrypted"
            with open(path, "wb") as stream:
                stream.write(encrypted)
            os.chmod(path, 0o600)
            serialization.load_pem_private_key(encrypted, password=secret.encode("utf-8"),
                                               backend=default_backend())
            try:
                serialization.load_pem_private_key(encrypted, password=b"wrong synthetic passphrase",
                                                   backend=default_backend())
            except (TypeError, ValueError):
                pass
            else:
                raise AssertionError("Synthetic encrypted fixture accepted a wrong password")
            numbers = generated.public_key().public_numbers()
            def ssh_string(value):
                return struct.pack(">I", len(value)) + value
            def mpint(value):
                data = value.to_bytes((value.bit_length() + 7) // 8, "big")
                return b"\x00" + data if data[0] & 128 else data
            expected_blob = base64.b64encode(ssh_string(b"ssh-rsa") + ssh_string(mpint(numbers.e)) +
                                             ssh_string(mpint(numbers.n))).decode("ascii")
            assert not os.path.exists(path + ".pub")
            agent = SSHAgent(runtime_dir=os.path.join(directory, "run"))
            socket_path = agent.ensure()
            assert agent.managed_pid is not None, "Test did not create an isolated agent"
            assert stat.S_IMODE(os.stat(agent.runtime_dir).st_mode) == 0o700
            print("PASS isolated per-user agent startup and private runtime", flush=True)
            try:
                try:
                    agent.unlock(path, "wrong synthetic passphrase")
                except SSHProfileError as exc:
                    assert "wrong synthetic" not in str(exc)
                    assert secret not in str(exc)
                else:
                    raise AssertionError("Incorrect passphrase was accepted")
                assert not agent.key_loaded(path)
                print("PASS wrong passphrase fails without leaking output or loading a key", flush=True)

                identity = agent.unlock(path, secret)
                assert identity["blob"] == expected_blob
                assert agent.key_loaded(path, identity["fingerprint"])
                assert len(agent.identities()) == 1
                print("PASS encrypted PEM without .pub unlocks through private askpass IPC", flush=True)

                second = SSHAgent(runtime_dir=agent.runtime_dir)
                assert second.ensure() == socket_path
                assert second.key_loaded(path, identity["fingerprint"])
                descriptor = second.public_identity_file(path, identity["fingerprint"])
                with open(descriptor, "r", encoding="ascii") as stream:
                    assert stream.read() == identity["type"] + " " + identity["blob"] + "\n"
                assert stat.S_IMODE(os.stat(descriptor).st_mode) == 0o600
                print("PASS a second application reuses the unlocked agent and exact public identity", flush=True)

                store = ProfileStore(os.path.join(directory, "profiles.json"))
                store.upsert({"id": "synthetic", "name": "Synthetic key",
                              "key_file": path, "fingerprint": identity["fingerprint"]})
                reloaded = ProfileStore(store.path).get()
                assert second.key_loaded(reloaded["key_file"], reloaded["fingerprint"])
                with open(store.path, "r", encoding="utf-8") as stream:
                    metadata = stream.read()
                assert secret not in metadata and "PRIVATE KEY" not in metadata
                assert not any(name.startswith("ask-") for name in os.listdir(agent.runtime_dir))
                print("PASS restart metadata contains no passphrase/private material and IPC is removed", flush=True)
                print("SSH_AGENT_SMOKE_OK", flush=True)
            finally:
                # This agent was created in this unique test directory, never
                # inherited. Verify the still-live socket/process before cleanup.
                pid = agent.managed_pid
                if pid and _read_identities(agent.socket_path) is not None:
                    try:
                        with open("/proc/%d/comm" % pid, "r") as stream:
                            own_agent = stream.read().strip() == "ssh-agent"
                        if own_agent and os.stat("/proc/%d" % pid).st_uid == os.getuid():
                            os.kill(pid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
    finally:
        if original_socket is not None:
            os.environ["SSH_AUTH_SOCK"] = original_socket
        else:
            os.environ.pop("SSH_AUTH_SOCK", None)


if __name__ == "__main__":
    main()
