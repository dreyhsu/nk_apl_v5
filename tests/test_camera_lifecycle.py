"""Station failures cannot gate camera teardown."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import visionedge_server as web
from camera_lifecycle import CameraLifecycle
from edge_runtime import EdgeRuntime


class LifecycleTests(unittest.TestCase):
    def test_acknowledged_source_release_failure_allows_explicit_recovery(self):
        self.edge.stop.return_value = {'success': False, 'safe': False,
                                       'reopen_eligible': True, 'error': 'StopCamera failed'}
        result = self.control.stop()
        self.assertFalse(result['safe'])
        self.assertTrue(self.control.start()['success'])
        self.edge.start.assert_called_once()

    def setUp(self):
        self.edge = Mock(thread=None, _camera_generation=0)
        self.edge.stop_event = threading.Event()
        self.edge.stop.return_value = {'success': True, 'safe': True}
        self.edge.start.return_value = {'success': True}
        self.notify = Mock()
        self.control = CameraLifecycle(self.edge, self.notify, wait_seconds=0.5, notify_seconds=0.02)

    def test_journal_failure_still_releases_once(self):
        self.notify.side_effect = sqlite3.OperationalError('database is locked')
        first = self.control.stop()
        self.assertEqual(first, self.control.stop())
        self.edge.stop.assert_called_once()
        self.assertTrue(first['safe'])
        self.assertFalse(first['success'])
        self.assertIn('database is locked', first['error'])

    def test_blocked_station_does_not_delay_hardware_or_status(self):
        release = threading.Event()
        entered = threading.Event()
        def note():
            entered.set()
            release.wait(2)
        self.notify.side_effect = note
        try:
            result = self.control.stop()
            self.assertTrue(entered.is_set())
            self.assertTrue(result['safe'])
            self.assertTrue(result['station_update']['pending'])
            self.assertEqual(self.control.status()['phase'], 'STOPPED')
            self.assertFalse(self.control.start()['success'])
        finally:
            release.set()
            self.control.operation['notified'].wait(1)

    def test_http_wait_timeout_does_not_abandon_stop(self):
        release = threading.Event()
        self.edge.stop.side_effect = lambda: (release.wait(2), {'success': True, 'safe': True})[1]
        self.control.wait_seconds = .01
        try:
            result = self.control.stop()
            self.assertTrue(result['pending'])
            self.assertEqual(self.control.status()['phase'], 'STOPPING')
            self.assertFalse(self.control.start()['success'])
        finally:
            release.set()
            self.control.operation['done'].wait(1)
        self.assertTrue(self.control.stop()['success'])
        self.edge.stop.assert_called_once()

    def test_explicit_stop_cancels_restart(self):
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set()
            release.wait(2)
            return {'success': True, 'safe': True}
        self.edge.stop.side_effect = stop
        results = []
        worker = threading.Thread(target=lambda: results.append(self.control.configure(restart=True)))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.control.wait_seconds = .01
        try:
            self.control.stop()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(results[0]['success'])
        self.edge.start.assert_not_called()

    def test_failed_release_never_applies_config_or_restarts(self):
        self.edge.stop.return_value = {'success': False, 'safe': False, 'error': 'NULL failed'}
        self.assertFalse(self.control.configure({'rotation': 0}, restart=True)['success'])
        self.edge.update_config.assert_not_called()
        self.edge.start.assert_not_called()

    def test_new_runtime_generation_has_its_own_stop(self):
        self.control.stop()
        self.edge._camera_generation += 1
        self.control.stop()
        self.assertEqual(self.edge.stop.call_count, 2)

    def test_closing_blocks_start_and_restart(self):
        self.control.closing = True
        self.assertFalse(self.control.start()['success'])
        self.assertFalse(self.control.configure(restart=True)['success'])
        self.edge.start.assert_not_called()

    def test_record_finalize_timeout_is_supervised_and_escalates(self):
        release = threading.Event()
        self.edge.stop_recording.side_effect = lambda: (release.wait(2), {'success': True})[1]
        try:
            result = self.control.stop_recording(wait_seconds=.01)
            self.assertFalse(result['success'])
            self.assertTrue(result['camera_release']['safe'])
            self.edge.stop.assert_called_once()
        finally:
            release.set()
            self.control.record_operation['done'].wait(1)

    def test_repeat_record_stop_uses_one_finalization(self):
        self.edge.stop_recording.return_value = {'success': True, 'recorder_null': True}
        first = self.control.stop_recording()
        self.assertEqual(first, self.control.stop_recording())
        self.edge.stop_recording.assert_called_once()
        self.edge.stop.assert_not_called()


class WebStopTests(unittest.TestCase):
    def setUp(self):
        self.edge = Mock(thread=None, _camera_generation=0)
        self.edge.stop_event = threading.Event()
        self.edge.stop.return_value = {'success': True, 'safe': True}
        self.station = Mock(storage_error='')
        self.patches = [patch.object(web, 'EDGE', self.edge), patch.object(web, 'STATION', self.station),
                        patch.object(web, '_camera_lifecycle', None),
                        patch.object(web, '_shutdown_requested', threading.Event())]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_web_stop_is_independent_of_all_station_states(self):
        for state in ['WAITING_SN', 'WAITING_MCIO_SN', 'SN_CONFIRM', 'WAITING_CAPTURE',
                      'JUDGING', 'NOT_MATCHED', 'PASS_CONFIRM', 'UPLOADING', 'RECOVERY_REQUIRED']:
            with self.subTest(state=state):
                self.station.status.return_value = {'state': state, 'operator': ''}
                self.edge._camera_generation += 1
                response = web.app.test_client().post('/api/edge/stop')
                self.assertTrue(response.json['camera_release']['safe'])
        self.assertEqual(self.edge.stop.call_count, 9)
        self.station.status.assert_not_called()

    def test_web_journal_exception_does_not_skip_stop(self):
        self.station.camera_fault.side_effect = sqlite3.OperationalError('disk full')
        response = web.app.test_client().post('/api/edge/stop')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['success'])
        self.assertTrue(response.json['safe'])
        self.edge.stop.assert_called_once()

    def test_station_global_lock_cannot_block_stop_or_lifecycle_status(self):
        entered, release = threading.Event(), threading.Event()
        def lock_station():
            with web._station_lock:
                entered.set()
                release.wait(2)
        worker = threading.Thread(target=lock_station)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            self.assertTrue(web.app.test_client().post('/api/edge/stop').json['safe'])
            state = web.app.test_client().get('/api/edge/lifecycle').json
            self.assertEqual(state['phase'], 'STOPPED')
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(web.responsive_station_status()['status_stale'])
            self.edge.cfg = SimpleNamespace(stream_enabled=True, live_view_mode='webrtc', source='0', backend='qti')
            self.edge.status.return_value = {'status': 'STOPPED', 'running': False}
            with patch.object(web, 'INPUTS', None), patch.object(web, '_station_snapshot', None):
                response = web.app.test_client().get('/api/edge/status')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['station']['status_stale'])
            self.assertEqual(response.json['station']['status_stale_reason'], 'global_lock')
            self.assertIn('status_snapshot_age_ms', response.json['station'])
        finally:
            release.set()
            worker.join(1)

    def test_unsafe_recorder_stop_escalates_camera_stop(self):
        self.edge.stop_recording.return_value = {'success': False, 'requires_camera_stop': True}
        result = web.app.test_client().post('/api/edge/record/stop').json
        self.assertFalse(result['success'])
        self.assertTrue(result['camera_release']['safe'])
        self.edge.stop.assert_called_once()

    def test_successful_recorder_stop_keeps_camera(self):
        self.edge.stop_recording.return_value = {'success': True, 'recorder_null': True}
        self.assertTrue(web.app.test_client().post('/api/edge/record/stop').json['success'])
        self.edge.stop.assert_not_called()

    def test_shutdown_releases_before_blocked_scanner(self):
        release = threading.Event()
        scanner = Mock()
        def scanner_stop():
            self.edge.stop.assert_called_once()
            release.wait(3)
        scanner.stop.side_effect = scanner_stop
        with patch.object(web, 'SCANNER', scanner), patch.object(web, 'INPUTS', None), \
                patch.object(web, '_shutdown_result', None):
            try:
                result = web._shutdown()
                self.assertTrue(result['safe'])
                self.assertIn('Scanner shutdown timed out', result['error'])
            finally:
                release.set()


class ConfigTransactionTests(unittest.TestCase):
    def test_config_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'edge.ini'
            edge = EdgeRuntime(Mock(), path)
            edge.cfg.save(path)
            original = path.read_bytes()
            with patch('edge_runtime.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    edge.update_config({'rotation': 0})
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(edge._configuring)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_running_config_is_pending_until_next_start(self):
        with tempfile.TemporaryDirectory() as folder:
            edge = EdgeRuntime(Mock(), Path(folder) / 'edge.ini')
            old = edge.cfg.rotation
            edge.thread = Mock()
            edge.thread.is_alive.return_value = True
            result = edge.update_config({'rotation': 0 if old else 180})
            self.assertTrue(result['restart_required'])
            self.assertEqual(edge.cfg.rotation, old)
            self.assertNotEqual(edge._pending_config.rotation, old)

    def test_failed_stop_does_not_persist_new_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'edge.ini'
            edge = EdgeRuntime(Mock(), path)
            edge.thread = Mock()
            edge.thread.is_alive.return_value = True
            with patch.object(edge, 'stop', return_value={'success': False, 'safe': False}):
                self.assertFalse(edge.update_config({'rotation': 0}, restart=True)['success'])
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
