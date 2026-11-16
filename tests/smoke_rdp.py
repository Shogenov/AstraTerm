"""Opt-in actual RDP server test. Read {host,username,password} from private stdin, never argv."""
from __future__ import print_function
import json
import os
import subprocess
import sys
import tempfile
import time


def capture_x11(window, destination):
    # GDK/Cairo snapshots of a foreign native surface can assert on Astra 1.6.
    # Read only this test's owned X11 window directly, without Cairo/CopyArea.
    import ctypes, ctypes.util, struct, zlib
    from gi.repository import GdkX11
    class Image(ctypes.Structure):
        _fields_=[('width',ctypes.c_int),('height',ctypes.c_int),('offset',ctypes.c_int),('format',ctypes.c_int),
                  ('data',ctypes.c_void_p),('byte_order',ctypes.c_int),('unit',ctypes.c_int),('bit_order',ctypes.c_int),
                  ('pad',ctypes.c_int),('depth',ctypes.c_int),('stride',ctypes.c_int),('bpp',ctypes.c_int),
                  ('red',ctypes.c_ulong),('green',ctypes.c_ulong),('blue',ctypes.c_ulong)]
    lib=ctypes.CDLL(ctypes.util.find_library('X11'));lib.XOpenDisplay.argtypes=[ctypes.c_char_p];lib.XOpenDisplay.restype=ctypes.c_void_p
    lib.XGetImage.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_int,ctypes.c_uint,ctypes.c_uint,ctypes.c_ulong,ctypes.c_int]
    lib.XGetImage.restype=ctypes.POINTER(Image);lib.XDestroyImage.argtypes=[ctypes.POINTER(Image)];lib.XCloseDisplay.argtypes=[ctypes.c_void_p]
    gdk=ctypes.CDLL(ctypes.util.find_library('gdk-3'));gdk.gdk_x11_get_default_xdisplay.restype=ctypes.c_void_p
    display=gdk.gdk_x11_get_default_xdisplay();assert display
    image=None
    try:
        image=lib.XGetImage(display,GdkX11.X11Window.get_xid(window.get_window()),0,0,window.get_allocated_width(),window.get_allocated_height(),0xffffffff,2)
        assert image, 'XGetImage failed for the owned test window'
        item=image.contents;assert item.bpp==32 and item.byte_order==0 and item.red==0xff0000
        raw=ctypes.string_at(item.data,item.stride*item.height);rows=[]
        for y in range(item.height):
            row=raw[y*item.stride:y*item.stride+item.width*4];rgb=bytearray(item.width*3)
            rgb[0::3]=row[2::4];rgb[1::3]=row[1::4];rgb[2::3]=row[0::4];rows.append(b'\x00'+bytes(rgb))
        def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        with open(destination,'wb') as stream:
            stream.write(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',item.width,item.height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b''))
    finally:
        if image:lib.XDestroyImage(image)
        # The X connection belongs to GTK; never close it here.
        pass


def main():
    configuration = json.load(sys.stdin)
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'shared', 'contracts', 'rdp.json')) as source:
        input_contract=json.load(source)['native_input']
    if '--child' not in sys.argv:
        # All pointer/screenshot operations are confined to an owned nested X server.
        with tempfile.TemporaryDirectory(prefix='astraterm-rdp-test-') as directory:
            display = next(number for number in range(100, 200) if not os.path.exists('/tmp/.X11-unix/X' + str(number)))
            nested = subprocess.Popen(['Xephyr', ':' + str(display), '-screen', '1440x1000', '-nolisten', 'tcp', '-ac'],
                env=dict(os.environ, DISPLAY=':0'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for attempt in range(100):
                    if os.path.exists('/tmp/.X11-unix/X' + str(display)): break
                    if nested.poll() is not None: raise RuntimeError('Xephyr failed')
                    time.sleep(.1)
                environment = dict(os.environ, DISPLAY=':' + str(display), HOME=directory, XDG_CONFIG_HOME=directory)
                child = subprocess.Popen([sys.executable, __file__, '--child'], stdin=subprocess.PIPE, env=environment)
                child.communicate(json.dumps(configuration).encode('utf-8'))
                return child.returncode
            finally:
                nested.terminate(); nested.wait()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib, Gdk
    from astraterm.app import MainWindow
    from astraterm.core import SessionStore, default_session
    from astraterm.rdp import RdpPane
    from unittest import mock
    from smoke_multiexec import pump, wait_for
    report = {'platform': 'astra-linux', 'checks': [], 'result': 'RUNNING'}
    output = os.environ.get('ASTRA_RDP_TEST_OUTPUT', os.getcwd())
    window = None
    def inspect_certificate(dialog):
        # Test-only acceptance. Assert this is the expected RDP warning, never any authentication/error prompt.
        title = dialog.get_property('text')
        assert title == 'Проверка сертификата RDP · ' + configuration['host']
        details = dialog.get_property('secondary-text')
        assert 'thumbprint:' in details.lower() or 'fingerprint:' in details.lower(), 'Certificate details must be visible'
        report['checks'].append('explicit once-only certificate warning includes fingerprint')
        return Gtk.ResponseType.OK
    try:
        with tempfile.TemporaryDirectory(prefix='astraterm-rdp-data-') as directory:
            window = MainWindow(SessionStore(os.path.join(directory, 'sessions.json')), start_local=False)
            window.resize(1200, 820); window.present()
            profile = default_session('rdp');profile.update(host=configuration['host'],username=configuration['username'],name='RDP integration',_credential_provider=lambda:configuration['password'])
            with mock.patch.object(Gtk.MessageDialog, 'run', inspect_certificate):
                workspace = window.open_session(profile); assert workspace
                pane = workspace.active; assert isinstance(pane, RdpPane)
                wait_for(lambda: pane.child_xid is not None, 'real RDP desktop embedded', timeout=60)
                pump(5)
            assert pane.pid and pane.process.poll() is None
            assert pane.child_xid in pane.x11.children(pane.terminal.get_id())
            report['checks'].append('actual desktop authenticated inside owned GTK X11 child')
            assert not window.multiexec.eligible(pane)
            report['checks'].append('RDP excluded from MultiExec')
            first = pane.terminal.get_allocation()
            window.resize(1380, 950); pump(2)
            assert pane.terminal.get_allocated_width() > first.width
            assert pane.terminal.get_allocated_height() > first.height
            report['checks'].append('native view resizes with window')
            process, child_xid, socket_xid = pane.process, pane.child_xid, pane.terminal.get_id()
            print('RDP_FLOAT_BEGIN',flush=True)
            assert window.float_window(workspace);pump(1)
            print('RDP_FLOAT_MAPPED',flush=True)
            floating = window.floating_windows[workspace]
            assert pane.process is process and pane.child_xid == child_xid and pane.terminal.get_id() == socket_xid
            assert pane.child_xid in pane.x11.children(pane.terminal.get_id())
            assert pane.terminal.get_mapped() and process.poll() is None
            capture_window = floating.window
            report['checks'].append('RDP floats with identical FreeRDP process, child XID and Gtk.Socket XID')
            # Harmless real keyboard/clipboard check: fill the remote Run box, never execute it.
            import ctypes, ctypes.util
            from smoke_tabs import Pointer
            pointer = Pointer()
            try:
                x11, xtest = pointer.x11, pointer.xtest
                x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                x11.XKeysymToKeycode.restype = ctypes.c_uint
                xtest.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
                # Probe the X server's actual active keyboard grab. Sending Win in
                # an empty Xephyr alone cannot prove a host WM would not intercept it.
                x11.XDefaultRootWindow.argtypes=[ctypes.c_void_p];x11.XDefaultRootWindow.restype=ctypes.c_ulong
                x11.XGrabKeyboard.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_ulong]
                x11.XUngrabKeyboard.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
                wm=x11.XOpenDisplay(None);assert wm
                root=x11.XDefaultRootWindow(wm)
                def keyboard_grabbed():
                    result=x11.XGrabKeyboard(wm,root,False,1,1,0)
                    if result==0:x11.XUngrabKeyboard(wm,0);x11.XFlush(wm)
                    assert result in [0,1], 'Unexpected keyboard grab result: '+str(result)
                    return result==1
                def key(symbol, pressed):
                    xtest.XTestFakeKeyEvent(pointer.display, x11.XKeysymToKeycode(pointer.display, Gdk.keyval_from_name(symbol)), int(pressed), 0)
                    x11.XFlush(pointer.display);pump(.08)
                print('RDP_INPUT_BEGIN',flush=True)
                pointer.move(*pointer.center(floating.window.get_children()[0].get_children()[0]));pump(.2)
                from gi.repository import GdkX11
                x11.XSetInputFocus.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
                x11.XSetInputFocus(pointer.display,GdkX11.X11Window.get_xid(floating.window.get_window()),1,0)
                x11.XFlush(pointer.display);pump(.3)
                key('Super_L',True);key('Super_L',False);pump(.3)
                assert not keyboard_grabbed(), 'Client grabbed keyboard outside the remote view'
                pointer.move(*pointer.center(pane.terminal));pointer.button(1,True);pointer.button(1,False)
                pane.x11.focus(pane.child_xid);pump(.2)
                assert keyboard_grabbed(), 'Client must own keyboard while remote view is focused'
                for start_key in input_contract['start_keys']:
                    key(start_key['keysym'],True);key(start_key['keysym'],False);pump(1)
                    assert keyboard_grabbed(), 'Keyboard capture lost on Windows key'
                    capture_x11(capture_window,os.path.join(output,start_key['screenshot']))
                    key('Escape',True);key('Escape',False);pump(.3)
                run_key=input_contract['run_key']['keysym']
                key('Super_L',True);key(run_key,True);key(run_key,False);key('Super_L',False);pump(1)
                assert keyboard_grabbed(), 'Keyboard capture lost on Win+R'
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clipboard.set_text('AstraTerm RDP clipboard check', -1);pump(.8)
                key('Control_L',True);key('v',True);key('v',False);key('Control_L',False);pump(1)
                print('RDP_INPUT_CAPTURE',flush=True)
                capture_x11(capture_window,os.path.join(output,'input.png'))
                key('Escape',True);key('Escape',False);pump(.5)
                report['checks'].append('server confirms exclusive keyboard grab; left/right Win and Win+R remote Start/Run screenshots captured')
                # Leaving the RDP surface must release the native client's keyboard grab.
                pointer.move(*pointer.center(floating.window.get_children()[0].get_children()[0]));pump(.2)
                x11.XSetInputFocus(pointer.display,GdkX11.X11Window.get_xid(floating.window.get_window()),1,0)
                x11.XFlush(pointer.display);pump(.3)
                key('Super_L',True);key('Super_L',False);pump(.3)
                assert not keyboard_grabbed(), 'RDP did not release keyboard after local focus'
                report['checks'].append('leaving RDP releases keyboard to the local desktop')
                x11.XCloseDisplay(wm)
            finally: pointer.close()
            print('RDP_DOCK_BEGIN',flush=True)
            floating.window.close();pump(.8)
            print('RDP_DOCK_MAPPED',flush=True)
            assert workspace not in window.floating_windows
            assert pane.process is process and pane.child_xid == child_xid and pane.terminal.get_id() == socket_xid
            assert pane.terminal.get_mapped() and process.poll() is None
            report['checks'].append('native floating window close returns RDP tab without reconnect or socket recreation')
            pixbuf = Gdk.pixbuf_get_from_window(window.get_window(), 0, 0, window.get_allocated_width(), window.get_allocated_height())
            if pixbuf: pixbuf.savev(os.path.join(output, 'desktop.png'), 'png', [], [])
            process = pane.process
            local = window.new_local(); pump(.8)
            assert pane.process is process and not pane.terminal.get_mapped()
            window.notebook.set_current_page(window.notebook.page_num(workspace));pump(.5)
            assert pane.terminal.get_mapped() and process.poll() is None
            report['checks'].append('tab switches preserve connection and hide desktop')
            with mock.patch.object(Gtk.MessageDialog, 'run', inspect_certificate):
                pane.restart()
                wait_for(lambda: pane.child_xid is not None, 'RDP reconnect', timeout=60);pump(1)
            assert pane.process is not process and process.poll() is not None
            process = pane.process
            report['checks'].append('reconnect replaces and reaps only its own RDP client')
            window.close_tab(workspace=workspace, ask=False)
            wait_for(lambda: process.poll() is not None, 'RDP process reaped')
            report['checks'].append('closing tab reaps owned FreeRDP process')
            report['result'] = 'PASS'
    except Exception as exc:
        report['result'] = 'FAIL';report['error'] = str(exc)
        if window:
            for pane in window.all_panes():
                if isinstance(pane, RdpPane): report['diagnostic'] = {'exit_code':pane.exit_code,'message':pane.message.get_text()}
        raise
    finally:
        if window: window.shutdown();window.destroy();pump(.2)
        with open(os.path.join(output, 'report.json'), 'w') as stream: json.dump(report, stream, indent=2)
        print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__': sys.exit(main())
