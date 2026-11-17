"""Read the working directory reported by an SSH shell."""
import re
from urllib.parse import unquote


UNKNOWN_DIRECTORY = "Терминал не сообщил текущий каталог."
REMOTE_HOSTNAME = "/proc/sys/kernel/hostname"
_HOST = r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}|\[[0-9A-Fa-f:.]+\])"
_TITLE = re.compile(r"^(?:\([^()]+\))?([A-Za-z0-9_.-]+)@(" + _HOST + r"):\s?(.*)$")


def directory_report(kind, value):
    if not isinstance(value, str) or not value or len(value) > 32767 or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    if kind == "osc7":
        match = re.fullmatch(r"file://(" + _HOST + r")?(/[^?#]*)", value)
        if not match or re.search(r"%(?![0-9a-fA-F]{2})", match.group(2)):
            return None
        try:
            host, path = match.group(1) or "", unquote(match.group(2), errors="strict")
        except UnicodeError:
            return None
        if host.lower() == "localhost":
            host = ""
        user = ""
    elif kind == "title":
        match = _TITLE.fullmatch(value)
        if not match:
            return None
        user, host, path = match.groups()
    else:
        return None
    if re.search(r"[\x00-\x1f\x7f]", path) or not (path.startswith(("/", "~/")) or path == "~"):
        return None
    return (host, user, path)


def _matches_host(reported, expected):
    reported = reported.strip("[]").rstrip(".").lower()
    expected = expected.strip("[]").rstrip(".").lower()
    return reported == expected or ("." not in reported and expected.startswith(reported + "."))


def resolve_terminal_directory(report, host, user, home, read_hostname):
    reported_host, reported_user, path = report
    if reported_host and not _matches_host(reported_host, host):
        if not _matches_host(reported_host, read_hostname()):
            raise IOError("Текущий каталог относится к другому SSH-хосту или имя сервера не подтверждено.")
    if path.startswith("~"):
        if reported_user != user or not home or not home.startswith("/"):
            raise IOError("Терминал сообщил домашний каталог другого пользователя. Нужен абсолютный путь из оболочки.")
        path = home.rstrip("/") + "/" + path[2:]
    return path
