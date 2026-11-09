"""Exercise callback, mux, runtime and HTTP shutdown failures without hardware."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import edge_runtime as runtime
import visionedge_server as web


def fake_gst():
    return SimpleNamespace(State=SimpleNamespace(NULL='NULL', PLAYING='PLAYING', PAUSED='PAUSED'),
                           StateChangeReturn=SimpleNamespace(FAILURE='FAILURE', SUCCESS='SUCCESS'),
                           FlowReturn=SimpleNamespace(OK=0),
                           MessageType=SimpleNamespace(EOS=1, ERROR=2, WARNING=4),
                           Event=SimpleNamespace(new_eos=lambda: 'EOS'), SECOND=1000000000)


class PipelineShutdownTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(runtime, 'GST_AVAILABLE', True), patch.object(runtime, 'Gst', fake_gst())]
        for item in self.patches:
            item.start()
        self.backend = runtime.QtiGstCameraBackend(SimpleNamespace())
        self.pipeline = Mock()
        self.pipeline.set_state.return_value = 'SUCCESS'
        self.pipeline.get_state.return_value = ('SUCCESS', 'NULL', None)
        self.pipeline.get_by_name.return_value = None
        self.backend.pipeline = self.pipeline

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_callback_timeout_does_not_free_pipeline(self):
        self.backend._overlay = Mock()
        self.backend._overlay.stop.return_value = False
        result = self.backend.stop()
        self.assertFalse(result['safe'])
        self.assertFalse(result['callbacks_drained'])
        self.pipeline.set_state.assert_not_called()
        self.assertEqual(self.backend.stop(), result)

    def test_sample_timeout_does_not_free_pipeline(self):
        self.backend._sample_lock = Mock()
        self.backend._sample_lock.acquire.return_value = False
        result = self.backend.stop()
        self.assertFalse(result['safe'])
        self.assertIn('sample callback drain timeout', result['error'])
        self.pipeline.set_state.assert_not_called()

    def test_recorder_error_still_releases_camera(self):
        self.backend._recorder = Mock()
        self.backend._recorder.stop.return_value = {'success': False, 'error': 'EOS timeout',
                                                    'recorder_null': True, 'recorder_finalized': False}
        result = self.backend.stop()
        self.assertTrue(result['safe'])
        self.assertFalse(result['success'])
        self.pipeline.set_state.assert_called_once_with('NULL')
        self.assertFalse(result['recorder_finalized'])

    def test_failed_null_return_is_unsafe_even_when_state_says_null(self):
        self.pipeline.set_state.return_value = 'FAILURE'
        result = self.backend.stop()
        self.assertFalse(result['safe'])
        self.assertIn('state change failed', result['error'])
        self.assertNotIn('before deadline', result['error'])
        self.assertEqual(result['camera_state_change']['set_return'], 'FAILURE')

    def playing_pipeline(self):
        self.pipeline.get_state.side_effect = [('SUCCESS', 'PLAYING', None), ('SUCCESS', 'NULL', None)]
        self.pipeline.send_event.return_value = True
        self.pipeline.get_bus.return_value.timed_pop_filtered.return_value = SimpleNamespace(type=1)

    def test_main_eos_precedes_null_and_is_not_recorder_eos(self):
        self.playing_pipeline()
        order = []
        self.pipeline.send_event.side_effect = lambda event: order.append(event) or True
        self.pipeline.set_state.side_effect = lambda state: order.append(state) or 'SUCCESS'
        result = self.backend.stop()
        self.assertTrue(result['safe'])
        self.assertTrue(result['camera_eos'])
        self.assertEqual(order, ['EOS', 'NULL'])
        self.assertGreater(self.pipeline.get_state.call_args.args[0], 2 * runtime.Gst.SECOND)

    def test_main_eos_timeout_falls_back_to_confirmed_null(self):
        self.playing_pipeline()
        self.pipeline.get_bus.return_value.timed_pop_filtered.return_value = None
        result = self.backend.stop()
        self.assertTrue(result['safe'])
        self.assertFalse(result['camera_eos'])
        self.assertIn('EOS wait timed out', result['camera_eos_warning'])
        self.pipeline.set_state.assert_called_once_with('NULL')

    def test_eos_native_exception_does_not_skip_null(self):
        self.playing_pipeline()
        self.pipeline.send_event.side_effect = RuntimeError('source EOS failure')
        result = self.backend.stop()
        self.assertTrue(result['safe'])
        self.assertIn('source EOS failure', result['camera_eos_warning'])
        self.pipeline.set_state.assert_called_once_with('NULL')

    def test_null_timeout_keeps_pipeline_and_element_diagnostics(self):
        self.playing_pipeline()
        self.pipeline.get_state.side_effect = [('SUCCESS', 'PLAYING', None), ('ASYNC', 'READY', 'NULL')]
        element = Mock()
        element.get_state.return_value = ('FAILURE', 'READY', 'NULL')
        self.pipeline.get_by_name.return_value = element
        with patch.object(self.backend, '_startup_bus_error', return_value='camsrc: StopCamera failed'):
            result = self.backend.stop()
        self.assertFalse(result['safe'])
        self.assertIs(self.backend.pipeline, self.pipeline)
        self.assertIn('before deadline', result['error'])
        self.assertIn('StopCamera failed', result['error'])
        self.assertEqual(result['camera_elements']['camsrc']['current'], 'READY')

    def test_unfinished_bus_reader_does_not_race_stop_bus_consumer(self):
        self.backend._bus_thread = Mock()
        self.backend._bus_thread.is_alive.return_value = True
        result = self.backend.stop()
        self.assertFalse(result['safe'])
        self.assertIn('before EOS', result['error'])
        self.pipeline.send_event.assert_not_called()

    def test_async_null_snapshot_is_not_a_completed_release(self):
        self.pipeline.get_state.side_effect = [('SUCCESS', 'NULL', None), ('ASYNC', 'NULL', 'READY')]
        with patch.object(self.backend, '_startup_bus_error', return_value=''):
            self.assertFalse(self.backend.stop()['safe'])

    def test_sink_release_settings_are_supported_and_applied(self):
        sink = Mock()
        for prop in ('enable-last-sample', 'wait-on-eos'):
            self.assertTrue(self.backend._set_if_property(sink, prop, False))
        self.assertEqual(sink.set_property.call_args_list[0].args, ('enable-last-sample', False))
        self.assertEqual(sink.set_property.call_args_list[1].args, ('wait-on-eos', False))

    def test_successful_shutdown_is_idempotent(self):
        first = self.backend.stop()
        self.assertTrue(first['safe'])
        self.assertEqual(self.backend.stop(), first)
        self.pipeline.set_state.assert_called_once()

    def test_callbacks_and_recorder_finish_before_camera_null(self):
        order = []
        self.backend._overlay = Mock()
        self.backend._overlay.stop.side_effect = lambda **kwargs: order.append('overlay') or True
        self.backend._recorder = Mock()
        self.backend._recorder.stop.side_effect = lambda **kwargs: order.append('recorder') or {
            'success': True, 'recorder_null': True, 'recorder_finalized': True}
        self.pipeline.set_state.side_effect = lambda state: order.append('camera-null') or 'SUCCESS'
        self.assertTrue(self.backend.stop()['safe'])
        self.assertEqual(order, ['overlay', 'recorder', 'camera-null'])

    def test_h264_callback_timeout_is_unsafe(self):
        self.backend._h264_lock = Mock()
        self.backend._h264_lock.acquire.return_value = False
        self.assertFalse(self.backend.stop()['safe'])
        self.pipeline.set_state.assert_not_called()

    def test_recorder_exception_blocks_replacement_and_is_preserved(self):
        self.backend._recorder = Mock()
        recorder = self.backend._recorder
        self.backend._recorder.stop.side_effect = RuntimeError('native mux failure')
        result = self.backend.stop_recording()
        self.assertFalse(result['recorder_null'])
        self.assertIs(self.backend._unreleased_recorder, recorder)
        self.assertFalse(self.backend.start_recording(Path('another.mp4'))[0])
        self.assertFalse(self.backend.stop()['safe'])

    def test_recorder_finalize_failure_with_null_does_not_stop_preview(self):
        recorder = Mock()
        self.backend._recorder = recorder
        recorder.stop.return_value = {'success': False, 'recorder_null': True,
                                     'recorder_finalized': False, 'error': 'EOS timeout'}
        result = self.backend.stop_recording()
        self.assertFalse(result['success'])
        self.assertIsNone(self.backend._unreleased_recorder)
        self.pipeline.set_state.assert_not_called()

    def recorder(self, folder):
        rec = runtime.GstH264Mp4Recorder(Path(folder) / 'test.mp4', None)
        rec.pipeline = Mock()
        rec.pipeline.set_state.return_value = 'SUCCESS'
        rec.pipeline.get_state.return_value = ('SUCCESS', 'NULL', None)
        rec.src = Mock()
        rec.src.emit.return_value = 0
        rec.started = True
        rec.frames = 10
        return rec

    def test_eos_exception_still_attempts_recorder_null(self):
        with tempfile.TemporaryDirectory() as folder:
            rec = self.recorder(folder)
            rec.src.emit.side_effect = RuntimeError('EOS failed')
            pipeline = rec.pipeline
            result = rec.stop()
            pipeline.set_state.assert_called_once_with('NULL')
            self.assertTrue(result['recorder_null'])
            self.assertFalse(result['recorder_finalized'])
            self.assertFalse(result['success'])

    def test_recorder_null_must_be_confirmed(self):
        with tempfile.TemporaryDirectory() as folder:
            rec = self.recorder(folder)
            rec.pipeline.get_bus.return_value.timed_pop_filtered.return_value = SimpleNamespace(type=1)
            rec.pipeline.get_state.return_value = ('SUCCESS', 'PLAYING', None)
            self.assertFalse(rec.stop()['recorder_null'])

    def test_empty_recording_is_not_reported_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            rec = self.recorder(folder)
            rec.frames = 0
            rec.pipeline.get_bus.return_value.timed_pop_filtered.return_value = SimpleNamespace(type=1)
            result = rec.stop()
            self.assertTrue(result['recorder_null'])
            self.assertFalse(result['success'])
            self.assertIn('no encoded frames', result['error'])


class RuntimeShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.edge = runtime.EdgeRuntime(Mock(), Path(self.temp.name) / 'edge.ini')
        self.addCleanup(self.temp.cleanup)

    def test_backend_release_failure_survives_runtime_thread_exit(self):
        backend = Mock()
        backend.read.side_effect = RuntimeError('source error')
        backend.status.return_value = {}
        backend.stop.return_value = {'success': False, 'safe': False, 'error': 'NULL failed'}
        with patch.object(self.edge, '_make_backend', return_value=backend):
            self.edge._run()
        result = self.edge.stop()
        self.assertFalse(result['success'])
        self.assertEqual(self.edge.status_text, 'RELEASE_FAILED')
        self.assertIn('NULL failed', self.edge.last_error)
        self.assertFalse(self.edge.start()['success'])

    def test_starting_qti_is_cancelled_and_cleaned(self):
        self.edge.cfg.backend = 'qti'
        backend = Mock()
        entered = threading.Event()
        cancelled = threading.Event()
        def start():
            entered.set()
            if not cancelled.wait(2):
                raise RuntimeError('test startup timeout')
            raise RuntimeError('startup cancelled')
        backend.start.side_effect = start
        backend.request_stop.side_effect = cancelled.set
        backend.stop.return_value = {'success': True, 'safe': True}
        backend.status.return_value = {}
        with patch('camera_worker.WorkerCameraBackend', return_value=backend), \
                patch.object(runtime.QtiGstCameraBackend, 'available', side_effect=AssertionError('parent probe')):
            self.edge.start()
            self.assertTrue(entered.wait(2))
            result = self.edge.stop()
        self.assertTrue(result['safe'])
        self.assertEqual(self.edge.status_text, 'STOPPED')
        self.assertGreaterEqual(backend.stop.call_count, 1)

    def test_start_during_stop_is_rejected(self):
        self.edge._stopping = True
        self.assertFalse(self.edge.start()['success'])

    def test_background_inspection_wait_uses_remaining_stop_budget(self):
        self.edge._stop_deadline = 0
        worker = Mock()
        worker.is_alive.return_value = True
        self.edge._auto_thread = worker
        with patch.object(self.edge, '_make_backend', side_effect=RuntimeError('cancelled')):
            self.edge._run()
        worker.join.assert_called_once_with(timeout=0.0)
        self.assertIn('Automatic capture did not finish', self.edge.last_error)
        self.assertFalse(self.edge.start()['success'])

    def test_manual_writer_blocks_reopening_until_done(self):
        done = threading.Event()
        self.edge.station = SimpleNamespace(_writer_done=done)
        self.assertTrue(self.edge.inspection_worker_alive())
        self.assertFalse(self.edge.start()['success'])
        done.set()
        self.assertFalse(self.edge.inspection_worker_alive())

    def test_explicit_opencv_does_not_probe_qti(self):
        self.edge.cfg.backend = 'opencv'
        with patch.object(self.edge, '_probe_qti', side_effect=AssertionError('unnecessary probe')), \
                patch.object(runtime, 'OpenCVCameraBackend') as cls:
            self.edge._make_backend()
        cls.return_value.start.assert_called_once()

    def test_cancelled_probe_is_killed_and_reaped(self):
        self.edge.stop_event.set()
        proc = Mock(pid=123)
        proc.poll.return_value = None
        with patch.object(runtime, 'GST_AVAILABLE', True), \
                patch.object(runtime.subprocess, 'Popen', return_value=proc), \
                patch.object(runtime.os, 'name', 'nt'):
            with self.assertRaisesRegex(RuntimeError, 'cancelled during plugin discovery'):
                self.edge._probe_qti()
        proc.kill.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2.0)
        self.assertIsNone(self.edge._probe_process)

    def test_unreaped_probe_is_not_overwritten_as_safe_cleanup(self):
        self.edge._probe_process = Mock(pid=123)
        self.edge._probe_process.poll.return_value = None
        with patch.object(self.edge, '_make_backend', side_effect=RuntimeError('probe wait timed out')), \
                patch('camera_worker._persist_recovery') as marker:
            self.edge._run()
        marker.assert_called_once()
        self.assertEqual(self.edge.status_text, 'RELEASE_FAILED')
        self.assertFalse(self.edge.stop()['safe'])
        self.assertFalse(self.edge.start()['success'])


class ServerShutdownTests(unittest.TestCase):
    def test_signal_during_http_construction_does_not_enter_serve_loop(self):
        from werkzeug.serving import ThreadedWSGIServer
        stopping = threading.Event()
        with patch.object(web, '_shutdown_requested', stopping), \
                patch.object(web, '_http_server', None), patch.object(web, '_shutdown') as cleanup, \
                patch.object(ThreadedWSGIServer, '__init__', side_effect=lambda *a, **kw: stopping.set()), \
                patch.object(ThreadedWSGIServer, 'serve_forever') as serve, \
                patch.object(ThreadedWSGIServer, 'server_close') as close:
            web._serve('127.0.0.1', 0, None, 8)
            self.assertIsNone(web._http_server)
        serve.assert_not_called()
        close.assert_called_once()
        cleanup.assert_called_once()

    def test_repeat_signal_does_not_start_multiple_shutdown_threads(self):
        http = Mock()
        edge = Mock()
        with patch.object(web, '_shutdown_requested', threading.Event()), \
                patch.object(web, '_http_server', http), patch.object(web, 'EDGE', edge), \
                patch.object(web.threading, 'Thread') as thread:
            web._request_shutdown(2, None)
            web._request_shutdown(2, None)
        edge.stop_event.set.assert_called_once()
        thread.assert_called_once()

    def test_shutdown_is_once_and_scanner_failure_does_not_skip_camera(self):
        edge = Mock(thread=None)
        edge.inspection_worker_alive.return_value = False
        edge.stop.return_value = {'success': True, 'safe': True}
        scanner = Mock()
        scanner.stop.side_effect = RuntimeError('scanner failed')
        station = Mock()
        with patch.object(web, '_shutdown_result', None), \
                patch.object(web, '_shutdown_requested', threading.Event()), \
                patch.object(web, 'EDGE', edge), patch.object(web, 'SCANNER', scanner), \
                patch.object(web, 'STATION', station), patch.object(web, 'INPUTS', None):
            first = web._shutdown()
            self.assertEqual(web._shutdown(), first)
        self.assertFalse(first['success'])
        edge.stop.assert_called_once()
        station.close.assert_called_once()

    def test_shutdown_blocks_new_http_commands_and_scanner_input(self):
        stopping = threading.Event()
        stopping.set()
        with patch.object(web, '_shutdown_requested', stopping):
            response = web.app.test_client().post('/api/edge/start', json={})
            self.assertEqual(response.status_code, 503)
            self.assertFalse(web.scanner_scan('SN')['success'])

    def test_live_runtime_keeps_storage_open(self):
        edge = Mock()
        edge.thread.is_alive.return_value = True
        edge.stop.return_value = {'success': False, 'safe': False, 'error': 'timeout'}
        station = Mock()
        with patch.object(web, '_shutdown_result', None), \
                patch.object(web, '_shutdown_requested', threading.Event()), \
                patch.object(web, 'EDGE', edge), patch.object(web, 'SCANNER', None), \
                patch.object(web, 'STATION', station), patch.object(web, 'INPUTS', None):
            result = web._shutdown()
        self.assertFalse(result['success'])
        station.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
