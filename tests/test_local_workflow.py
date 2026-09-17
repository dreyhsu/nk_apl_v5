"""Local SN gates, ordered evidence, and durable completion."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apl_station import APLStation
from test_station import FakeMatcher, STEPS
from video_test import VideoTestInputs
from workflow_view import workflow_view, overlay_commands

ROOT = Path(__file__).resolve().parents[1]


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        source_config = ROOT / 'config' / 'apl_workflow.json'
        config = json.loads(source_config.read_text(encoding='utf-8'))
        config['inspection_mode'] = 'legacy_workflow'
        config['template_root'] = str((ROOT / 'templates').resolve())
        self.config = Path(self.temp.name) / 'legacy_workflow.json'
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.station = APLStation(self.config, Path(self.temp.name), local_mode=True, matcher=FakeMatcher())

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def feed(self, name, count=2):
        for _ in range(count):
            self.station.process_frame(name)

    def test_local_gates_order_and_durable_pass_without_network(self):
        with patch('dmz_client.DMZClient.send', side_effect=AssertionError('Network must not be called')):
            cycle = self.station.scan('LOCAL-SN-001')['station']['cycle_id']
            for step in STEPS:
                self.feed(step)
            self.assertEqual(self.station.status()['step_index'], 0)
            self.assertIsNone(self.station.status()['sop']['current_step_id'])
            self.assertTrue(self.station.confirm_sn(cycle)['success'])
            self.assertEqual(self.station.status()['sop']['current_step_id'], 1)
            self.feed(STEPS[2])
            self.assertEqual(self.station.status()['step_index'], 0)
            for i, step in enumerate(STEPS):
                self.feed(step)
                self.assertEqual(self.station.status()['step_index'], i+1)
            self.assertEqual(self.station.status()['state'], 'RESULT_CONFIRM')
            self.feed('UNKNOWN', 10)
            self.assertEqual(self.station.status()['state'], 'RESULT_CONFIRM')
            self.assertTrue(self.station.confirm_result(cycle)['success'])
            saved = self.station.status()['last_result']
            self.assertFalse(saved['reported'])
            self.assertEqual(saved['mode'], 'local')
            self.assertEqual(self.station.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
            self.station.close()
            self.station = APLStation(self.config, Path(self.temp.name), local_mode=True, matcher=FakeMatcher())
            self.assertEqual(self.station.status()['last_result'], saved)
            self.assertEqual(self.station.status()['state'], 'WAITING_SN')

    def test_local_ng_requires_removal(self):
        cycle = self.station.scan('NG-TEST')['station']['cycle_id']
        self.station.confirm_sn(cycle)
        self.feed('30_PRODUCT_LOADED')
        self.assertEqual(self.station.status()['state'], 'NG')
        self.feed('UNKNOWN', 10)
        self.assertEqual(self.station.status()['state'], 'NG')
        self.feed('EMPTY_FIXTURE')
        self.assertEqual(self.station.status()['state'], 'WAITING_SN')
        self.assertEqual(self.station.status()['step_index'], 0)

    def test_workflow_colors_follow_real_state_and_stale_has_no_done(self):
        view = workflow_view(self.station.status())
        self.assertTrue(all(row['status'] == 'PENDING' for row in view['rows']))
        cycle = self.station.scan('VIEW-TEST')['station']['cycle_id']
        self.station.confirm_sn(cycle)
        self.feed(STEPS[0])
        state = self.station.status()
        self.assertEqual([r['status'] for r in workflow_view(state)['rows']], ['DONE','CURRENT','PENDING','PENDING','PENDING'])
        self.assertTrue(all(row['status'] == 'PENDING' for row in workflow_view(state, stale=True)['rows']))
        for command in overlay_commands(state, 1920, 1080):
            if command[0] == 'text':
                self.assertNotIn('DMZ', command[1])

    def test_reference_video_advances_five_steps(self):
        import cv2
        path = ROOT.parent / 'test1.mp4'
        if not path.exists():
            self.skipTest('Reference video is not deployed')
        self.station.matcher = None
        cycle = self.station.scan('VIDEO-REGRESSION')['station']['cycle_id']
        self.station.confirm_sn(cycle)
        cap = cv2.VideoCapture(str(path))
        try:
            stride = max(1, round(cap.get(cv2.CAP_PROP_FPS) / 10))
            for frame_index in range(int(cap.get(cv2.CAP_PROP_FRAME_COUNT))):
                ok, frame = cap.read()
                self.assertTrue(ok)
                if frame_index % stride:
                    continue
                self.station.process_frame(frame)
                if self.station.status()['state'] == 'RESULT_CONFIRM':
                    break
        finally:
            cap.release()
        self.assertEqual(self.station.status()['step_index'], 5, self.station.status()['matching'])

    def test_reference_trace_passes_with_slow_preview_updates(self):
        import cv2
        path = ROOT.parent / 'test1.mp4'
        if not path.exists():
            self.skipTest('Reference video is not deployed')
        self.station.matcher = None
        builder = VideoTestInputs.__new__(VideoTestInputs)
        builder.production_station = self.station
        builder.analysis_fps = 10.0
        trace = builder._build_trace(path, {'fps': 30.0})
        cycle = self.station.scan('SLOW-PREVIEW-REGRESSION')['station']['cycle_id']
        self.station.confirm_sn(cycle)
        cursor = 0
        duration = trace[-1]['time_seconds'] + 0.5
        display_time = 0.0
        while display_time <= duration and cursor < len(trace):
            due = []
            while cursor < len(trace) and trace[cursor]['time_seconds'] <= display_time + 1e-6:
                due.append(trace[cursor]['match'])
                cursor += 1
            self.station.process_matches('preview-frame', due)
            display_time += 1.0 / 5.1
        self.assertEqual(self.station.status()['state'], 'RESULT_CONFIRM')
        self.assertEqual(self.station.status()['step_index'], 5)

    def test_no_card_video_matches_baseline_incomplete_result(self):
        import cv2
        path = ROOT.parent / 'NG_蓋板不插卡.mp4'
        if not path.exists():
            self.skipTest('No-card reference video is not deployed')
        self.station.matcher = None
        cycle = self.station.scan('NO-CARD-REGRESSION')['station']['cycle_id']
        self.station.confirm_sn(cycle)
        cap = cv2.VideoCapture(str(path))
        try:
            stride = max(1, round(cap.get(cv2.CAP_PROP_FPS) / 10))
            for frame_index in range(int(cap.get(cv2.CAP_PROP_FRAME_COUNT))):
                ok, frame = cap.read()
                self.assertTrue(ok)
                if frame_index % stride == 0:
                    self.station.process_frame(frame)
        finally:
            cap.release()
        status = self.station.status()
        self.assertEqual(status['state'], 'RUNNING')
        self.assertEqual(status['step_index'], 1)
        self.assertIsNone(status['result'])

    def test_incomplete_fixture_video_matches_baseline_30_ng(self):
        import cv2
        path = ROOT.parent / 'NG_不蓋模具完整.mp4'
        if not path.exists():
            self.skipTest('Incomplete-fixture reference video is not deployed')
        self.station.matcher = None
        cycle = self.station.scan('NG-REGRESSION')['station']['cycle_id']
        self.station.confirm_sn(cycle)
        cap = cv2.VideoCapture(str(path))
        observed_ng = False
        try:
            stride = max(1, round(cap.get(cv2.CAP_PROP_FPS) / 10))
            for frame_index in range(int(cap.get(cv2.CAP_PROP_FRAME_COUNT))):
                ok, frame = cap.read()
                self.assertTrue(ok)
                if frame_index % stride:
                    continue
                self.station.process_frame(frame)
                if self.station.status()['state'] == 'NG':
                    observed_ng = True
        finally:
            cap.release()
        self.assertTrue(observed_ng, self.station.status()['matching'])
        self.assertEqual(self.station.status()['state'], 'WAITING_SN')
        self.assertEqual(self.station.status()['last_result']['result'], 'NG')
        self.assertEqual(self.station.status()['last_result']['reason'], '30_REMOVED_EMPTY_FIXTURE')
