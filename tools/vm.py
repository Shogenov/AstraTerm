"""Development SSH helper. Credentials are read from the environment only."""
from __future__ import print_function

import argparse
import base64
import hashlib
import os
import sys

import paramiko


class PinnedKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def missing_host_key(self, client, hostname, key):
        actual = hashlib.md5(key.asbytes()).hexdigest()
        sha = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if self.fingerprint.replace(":", "") != actual and self.fingerprint != sha:
            raise paramiko.SSHException("Server fingerprint mismatch: " + sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    parser.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--stdin-env", help="Send the named local environment value to remote stdin")
    parser.add_argument("command", nargs="?")
    args = parser.parse_args()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedKey(args.fingerprint))
    client.connect(args.host, username=args.user, password=os.environ.get("ASTRA_VM_PASSWORD"),
                   timeout=15, look_for_keys=False, allow_agent=False)
    try:
        if args.put or args.get:
            with client.open_sftp() as sftp:
                if args.put:
                    sftp.put(*args.put)
                if args.get:
                    sftp.get(*args.get)
        if args.command:
            stdin, stdout, stderr = client.exec_command(args.command, timeout=args.timeout)
            if args.stdin_env:
                stdin.write(os.environ[args.stdin_env] + "\n")
                stdin.flush()
                stdin.channel.shutdown_write()
            # Continuously drain both streams, even for verbose package installation.
            channel = stdout.channel
            import time
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if channel.recv_ready():
                    sys.stdout.buffer.write(channel.recv(65536))
                    sys.stdout.buffer.flush()
                if channel.recv_stderr_ready():
                    sys.stderr.buffer.write(channel.recv_stderr(65536))
                    sys.stderr.buffer.flush()
                time.sleep(0.03)
            sys.exit(channel.recv_exit_status())
    finally:
        client.close()


if __name__ == "__main__":
    main()
