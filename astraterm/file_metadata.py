"""Optional remote birth times and portable file permissions."""
from __future__ import unicode_literals

import stat
import threading


def permissions(mode):
    return stat.filemode(mode)[1:] if isinstance(mode, int) else None


def parse_birth_times(data):
    fields = data.decode('utf-8', 'replace').split('\x00')
    times = {}
    for index in range(0, len(fields) - 2, 2):
        value, filename = fields[index:index + 2]
        if value and len(value) <= 12 and all('0' <= char <= '9' for char in value) and filename.startswith('/'):
            timestamp = int(value)
            if 0 < timestamp <= 253402300799:
                times[filename] = timestamp
    return times


def remote_birth_times(client, directory):
    if (client is None or not hasattr(client, 'get_transport') or not directory.startswith('/') or
            '\r' in directory or '\n' in directory):
        return {}
    transport = client.get_transport()
    if transport is None:
        return {}
    command = ("sh -c 'IFS= read -r directory || exit; find -H \"$directory\" "
               "-mindepth 1 -maxdepth 1 -exec stat --printf=\"%W\\0%n\\0\" -- {} + 2>/dev/null'")
    channel = None
    timeout = None
    try:
        channel = transport.open_session(timeout=1)
        timeout = threading.Timer(1.5, channel.close)
        timeout.daemon = True
        timeout.start()
        channel.exec_command(command)
        channel.sendall((directory + '\n').encode('utf-8'))
        channel.shutdown_write()
        blocks = []
        size = 0
        while True:
            data = channel.recv(65536)
            if not data:
                return parse_birth_times(b''.join(blocks))
            size += len(data)
            if size > 2 * 1024 * 1024:
                return {}
            blocks.append(data)
    except Exception:
        return {}
    finally:
        if timeout is not None:
            timeout.cancel()
        if channel is not None:
            channel.close()
