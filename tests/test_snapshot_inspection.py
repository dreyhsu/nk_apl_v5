"""Regression tests for the operator-triggered snapshot inspection mode."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from apl_matching import APLMatcher
from apl_station import APLStation
from edge_runtime import EdgeRuntime


class SnapshotMatcher:
    def __init__(self):
        # Lets a test prove the automatic probe ran without writing anything.
        self.calls = 0

    def evaluate_snapshot(self, frame):
        self.calls += 1
        accepted = int(frame[0, 0, 0]) == 1
        return {
            "raw": "50_COVER_CLOSED_CARD_PRESENT" if accepted else "UNKNOWN",
            "stable": "50_COVER_CLOSED_CARD_PRESENT" if accepted else "UNKNOWN",
            "accepted": accepted, "reason": "accepted" if accepted else "below_threshold",
            "score": 0.95 if accepted else 0.40, "gap": 0.20 if accepted else -0.10,
            "box": [1, 1, 2, 2] if accepted else None, "roi": [0, 0, 4, 4],
            "template": "sample.png",
            "class_scores": {"50_COVER_CLOSED_CARD_PRESENT": 0.95 if accepted else 0.40},
            "conditions": {"cover_closed": accepted, "mcio_card_present": accepted},
        }

    @staticmethod
    def draw(frame, station):
        return frame.copy()


class Transport:
    base_url = "https://sifi.example"

    def __init__(self):
        self.fail = False
        self.calls = []

    def status(self):
        return {"mode": "http", "configured": True, "label": "SIFI"}

    def send(self, path, payload):
        self.calls.append((path, dict(payload)))
        if self.fail:
            raise TimeoutError("SIFI timeout")
        return {**payload, "accepted": True}


class SnapshotInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "workflow.json"
        self.config.write_text(json.dumps({
            "station_id": "TEST", "inspection_mode": "single_snapshot_cover_card",
            "target_state": "50_COVER_CLOSED_CARD_PRESENT",
            "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                      "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
            "template_root": "templates", "station_mode": "remote",
            "dmz": {"mode": "http"},
            "sn_prefix": "K2V5", "mcio_sn_prefix": "APL",
        }), encoding="utf-8")
        self.transport = Transport()
        self.station = APLStation(self.config, self.root / "data",
                                  transport=self.transport, matcher=SnapshotMatcher())
        self.station.set_operator("OP-TEST")

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    @staticmethod
    def frame(accepted):
        return np.full((8, 8, 3), 1 if accepted else 0, dtype=np.uint8)

    def start_cycle(self, sn="K2V5-001", mcio="APL-001"):
        """Snapshot mode runs SFIS Step1 on the product scan, then scans MCIO."""
        self.station.scan(sn)
        return self.station.scan(mcio)

    def test_scan_requires_operator_and_sends_emp_no(self):
        self.station.clear_operator()
        blocked = self.station.scan("K2V5-001")
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["station"]["state"], "WAITING_SN")
        self.assertEqual(self.transport.calls, [])
        self.station.set_operator("M999")
        accepted = self.start_cycle()
        self.assertTrue(accepted["success"])
        self.assertEqual(self.transport.calls[0][1]["emp_no"], "M999")
        self.assertEqual(accepted["station"]["operator"], "M999")
        self.assertEqual(accepted["station"]["cycle_operator"], "M999")

    def test_camera_cancel_discards_late_matching_without_sending_sfis(self):
        self.start_cycle()
        calls = len(self.transport.calls)
        stopped = threading.Event()
        original = self.station.matcher.evaluate_snapshot
        def evaluate(frame):
            stopped.set()
            return original(frame)
        self.station.matcher.evaluate_snapshot = evaluate
        result = self.station.inspect_snapshot(self.frame(True), source={'mode': 'camera'},
                                                cancelled=stopped.is_set)
        self.assertFalse(result['success'])
        self.assertNotEqual(self.station.state, 'PASS_CONFIRM')
        self.assertEqual(len(self.transport.calls), calls)

    def test_stop_preserves_pending_step4_and_accepts_its_existing_response(self):
        self.start_cycle()
        self.station.inspect_snapshot(self.frame(True))
        original = self.transport.send
        def send(path, payload):
            self.assertEqual(self.station.state, 'UPLOADING')
            token = self.station.request_token
            self.station.camera_fault('Camera stopped by operator')
            self.assertEqual(self.station.request_token, token)
            return original(path, payload)
        self.transport.send = send
        result = self.station.confirm_result(self.station.cycle_id)
        self.assertTrue(result['success'])
        self.assertEqual(self.station.state, 'WAITING_SN')

    def test_test_mode_still_requires_operator_before_sn(self):
        self.station.clear_operator()
        self.station.set_test_mode(True)
        blocked = self.station.scan("K2V5-TEST", source="scanner")
        self.assertFalse(blocked["success"])
        self.assertTrue(blocked["station"]["operator_required"])
        self.assertEqual(blocked["station"]["sn"], "")
        self.assertEqual(blocked["station"]["cycle_id"], "")
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(self.station.set_operator("M999")["success"])
        accepted = self.station.scan("K2V5-TEST", source="scanner")
        self.assertTrue(accepted["success"])
        self.assertEqual(accepted["station"]["state"], "WAITING_MCIO_SN")
        self.assertEqual(accepted["station"]["cycle_operator"], "M999")
        self.assertEqual(self.transport.calls, [])

    def test_operator_can_switch_during_an_active_cycle(self):
        # The employee number is never locked, so it stays correctable mid-cycle.
        self.station.scan("K2V5-001")
        self.assertTrue(self.station.set_operator("M888")["success"])
        self.assertEqual(self.station.operator, "M888")
        self.assertEqual(self.station.cycle_operator, "M888")

    def test_scan_starts_and_locks_until_termination(self):
        first = self.start_cycle()
        self.assertTrue(first["success"])
        self.assertEqual(first["station"]["state"], "WAITING_CAPTURE")
        self.assertEqual(self.transport.calls[0][0], "/sn/check")
        # Re-scanning a label this cycle already holds is a harmless no-op; a
        # different serial still reports the locked SN.
        self.assertTrue(self.station.scan("K2V5-001")["success"])
        self.assertTrue(self.station.scan("APL-001")["success"])
        self.assertFalse(self.station.scan("K2V5-002")["success"])
        cycle = first["station"]["cycle_id"]
        self.assertTrue(self.station.terminate(cycle)["success"])
        self.assertEqual(self.station.status()["state"], "WAITING_SN")
        self.assertEqual(self.station.status()["last_result"]["cycle_status"], "TERMINATED")

    def test_every_attempt_is_saved_and_pass_upload_unlocks(self):
        cycle = self.start_cycle()["station"]["cycle_id"]
        first = self.station.inspect_snapshot(self.frame(False), source={"mode": "video_test"})
        self.assertTrue(first["success"])
        self.assertEqual(first["station"]["state"], "NOT_MATCHED")
        second = self.station.inspect_snapshot(self.frame(True), source={"mode": "video_test"})
        self.assertTrue(second["success"])
        self.assertEqual(second["station"]["state"], "PASS_CONFIRM")
        self.assertNotEqual(first["attempt"]["attempt_id"], second["attempt"]["attempt_id"])
        self.assertEqual(second["attempt"]["mcio_sn"], "APL-001")
        self.assertEqual(second["attempt"]["schema_version"], 2)
        detail = self.station.history_detail(cycle)
        self.assertEqual([item["visual_result"] for item in detail["attempts"]],
                         ["NOT_MATCHED", "OK"])
        self.assertTrue(all((self.station.evidence.root / item["raw_path"]).is_file()
                            for item in detail["attempts"]))
        self.assertTrue(self.station.confirm_result(cycle)["success"])
        self.assertEqual([path for path, _ in self.transport.calls], ["/sn/check", "/result"])
        self.assertEqual(self.transport.calls[-1][1]["mcio_sn"], "APL-001")
        self.assertEqual(self.station.status()["state"], "WAITING_SN")
        self.assertTrue(self.station.status()["last_result"]["reported"])

    def test_failed_upload_keeps_sn_locked(self):
        cycle = self.start_cycle()["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(True))
        self.transport.fail = True
        self.assertFalse(self.station.confirm_result(cycle)["success"])
        self.assertEqual(self.station.status()["state"], "PASS_CONFIRM")
        self.assertEqual(self.station.status()["sn"], "K2V5-001")
        self.assertEqual(self.station.status()["mcio_sn"], "APL-001")
        self.assertFalse(self.station.scan("K2V5-002")["success"])

    def test_failed_step1_keeps_the_sn_and_a_rescan_retries_it(self):
        self.transport.fail = True
        rejected = self.station.scan("K2V5-BAD")
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["station"]["state"], "SN_CONFIRM")
        self.assertEqual(rejected["station"]["sn"], "K2V5-BAD")
        self.assertTrue(rejected["station"]["step1_retry_required"])
        # Re-scanning the same product SN re-sends Step1; it passes this time.
        self.transport.fail = False
        retried = self.station.scan("K2V5-BAD")
        self.assertTrue(retried["success"])
        self.assertEqual(retried["station"]["state"], "WAITING_MCIO_SN")
        checks = [call for call in self.transport.calls if call[0] == "/sn/check"]
        self.assertEqual(len(checks), 2)

    def test_terminate_after_ok_preserves_visual_result_without_pass_upload(self):
        cycle = self.start_cycle()["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(True))
        self.assertTrue(self.station.terminate(cycle)["success"])
        result = self.station.status()["last_result"]
        self.assertEqual(result["visual_result"], "OK")
        self.assertEqual(result["upload_status"], "NOT_SENT_BY_OPERATOR")
        self.assertEqual(result["mcio_sn"], "APL-001")
        self.assertFalse(result["reported"])

    def test_runtime_inspects_supplied_browser_frame_with_the_same_matcher(self):
        self.start_cycle(sn="K2V5-BROWSER", mcio="APL-BROWSER")
        runtime = EdgeRuntime.__new__(EdgeRuntime)
        runtime.lock = threading.RLock()
        runtime.station = self.station
        runtime.cfg = SimpleNamespace(snapshot_quality=92, preview_quality=80)
        runtime.latest_result = None
        runtime.latest_result_jpeg = b""
        runtime.frame_pass = False
        runtime.verdict = "WAIT"
        runtime.last_infer_at = None
        runtime.infer_seq = 0
        result = runtime.inspect_frame(
            self.frame(True),
            {
                "mode": "browser_video",
                "name": "測試影片.mp4",
                "selected_seconds": 4.25,
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["station"]["state"], "PASS_CONFIRM")
        self.assertEqual(result["attempt"]["source"]["mode"], "browser_video")
        self.assertEqual(result["attempt"]["source"]["selected_seconds"], 4.25)
        self.assertTrue(runtime.frame_pass)
        self.assertEqual(runtime.verdict, "OK")


class GoldenSampleDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        config_path = cls.root / "config" / "apl_workflow.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        cls.matcher = APLMatcher(config, config_path)

    def sample_frame(self, folder):
        import cv2

        path = next((self.root / "templates" / folder).glob("*.png"))
        gray = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
        width, height = self.matcher.size
        x, y, _, _ = self.matcher.roi
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[y:y + gray.shape[0], x:x + gray.shape[1]] = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return frame

    def test_target_sample_passes_both_conditions(self):
        result = self.matcher.evaluate_snapshot(
            self.sample_frame("50_COVER_CLOSED_CARD_PRESENT_CROOP")
        )
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["conditions"]["cover_closed"])
        self.assertTrue(result["conditions"]["mcio_card_present"])

    def test_missing_one_condition_is_rejected(self):
        for folder in ("50_COVER_CLOSED_CROOP", "50_COVER_OPEN_CARD_PRESENT_CROOP"):
            with self.subTest(folder=folder):
                result = self.matcher.evaluate_snapshot(self.sample_frame(folder))
                self.assertFalse(result["accepted"], result)


class AutoCaptureProbeTests(unittest.TestCase):
    """Continuous template matching: the probe writes nothing until a frame is
    accepted, and the frame it accepts is committed through inspect_frame."""

    # Comfortably wider than the 1/fps probe period, so float drift in this fake
    # clock can never skip a window and make a count assertion flaky.
    STEP = 0.5

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "workflow.json"
        self.transport = Transport()
        self.matcher = SnapshotMatcher()
        self.station = None

    def build(self, **auto):
        """A station parked in WAITING_CAPTURE, plus a runtime carrying only the
        fields _auto_capture_tick and inspect_frame actually touch."""
        payload = {
            "station_id": "TEST", "inspection_mode": "single_snapshot_cover_card",
            "target_state": "50_COVER_CLOSED_CARD_PRESENT",
            "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                      "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
            "template_root": "templates", "station_mode": "remote",
            "dmz": {"mode": "http"}, "sn_prefix": "K2V5", "mcio_sn_prefix": "APL",
        }
        if auto:
            payload["auto_capture"] = auto
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.station = APLStation(self.config, self.root / "data",
                                  transport=self.transport, matcher=self.matcher)
        self.addCleanup(self.station.close)
        self.station.set_operator("OP-AUTO")
        self.station.scan("K2V5-001")
        scanned = self.station.scan("APL-001")
        self.assertEqual(scanned["station"]["state"], "WAITING_CAPTURE")

        runtime = EdgeRuntime.__new__(EdgeRuntime)
        runtime.lock = threading.RLock()
        runtime.station = self.station
        runtime.cfg = SimpleNamespace(snapshot_quality=92, preview_quality=80)
        runtime.latest_result = None
        runtime.latest_result_jpeg = b""
        runtime.frame_pass = False
        runtime.verdict = "WAIT"
        runtime.last_infer_at = None
        runtime.infer_seq = 0
        runtime.frame_seq = 7
        runtime.stop_event = threading.Event()
        runtime._stopping = False
        runtime._release_failed = False
        runtime._camera_generation = 0
        runtime.video_trace = None
        return runtime

    @staticmethod
    def frame(accepted):
        return np.full((8, 8, 3), 1 if accepted else 0, dtype=np.uint8)

    def tick(self, runtime, accepted, now, backend_status=None):
        """One inference slot, then wait out any commit the probe started."""
        runtime._auto_capture_tick(self.frame(accepted), self.station.status(),
                                   backend_status or {}, now)
        thread = runtime._auto_thread
        if thread is not None:
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive(), "automatic commit did not finish")

    def attempts(self):
        return int(self.station.status()["attempt_count"])

    def evidence_files(self):
        root = self.root / "data" / "evidence"
        return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def assert_disarmed_with_an_accepted_frame(self, runtime, reason, backend_status=None):
        self.tick(runtime, True, 0.0, backend_status=backend_status)
        auto = runtime._auto_capture_status()
        self.assertEqual(self.attempts(), 0)
        self.assertFalse(auto["armed"])
        self.assertEqual(auto["reason"], reason)
        self.assertEqual(self.matcher.calls, 0)

    def test_probe_never_writes_evidence_while_rejecting(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=2)
        for index in range(20):
            self.tick(runtime, False, self.STEP * index)
        auto = runtime._auto_capture_status()
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.evidence_files(), [])
        self.assertEqual(self.station.status()["state"], "WAITING_CAPTURE")
        self.assertEqual(auto["hits"], 0)
        self.assertTrue(auto["armed"])
        self.assertEqual(auto["score"], 0.40)
        self.assertEqual(self.matcher.calls, 20)

    def test_probe_commits_one_attempt_on_the_required_consecutive_accepts(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=2)
        self.tick(runtime, True, 0.0)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(runtime._auto["hits"], 1)
        self.tick(runtime, True, self.STEP)
        self.assertEqual(self.attempts(), 1)
        self.assertEqual(self.station.status()["state"], "PASS_CONFIRM")
        for index in range(2, 12):
            self.tick(runtime, True, self.STEP * index)
        self.assertEqual(self.attempts(), 1)
        auto = runtime._auto_capture_status()
        self.assertFalse(auto["armed"])
        self.assertEqual(auto["reason"], "state:PASS_CONFIRM")
        self.assertEqual(auto["last_commit"]["trigger"], "accepted")

    def test_a_reject_between_accepts_resets_the_hit_counter(self):
        """StableStateFilter latches its stable state forever, so the probe has to
        count consecutive accepts itself."""
        runtime = self.build(enabled=True, fps=5.0, required_hits=2)
        for index, accepted in enumerate((True, False, True)):
            self.tick(runtime, accepted, self.STEP * index)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(runtime._auto["hits"], 1)
        self.assertEqual(self.station.status()["state"], "WAITING_CAPTURE")

    def test_timeout_commits_one_not_matched_attempt_then_stops(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=2, timeout_sec=5.0)
        self.tick(runtime, False, 0.0)
        self.assertEqual(self.attempts(), 0)
        self.tick(runtime, False, 5.0)
        self.assertEqual(self.attempts(), 1)
        self.assertEqual(self.station.status()["state"], "NOT_MATCHED")
        self.assertEqual(self.station.status()["result"], "NOT_MATCHED")
        for index in range(10):
            self.tick(runtime, False, 6.0 + index)
        self.assertEqual(self.attempts(), 1)
        auto = runtime._auto_capture_status()
        self.assertFalse(auto["armed"])
        self.assertEqual(auto["reason"], "timeout_stopped")
        self.assertTrue(auto["suspended"])
        self.assertEqual(auto["last_commit"]["trigger"], "timeout")

    def test_a_manual_capture_resumes_automatic_inspection_after_a_timeout(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=2, timeout_sec=5.0)
        self.tick(runtime, False, 0.0)
        self.tick(runtime, False, 5.0)
        self.assertEqual(self.attempts(), 1)
        # The operator presses the button; the product is still not seated right.
        manual = runtime.inspect_frame(self.frame(False), {"mode": "camera"})
        self.assertTrue(manual["success"])
        self.assertEqual(self.attempts(), 2)
        self.assertFalse(runtime._auto_capture_status()["suspended"])
        self.tick(runtime, True, 10.0)
        self.tick(runtime, True, 11.0)
        self.assertEqual(self.attempts(), 3)
        self.assertEqual(self.station.status()["state"], "PASS_CONFIRM")

    def test_auto_capture_stays_disarmed_in_browser_test_mode(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=1)
        self.station.set_test_mode(True)
        self.assert_disarmed_with_an_accepted_frame(runtime, "test_mode")

    def test_auto_capture_stays_disarmed_while_replaying_a_video_trace(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=1)
        runtime.video_trace = []
        self.assert_disarmed_with_an_accepted_frame(runtime, "video_trace")

    def test_auto_capture_stays_disarmed_while_the_camera_is_stopping(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=1)
        runtime.stop_event.set()
        self.assert_disarmed_with_an_accepted_frame(runtime, "stopping")

    def test_auto_capture_stays_disarmed_on_a_file_source(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=1)
        self.assert_disarmed_with_an_accepted_frame(runtime, "file_source",
                                                   backend_status={"is_file": True})

    def test_auto_capture_is_disabled_by_default(self):
        runtime = self.build()
        self.assertFalse(self.station.auto_capture["enabled"])
        for index in range(5):
            self.tick(runtime, True, self.STEP * index)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.matcher.calls, 0)
        self.assertEqual(runtime._auto_capture_status()["reason"], "disabled")

    def test_evidence_records_the_automatic_capture(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=1)
        cycle_id = self.station.status()["cycle_id"]
        self.tick(runtime, True, 0.0)
        attempt = self.station.history_detail(cycle_id)["attempts"][0]
        self.assertEqual(attempt["visual_result"], "OK")
        self.assertTrue(attempt["source"]["auto_capture"])
        self.assertEqual(attempt["source"]["auto_capture_trigger"], "accepted")
        self.assertEqual(attempt["source"]["captured_from_frame_seq"], 7)
        # History filtering keys off the mode, which has to stay 'camera'.
        self.assertEqual(attempt["source"]["mode"], "camera")
        payload = json.loads(
            self.station.resolve_evidence(attempt["json_path"]).read_text(encoding="utf-8"))
        self.assertTrue(payload["source"]["auto_capture"])
        self.assertEqual(payload["matching"]["reason"], "accepted")

    def test_a_probe_exception_never_reaches_the_camera_loop(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=1)

        def boom(frame):
            raise RuntimeError("template root vanished")

        self.station.matcher = SimpleNamespace(evaluate_snapshot=boom,
                                               draw=SnapshotMatcher.draw)
        self.tick(runtime, True, 0.0)
        auto = runtime._auto_capture_status()
        self.assertEqual(auto["reason"], "probe_error")
        self.assertIn("template root vanished", auto["error"])
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.station.status()["state"], "WAITING_CAPTURE")

    def test_the_overlay_shows_live_probe_scores_without_polluting_the_evidence(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=2)
        self.tick(runtime, False, 0.0)
        self.assertEqual(self.station.preview_matching["score"], 0.40)
        # What the station reports as its own match - and therefore journals and
        # saves as evidence - stays untouched.
        self.assertEqual(self.station.status()["matching"]["raw"], "UNKNOWN")
        self.assertEqual(self.station.status()["matching"]["class_scores"], {})
        # The overlay draw path, though, sees the live scores.
        results = self.station.process_frame(self.frame(False))[2]
        self.assertEqual([row["score"] for row in results], [0.40])

    def test_a_committed_attempt_clears_the_overlay_preview(self):
        runtime = self.build(enabled=True, fps=5.0, required_hits=2, timeout_sec=5.0)
        self.tick(runtime, False, 0.0)
        self.assertIsNotNone(self.station.preview_matching)
        self.tick(runtime, False, 5.0)
        self.assertEqual(self.attempts(), 1)
        # The saved NOT_MATCHED result owns the overlay now, not the last probe.
        self.assertIsNone(self.station.preview_matching)
        self.assertIsNone(runtime._auto["last_match"])
        self.assertEqual(self.station.status()["matching"]["reason"], "below_threshold")


if __name__ == "__main__":
    unittest.main()
