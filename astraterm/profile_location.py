"""Select the legacy data directory or a fully restored profile at startup."""
import json
import os
import re


MARKER = 'active-profile.json'
PROFILES = 'restored-profiles'


def profile_root():
    if os.name == 'nt':
        return os.path.abspath(os.path.join(os.environ.get('APPDATA') or
                               os.path.expanduser('~/AppData/Roaming'), 'AstraTerm'))
    return os.path.abspath(os.path.join(os.environ.get('XDG_CONFIG_HOME') or
                           os.path.expanduser('~/.config'), 'astraterm'))


def active_profile(root):
    root = os.path.abspath(root)
    try:
        with open(os.path.join(root, MARKER), 'rb') as stream:
            raw = stream.read(1025)
    except FileNotFoundError:
        return root
    try:
        identifier = json.loads(raw.decode('utf-8'))['directory']
        if len(raw) > 1024 or not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError()
        parent = os.path.join(root, PROFILES)
        directory = os.path.join(parent, identifier)
        if os.path.islink(parent) or os.path.islink(directory) or not os.path.isdir(directory):
            raise ValueError()
        return directory
    except (KeyError, TypeError, ValueError):
        raise ValueError('Не удалось открыть восстановленный профиль AstraTerm.') from None
