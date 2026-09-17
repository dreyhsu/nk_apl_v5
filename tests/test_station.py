import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from apl_station import APLStation


STEPS = ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED", "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"]


class FakeTransport:
    def __init__(self, mode="mock", endpoint="http://127.0.0.1:8791"):
        self.mode, self.base_url = mode, endpoint
        self.calls = []
        self.fail = False
        self.bad_field = None
        self.mock_response = False
        self.entered = None
        self.release = None

    def status(self):
        return {"mode": self.mode, "configured": self.mode != "disabled", "label": self.mode.upper()}

    def send(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        if self.entered:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test wait timed out")
        if self.fail:
            raise TimeoutError("DMZ unavailable")
        reply = {**payload, "accepted": True}
        if self.bad_field:
            reply[self.bad_field] = "wrong"
        if self.mock_response:
            reply["mock"] = True
        return reply


class FakeMatcher:
    def evaluate(self, frame):
        return {"raw": frame, "box": None, "roi": [0,0,20,20], "reason": "test", "class_scores": {frame: 1.0}}


class StationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "workflow.json"
        self.config.write_text(json.dumps({"steps": STEPS, "stable_frames": 2, "ng_states": ["30_PRODUCT_LOADED", "30_COVER_CLOSED"],
                                          "template_root": "templates", "dmz": {"mode": "disabled"}}))
        self.transport = FakeTransport()
        self.station = APLStation(self.config, self.root / "data", transport=self.transport, matcher=FakeMatcher())

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def scan(self):
        self.assertTrue(self.station.scan("10103704EU")["success"])
        return self.station.status()["cycle_id"]

    def begin(self):
        cycle = self.scan()
        self.assertTrue(self.station.confirm_sn(cycle)["success"])
        return cycle

    def frames(self, state, count=2):
        for _ in range(count):
            self.station.process_frame(state)

    def finish(self):
        cycle = self.begin()
        for step in STEPS:
            self.frames(step)
        self.assertEqual(self.station.status()["state"], "RESULT_CONFIRM")
        return cycle

    def reopen(self, transport=None):
        self.station.close()
        self.station = APLStation(self.config, self.root / "data", transport=transport or self.transport, matcher=FakeMatcher())

    def test_frames_before_acceptance_never_count(self):
        self.frames(STEPS[0])
        cycle = self.scan()
        for step in STEPS:
            self.frames(step)
        self.assertEqual(self.station.status()["step_index"], 0)
        self.station.confirm_sn(cycle)
        self.assertEqual(self.station.status()["matching"]["stable"], "UNKNOWN")
        self.frames(STEPS[0], 1)
        self.assertEqual(self.station.status()["step_index"], 0)
        self.frames(STEPS[0], 1)
        self.assertEqual(self.station.status()["step_index"], 1)

    def test_out_of_order_and_single_hits_do_not_finish(self):
        self.begin()
        self.frames(STEPS[3])
        self.assertEqual(self.station.status()["step_index"], 0)
        self.frames(STEPS[0], 1)
        self.frames("UNKNOWN", 1)
        self.assertEqual(self.station.status()["step_index"], 0)

    def test_precomputed_video_matches_keep_stable_ordering(self):
        self.begin()
        matches = []
        for state in STEPS:
            match = {"raw": state, "box": None, "roi": [0, 0, 20, 20],
                     "reason": "test", "class_scores": {state: 1.0}}
            matches.extend([match, dict(match)])
        self.station.process_matches("frame", matches)
        status = self.station.status()
        self.assertEqual(status["step_index"], len(STEPS))
        self.assertEqual(status["state"], "RESULT_CONFIRM")

    def test_ng_requires_empty_and_empty_never_counts_step(self):
        cycle = self.begin()
        self.frames("30_PRODUCT_LOADED")
        self.assertEqual(self.station.status()["state"], "NG")
        self.frames("UNKNOWN", 5)
        self.assertEqual(self.station.status()["state"], "NG")
        self.assertFalse(self.station.reset(cycle)["success"])
        self.frames("EMPTY_FIXTURE")
        status = self.station.status()
        self.assertEqual(status["state"], "WAITING_SN")
        self.assertEqual(status["step_index"], 0)
        self.assertEqual(status["last_result"]["result"], "NG")
        self.assertEqual(len(self.transport.calls), 1)

    def test_ng_survives_restart_until_removal(self):
        cycle = self.begin()
        self.frames("30_COVER_CLOSED")
        self.reopen()
        self.assertEqual(self.station.status()["state"], "NG")
        self.assertFalse(self.station.reset(cycle)["success"])
        self.frames("EMPTY_FIXTURE")
        self.assertEqual(self.station.status()["state"], "WAITING_SN")

    def test_complete_holds_for_manual_ack(self):
        cycle = self.finish()
        self.frames("50_COVER_CLOSED", 50)
        self.frames("30_PRODUCT_LOADED")
        self.assertEqual(self.station.status()["state"], "RESULT_CONFIRM")
        self.assertFalse(self.station.reset(cycle)["success"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(self.station.confirm_result(cycle)["success"])
        status = self.station.status()
        self.assertEqual(status["state"], "WAITING_SN")
        self.assertTrue(status["last_result"]["reported"])
        self.assertEqual(status["matching"]["stable"], "UNKNOWN")

    def test_result_retry_payload_is_identical_and_durable(self):
        cycle = self.finish()
        self.transport.fail = True
        self.assertFalse(self.station.confirm_result(cycle)["success"])
        first = self.transport.calls[-1][1]
        self.assertEqual(len(first["steps"]), 5)
        self.assertIsNotNone(first["completed_at"])
        self.reopen()
        self.assertEqual(self.station.status()["state"], "RESULT_CONFIRM")
        self.transport.fail = False
        self.assertTrue(self.station.confirm_result(cycle)["success"])
        self.assertEqual(first, self.transport.calls[-1][1])
        row = self.station.db.execute("SELECT status,attempts FROM outbox").fetchone()
        self.assertEqual(tuple(row), ("ACKED", 2))

    def test_mode_and_endpoint_changes_cannot_relabel_old_pass(self):
        cycle = self.finish()
        changed = FakeTransport("http", "https://dmz.example")
        self.reopen(changed)
        self.assertFalse(self.station.status()["dmz"]["configuration_matches_cycle"])
        self.assertFalse(self.station.confirm_result(cycle)["success"])
        self.assertEqual(changed.calls, [])
        self.assertFalse(self.station.reset(cycle)["success"])
        self.reopen(FakeTransport("mock", "http://127.0.0.1:9900"))
        self.assertFalse(self.station.confirm_result(cycle)["success"])

    def test_http_mode_rejects_explicit_mock_reply(self):
        self.transport.mode = "http"
        self.transport.mock_response = True
        cycle = self.scan()
        self.assertFalse(self.station.confirm_sn(cycle)["success"])
        self.assertEqual(self.station.status()["state"], "SN_CONFIRM")

    def test_wrong_sn_or_request_id_response_does_not_start(self):
        cycle = self.scan()
        for field in ("sn", "cycle_id", "request_id"):
            self.transport.bad_field = field
            self.assertFalse(self.station.confirm_sn(cycle)["success"])
            self.assertEqual(self.station.status()["state"], "SN_CONFIRM")
        ids = {body["request_id"] for _, body in self.transport.calls}
        self.assertEqual(len(ids), 1)

    def test_early_result_confirmation_rejected(self):
        cycle = self.begin()
        self.assertFalse(self.station.confirm_result(cycle)["success"])
        self.assertEqual(len(self.transport.calls), 1)

    def test_duplicate_candidate_is_idempotent(self):
        cycle = self.scan()
        self.station.scan("10103704EU")
        self.assertEqual(self.station.status()["cycle_id"], cycle)
        self.assertFalse(self.station.scan("OTHER")["success"])

    def test_restart_running_fails_closed(self):
        cycle = self.begin()
        self.frames(STEPS[0])
        self.reopen()
        self.assertEqual(self.station.status()["state"], "RECOVERY_REQUIRED")
        for step in STEPS:
            self.frames(step)
        self.assertFalse(self.station.confirm_result(cycle)["success"])
        self.assertTrue(self.station.reset(cycle)["success"])

    def test_camera_fault_invalidates_pending_dmz_reply(self):
        cycle = self.scan()
        self.transport.entered, self.transport.release = threading.Event(), threading.Event()
        replies = []
        thread = threading.Thread(target=lambda: replies.append(self.station.confirm_sn(cycle)))
        thread.start()
        self.assertTrue(self.transport.entered.wait(2))
        self.station.camera_fault("camera lost")
        self.transport.release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.station.status()["state"], "RECOVERY_REQUIRED")
        self.assertFalse(replies[0]["success"])

    def test_frames_still_process_while_network_waits_and_double_ok_rejected(self):
        cycle = self.scan()
        self.transport.entered, self.transport.release = threading.Event(), threading.Event()
        thread = threading.Thread(target=lambda: self.station.confirm_sn(cycle))
        thread.start()
        self.assertTrue(self.transport.entered.wait(2))
        self.frames(STEPS[0])
        self.assertEqual(self.station.status()["step_index"], 0)
        self.assertFalse(self.station.confirm_sn(cycle)["success"])
        self.transport.release.set()
        thread.join(3)
        self.assertEqual(self.station.status()["matching"]["stable"], "UNKNOWN")

    def test_frame_started_before_dmz_acceptance_is_discarded(self):
        cycle = self.scan()
        entered, release = threading.Event(), threading.Event()
        original = self.station.matcher.evaluate
        def blocked(frame):
            entered.set()
            release.wait(3)
            return original(frame)
        self.station.matcher.evaluate = blocked
        thread = threading.Thread(target=lambda: self.station.process_frame(STEPS[0]))
        thread.start()
        self.assertTrue(entered.wait(2))
        self.station.confirm_sn(cycle)
        release.set()
        thread.join(3)
        self.assertEqual(self.station.status()["matching"]["stable"], "UNKNOWN")
        self.station.matcher.evaluate = original
        self.frames(STEPS[0], 1)
        self.assertEqual(self.station.status()["step_index"], 0)

    def test_completed_result_survives_camera_fault(self):
        cycle = self.finish()
        self.station.camera_fault("camera stopped")
        self.assertEqual(self.station.status()["state"], "RESULT_CONFIRM")
        self.assertTrue(self.station.confirm_result(cycle)["success"])

    def test_storage_failure_prevents_network_side_effects(self):
        cycle = self.scan()
        self.station.db.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.station.confirm_sn(cycle)
        self.assertTrue(self.station.status()["storage_error"])
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(ValueError):
            self.station.confirm_sn(cycle)

    def test_sn_rejects_controls_and_empty_without_fixed_length(self):
        for value in ("", "A\nB", "A B", "A\x00B", "x" * 129):
            with self.assertRaises(ValueError):
                self.station.scan(value)
        self.assertTrue(self.station.scan("SHORT-1")["success"])


if __name__ == "__main__":
    unittest.main()
