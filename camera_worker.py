"""Own Qualcomm resources in a supervised process with bounded local IPC."""
from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
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
_recovery_required = ''
GRACE_SECONDS = 22.0
TERM_SECONDS = 3.0
KILL_SECONDS = 2.0
log = logging.getLogger('camera.lifecycle')
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False


def _configure_lifecycle_file_log(role):
    name = f'camera_{role}.log'
    if any(getattr(handler, '_camera_log_role', None) == role for handler in log.handlers):
        return
    try:
        directory = BASE_DIR / 'runtime_data' / 'edge' / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / name, maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding='utf-8')
        handler._camera_log_role = role
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        log.addHandler(handler)
    except OSError as exc:
        log.warning('Camera lifecycle file log unavailable: %s', exc)


def _shared_recovery_file(camera):
    return Path(f'/run/visionedge-camera-{int(camera)}.recovery.json') if os.name == 'posix' else None


def _persist_recovery(reason, session, pid, camera, release):
    global _recovery_required
    _recovery_required = reason
    value = {'reason': reason, 'time': time.time(), 'monotonic': time.monotonic(),
             'boot_id': _read_boot_id(), 'session': session, 'release': release, 'pid': pid}
    failures = []
    for path in (RECOVERY_FILE, _shared_recovery_file(camera)):
        if path is None:
            continue
        temporary = path.with_name(path.name + '.' + session + '.' + secrets.token_hex(8) + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open('w', encoding='utf-8') as handle:
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            failures.append(f'{path}: {exc}')
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    if failures:
        raise OSError('; '.join(failures))


def _watchdog_exit(session, camera, release):
    try:
        _persist_recovery('Worker cleanup watchdog expired; release unconfirmed',
                          session, os.getpid(), camera, release)
    except OSError:
        pass
    finally:
        os._exit(70)


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


def _send_packet(sock, value, lock, frame=False, timeout=1.0):
    if frame:
        timestamp, jpeg = value
        payload = b'F' + struct.pack('!d', timestamp) + jpeg
    else:
        payload = b'J' + json.dumps(value, ensure_ascii=True, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_PACKET:
        raise ValueError('Camera IPC payload too large')
    if not lock.acquire(timeout=timeout):
        raise TimeoutError('Camera IPC send lock timed out')
    try:
        sock.sendall(struct.pack('!I', len(payload)) + payload)
    finally:
        lock.release()


def _read_boot_id():
    try:
        value = BOOT_ID_FILE.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        return ''
    return value if value and len(value) <= 128 else ''


def _archive_stale_recovery(marker_boot_id, current_boot_id, path=None):
    path = path or RECOVERY_FILE
    archive = RECOVERY_ARCHIVE_DIR if path == RECOVERY_FILE else path.parent / 'visionedge-recovery-archive'
    archive.mkdir(parents=True, exist_ok=True)
    old_id = ''.join(c for c in marker_boot_id if c.isalnum() or c in '-_')[:64] or 'unknown'
    current_id = ''.join(c for c in current_boot_id if c.isalnum() or c in '-_')[:64] or 'unknown'
    target = archive / f'{path.stem}_{old_id}_before_{current_id}.json'
    if target.exists():
        target = archive / f'{path.stem}_{old_id}_{time.time_ns()}.json'
    path.replace(target)
    return target


def _enforce_recovery_guard(path=None):
    if _recovery_required:
        raise RuntimeError(_recovery_required)
    path = path or RECOVERY_FILE
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding='utf-8'))
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
        return _archive_stale_recovery(marker_boot_id, current_boot_id, path)
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
        self.session = secrets.token_hex(16)
        self._state_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._cancel = threading.Event()
        self._authenticated = False
        self._stop_result = None
        self._release = {}

    def _mark_recovery(self, reason):
        _persist_recovery(reason, self.session, self.process.pid if self.process else None,
                          self.cfg.camera, self._release)

    def request_stop(self):
        self._cancel.set()
        with self._condition:
            self._condition.notify_all()
        if self.sock is not None and self._authenticated:
            try:
                _send_packet(self.sock, {'action': 'stop', 'session': self.session}, self._send_lock)
            except (OSError, TimeoutError):
                pass

    def start(self):
        global _owner
        _configure_lifecycle_file_log('parent')
        with _owner_lock:
            if self._cancel.is_set() or self._stop_result is not None:
                raise RuntimeError('Camera startup cancelled')
            _enforce_recovery_guard()
            shared = _shared_recovery_file(self.cfg.camera)
            if shared is not None:
                _enforce_recovery_guard(shared)
            if _owner is not None and _owner is not self:
                raise RuntimeError('Previous camera worker has not completed release; reacquisition blocked')
            _owner = self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(0.2)
        token = secrets.token_hex(32)
        port = listener.getsockname()[1]
        try:
            with self._state_lock:
                if self._cancel.is_set():
                    raise RuntimeError('Camera startup cancelled')
                self.process = subprocess.Popen(
                    [sys.executable, '-u', str(Path(__file__).resolve()), '--ipc-port', str(port)],
                    cwd=str(BASE_DIR), stdin=subprocess.PIPE, text=True,
                    start_new_session=(os.name == 'posix'),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0,
                )
            self.process.stdin.write(json.dumps({'token': token, 'session': self.session,
                                                'config': dataclasses.asdict(self.cfg)}) + '\n')
            self.process.stdin.close()
            deadline = time.monotonic() + 10.0
            while True:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Camera worker connection failed or timed out')
                try:
                    self.sock, _ = listener.accept()
                    break
                except socket.timeout:
                    continue
            self.sock.settimeout(1.0)
            kind, auth = _recv_packet(self.sock, time.monotonic() + 5.0)
            if kind != 'json' or not hmac.compare_digest(str(auth.get('token', '')), token):
                raise RuntimeError('Camera IPC authentication failed')
            self._authenticated = True
            if self._cancel.is_set():
                self.request_stop()
            self._receiver = threading.Thread(target=self._receive, daemon=True, name='apl-camera-ipc')
            self._receiver.start()
            deadline = time.monotonic() + 20.0
            while not self._ready.wait(0.1):
                if self._cancel.is_set():
                    raise RuntimeError('Camera startup cancelled')
                if time.monotonic() >= deadline:
                    raise RuntimeError('Camera worker startup timed out')
            if self._cancel.is_set():
                raise RuntimeError('Camera startup cancelled')
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
        connection = self.sock
        try:
            while True:
                kind, data = _recv_packet(connection)
                if kind == 'frame':
                    if not self._cancel.is_set():
                        _latest_put(self._frames, data)
                    continue
                if data.get('session') != self.session:
                    raise ValueError('Camera IPC session mismatch')
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
                    self._release = data.get('release') or {}
                    self._safe_stop = bool(data.get('safe') and self._release.get('camera_null')
                                           and self._release.get('callbacks_drained')
                                           and self._release.get('recorder_null') is not False)
                    if not self._safe_stop:
                        self._fatal = data.get('error') or self._release.get('error') or self._fatal
                    self._stopped.set()
                    log.info('WORKER_ACK session=%s safe=%s release=%s', self.session, self._safe_stop, self._release)
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
        if self._cancel.is_set() or self.sock is None or self.process is None or self.process.poll() is not None:
            return {'success': False, 'error': self._fatal or 'Camera worker unavailable'}
        request_id = secrets.token_hex(8)
        data = {'action': action, 'id': request_id, 'session': self.session, **(values or {})}
        try:
            _send_packet(self.sock, data, self._send_lock)
        except (OSError, TimeoutError) as exc:
            return {'success': False, 'error': str(exc)}
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._cancel.is_set() or self.process.poll() is not None:
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
        if self.sock is None or self._cancel.is_set():
            return
        now = time.monotonic()
        if now - self._last_overlay_send < 0.05:
            return
        self._last_overlay_send = now
        try:
            _send_packet(self.sock, {'action': 'overlay', 'state': state, 'session': self.session}, self._send_lock)
        except (OSError, TimeoutError) as exc:
            self._fatal = f'Camera overlay IPC failed: {exc}'

    def start_recording(self, path):
        result = self._command('record_start', {'path': str(path)})
        return bool(result.get('success')), result.get('path') or result.get('error', '')

    def stop_recording(self):
        result = self._command('record_stop', timeout=9.0)
        if not result.get('success') and result.get('recorder_null') is not True:
            result['requires_camera_stop'] = True
        return result

    def _signal_worker(self, hard=False):
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == 'posix':
            os.killpg(self.process.pid, signal.SIGKILL if hard else signal.SIGTERM)
        elif hard:
            self.process.kill()
        else:
            self.process.terminate()

    def stop(self):
        global _owner
        with self._stop_lock:
            if self._stop_result is not None:
                return dict(self._stop_result)
            started = time.monotonic()
            self._cancel.set()
            with self._state_lock:
                self.request_stop()
            log.info('STOP_REQUESTED session=%s pid=%s', self.session,
                     self.process.pid if self.process else None)
            errors = []
            forced = False
            try:
                for duration, tier in ((GRACE_SECONDS, 'grace'), (TERM_SECONDS, 'term'), (KILL_SECONDS, 'kill')):
                    if self.process is None:
                        break
                    if self.process.poll() is None and tier != 'grace':
                        forced = True
                        if tier == 'term':
                            try:
                                self._mark_recovery('Worker graceful release timed out; forced exit required')
                            except OSError as exc:
                                errors.append(f'Recovery marker write failed: {exc}')
                        log.warning('WORKER_%s session=%s pid=%s', tier.upper(), self.session, self.process.pid)
                        try:
                            self._signal_worker(hard=(tier == 'kill'))
                        except OSError as exc:
                            errors.append(str(exc))
                    try:
                        self.process.wait(timeout=duration)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if self.process is not None:
                    self._stopped.wait(0.5)
            finally:
                if self.sock is not None:
                    try:
                        self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        self.sock.close()
                    except OSError as exc:
                        errors.append(f'Camera IPC close failed: {exc}')
                    self.sock = None
                if self._receiver:
                    self._receiver.join(timeout=1.0)
            reaped = self.process is None or self.process.poll() is not None
            log.info('WORKER_REAP session=%s reaped=%s exit=%s', self.session, reaped,
                     self.process.returncode if self.process else None)
            safe = reaped and not forced and (self.process is None or self._safe_stop)
            if not safe:
                errors.append(self._fatal or 'Unconfirmed camera release; reacquisition blocked')
                try:
                    self._mark_recovery('; '.join(errors))
                except OSError as exc:
                    errors.append(f'Recovery marker write failed: {exc}')
            errors.extend(self._release.get('errors') or [])
            errors = list(dict.fromkeys(errors))
            self._stop_result = {**self._release, 'success': safe and not errors, 'safe': safe,
                                 'worker_reaped': reaped, 'forced_exit': forced,
                                 'error': '; '.join(errors), 'errors': errors, 'session': self.session}
            with _owner_lock:
                if _owner is self and safe:
                    _owner = None
            log.info('%s session=%s duration=%.3f result=%s',
                     'STOP_COMPLETE' if safe else 'RELEASE_FAILED', self.session,
                     time.monotonic() - started, self._stop_result)
            return dict(self._stop_result)

    def status(self):
        return {'backend': 'qti', 'hardware_recording': True,
                'hardware_recording_mode': 'result', **self._status,
                'last_error': self._fatal or self._status.get('last_error', ''),
                'worker_pid': self.process.pid if self.process else None,
                'release': self._stop_result,
                'worker_alive': bool(self.process and self.process.poll() is None)}


def worker_main(port):
    _configure_lifecycle_file_log('worker')
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
    release = {}
    camera_lock = None
    loop = None
    session = bootstrap['session']
    last_frame = [time.monotonic()]
    log.info('WORKER_BOOT session=%s pid=%s camera=%s', session, os.getpid(), bootstrap['config']['camera'])

    def request_stop(*args):
        stop_event.set()
        if loop is not None:
            loop.quit()
        return True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)

    def send_message(value):
        value['session'] = session
        messages.put_nowait(value)

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
                    message['session'] = session
                    _send_packet(sock, message, send_lock)
        except (OSError, ValueError, TimeoutError):
            stop_event.set()

    def receive_loop():
        try:
            while not stop_event.is_set():
                kind, command = _recv_packet(sock)
                if kind != 'json':
                    raise ValueError('Unexpected camera command packet')
                if command.get('session') != session:
                    raise ValueError('Camera command session mismatch')
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
        if stop_event.is_set():
            return
        if os.name == 'posix':
            import fcntl
            camera_lock = Path(f'/run/visionedge-camera-{int(bootstrap["config"]["camera"])}.lock').open('a+b')
            fcntl.flock(camera_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _enforce_recovery_guard(_shared_recovery_file(bootstrap['config']['camera']))
        from edge_runtime import EdgeConfig, QtiGstCameraBackend
        from gi.repository import GLib
        if stop_event.is_set():
            return
        cfg = EdgeConfig(**bootstrap['config'])
        backend = QtiGstCameraBackend(cfg)
        backend._frame_callback = publish_frame
        loop = GLib.MainLoop()

        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, request_stop)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, request_stop)
        backend.start()
        if stop_event.is_set():
            return
        send_message({'event': 'ready', 'success': True, 'status': backend.status()})
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
        sender_stop.set()
        watchdog = threading.Timer(20.0, _watchdog_exit,
                                   args=(session, bootstrap['config']['camera'], release))
        watchdog.daemon = True
        watchdog.start()
        try:
            sender.join(timeout=1.0)
            if backend is not None:
                release = backend.stop()
            else:
                release = {'safe': True, 'camera_null': True, 'callbacks_drained': True,
                           'recorder_null': None, 'recorder_finalized': None, 'errors': []}
            safe = bool(release.get('safe'))
        except Exception as exc:
            error = f'Camera cleanup failed: {exc}'
        if not safe:
            try:
                _persist_recovery(error or release.get('error') or 'Worker release unconfirmed',
                                  session, os.getpid(), bootstrap['config']['camera'], release)
            except OSError as exc:
                error += f'; recovery marker write failed: {exc}'
        try:
            _send_packet(sock, {'event': 'stopped', 'session': session, 'safe': safe,
                                'error': error, 'release': release}, send_lock)
        except (OSError, TimeoutError):
            pass
        sender_stop.set()
        sender.join(timeout=1.0)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        watchdog.cancel()
        # Keep the camera lock until process exit, including unsafe teardown.
        # Avoid native finalizers when callback drain or NULL cannot be confirmed.
        os._exit(0 if safe else 70)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ipc-port', type=int)
    parser.add_argument('--probe-qti', action='store_true')
    args = parser.parse_args()
    if args.probe_qti:
        from edge_runtime import QtiGstCameraBackend
        raise SystemExit(0 if QtiGstCameraBackend.available() else 1)
    if args.ipc_port is None:
        parser.error('--ipc-port is required for camera ownership')
    worker_main(args.ipc_port)
