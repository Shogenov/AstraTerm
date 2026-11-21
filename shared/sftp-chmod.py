import os
import stat
import sys

# Run only on the SSH server. Bind chmod to a descriptor and refuse links at every level.
filename, mode, *expected = sys.argv[1:]
parts = filename.split("/")
if parts[0] or any(part in ("", ".", "..") for part in parts[1:]):
    sys.exit(56)
flags = os.O_PATH | os.O_NOFOLLOW
descriptor = os.open("/", flags | os.O_DIRECTORY)
try:
    for index, part in enumerate(parts[1:], 2):
        next_descriptor = os.open(part, flags | (os.O_DIRECTORY if index < len(parts) else 0), dir_fd=descriptor)
        os.close(descriptor)
        descriptor = next_descriptor
    status = os.fstat(descriptor)
    if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
        sys.exit(56)
    revision = (status.st_mode, status.st_uid, status.st_gid, status.st_size, int(status.st_mtime))
    if revision != tuple(map(int, expected)):
        sys.exit(55)
    os.chmod("/proc/self/fd/{}".format(descriptor), int(mode, 8))
finally:
    os.close(descriptor)
