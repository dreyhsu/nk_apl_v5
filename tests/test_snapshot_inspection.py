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
    def evaluate_snapshot(self, frame):
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

    def test_scan_requires_operator_and_sends_emp_no(self):
        self.station.clear_operator()
        blocked = self.station.scan("SN-001")
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["station"]["state"], "WAITING_SN")
        self.assertEqual(self.transport.calls, [])
        self.station.set_operator("M999")
        accepted = self.station.scan("SN-001")
        self.assertTrue(accepted["success"])
        self.assertEqual(self.transport.calls[0][1]["emp_no"], "M999")
        self.assertEqual(accepted["station"]["operator"], "M999")
        self.assertEqual(accepted["station"]["cycle_operator"], "M999")

    def test_operator_cannot_switch_during_active_cycle(self):
        self.station.scan("SN-001")
        self.assertFalse(self.station.set_operator("M888")["success"])
        self.assertFalse(self.station.clear_operator()["success"])
        self.assertEqual(self.station.operator, "OP-TEST")

    def test_scan_starts_and_locks_until_termination(self):
        first = self.station.scan("SN-001")
        self.assertTrue(first["success"])
        self.assertEqual(first["station"]["state"], "WAITING_CAPTURE")
        self.assertEqual(self.transport.calls[0][0], "/sn/check")
        self.assertFalse(self.station.scan("SN-001")["success"])
        self.assertFalse(self.station.scan("SN-002")["success"])
        cycle = first["station"]["cycle_id"]
        self.assertTrue(self.station.terminate(cycle)["success"])
        self.assertEqual(self.station.status()["state"], "WAITING_SN")
        self.assertEqual(self.station.status()["last_result"]["cycle_status"], "TERMINATED")

    def test_every_attempt_is_saved_and_pass_upload_unlocks(self):
        cycle = self.station.scan("SN-001")["station"]["cycle_id"]
        first = self.station.inspect_snapshot(self.frame(False), source={"mode": "video_test"})
        self.assertTrue(first["success"])
        self.assertEqual(first["station"]["state"], "NOT_MATCHED")
        second = self.station.inspect_snapshot(self.frame(True), source={"mode": "video_test"})
        self.assertTrue(second["success"])
        self.assertEqual(second["station"]["state"], "PASS_CONFIRM")
        self.assertNotEqual(first["attempt"]["attempt_id"], second["attempt"]["attempt_id"])
        detail = self.station.history_detail(cycle)
        self.assertEqual([item["visual_result"] for item in detail["attempts"]],
                         ["NOT_MATCHED", "OK"])
        self.assertTrue(all((self.station.evidence.root / item["raw_path"]).is_file()
                            for item in detail["attempts"]))
        self.assertTrue(self.station.confirm_result(cycle)["success"])
        self.assertEqual([path for path, _ in self.transport.calls], ["/sn/check", "/result"])
        self.assertEqual(self.station.status()["state"], "WAITING_SN")
        self.assertTrue(self.station.status()["last_result"]["reported"])

    def test_failed_upload_keeps_sn_locked(self):
        cycle = self.station.scan("SN-001")["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(True))
        self.transport.fail = True
        self.assertFalse(self.station.confirm_result(cycle)["success"])
        self.assertEqual(self.station.status()["state"], "PASS_CONFIRM")
        self.assertEqual(self.station.status()["sn"], "SN-001")
        self.assertFalse(self.station.scan("SN-002")["success"])

    def test_failed_step1_releases_sn_for_rescan(self):
        self.transport.fail = True
        rejected = self.station.scan("SN-BAD")
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["station"]["state"], "WAITING_SN")
        self.assertEqual(rejected["station"]["sn"], "")
        self.transport.fail = False
        accepted = self.station.scan("SN-GOOD")
        self.assertTrue(accepted["success"])
        self.assertEqual(accepted["station"]["state"], "WAITING_CAPTURE")

    def test_terminate_after_ok_preserves_visual_result_without_pass_upload(self):
        cycle = self.station.scan("SN-001")["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(True))
        self.assertTrue(self.station.terminate(cycle)["success"])
        result = self.station.status()["last_result"]
        self.assertEqual(result["visual_result"], "OK")
        self.assertEqual(result["upload_status"], "NOT_SENT_BY_OPERATOR")
        self.assertFalse(result["reported"])

    def test_runtime_inspects_supplied_browser_frame_with_the_same_matcher(self):
        self.station.scan("SN-BROWSER")
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


if __name__ == "__main__":
    unittest.main()
