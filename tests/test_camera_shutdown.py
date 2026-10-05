"""Supervision faults must not leave an owner or report a safe release."""
import json
import dataclasses
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import camera_worker as worker


class WorkerShutdownTests(unittest.TestCase):
    def backend(self):
        backend = worker.WorkerCameraBackend(SimpleNamespace(camera=0))
        backend.process = Mock(pid=123, returncode=0)
        backend.process.poll.return_value = 0
        backend.sock = Mock()
        backend._authenticated = True
        backend._safe_stop = True
        backend._stopped.set()
        backend._release = {'camera_null': True, 'callbacks_drained': True,
                            'recorder_null': None, 'errors': []}
        return backend

    def test_normal_stop_reaps_closes_and_is_idempotent(self):
        backend = self.backend()
        connection = backend.sock
        with patch.object(worker, '_owner', backend):
            first = backend.stop()
            self.assertIsNone(worker._owner)
            self.assertEqual(backend.stop(), first)
        self.assertTrue(first['safe'])
        self.assertTrue(first['success'])
        backend.process.wait.assert_called_once()
        connection.close.assert_called_once()

    def test_hung_worker_escalates_term_kill_and_closes_ipc(self):
        backend = self.backend()
        backend.process.poll.return_value = None
        backend.process.wait.side_effect = [subprocess.TimeoutExpired('camera', 22),
                                            subprocess.TimeoutExpired('camera', 3), 0]
        connection = backend.sock
        def signal_worker(hard=False):
            if hard:
                backend.process.poll.return_value = -9
        with patch.object(backend, '_signal_worker', side_effect=signal_worker) as signals, \
                patch.object(backend, '_mark_recovery') as marker:
            result = backend.stop()
        self.assertEqual([call.kwargs['hard'] for call in signals.call_args_list], [False, True])
        self.assertTrue(result['worker_reaped'])
        self.assertTrue(result['forced_exit'])
        self.assertFalse(result['safe'])
        self.assertEqual(marker.call_count, 2)
        connection.close.assert_called_once()

    def test_unkillable_worker_is_not_success(self):
        backend = self.backend()
        backend.process.poll.return_value = None
        backend.process.wait.side_effect = subprocess.TimeoutExpired('camera', 2)
        with patch.object(backend, '_signal_worker'), patch.object(backend, '_mark_recovery'):
            result = backend.stop()
        self.assertFalse(result['worker_reaped'])
        self.assertFalse(result['success'])
        self.assertIsNone(backend.sock)

    def test_missing_ack_blocks_even_a_zero_exit_code(self):
        backend = self.backend()
        backend._safe_stop = False
        with patch.object(backend, '_mark_recovery') as marker:
            self.assertFalse(backend.stop()['safe'])
        marker.assert_called_once()

    def test_recorder_finalize_failure_keeps_camera_safe_but_reports_error(self):
        backend = self.backend()
        backend._release['errors'] = ['MP4 finalize timed out']
        with patch.object(backend, '_mark_recovery') as marker:
            result = backend.stop()
        self.assertTrue(result['safe'])
        self.assertFalse(result['success'])
        marker.assert_not_called()

    def test_ack_from_another_session_is_rejected(self):
        backend = self.backend()
        backend._safe_stop = False
        backend._stopped.clear()
        with patch.object(worker, '_recv_packet', return_value=('json', {
                'session': 'stale', 'event': 'stopped', 'safe': True})):
            backend._receive()
        self.assertFalse(backend._safe_stop)
        self.assertIn('session mismatch', backend._fatal)

    def test_cancel_before_spawn_creates_no_process(self):
        backend = worker.WorkerCameraBackend(SimpleNamespace(camera=0))
        backend.request_stop()
        with patch.object(worker, '_owner', None), patch.object(worker, '_enforce_recovery_guard'), \
                patch.object(worker.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                backend.start()
        spawn.assert_not_called()

    def test_start_after_completed_stop_does_not_reserve_owner(self):
        backend = worker.WorkerCameraBackend(SimpleNamespace(camera=0))
        with patch.object(worker, '_owner', None):
            self.assertTrue(backend.stop()['safe'])
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                backend.start()
            self.assertIsNone(worker._owner)

    def test_atomic_marker_and_failed_write_latch(self):
        backend = self.backend()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(worker, 'RECOVERY_FILE', Path(folder) / 'recovery.json'), \
                patch.object(worker, '_recovery_required', ''), \
                patch.object(worker, '_read_boot_id', return_value='boot-a'):
            backend._mark_recovery('failed NULL')
            value = json.loads(worker.RECOVERY_FILE.read_text())
            self.assertEqual(value['session'], backend.session)
            self.assertEqual(value['boot_id'], 'boot-a')
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])
            worker.RECOVERY_FILE.unlink()
            with patch.object(worker.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    backend._mark_recovery('failed NULL')
            with self.assertRaisesRegex(RuntimeError, 'failed NULL'):
                worker._enforce_recovery_guard()

    def test_ipc_send_lock_has_a_deadline(self):
        lock = threading.Lock()
        lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                worker._send_packet(Mock(), {}, lock, timeout=0.001)
        finally:
            lock.release()

    def test_shared_guard_blocks_a_different_deployment(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, '_recovery_required', ''), \
                patch.object(worker, '_read_boot_id', return_value='boot-a'):
            path = Path(folder) / 'shared.json'
            path.write_text(json.dumps({'boot_id': 'boot-a', 'reason': 'forced exit'}))
            with self.assertRaisesRegex(RuntimeError, 'during this boot'):
                worker._enforce_recovery_guard(path)

    def test_watchdog_marks_recovery_before_forced_exit(self):
        order = []
        with patch.object(worker, '_persist_recovery', side_effect=lambda *a: order.append('guard')), \
                patch.object(worker.os, '_exit', side_effect=lambda code: order.append(code)):
            worker._watchdog_exit('session', 0, {})
        self.assertEqual(order, ['guard', 70])


FAKE_WORKER = '''
import json, socket, sys, time
from camera_worker import _send_packet, _recv_packet
import threading
bootstrap = json.loads(sys.stdin.readline())
sock = socket.create_connection(('127.0.0.1', int(sys.argv[1])))
sock.settimeout(1)
lock = threading.Lock()
session = bootstrap['session']
if sys.argv[2] == 'pre_auth':
    time.sleep(0.25)
_send_packet(sock, {'token': bootstrap['token']}, lock)
if sys.argv[2] == 'ready':
    _send_packet(sock, {'event': 'ready', 'success': True, 'session': session}, lock)
while True:
    kind, command = _recv_packet(sock)
    if command.get('action') == 'stop':
        _send_packet(sock, {'event': 'stopped', 'session': session, 'safe': True,
                           'release': {'camera_null': True, 'callbacks_drained': True,
                                       'recorder_null': None, 'errors': []}}, lock)
        break
sock.close()
'''


class WorkerProcessTests(unittest.TestCase):
    def spawn_fixture(self, mode):
        real_spawn = subprocess.Popen
        def spawn(command, **kwargs):
            self.assertEqual(kwargs['start_new_session'], os.name == 'posix')
            return real_spawn([command[0], '-B', '-c', FAKE_WORKER, command[-1], mode], **kwargs)
        return spawn

    def test_real_ipc_process_can_start_stop_and_reacquire(self):
        cfg_type = dataclasses.make_dataclass('TestConfig', [('camera', int)])
        with patch.object(worker, '_owner', None), patch.object(worker, '_enforce_recovery_guard'), \
                patch.object(worker.subprocess, 'Popen', side_effect=self.spawn_fixture('ready')):
            for _ in range(3):
                backend = worker.WorkerCameraBackend(cfg_type(0))
                try:
                    backend.start()
                    result = backend.stop()
                    self.assertTrue(result['safe'])
                    self.assertTrue(result['worker_reaped'])
                    self.assertIsNone(worker._owner)
                finally:
                    if backend.process is not None and backend.process.poll() is None:
                        backend.process.kill()
                        backend.process.wait(timeout=2)

    def test_real_pending_start_cancels_with_a_safe_ack(self):
        cfg_type = dataclasses.make_dataclass('TestConfig', [('camera', int)])
        backend = worker.WorkerCameraBackend(cfg_type(0))
        errors = []
        def start():
            try:
                backend.start()
            except RuntimeError as exc:
                errors.append(str(exc))
        with patch.object(worker, '_owner', None), patch.object(worker, '_enforce_recovery_guard'), \
                patch.object(worker.subprocess, 'Popen', side_effect=self.spawn_fixture('pending')):
            thread = threading.Thread(target=start)
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not backend._authenticated and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(backend._authenticated)
                backend.request_stop()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertIn('cancelled', errors[0])
                self.assertTrue(backend.stop()['safe'])
            finally:
                if backend.process is not None and backend.process.poll() is None:
                    backend.process.kill()
                    backend.process.wait(timeout=2)
                thread.join(timeout=2)

    def test_stop_before_ipc_authentication_is_delivered_after_handshake(self):
        cfg_type = dataclasses.make_dataclass('TestConfig', [('camera', int)])
        backend = worker.WorkerCameraBackend(cfg_type(0))
        errors = []
        def start():
            try:
                backend.start()
            except RuntimeError as exc:
                errors.append(str(exc))
        with patch.object(worker, '_owner', None), patch.object(worker, '_enforce_recovery_guard'), \
                patch.object(worker.subprocess, 'Popen', side_effect=self.spawn_fixture('pre_auth')):
            thread = threading.Thread(target=start)
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while backend.process is None and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertIsNotNone(backend.process)
                self.assertFalse(backend._authenticated)
                self.assertTrue(backend.stop()['safe'])
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertIn('cancelled', errors[0])
                self.assertIsNone(worker._owner)
            finally:
                if backend.process is not None and backend.process.poll() is None:
                    backend.process.kill()
                    backend.process.wait(timeout=2)
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
