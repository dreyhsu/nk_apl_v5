"""Two-serial snapshot flow: product SN, then MCIO card SN, then SFIS Step1."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apl_station import APLStation
from inspection_store import InspectionStore


class Transport:
    mode = "http"
    base_url = "http://sfis.example/api/SFISAPI"

    def __init__(self):
        self.calls = []
        self.fail_message = None

    def status(self):
        return {"mode": self.mode, "configured": True, "label": "SFIS"}

    def send(self, path, payload):
        self.calls.append((path, dict(payload)))
        if self.fail_message:
            from dmz_client import DMZError
            raise DMZError(self.fail_message)
        reply = {**payload, "accepted": True}
        if path == "/result":
            reply["result"] = "PASS"
        return reply


class MCIOScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "workflow.json"
        self.config.write_text(json.dumps({
            "station_id": "AI-CAM", "station_mode": "remote",
            "inspection_mode": "single_snapshot_cover_card",
            "target_state": "50_COVER_CLOSED_CARD_PRESENT",
            "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                      "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
            "template_root": "templates", "dmz": {"mode": "http"},
            "sn_prefix": "K2V5", "mcio_sn_prefix": "APL",
        }), encoding="utf-8")
        self.transport = Transport()
        self.station = self.open_station()
        self.station.set_operator("M031978")

    def open_station(self, **kwargs):
        kwargs.setdefault("transport", self.transport)
        kwargs.setdefault("matcher", object())
        return APLStation(self.config, self.root / "data", **kwargs)

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def events(self):
        rows = self.station.db.execute("SELECT event FROM events ORDER BY id").fetchall()
        return [row["event"] for row in rows]

    def start_cycle(self, sn="K2V5-001", mcio="APL-001"):
        self.station.scan(sn)
        return self.station.scan(mcio)

    # --- the two accepted steps -------------------------------------------------

    def test_product_scan_waits_for_the_mcio_card_serial_number(self):
        reply = self.station.scan("K2V5-001")
        station = reply["station"]
        self.assertTrue(reply["success"])
        self.assertEqual(station["state"], "WAITING_MCIO_SN")
        self.assertEqual(station["sn"], "K2V5-001")
        self.assertEqual(station["mcio_sn"], "")
        self.assertEqual(station["active_scan_field"], "mcio_sn")
        self.assertFalse(station["can_capture"])
        self.assertTrue(station["can_terminate"])
        self.assertEqual(self.transport.calls, [])

    def test_mcio_scan_sends_step1_and_opens_capture(self):
        station = self.start_cycle()["station"]
        self.assertEqual(station["state"], "WAITING_CAPTURE")
        self.assertEqual(station["mcio_sn"], "APL-001")
        self.assertEqual(station["active_scan_field"], "")
        self.assertTrue(station["can_capture"])
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])
        self.assertEqual(self.events(), ["SN_SCANNED_AND_STARTED", "MCIO_SN_SCANNED",
                                         "SN_CHECK_REQUESTED", "SN_ACCEPTED"])

    def test_snapshot_publishes_both_prefixes_for_the_operator_panel(self):
        station = self.station.status()
        self.assertEqual(station["sn_prefix"], "K2V5")
        self.assertEqual(station["mcio_sn_prefix"], "APL")
        self.assertEqual(station["active_scan_field"], "sn")

    # --- wrong label at a known step -------------------------------------------

    def test_mcio_label_at_the_product_step_is_rejected_without_a_cycle(self):
        reply = self.station.scan("APL-001")
        self.assertFalse(reply["success"])
        self.assertIn("must start with K2V5", reply["error"])
        self.assertIn("Scanned: APL-001", reply["error"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(reply["station"]["cycle_id"], "")
        self.assertEqual(self.transport.calls, [])
        # No cycle_id exists yet, so nothing may be journaled.
        self.assertEqual(self.events(), [])
        self.assertEqual(self.station.db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0], 0)

    def test_product_label_at_the_mcio_step_is_rejected_and_journaled(self):
        self.station.scan("K2V5-001")
        reply = self.station.scan("K2V5-002")
        self.assertFalse(reply["success"])
        self.assertIn("must start with APL", reply["error"])
        self.assertEqual(reply["station"]["state"], "WAITING_MCIO_SN")
        self.assertEqual(reply["station"]["sn"], "K2V5-001")
        self.assertEqual(reply["station"]["mcio_sn"], "")
        self.assertEqual(self.transport.calls, [])
        self.assertIn("MCIO_SN_REJECTED", self.events())

    def test_prefix_matching_ignores_scanner_case(self):
        self.assertTrue(self.station.scan("k2v5-001")["success"])
        reply = self.station.scan("apl-001")
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["sn"], "k2v5-001")
        self.assertEqual(reply["station"]["mcio_sn"], "apl-001")

    def test_malformed_text_still_raises_for_both_steps(self):
        with self.assertRaisesRegex(ValueError, "SN must contain printable ASCII"):
            self.station.scan("K2V5 001")
        self.station.scan("K2V5-001")
        with self.assertRaisesRegex(ValueError, "SN must contain printable ASCII"):
            self.station.scan("APL 001")

    def test_a_double_fired_scanner_shot_does_not_raise_the_locked_sn_alarm(self):
        self.start_cycle()
        for repeated in ("K2V5-001", "APL-001"):
            reply = self.station.scan(repeated)
            self.assertTrue(reply["success"], repeated)
            self.assertEqual(reply["station"]["state"], "WAITING_CAPTURE")
            self.assertEqual(reply["station"]["last_error"], "")
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])

    def test_a_different_serial_mid_cycle_is_still_refused(self):
        self.start_cycle()
        reply = self.station.scan("K2V5-999")
        self.assertFalse(reply["success"])
        self.assertIn("The current SN is locked", reply["error"])
        self.assertEqual(reply["station"]["sn"], "K2V5-001")

    # --- escape hatches --------------------------------------------------------

    def test_terminate_at_the_mcio_step_clears_both_serial_numbers(self):
        scanned = self.station.scan("K2V5-001")
        reply = self.station.terminate(scanned["station"]["cycle_id"])
        station = reply["station"]
        self.assertTrue(reply["success"])
        self.assertEqual(station["state"], "WAITING_SN")
        self.assertEqual(station["sn"], "")
        self.assertEqual(station["mcio_sn"], "")
        self.assertEqual(station["last_result"]["cycle_status"], "TERMINATED")
        self.assertEqual(station["last_result"]["mcio_sn"], "")

    def test_camera_loss_at_the_mcio_step_kills_the_cycle(self):
        self.station.scan("K2V5-001")
        reply = self.station.camera_fault("camera stopped")
        self.assertEqual(reply["station"]["state"], "RECOVERY_REQUIRED")
        self.assertTrue(reply["station"]["can_terminate"])

    def test_capture_is_unavailable_until_the_mcio_card_is_scanned(self):
        self.station.scan("K2V5-001")
        reply = self.station.inspect_snapshot(object())
        self.assertFalse(reply["success"])
        self.assertIn("unavailable in the current state", reply["error"])

    def test_the_operator_cannot_be_changed_at_the_mcio_step(self):
        self.station.scan("K2V5-001")
        self.assertFalse(self.station.set_operator("TW031561")["success"])
        self.assertFalse(self.station.clear_operator()["success"])
        self.assertEqual(self.station.operator, "M031978")

    # --- Step1 rollback --------------------------------------------------------

    def test_step1_rejection_on_the_mcio_scan_rolls_the_whole_cycle_back(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        reply = self.start_cycle()
        self.assertFalse(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(reply["station"]["sn"], "")
        self.assertEqual(reply["station"]["mcio_sn"], "")
        self.assertEqual(self.events(), ["SN_SCANNED_AND_STARTED", "MCIO_SN_SCANNED",
                                         "SN_CHECK_REQUESTED", "SN_CHECK_FAILED",
                                         "SFIS_STEP1_REJECTED"])
        state = self.station.db.execute("SELECT state FROM cycles").fetchone()["state"]
        self.assertEqual(state, "TERMINATED")
        self.assertEqual(self.station.operator, "M031978")

    def test_no_emp_rejection_on_the_mcio_scan_clears_the_operator(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1, NO EMP"
        reply = self.start_cycle()
        self.assertFalse(reply["success"])
        self.assertEqual(self.station.operator, "")
        self.assertIn("Employee number rejected by SFIS", reply["station"]["last_error"])

    # --- modes that never contact SFIS ----------------------------------------

    def test_local_mode_reaches_capture_without_any_step1(self):
        self.station.close()
        self.station = self.open_station(transport=None, local_mode=True)
        self.station.set_operator("M031978")
        station = self.start_cycle()["station"]
        self.assertEqual(station["state"], "WAITING_CAPTURE")
        self.assertEqual(station["mcio_sn"], "APL-001")
        self.assertEqual(self.transport.calls, [])

    def test_browser_test_mode_reaches_capture_without_any_step1(self):
        self.station.close()
        self.station = self.open_station(test_mode=True)
        self.station.set_operator("M031978")
        station = self.start_cycle()["station"]
        self.assertEqual(station["state"], "WAITING_CAPTURE")
        self.assertEqual(self.transport.calls, [])

    # --- durability ------------------------------------------------------------

    def test_a_cycle_waiting_for_the_mcio_card_reopens_at_the_same_step(self):
        self.station.scan("K2V5-001")
        self.station.close()
        self.station = self.open_station()
        station = self.station.status()
        self.assertEqual(station["state"], "WAITING_MCIO_SN")
        self.assertEqual(station["sn"], "K2V5-001")
        self.assertEqual(station["mcio_sn"], "")
        self.assertIn("MCIO card serial number", station["last_error"])
        self.assertTrue(station["can_terminate"])

    def test_a_recovered_pass_still_carries_the_mcio_card_serial_number(self):
        scanned = self.start_cycle()
        cycle_id = scanned["station"]["cycle_id"]
        with self.station.lock:
            self.station.state = "PASS_CONFIRM"
            self.station.result = "PASS"
            self.station.step_index = len(self.station.step_names)
            self.station.step_times = [123.0]
            self.station.completed_at = 123.0
            self.station._save("INSPECTION_OK")
        self.station.close()
        self.station = self.open_station()
        self.assertEqual(self.station.status()["mcio_sn"], "APL-001")
        reply = self.station.confirm_result(cycle_id)
        self.assertTrue(reply["success"])
        self.assertEqual(self.transport.calls[-1][1]["mcio_sn"], "APL-001")


class EvidenceRecordTests(unittest.TestCase):
    def test_attempt_record_carries_both_serial_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            import sqlite3
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            self.addCleanup(db.close)
            store = InspectionStore(root / "evidence", db)
            with patch.object(InspectionStore, "_encode_jpeg", staticmethod(lambda frame, quality=92: b"jpeg")):
                payload = store.save_attempt(
                    cycle_id="cycle-1", sn="K2V5-001", mcio_sn="APL-001",
                    visual_result="OK", raw_frame=object(), result_frame=object(),
                    matching={"raw": "50_COVER_CLOSED_CARD_PRESENT"},
                )
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["mcio_sn"], "APL-001")
            saved = json.loads((store.root / payload["raw_image"]).parent.joinpath(
                Path(payload["raw_image"]).name.replace("_raw.jpg", ".json")).read_text(encoding="utf-8"))
            self.assertEqual(saved["mcio_sn"], "APL-001")


if __name__ == "__main__":
    unittest.main()
