"""Real GTK update buttons, cancellation and package command; synthetic metadata."""
import json
import os
import sys
import tempfile
import threading
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
from astraterm.app import MainWindow
from astraterm.core import SessionStore
from smoke_multiexec import wait_for, pump


with tempfile.TemporaryDirectory(prefix='astraterm-update-gui-') as directory:
    window = MainWindow(SessionStore(os.path.join(directory, 'sessions.json')), start_local=False)
    try:
        item = {'available':True,'version':'99.0.0','notes':'Synthetic update acceptance test'}
        with mock.patch('astraterm.update_ui.updates.check', return_value=item):
            window.updates.check()
            wait_for(lambda: not window.updates.busy, 'update check completed')
        assert window.updates.action.get_sensitive()
        assert '99.0.0' in window.updates.label.get_text()
        filename = os.path.join(directory, 'fixture.deb')
        with open(filename, 'wb') as target:
            target.write(b'not an installable package')
        contract_path = os.path.join(os.path.dirname(__file__), '..', 'shared', 'contracts', 'updates.json')
        with open(contract_path, encoding='utf8') as stream:
            vector = json.load(stream)['download_progress']
        downloaded, verified = threading.Event(), threading.Event()

        def download(unused_item, unused_directory, progress):
            for received in vector['received'][:3]:
                progress(received, vector['total'])
            if not downloaded.wait(5):
                raise RuntimeError('Progress was not displayed')
            for received in vector['received'][3:]:
                progress(received, vector['total'])
            if not verified.wait(5):
                raise RuntimeError('Verification was not displayed')
            return filename

        with mock.patch('astraterm.update_ui.updates.download', side_effect=download):
            window.updates.dialog.response(Gtk.ResponseType.ACCEPT)
            assert not window.updates.action.get_sensitive()
            assert window.updates.progress.get_visible()
            wait_for(lambda: window.updates.progress.get_fraction() == vector['percent'][1] / 100, 'intermediate progress')
            downloaded.set()
            wait_for(lambda: window.updates.progress.get_fraction() == 1, 'all bytes received')
            assert 'SHA-256' in window.updates.label.get_text()
            assert not window.updates.action.get_sensitive()
            verified.set()
            wait_for(lambda: not window.updates.busy, 'verified download UI')
        assert not window.updates.progress.get_visible()
        assert window.updates.action.get_label() == 'Запустить установщик'
        with mock.patch('astraterm.update_ui.confirm', return_value=True), \
                mock.patch('astraterm.update_ui.shutil.which', side_effect=lambda name: '/usr/bin/pkexec' if name == 'pkexec' else None), \
                mock.patch('astraterm.update_ui.subprocess.check_call') as install:
            window.updates.dialog.response(Gtk.ResponseType.ACCEPT)
            wait_for(lambda: not window.updates.busy, 'installation result UI')
            install.assert_called_once_with(['/usr/bin/pkexec', '/usr/bin/dpkg', '-i', filename])
        assert 'Обновление установлено' in window.updates.label.get_text()
        assert not window.updates.action.get_sensitive()
        window.updates.dialog.response(Gtk.ResponseType.CLOSE)
        assert window.updates.dialog is None
        print('UPDATES_GUI_PASS signed metadata UI / download / explicit system installer / completion', flush=True)
    finally:
        window.shutdown()
        window.destroy()
