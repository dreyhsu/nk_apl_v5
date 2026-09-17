"""Own Qualcomm resources in a supervised process with bounded local IPC."""
from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

MAX_PACKET = 16 * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
RECOVERY_FILE = BASE_DIR / 'runtime_data' / 'edge' / 'camera_recovery_required.json'
RECOVERY_ARCHIVE_DIR = RECOVERY_FILE.parent / 'recovery_archive'
BOOT_ID_FILE = Path('/proc/sys/kernel/random/boot_id')
_owner_lock = threading.Lock()
_owner = None


def _latest_put(target, value):
    try:
        target.get_nowait()
    except queue.Empty:
        pass
    try:
        target.put_nowait(value)
    except queue.Full:
        pass


def _recv_exact(sock, size, deadline=None):
    chunks = bytearray()
    while len(chunks) < size:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('Camera IPC handshake timed out')
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout:
            continue
        if not chunk:
            raise EOFError('Camera IPC disconnected')
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_packet(sock, deadline=None):
    length = struct.unpack('!I', _recv_exact(sock, 4, deadline))[0]
    if not 1 <= length <= MAX_PACKET:
        raise ValueError('Camera IPC packet size rejected')
    payload = _recv_exact(sock, length, deadline)
    if payload[:1] == b'F':
        if length < 10:
            raise ValueError('Camera IPC frame rejected')
        return 'frame', (struct.unpack('!d', payload[1:9])[0], payload[9:])
    if payload[:1] != b'J':
        raise ValueError('Camera IPC packet type rejected')
    data = json.loads(payload[1:])
    if not isinstance(data, dict):
        raise ValueError('Camera IPC object required')
    return 'json', data


def _send_packet(sock, value, lock, frame=False):
    if frame:
        timestamp, jpeg = value
        payload = b'F' + struct.pack('!d', timestamp) + jpeg
    else:
        payload = b'J' + json.dumps(value, ensure_ascii=True, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_PACKET:
        raise ValueError('Camera IPC payload too large')
    with lock:
        sock.sendall(struct.pack('!I', len(payload)) + payload)


def _read_boot_id():
    try:
        value = BOOT_ID_FILE.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        return ''
    return value if value and len(value) <= 128 else ''


def _archive_stale_recovery(marker_boot_id, current_boot_id):
    RECOVERY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    old_id = ''.join(c for c in marker_boot_id if c.isalnum() or c in '-_')[:64] or 'unknown'
    current_id = ''.join(c for c in current_boot_id if c.isalnum() or c in '-_')[:64] or 'unknown'
    target = RECOVERY_ARCHIVE_DIR / f'camera_recovery_required_{old_id}_before_{current_id}.json'
    if target.exists():
        target = RECOVERY_ARCHIVE_DIR / f'camera_recovery_required_{old_id}_{time.time_ns()}.json'
    RECOVERY_FILE.replace(target)
    return target


def _enforce_recovery_guard():
    if not RECOVERY_FILE.exists():
        return None
    try:
        marker = json.loads(RECOVERY_FILE.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            'Camera recovery marker is unreadable; inspect camera_recovery_required.json before recommissioning.'
        ) from exc
    if not isinstance(marker, dict):
        raise RuntimeError(
            'Camera recovery marker is invalid; inspect camera_recovery_required.json before recommissioning.'
        )
    marker_boot_id = str(marker.get('boot_id') or '').strip()
    current_boot_id = _read_boot_id()
    if marker_boot_id and current_boot_id and marker_boot_id != current_boot_id:
        return _archive_stale_recovery(marker_boot_id, current_boot_id)
    if not marker_boot_id:
        raise RuntimeError(
            'Legacy camera recovery marker requires one-time inspection or archival before recommissioning.'
        )
    if not current_boot_id:
        raise RuntimeError(
            'Camera recovery cannot verify the current boot; inspect camera_recovery_required.json before recommissioning.'
        )
    raise RuntimeError(
        'Camera recovery is required during this boot. Reboot before recommissioning.'
    )


class WorkerCameraBackend:
    name = 'qti'
    hardware_recording = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.sock = None
        self._send_lock = threading.Lock()
        self._frames = queue.Queue(maxsize=1)
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._condition = threading.Condition()
        self._responses = {}
        self._status = {}
        self._fatal = ''
        self._safe_stop = False
        self._receiver = None
        self._last_overlay_send = 0.0
        self.last_frame_monotonic = 0.0

    def _mark_recovery(self, reason):
        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(json.dumps({'reason': reason, 'time': time.time(),
                                            'monotonic': time.monotonic(),
                                            'boot_id': _read_boot_id(),
                                            'pid': self.process.pid if self.process else None}), encoding='utf-8')

    def start(self):
        global _owner
        with _owner_lock:
            _enforce_recovery_guard()
            if _owner is not None and _owner.process is not None and _owner.process.poll() is None:
                raise RuntimeError('Previous camera worker is still alive; reacquisition blocked')
            _owner = self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(10.0)
        token = secrets.token_hex(32)
        port = listener.getsockname()[1]
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-u', str(Path(__file__).resolve()), '--ipc-port', str(port)],
                cwd=str(BASE_DIR), stdin=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0,
            )
            self.process.stdin.write(json.dumps({'token': token, 'config': dataclasses.asdict(self.cfg)}) + '\n')
            self.process.stdin.close()
            self.sock, _ = listener.accept()
            self.sock.settimeout(1.0)
            kind, auth = _recv_packet(self.sock, time.monotonic() + 5.0)
            if kind != 'json' or not hmac.compare_digest(str(auth.get('token', '')), token):
                raise RuntimeError('Camera IPC authentication failed')
            self._receiver = threading.Thread(target=self._receive, daemon=True, name='apl-camera-ipc')
            self._receiver.start()
            if not self._ready.wait(20.0):
                raise RuntimeError('Camera worker startup timed out')
            if self._fatal:
                raise RuntimeError(self._fatal)
        except Exception:
            try:
                self.stop()
            except Exception:
                pass
            raise
        finally:
            listener.close()

    def _receive(self):
        try:
            while True:
                kind, data = _recv_packet(self.sock)
                if kind == 'frame':
                    _latest_put(self._frames, data)
                    continue
                event = data.get('event')
                if event == 'ready':
                    self._status = data.get('status') or {}
                    if not data.get('success'):
                        self._fatal = data.get('error') or 'Camera worker failed to start'
                    self._ready.set()
                elif event == 'status':
                    self._status = data.get('status') or {}
                elif event == 'error':
                    self._fatal = data.get('error') or 'Camera worker error'
                    self._ready.set()
                elif event == 'stopped':
                    self._safe_stop = bool(data.get('safe'))
                    if data.get('error'):
                        self._fatal = data['error']
                    self._stopped.set()
                elif event == 'reply':
                    with self._condition:
                        self._responses[data.get('id')] = data.get('result') or {}
                        self._condition.notify_all()
        except (OSError, EOFError, ValueError) as exc:
            if not self._stopped.is_set():
                self._fatal = self._fatal or str(exc)
            self._ready.set()
            with self._condition:
                self._condition.notify_all()

    def _command(self, action, values=None, timeout=8.0):
        if self.sock is None or self.process is None or self.process.poll() is not None:
            return {'success': False, 'error': self._fatal or 'Camera worker unavailable'}
        request_id = secrets.token_hex(8)
        data = {'action': action, 'id': request_id, **(values or {})}
        try:
            _send_packet(self.sock, data, self._send_lock)
        except OSError as exc:
            return {'success': False, 'error': str(exc)}
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.process.poll() is not None:
                    return {'success': False, 'error': f'Camera command timed out: {action}'}
                self._condition.wait(min(remaining, 0.25))
            return self._responses.pop(request_id)

    def read(self, timeout=1.0):
        if self._fatal:
            raise RuntimeError(self._fatal)
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError('Camera worker exited')
        try:
            timestamp, jpeg = self._frames.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return None
        self.last_frame_monotonic = timestamp
        from edge_runtime import _decode_jpeg
        return _decode_jpeg(jpeg)

    def set_overlay(self, state):
        if self.sock is None or self._stopped.is_set():
            return
        now = time.monotonic()
        if now - self._last_overlay_send < 0.05:
            return
        self._last_overlay_send = now
        try:
            _send_packet(self.sock, {'action': 'overlay', 'state': state}, self._send_lock)
        except OSError as exc:
            self._fatal = f'Camera overlay IPC failed: {exc}'

    def start_recording(self, path):
        result = self._command('record_start', {'path': str(path)})
        return bool(result.get('success')), result.get('path') or result.get('error', '')

    def stop_recording(self):
        return self._command('record_stop', timeout=9.0)

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                if self.sock is not None:
                    _send_packet(self.sock, {'action': 'stop'}, self._send_lock)
                else:
                    self.process.terminate()
            except OSError:
                pass
            try:
                self.process.wait(timeout=18.0)
            except subprocess.TimeoutExpired as exc:
                reason = 'Camera worker did not exit after graceful stop; automatic reacquisition blocked'
                self._mark_recovery(reason)
                raise RuntimeError(reason) from exc
        self._stopped.wait(0.5)
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self._receiver:
            self._receiver.join(timeout=1.0)
        if not self._safe_stop:
            reason = self._fatal or f'Unconfirmed camera release (worker exit {self.process.returncode})'
            self._mark_recovery(reason)
            raise RuntimeError(reason)

    def status(self):
        return {'backend': 'qti', 'hardware_recording': True,
                'hardware_recording_mode': 'result', **self._status,
                'last_error': self._fatal or self._status.get('last_error', ''),
                'worker_pid': self.process.pid if self.process else None,
                'worker_alive': bool(self.process and self.process.poll() is None)}


def worker_main(port):
    bootstrap = json.loads(sys.stdin.readline())
    sock = socket.create_connection(('127.0.0.1', port), timeout=5.0)
    sock.settimeout(1.0)
    send_lock = threading.Lock()
    _send_packet(sock, {'token': bootstrap['token']}, send_lock)
    commands = queue.Queue(maxsize=32)
    messages = queue.Queue(maxsize=64)
    frames = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    sender_stop = threading.Event()
    backend = None
    safe = False
    error = ''
    last_frame = [time.monotonic()]

    def send_loop():
        try:
            while not sender_stop.is_set() or not messages.empty():
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    try:
                        frame = frames.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    _send_packet(sock, frame, send_lock, frame=True)
                else:
                    _send_packet(sock, message, send_lock)
        except (OSError, ValueError):
            stop_event.set()

    def receive_loop():
        try:
            while not stop_event.is_set():
                kind, command = _recv_packet(sock)
                if kind != 'json':
                    raise ValueError('Unexpected camera command packet')
                if command.get('action') == 'overlay':
                    if backend is not None:
                        backend.set_overlay(command.get('state') or {})
                elif command.get('action') == 'stop':
                    stop_event.set()
                else:
                    commands.put(command, timeout=1.0)
        except (OSError, EOFError, ValueError, queue.Full):
            stop_event.set()

    def publish_frame(jpeg, timestamp):
        last_frame[0] = timestamp
        _latest_put(frames, (timestamp, jpeg))

    sender = threading.Thread(target=send_loop, daemon=True, name='apl-camera-send')
    receiver = threading.Thread(target=receive_loop, daemon=True, name='apl-camera-control')
    sender.start()
    receiver.start()
    try:
        from edge_runtime import EdgeConfig, QtiGstCameraBackend
        from gi.repository import GLib
        cfg = EdgeConfig(**bootstrap['config'])
        backend = QtiGstCameraBackend(cfg)
        backend._frame_callback = publish_frame
        loop = GLib.MainLoop()

        def request_stop(*args):
            stop_event.set()
            loop.quit()
            return GLib.SOURCE_CONTINUE

        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, request_stop)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, request_stop)
        backend.start()
        messages.put({'event': 'ready', 'success': True, 'status': backend.status()})
        last_status = [0.0]

        def pump():
            if stop_event.is_set():
                loop.quit()
                return GLib.SOURCE_REMOVE
            status = backend.status()
            fatal = status.get('last_error', '')
            if fatal and not str(fatal).startswith('warning:'):
                messages.put({'event': 'error', 'error': fatal})
                loop.quit()
                return GLib.SOURCE_REMOVE
            if time.monotonic() - last_frame[0] > 10.0:
                messages.put({'event': 'error', 'error': 'Camera produced no frames for 10 seconds'})
                loop.quit()
                return GLib.SOURCE_REMOVE
            try:
                command = commands.get_nowait()
            except queue.Empty:
                command = None
            if command:
                try:
                    if command.get('action') == 'record_start':
                        target = Path(str(command.get('path', ''))).resolve()
                        allowed = (BASE_DIR / 'runtime_data' / 'edge' / 'recordings').resolve()
                        if target.parent != allowed or target.suffix.lower() != '.mp4':
                            raise ValueError('Recording path is outside the recording folder')
                        ok, detail = backend.start_recording(target)
                        result = {'success': ok, 'path': detail if ok else '', 'error': '' if ok else detail}
                    elif command.get('action') == 'record_stop':
                        result = backend.stop_recording()
                    else:
                        result = {'success': False, 'error': 'Unknown camera command'}
                except Exception as exc:
                    result = {'success': False, 'error': str(exc)}
                messages.put({'event': 'reply', 'id': command.get('id'), 'result': result})
            if time.monotonic() - last_status[0] >= 0.5:
                last_status[0] = time.monotonic()
                messages.put({'event': 'status', 'status': backend.status()})
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(50, pump)
        loop.run()
    except Exception as exc:
        error = str(exc)
        try:
            messages.put_nowait({'event': 'error', 'error': error})
        except queue.Full:
            pass
    finally:
        stop_event.set()
        watchdog = threading.Timer(12.0, lambda: os._exit(70))
        watchdog.daemon = True
        watchdog.start()
        try:
            if backend is not None:
                backend.stop()
            safe = True
        except Exception as exc:
            error = f'Camera cleanup failed: {exc}'
        try:
            _send_packet(sock, {'event': 'stopped', 'safe': safe, 'error': error}, send_lock)
        except OSError:
            pass
        sender_stop.set()
        sender.join(timeout=1.0)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        watchdog.cancel()
        # Bypass native-element finalizers only after NULL release was attempted.
        os._exit(0 if safe else 70)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ipc-port', type=int, required=True)
    worker_main(parser.parse_args().ipc_port)
