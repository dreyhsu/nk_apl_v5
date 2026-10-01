"""Video input isolation, stop failures and real temperature semantics."""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from werkzeug.datastructures import FileStorage
from apl_station import APLStation
from edge_runtime import EdgeConfig, EdgeRuntime, GstVideoFileCapture, OpenCVCameraBackend
from system_metrics import TemperatureReader
from video_test import VideoTestInputs

ROOT = Path(__file__).resolve().parents[1]


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.station = APLStation(ROOT/'config'/'apl_workflow.json', self.root/'production', local_mode=True)
        self.runtime = Mock(cfg=EdgeConfig(backend='qti', rotation=180, min_free_mb=0), backend=None)
        self.runtime.status.return_value = {'recording': False}
        self.runtime.stop.return_value = {'success': True}
        self.runtime.start.return_value = {'success': True}
        self.inputs = VideoTestInputs(self.runtime, self.station, self.root/'videos')

    def tearDown(self):
        self.inputs.station.close()
        self.station.close()
        self.temp.cleanup()

    def start_cycle(self, station, sn='K2V5-IN-PROGRESS', mcio='APL-CARD-1'):
        """The real config declares both label prefixes and both scans are required."""
        self.assertTrue(station.set_operator('OP-TEST')['success'])
        self.assertTrue(station.scan(sn)['success'])
        return station.scan(mcio)

    def upload(self):
        with patch.object(self.inputs, '_probe', return_value={'width':1920,'height':1080}), \
             patch.object(self.inputs, '_build_trace', return_value=[]):
            return self.inputs.upload(FileStorage(stream=io.BytesIO(b'test'), filename='same.mp4'))

    def test_duplicate_names_are_isolated_and_camera_config_restored(self):
        one, two = self.upload(), self.upload()
        self.assertNotEqual(one['id'], two['id'])
        with patch.object(self.inputs, '_probe'):
            self.inputs.start(one['id'])
        self.assertTrue(self.inputs.station.test_mode)
        self.assertIsNot(self.inputs.station, self.station)
        self.assertEqual(self.runtime.cfg.backend, 'opencv')
        self.assertEqual(self.runtime.cfg.rotation, 0)
        self.inputs.restore()
        self.assertIs(self.inputs.station, self.station)
        self.assertEqual(self.runtime.cfg.backend, 'qti')
        self.assertEqual(self.runtime.cfg.rotation, 180)

    def test_unicode_name_is_preserved_but_storage_name_is_internal(self):
        with patch.object(self.inputs, '_probe', return_value={'width':1920,'height':1080,'fps':30.0,'duration_seconds':12.5}), \
             patch.object(self.inputs, '_build_trace', return_value=[]):
            item = self.inputs.upload(FileStorage(stream=io.BytesIO(b'test'), filename='NG_蓋板不插卡.mp4'))
        self.assertEqual(item['name'], 'NG_蓋板不插卡.mp4')
        saved = self.inputs.files[item['id']]
        self.assertEqual(Path(saved['path']).name, 'source.mp4')
        self.assertEqual(saved['duration_seconds'], 12.5)

    def test_trace_uses_deterministic_source_frame_sampling(self):
        import numpy as np
        frames = [np.full((2, 2, 3), index, dtype=np.uint8) for index in range(31)]
        cap = Mock()
        cap.read.side_effect = [(True, frame) for frame in frames] + [(False, None)]
        matcher = Mock()
        matcher.evaluate.side_effect = lambda frame: {'raw': str(int(frame[0, 0, 0]))}
        metadata = {'fps': 30.0}
        with patch('video_test.open_video_file', return_value=cap), \
             patch('video_test.APLMatcher', return_value=matcher):
            trace = self.inputs._build_trace(self.root/'source.mp4', metadata)
        self.assertEqual([sample['match']['raw'] for sample in trace],
                         ['0', '3', '6', '9', '12', '15', '18', '21', '24', '27', '30'])
        self.assertEqual(metadata['analysis_fps'], 10.0)
        cap.release.assert_called_once()

    def test_stop_failure_cannot_switch_or_start_another_input(self):
        item = self.upload()
        self.runtime.stop.return_value = {'success':False,'error':'stop timeout'}
        with patch.object(self.inputs, '_probe'), self.assertRaisesRegex(ValueError, 'stop timeout'):
            self.inputs.start(item['id'])
        self.runtime.start.assert_not_called()
        self.assertIs(self.inputs.station, self.station)
        self.assertEqual(self.runtime.cfg.backend, 'qti')

    def test_active_sn_prevents_switching(self):
        self.assertTrue(self.station.set_operator('OP-TEST')['success'])
        self.assertTrue(self.station.scan('K2V5-IN-PROGRESS')['success'])
        with self.assertRaisesRegex(ValueError, 'Finish or reset'):
            self.inputs.restore()
        self.runtime.stop.assert_not_called()

    def test_invalid_upload_is_removed_and_wrong_resolution_rejected(self):
        import numpy as np
        cap = Mock()
        cap.read.return_value = (True, np.zeros((720,1280,3), dtype=np.uint8))
        with patch('video_test.cv2.VideoCapture', return_value=cap), self.assertRaisesRegex(ValueError, '1920 x 1080'):
            self.inputs.upload(FileStorage(stream=io.BytesIO(b'test'), filename='bad.mp4'))
        self.assertEqual(list((self.root/'videos').iterdir()), [])
        cap.release.assert_called_once()

    def test_non_mp4_upload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'H.264 MP4'):
            self.inputs.upload(FileStorage(stream=io.BytesIO(b'test'), filename='sample.mov'))

    def test_browser_video_uses_an_isolated_station_without_stopping_camera(self):
        original_config = self.runtime.cfg
        result = self.inputs.start_browser({
            'name': 'NG_蓋板不插卡.mp4',
            'width': 1920,
            'height': 1080,
            'duration_seconds': 12.5,
        })
        self.assertTrue(result['success'])
        self.assertEqual(self.inputs.status()['mode'], 'browser_video')
        self.assertEqual(self.inputs.status()['name'], 'NG_蓋板不插卡.mp4')
        self.assertTrue(self.inputs.station.test_mode)
        self.assertIsNot(self.inputs.station, self.station)
        self.assertIs(self.runtime.cfg, original_config)
        self.runtime.stop.assert_not_called()
        self.runtime.start.assert_not_called()
        self.inputs.restore()
        self.assertIs(self.inputs.station, self.station)
        self.assertIs(self.runtime.station, self.station)
        self.runtime.stop.assert_not_called()
        self.runtime.start.assert_not_called()

    def test_browser_video_can_be_reattached_after_scan_without_resetting_cycle(self):
        self.inputs.start_browser({
            'name': 'first.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 10,
        })
        browser_station = self.inputs.station
        self.assertTrue(self.start_cycle(browser_station)['success'])
        before = browser_station.status()

        result = self.inputs.start_browser({
            'name': 'replacement.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 20,
        })

        after = browser_station.status()
        self.assertIs(self.inputs.station, browser_station)
        self.assertEqual(after['state'], 'WAITING_CAPTURE')
        self.assertEqual(after['sn'], 'K2V5-IN-PROGRESS')
        self.assertEqual(after['mcio_sn'], 'APL-CARD-1')
        self.assertEqual(after['cycle_id'], before['cycle_id'])
        self.assertEqual(result['input']['id'], self.inputs.current['id'])
        self.assertEqual(result['input']['name'], 'replacement.mp4')
        self.assertEqual(result['input']['duration_seconds'], 20)

    def test_browser_video_can_be_replaced_while_the_mcio_card_scan_is_pending(self):
        self.inputs.start_browser({
            'name': 'first.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 10,
        })
        browser_station = self.inputs.station
        self.assertTrue(browser_station.set_operator('OP-TEST')['success'])
        self.assertTrue(browser_station.scan('K2V5-IN-PROGRESS')['success'])
        self.assertEqual(browser_station.status()['state'], 'WAITING_MCIO_SN')

        result = self.inputs.start_browser({
            'name': 'replacement.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 20,
        })

        after = browser_station.status()
        self.assertEqual(result['input']['name'], 'replacement.mp4')
        self.assertEqual(after['state'], 'WAITING_MCIO_SN')
        self.assertEqual(after['sn'], 'K2V5-IN-PROGRESS')
        self.assertTrue(browser_station.scan('APL-CARD-1')['success'])
        self.assertEqual(browser_station.status()['state'], 'WAITING_CAPTURE')

    def test_live_cycle_can_switch_to_browser_video_without_resetting_sn(self):
        self.assertTrue(self.start_cycle(self.station, sn='K2V5-LIVE-TO-VIDEO')['success'])
        before = self.station.status()

        result = self.inputs.start_browser({
            'name': 'selected-after-scan.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 30,
        })

        after = self.station.status()
        self.assertIs(self.inputs.station, self.station)
        self.assertEqual(after['state'], 'WAITING_CAPTURE')
        self.assertEqual(after['sn'], 'K2V5-LIVE-TO-VIDEO')
        self.assertEqual(after['mcio_sn'], 'APL-CARD-1')
        self.assertEqual(after['cycle_id'], before['cycle_id'])
        self.assertEqual(after['source'], 'browser_video')
        self.assertTrue(after['test_mode'])
        self.assertFalse(after['dmz']['configured'])
        self.assertEqual(result['input']['mode'], 'browser_video')
        self.runtime.stop.assert_not_called()

        self.station.terminate(after['cycle_id'])
        self.inputs.restore()
        self.assertFalse(self.station.status()['test_mode'])
        self.assertEqual(self.inputs.status()['mode'], 'camera')

    def test_browser_video_reattach_is_blocked_while_inspection_is_busy(self):
        self.inputs.start_browser({
            'name': 'first.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 10,
        })
        self.inputs.station.state = 'JUDGING'
        with self.assertRaisesRegex(ValueError, 'busy or complete'):
            self.inputs.start_browser({
                'name': 'replacement.mp4', 'width': 1920, 'height': 1080,
                'duration_seconds': 20,
            })
        self.assertEqual(self.inputs.status()['name'], 'first.mp4')

    def test_browser_video_rejects_wrong_dimensions_and_invalid_duration(self):
        with self.assertRaisesRegex(ValueError, '1920 x 1080'):
            self.inputs.start_browser({
                'name': 'wrong.mp4', 'width': 1280, 'height': 720,
                'duration_seconds': 10,
            })
        with self.assertRaisesRegex(ValueError, 'duration'):
            self.inputs.start_browser({
                'name': 'wrong.mp4', 'width': 1920, 'height': 1080,
                'duration_seconds': float('nan'),
            })

    def test_close_releases_both_browser_and_production_stations(self):
        self.inputs.start_browser({
            'name': 'test.mp4', 'width': 1920, 'height': 1080,
            'duration_seconds': 10,
        })
        browser_station = self.inputs.station
        self.inputs.close()
        self.assertTrue(browser_station.closed)
        self.assertTrue(self.station.closed)


class VideoTraceRuntimeTests(unittest.TestCase):
    def test_due_matches_catch_up_to_video_timestamp(self):
        runtime = EdgeRuntime.__new__(EdgeRuntime)
        runtime.video_trace = [
            {'time_seconds': 0.0, 'match': {'raw': 'A'}},
            {'time_seconds': 0.1, 'match': {'raw': 'B'}},
            {'time_seconds': 0.2, 'match': {'raw': 'C'}},
            {'time_seconds': 0.3, 'match': {'raw': 'D'}},
            {'time_seconds': 0.4, 'match': {'raw': 'E'}},
        ]
        runtime._video_trace_cursor = 0
        runtime._video_cycle_id = 'cycle'
        runtime.station = Mock()
        runtime.station.status.return_value = {'state': 'RUNNING', 'cycle_id': 'cycle'}
        backend = OpenCVCameraBackend(EdgeConfig(backend='opencv', source='source.mp4'))
        backend.playback_seconds = 0.31
        self.assertEqual([item['raw'] for item in runtime._video_matches_due(backend)],
                         ['A', 'B', 'C', 'D'])
        backend.playback_seconds = 0.5
        self.assertEqual([item['raw'] for item in runtime._video_matches_due(backend)], ['E'])


class VideoSeekTests(unittest.TestCase):
    def test_runtime_file_playback_requests_native_gstreamer_seek(self):
        cap = Mock()
        cap.isOpened.return_value = True
        cap.getBackendName.return_value = 'GSTREAMER'
        cap.duration_seconds = 10.0
        cap.get.side_effect = lambda prop: {
            5: 30.0,
            7: 300.0,
        }.get(prop, 0.0)
        backend = OpenCVCameraBackend(EdgeConfig(backend='opencv', source='source.mp4'))
        with patch('edge_runtime.os.path.isfile', return_value=True), \
             patch('edge_runtime.open_video_file', return_value=cap) as opener:
            backend.start()
        opener.assert_called_once_with('source.mp4', native_seek=True)

    def test_backend_uses_native_seek_result_without_opencv_position_setters(self):
        frame = np.full((2, 3, 3), 17, dtype=np.uint8)

        class NativeCapture:
            def __init__(self):
                self.set_calls = []

            def seek_time(self, seconds):
                self.requested = seconds
                return {'frame': frame, 'playback_seconds': 4.96, 'frame_index': 124}

            def set(self, prop, value):
                self.set_calls.append((prop, value))
                return False

        cap = NativeCapture()
        backend = OpenCVCameraBackend(EdgeConfig(backend='opencv', source='source.mp4'))
        backend.cap = cap
        backend._is_file = True
        backend._file_fps = 25.0
        backend.duration_seconds = 10.0
        result = backend.seek(5.0)
        self.assertEqual(cap.requested, 5.0)
        self.assertEqual(cap.set_calls, [])
        self.assertTrue(backend.paused)
        self.assertEqual(backend.frame_index, 124)
        self.assertAlmostEqual(result['playback_seconds'], 4.96)
        np.testing.assert_array_equal(backend.last_frame, frame)

    def test_native_seek_uses_time_format_flush_and_key_unit(self):
        import edge_runtime as runtime
        fake_gst = SimpleNamespace(
            SECOND=1_000_000_000,
            State=SimpleNamespace(PAUSED='paused', PLAYING='playing'),
            Format=SimpleNamespace(TIME='time'),
            SeekFlags=SimpleNamespace(FLUSH=1, KEY_UNIT=2),
        )
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        capture = GstVideoFileCapture.__new__(GstVideoFileCapture)
        capture._opened = True
        capture.pipeline = Mock()
        capture.pipeline.seek_simple.return_value = True
        capture.pace = Mock()
        capture.sink = Mock()
        capture.realtime = True
        capture.duration_seconds = 20.0
        capture.last_error = ''
        capture._decode_until = Mock(return_value=(frame, 7.0, 175))
        capture._rebuild_from_start = Mock()
        with patch.object(runtime, 'Gst', fake_gst):
            result = capture.seek_time(7.0)
        capture.pipeline.seek_simple.assert_called_once_with('time', 3, 7_000_000_000)
        capture._rebuild_from_start.assert_not_called()
        self.assertEqual(result['frame_index'], 175)

    def test_failed_native_seek_rebuilds_and_decodes_from_start(self):
        import edge_runtime as runtime
        fake_gst = SimpleNamespace(
            SECOND=1_000_000_000,
            State=SimpleNamespace(PAUSED='paused', PLAYING='playing'),
            Format=SimpleNamespace(TIME='time'),
            SeekFlags=SimpleNamespace(FLUSH=1, KEY_UNIT=2),
        )
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        capture = GstVideoFileCapture.__new__(GstVideoFileCapture)
        capture._opened = True
        capture.pipeline = Mock()
        capture.pipeline.seek_simple.return_value = False
        capture.pace = Mock()
        capture.sink = Mock()
        capture.realtime = True
        capture.duration_seconds = 20.0
        capture.last_error = ''
        capture._decode_until = Mock(return_value=(frame, 9.0, 225))
        capture._rebuild_from_start = Mock()
        with patch.object(runtime, 'Gst', fake_gst):
            result = capture.seek_time(9.0)
        capture._rebuild_from_start.assert_called_once_with()
        self.assertEqual(result['frame_index'], 225)


class TemperatureTests(unittest.TestCase):
    def test_cpu_max_excludes_voltage_and_missing_is_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i,(name,value) in enumerate([('vbat',5000),('cpu0-thermal',61000),('cpu1-thermal',65000),('pm8350c-thermal',70000)]):
                zone=root/f'thermal_zone{i}'
                zone.mkdir()
                (zone/'type').write_text(name)
                (zone/'temp').write_text(str(value))
            status = TemperatureReader(root).status()
            self.assertEqual(status['celsius'],65.0)
            self.assertEqual(status['sensor'],'cpu1-thermal')
            self.assertEqual(len(status['sensors']),3)
            missing=TemperatureReader(root/'missing').status()
            self.assertFalse(missing['available'])
            self.assertIsNone(missing['celsius'])


class UploadAPITests(unittest.TestCase):
    def test_feature_config_can_disable_video_test_mode(self):
        import visionedge_server as web

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'edge.ini'
            config.write_text('[features]\nvideo_test_enabled = false\n', encoding='utf-8')
            self.assertFalse(web._load_video_test_enabled(config))
            config.write_text('[features]\nvideo_test_enabled = true\n', encoding='utf-8')
            self.assertTrue(web._load_video_test_enabled(config))

    def test_disabled_video_test_routes_return_403_without_starting_inputs(self):
        import visionedge_server as web

        with patch.object(web, 'VIDEO_TEST_ENABLED', False), \
             patch.object(web, 'inputs') as get_inputs:
            client = web.app.test_client()
            start = client.post('/api/edge/browser-video/start', json={})
            upload = client.post('/api/edge/video-test/upload')
            capture = client.post('/api/apl/capture-inspect-frame')
        self.assertEqual(start.status_code, 403)
        self.assertEqual(upload.status_code, 403)
        self.assertEqual(capture.status_code, 403)
        self.assertIn('disabled', start.json['error'])
        get_inputs.assert_not_called()

    def test_native_seek_mode_uses_direct_gstreamer_capture(self):
        import edge_runtime as runtime
        expected = object()
        with patch.object(runtime, 'GST_AVAILABLE', True), patch.object(runtime, 'Gst') as gst, \
             patch.object(runtime, 'GstVideoFileCapture', return_value=expected) as capture:
            gst.ElementFactory.find.return_value = object()
            result = runtime.open_video_file(ROOT / 'sample.mp4', native_seek=True)
        self.assertIs(result, expected)
        capture.assert_called_once_with((ROOT / 'sample.mp4').resolve(), 'qtdemux', realtime=True)

    def test_qualcomm_video_uses_video_track_and_pixel_converter(self):
        import edge_runtime as runtime
        with patch.object(runtime, 'GST_AVAILABLE', True), patch.object(runtime, 'Gst'), \
             patch.object(runtime.cv2, 'VideoCapture') as capture:
            runtime.open_video_file(ROOT / 'sample.mp4')
        pipeline, backend = capture.call_args.args
        self.assertIn('qtdemux name=demux demux.video_0', pipeline)
        self.assertIn('qtivtransform ! video/x-raw,format=BGR', pipeline)
        self.assertIn('identity sync=true', pipeline)
        self.assertIn('appsink sync=false max-buffers=1 drop=true', pipeline)
        self.assertNotIn('qtiqmmfsrc', pipeline)
        self.assertEqual(backend, runtime.cv2.CAP_GSTREAMER)

    def test_offline_qualcomm_video_preserves_frames_without_clocking(self):
        import edge_runtime as runtime
        with patch.object(runtime, 'GST_AVAILABLE', True), patch.object(runtime, 'Gst'), \
             patch.object(runtime.cv2, 'VideoCapture') as capture:
            runtime.open_video_file(ROOT / 'sample.mp4', realtime=False)
        pipeline, backend = capture.call_args.args
        self.assertIn('identity sync=false', pipeline)
        self.assertIn('appsink sync=false max-buffers=4 drop=false', pipeline)
        self.assertEqual(backend, runtime.cv2.CAP_GSTREAMER)

    def test_upload_works_with_read_only_request_limit(self):
        import visionedge_server as web
        fake = Mock()
        fake.upload.return_value = {'id': 'upload-test', 'name': 'sample.mp4'}
        with patch.object(web, 'inputs', return_value=fake):
            response = web.app.test_client().post('/api/edge/video-test/upload',
                data={'video': (io.BytesIO(b'video'), 'sample.mp4')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['video']['id'], 'upload-test')
        self.assertIsNone(web.APLRequest.max_content_length.fset)

    def test_upload_limit_does_not_change_other_routes(self):
        import visionedge_server as web
        from flask import request
        with web.app.test_request_context('/api/edge/video-test/upload'):
            self.assertLessEqual(request.max_content_length, 1024*1024*1024 + 1024*1024)
        with web.app.test_request_context('/api/products'):
            self.assertEqual(request.max_content_length, web.app.config['MAX_CONTENT_LENGTH'])

    def test_browser_frame_endpoint_decodes_png_and_preserves_source_metadata(self):
        import cv2
        import visionedge_server as web

        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        encoded = cv2.imencode('.png', frame)[1].tobytes()
        fake_inputs = Mock()
        fake_inputs.status.return_value = {
            'mode': 'browser_video', 'id': 'browser-1',
            'name': '測試影片.mp4', 'duration_seconds': 15.0,
        }
        fake_station = Mock(config={'reference_size': [1920, 1080]})
        inspection = {
            'success': True,
            'station': {'state': 'NOT_MATCHED'},
            'attempt': {'result_image': 'result.jpg'},
        }
        with patch.object(web, 'inputs', return_value=fake_inputs), \
             patch.object(web, 'station', return_value=fake_station), \
             patch.object(web.EDGE, 'inspect_frame', return_value=inspection) as inspect:
            response = web.app.test_client().post(
                '/api/apl/capture-inspect-frame',
                data={
                    'test_id': 'browser-1',
                    'selected_seconds': '7.25',
                    'frame': (io.BytesIO(encoded), 'browser_capture.png', 'image/png'),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['success'])
        inspected_frame, source = inspect.call_args.args
        self.assertEqual(inspected_frame.shape, (1080, 1920, 3))
        self.assertEqual(source['mode'], 'browser_video')
        self.assertEqual(source['name'], '測試影片.mp4')
        self.assertEqual(source['selected_seconds'], 7.25)

    def test_browser_frame_endpoint_rejects_stale_session(self):
        import visionedge_server as web

        fake_inputs = Mock()
        fake_inputs.status.return_value = {
            'mode': 'browser_video', 'id': 'current',
            'name': 'test.mp4', 'duration_seconds': 15.0,
        }
        with patch.object(web, 'inputs', return_value=fake_inputs):
            response = web.app.test_client().post(
                '/api/apl/capture-inspect-frame',
                data={'test_id': 'old'},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn('stale', response.json['error'])
