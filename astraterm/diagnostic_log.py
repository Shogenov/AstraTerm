"""Opt-in diagnostic events without terminal or credential data."""
from __future__ import unicode_literals

import datetime
import json
import os
import re
import threading

from .core import _atomic_json, _config_lock

LEVELS = ('error', 'warning', 'info', 'debug')
MAX_LOG_BYTES = 1024 * 1024


class DiagnosticLog(object):
    def __init__(self, directory):
        self.settings_path = os.path.join(directory, 'logging.json')
        self.filename = os.path.join(directory, 'astraterm.log')
        self.enabled, self.level = False, 'info'
        self._lock = threading.Lock()
        try:
            with open(self.settings_path, encoding='utf-8') as stream:
                stored = json.load(stream)
            if isinstance(stored.get('enabled'), bool) and stored.get('level') in LEVELS:
                self.enabled, self.level = stored['enabled'], stored['level']
        except (OSError, ValueError, AttributeError):
            pass

    def configure(self, enabled, level):
        if not isinstance(enabled, bool) or level not in LEVELS:
            raise ValueError('Некорректные настройки журнала.')
        with _config_lock(self.settings_path):
            _atomic_json(self.settings_path, {'enabled': enabled, 'level': level})
        self.enabled, self.level = enabled, level

    def write(self, level, event):
        if (not self.enabled or level not in LEVELS or LEVELS.index(level) > LEVELS.index(self.level) or
                not re.fullmatch(r'[a-z][a-z0-9.:-]{0,79}', event)):
            return
        with self._lock:
            try:
                if os.path.isfile(self.filename) and os.path.getsize(self.filename) >= MAX_LOG_BYTES:
                    os.replace(self.filename, self.filename + '.1')
                timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
                entry = {'time': timestamp, 'level': level, 'event': event}
                descriptor = os.open(self.filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(json.dumps(entry) + '\n')
            except OSError:
                pass
