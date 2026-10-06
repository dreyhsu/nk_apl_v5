"""Two-serial snapshot flow: product SN then SFIS Step1, then MCIO card SN."""

import json
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apl_station import APLStation
from dmz_client import DMZError
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

    def test_product_scan_sends_step1_before_the_mcio_step(self):
        # Step1 fires on the product SN; the MCIO card is scanned afterwards.
        reply = self.station.scan("K2V5-001")
        station = reply["station"]
        self.assertTrue(reply["success"])
        self.assertEqual(station["state"], "WAITING_MCIO_SN")
        self.assertEqual(station["sn"], "K2V5-001")
        self.assertEqual(station["mcio_sn"], "")
        self.assertEqual(station["active_scan_field"], "mcio_sn")
        self.assertFalse(station["can_capture"])
        self.assertTrue(station["can_terminate"])
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])
        # The Step1 payload never carried the MCIO serial, which is why the gate
        # can run before the card is scanned.
        self.assertNotIn("mcio_sn", self.transport.calls[0][1])

    def test_mcio_scan_opens_capture_without_a_second_step1(self):
        station = self.start_cycle()["station"]
        self.assertEqual(station["state"], "WAITING_CAPTURE")
        self.assertEqual(station["mcio_sn"], "APL-001")
        self.assertEqual(station["active_scan_field"], "")
        self.assertTrue(station["can_capture"])
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])
        self.assertEqual(self.events(), ["SN_SCANNED_AND_STARTED", "SN_CHECK_REQUESTED",
                                         "SN_ACCEPTED", "MCIO_SN_SCANNED"])

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
        # Step1 already ran on the product SN; the rejected MCIO label adds none.
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])
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

    # --- Step1 failure keeps the SN for a re-scan retry ------------------------

    def test_step1_failure_keeps_the_product_sn_in_a_sticky_retry_state(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        reply = self.station.scan("K2V5-001")
        station = reply["station"]
        self.assertFalse(reply["success"])
        self.assertEqual(station["state"], "SN_CONFIRM")
        self.assertEqual(station["sn"], "K2V5-001")
        self.assertEqual(station["mcio_sn"], "")
        self.assertTrue(station["step1_retry_required"])
        self.assertEqual(station["active_scan_field"], "sn")
        self.assertTrue(station["can_terminate"])
        self.assertFalse(station["can_capture"])
        self.assertFalse(station["request_in_flight"])
        self.assertEqual(self.station.operator, "M031978")
        self.assertEqual(self.events(), ["SN_SCANNED_AND_STARTED", "SN_CHECK_REQUESTED",
                                         "SN_CHECK_FAILED"])
        self.assertNotIn("SFIS_STEP1_REJECTED", self.events())
        state = self.station.db.execute("SELECT state FROM cycles").fetchone()["state"]
        self.assertEqual(state, "SN_CONFIRM")

    def test_rescanning_the_same_product_sn_refires_step1(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        self.station.scan("K2V5-001")
        self.transport.fail_message = None
        reply = self.station.scan("K2V5-001")
        station = reply["station"]
        self.assertTrue(reply["success"])
        self.assertEqual(station["state"], "WAITING_MCIO_SN")
        checks = [call for call in self.transport.calls if call[0] == "/sn/check"]
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0][1]["cycle_id"], checks[1][1]["cycle_id"])
        self.assertNotEqual(checks[0][1]["request_id"], checks[1][1]["request_id"])
        self.assertIn("SN_RESCANNED_FOR_STEP1_RETRY", self.events())

    def test_a_different_product_sn_during_the_retry_is_still_refused(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        self.station.scan("K2V5-001")
        reply = self.station.scan("K2V5-999")
        self.assertFalse(reply["success"])
        self.assertIn("locked", reply["error"])
        self.assertEqual(reply["station"]["state"], "SN_CONFIRM")
        self.assertEqual(reply["station"]["sn"], "K2V5-001")
        self.assertEqual(len([c for c in self.transport.calls if c[0] == "/sn/check"]), 1)

    def test_terminate_clears_a_failed_step1_cycle(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        cycle = self.station.scan("K2V5-001")["station"]["cycle_id"]
        reply = self.station.terminate(cycle)
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(reply["station"]["sn"], "")
        self.assertEqual(reply["station"]["last_result"]["cycle_status"], "TERMINATED")
        state = self.station.db.execute("SELECT state FROM cycles").fetchone()["state"]
        self.assertEqual(state, "TERMINATED")

    def test_a_step1_retry_cannot_capture_or_upload(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        cycle = self.station.scan("K2V5-001")["station"]["cycle_id"]
        self.assertFalse(self.station.inspect_snapshot(object())["success"])
        self.assertFalse(self.station.confirm_result(cycle)["success"])

    def test_a_browser_video_cannot_be_attached_during_a_step1_retry(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        self.station.scan("K2V5-001")
        with self.assertRaises(ValueError):
            self.station.set_test_mode(True)

    # --- NO EMP clears nothing, only prompts a fix -----------------------------

    def test_no_emp_keeps_the_operator_the_sn_and_the_cycle(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1, NO EMP"
        reply = self.station.scan("K2V5-001")
        station = reply["station"]
        self.assertFalse(reply["success"])
        self.assertEqual(self.station.operator, "M031978")
        self.assertEqual(self.station.cycle_operator, "M031978")
        self.assertEqual(station["sn"], "K2V5-001")
        self.assertNotEqual(station["cycle_id"], "")
        self.assertEqual(station["state"], "SN_CONFIRM")
        self.assertIn("employee number", station["last_error"].lower())

    def test_changing_the_employee_number_during_the_retry_feeds_the_next_step1(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1, NO EMP"
        self.station.scan("K2V5-001")
        self.assertTrue(self.station.clear_operator()["success"])
        self.assertTrue(self.station.set_operator("TW031561")["success"])
        self.transport.fail_message = None
        reply = self.station.scan("K2V5-001")
        self.assertTrue(reply["success"])
        checks = [call for call in self.transport.calls if call[0] == "/sn/check"]
        self.assertEqual(checks[-1][1]["emp_no"], "TW031561")
        self.assertIn("OPERATOR_CHANGED", self.events())

    def test_the_operator_and_terminate_are_refused_while_step1_is_in_flight(self):
        inside = {}

        def blocking_send(path, payload):
            inside["state"] = self.station.state
            inside["set_operator"] = self.station.set_operator("TW031561")["success"]
            inside["clear_operator"] = self.station.clear_operator()["success"]
            inside["terminate"] = self.station.terminate(self.station.cycle_id)["success"]
            inside["can_terminate"] = self.station.status()["can_terminate"]
            raise DMZError("SFIS unavailable")

        self.transport.send = blocking_send
        self.station.scan("K2V5-001")
        self.assertEqual(inside["state"], "WAITING_DMZ")
        self.assertFalse(inside["set_operator"])
        self.assertFalse(inside["clear_operator"])
        self.assertFalse(inside["terminate"])
        self.assertFalse(inside["can_terminate"])

    def test_a_repeat_scan_while_step1_is_in_flight_is_a_silent_no_op(self):
        inside = {}

        def blocking_send(path, payload):
            inside["reply"] = self.station.scan("K2V5-001")
            inside["checks"] = len([c for c in self.transport.calls if c[0] == "/sn/check"])
            raise DMZError("SFIS unavailable")

        self.transport.send = blocking_send
        self.station.scan("K2V5-001")
        self.assertTrue(inside["reply"]["success"])
        # The in-flight request is the only /sn/check; the re-scan was swallowed.
        self.assertEqual(inside["checks"], 0)

    def test_a_cycle_interrupted_at_the_step1_gate_reopens_as_a_retry(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        self.station.scan("K2V5-001")
        self.station.close()
        self.station = self.open_station()
        station = self.station.status()
        self.assertEqual(station["state"], "SN_CONFIRM")
        self.assertEqual(station["sn"], "K2V5-001")
        self.assertTrue(station["step1_retry_required"])
        self.assertTrue(station["can_terminate"])
        self.transport.fail_message = None
        reply = self.station.scan("K2V5-001")
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_MCIO_SN")

    def test_a_cycle_interrupted_mid_step1_reopens_as_a_retry(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,PASM1"
        self.station.scan("K2V5-001")
        # Force the persisted row to the in-flight state the station can crash in.
        with self.station.lock:
            self.station.db.execute("UPDATE cycles SET state='WAITING_DMZ'")
            self.station.db.commit()
        self.station.close()
        self.station = self.open_station()
        station = self.station.status()
        self.assertEqual(station["state"], "SN_CONFIRM")
        self.assertTrue(station["step1_retry_required"])

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


class AutomaticStep4Tests(unittest.TestCase):
    """Backend contracts the automatic Step4 in captureInspection depends on."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "workflow.json"

    def station(self, *, local_mode=False, test_mode=False, **dmz):
        transport = dict({"mode": "http", "api_style": "sfis",
                          "base_url": "http://10.0.28.125:8097/api/SFISAPI",
                          "allow_insecure_http": True, "line": "TEST", "station": "AI-CAM"}, **dmz)
        self.config.write_text(json.dumps({
            "station_id": "AI-CAM", "station_mode": "remote",
            "inspection_mode": "single_snapshot_cover_card",
            "target_state": "50_COVER_CLOSED_CARD_PRESENT",
            "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                      "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
            "template_root": "templates", "dmz": transport,
            "sn_prefix": "K2V5", "mcio_sn_prefix": "APL",
        }), encoding="utf-8")
        station = APLStation(self.config, self.root / "data", matcher=object(),
                             local_mode=local_mode, test_mode=test_mode)
        self.addCleanup(station.close)
        station.set_operator("M031978")
        return station

    @staticmethod
    @contextlib.contextmanager
    def accepted_step1(station):
        """Let Step1 through without a network, unless bypass already handles it."""
        if station.transport is None or station.transport.bypass:
            yield
            return
        with patch.object(station.transport, "send",
                          side_effect=lambda path, payload: {**payload, "accepted": True}):
            yield

    def reach_pass_confirm(self, station, sn="K2V5-001", mcio="APL-001"):
        with self.accepted_step1(station):
            station.scan(sn)
            scanned = station.scan(mcio)
        cycle_id = scanned["station"]["cycle_id"]
        self.assertEqual(scanned["station"]["state"], "WAITING_CAPTURE")
        with station.lock:
            station.state = "PASS_CONFIRM"
            station.result = "PASS"
            station.step_index = len(station.step_names)
            station.step_times = [123.0]
            station.completed_at = 123.0
        return cycle_id

    def test_bypass_closes_the_unit_without_any_http(self):
        station = self.station(bypass_sfis=True, step4_enabled=False)
        with patch.object(station.transport.opener, "open") as opened:
            cycle_id = self.reach_pass_confirm(station)
            # The gate the UI reads before sending automatically.
            self.assertTrue(station.status()["can_confirm_result"])
            reply = station.confirm_result(cycle_id)
        opened.assert_not_called()
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(reply["station"]["sn"], "")
        self.assertEqual(reply["station"]["mcio_sn"], "")
        self.assertTrue(reply["station"]["last_result"]["reported"])

    def test_disabled_step4_closes_the_gate_so_the_ui_never_tries(self):
        station = self.station(bypass_sfis=False, step4_enabled=False)
        self.reach_pass_confirm(station)
        self.assertFalse(station.status()["can_confirm_result"])

    def test_business_rejection_stays_on_pass_confirm_and_offers_a_retry(self):
        station = self.station(bypass_sfis=False, step4_enabled=True)
        cycle_id = self.reach_pass_confirm(station)
        with patch.object(station.transport, "send",
                          side_effect=DMZError("SFIS business validation failed: FAIL1,PASM1")):
            reply = station.confirm_result(cycle_id)
        self.assertFalse(reply["success"])
        self.assertIn("FAIL1,PASM1", reply["error"])
        status = station.status()
        self.assertEqual(status["state"], "PASS_CONFIRM")
        self.assertIn("FAIL1,PASM1", status["last_error"])
        # True again, so the UI re-shows Retry SFIS Step4 instead of a dead end.
        self.assertTrue(status["can_confirm_result"])
        self.assertEqual(status["sn"], "K2V5-001")
        events = [row["event"] for row in
                  station.db.execute("SELECT event FROM events ORDER BY id")]
        self.assertIn("REPORT_RETRY_REQUIRED", events)

    def test_a_retry_while_the_request_is_in_flight_is_refused(self):
        station = self.station(bypass_sfis=True, step4_enabled=False)
        cycle_id = self.reach_pass_confirm(station)
        with station.lock:
            station.request_token = "in-flight"
        # This is why the timeout guidance says to wait instead of retrying.
        self.assertFalse(station.status()["can_confirm_result"])
        reply = station.confirm_result(cycle_id)
        self.assertFalse(reply["success"])
        self.assertIn("not available in the current state", reply["error"])

    def test_bypass_lets_a_browser_test_close_its_own_cycle(self):
        station = self.station(bypass_sfis=True, step4_enabled=False,
                               local_mode=True, test_mode=True)
        cycle_id = self.reach_pass_confirm(station)
        profile = station.status()["dmz"]
        self.assertEqual(profile["label"], "BROWSER TEST - SFIS BYPASS")
        self.assertTrue(station.status()["can_confirm_result"])
        reply = station.confirm_result(cycle_id)
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        # Closed locally, and still recorded as never reported to SFIS.
        self.assertFalse(reply["station"]["last_result"]["reported"])
        self.assertTrue(reply["station"]["last_result"]["test_mode"])

    def test_without_bypass_a_browser_test_can_never_post(self):
        station = self.station(bypass_sfis=False, step4_enabled=True,
                               local_mode=True, test_mode=True)
        cycle_id = self.reach_pass_confirm(station)
        self.assertEqual(station.status()["dmz"]["label"], "BROWSER TEST - SFIS DISABLED")
        self.assertFalse(station.status()["can_confirm_result"])
        reply = station.confirm_result(cycle_id)
        self.assertFalse(reply["success"])
        self.assertIn("Browser test data cannot be uploaded", reply["error"])
        self.assertEqual(station.status()["state"], "PASS_CONFIRM")

    def test_the_automatic_send_still_carries_the_mcio_card_serial_number(self):
        station = self.station(bypass_sfis=False, step4_enabled=True)
        cycle_id = self.reach_pass_confirm(station)
        sent = []
        with patch.object(station.transport, "send",
                          side_effect=lambda path, payload: sent.append((path, dict(payload)))
                          or {**payload, "accepted": True, "result": "PASS"}):
            self.assertTrue(station.confirm_result(cycle_id)["success"])
        self.assertEqual(sent[-1][0], "/result")
        self.assertEqual(sent[-1][1]["mcio_sn"], "APL-001")


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
