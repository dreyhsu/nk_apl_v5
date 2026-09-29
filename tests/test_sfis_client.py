import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apl_station import APLStation
from dmz_client import DMZClient, DMZError


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self.body


class SFISClientTests(unittest.TestCase):
    def setUp(self):
        self.client = DMZClient({
            "mode": "http", "api_style": "sfis",
            "base_url": "http://10.0.28.125:8097/api/SFISAPI",
            "allow_insecure_http": True,
            "step2_enabled": True,
            "line": "TEST", "station": "AI-CAM", "employee_no": "M031978",
        })
        self.payload = {"sn": "M0001000P153B52D01S", "cycle_id": "cycle-1", "request_id": "request-1"}

    def send_with(self, path, response, payload=None):
        with patch.object(self.client.opener, "open", return_value=FakeHTTPResponse(response)) as opened:
            result = self.client.send(path, payload or self.payload)
        return result, opened.call_args.args[0]

    def test_step1_uses_documented_url_and_body(self):
        result, request = self.send_with("/sn/check", {
            "success": True, "data": "PASS", "message": "", "errorCode": None,
        })
        self.assertTrue(result["accepted"])
        self.assertEqual(request.full_url, "http://10.0.28.125:8097/api/SFISAPI/Step1")
        self.assertEqual(json.loads(request.data), {
            "line": "TEST", "station": "AI-CAM",
            "sn": "M0001000P153B52D01S", "empNo": "M031978",
        })

    def test_step1_prefers_operator_emp_no_over_config(self):
        payload = {**self.payload, "emp_no": "TW031561"}
        _, request = self.send_with("/sn/check", {
            "success": True, "data": "PASS", "message": "", "errorCode": None,
        }, payload)
        self.assertEqual(json.loads(request.data)["empNo"], "TW031561")

    def test_step1_rejects_business_fail_even_when_success_is_true(self):
        with self.assertRaisesRegex(DMZError, "FAIL1,HS_ASSY"):
            self.send_with("/sn/check", {
                "success": True, "data": "FAIL1,HS_ASSY ", "message": "", "errorCode": None,
            })

    def test_step2_uses_same_confirmed_fields_and_normalizes_ack(self):
        payload = {**self.payload, "result": "PASS"}
        result, request = self.send_with("/result", {
            "success": True, "data": "OK", "message": "", "errorCode": None,
        }, payload)
        self.assertEqual(request.full_url, "http://10.0.28.125:8097/api/SFISAPI/Step2")
        self.assertEqual(json.loads(request.data)["sn"], payload["sn"])
        self.assertEqual(result["result"], "PASS")

    def test_step2_is_blocked_without_explicit_test_switch(self):
        client = DMZClient({
            "mode": "http", "api_style": "sfis",
            "base_url": "http://10.0.28.125:8097/api/SFISAPI",
            "allow_insecure_http": True,
            "line": "TEST", "station": "AI-CAM", "employee_no": "M031978",
        })
        payload = {**self.payload, "result": "PASS"}
        with patch.object(client.opener, "open") as opened:
            with self.assertRaisesRegex(DMZError, "Step2 is disabled"):
                client.send("/result", payload)
        opened.assert_not_called()


class GateTransport:
    mode = "http"
    base_url = "http://sfis.example/api/SFISAPI"

    def __init__(self):
        self.calls = []
        self.fail = False
        self.fail_message = None

    def status(self):
        return {"mode": self.mode, "configured": True, "label": "SFIS"}

    def send(self, path, payload):
        self.calls.append((path, dict(payload)))
        if self.fail_message:
            raise DMZError(self.fail_message)
        if self.fail:
            raise DMZError("SFIS unavailable")
        reply = {**payload, "accepted": True}
        if path == "/result":
            reply["result"] = "PASS"
        return reply


class SnapshotSFISGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "workflow.json"
        self.config.write_text(json.dumps({
            "station_id": "AI-CAM", "station_mode": "remote",
            "inspection_mode": "single_snapshot_cover_card",
            "target_state": "50_COVER_CLOSED_CARD_PRESENT",
            "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                      "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
            "template_root": "templates", "dmz": {"mode": "http"},
        }), encoding="utf-8")
        self.transport = GateTransport()
        self.station = APLStation(self.config, root / "data", transport=self.transport, matcher=object())
        self.station.set_operator("M031978")

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def test_scan_runs_step1_before_enabling_capture(self):
        reply = self.station.scan("SN-001")
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_CAPTURE")
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])

    def test_scan_blocked_without_operator(self):
        self.station.clear_operator()
        reply = self.station.scan("SN-001")
        self.assertFalse(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(self.transport.calls, [])

    def test_step1_failure_releases_serial_number(self):
        self.transport.fail = True
        reply = self.station.scan("SN-FAIL")
        self.assertFalse(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(reply["station"]["sn"], "")

    def test_no_emp_rejection_clears_operator(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1, NO EMP"
        reply = self.station.scan("SN-001")
        self.assertFalse(reply["success"])
        self.assertEqual(self.station.operator, "")
        self.assertEqual(reply["station"]["operator"], "")

    def test_other_business_failure_keeps_operator_signed_in(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,HS_ASSY"
        reply = self.station.scan("SN-001")
        self.assertFalse(reply["success"])
        self.assertEqual(self.station.operator, "M031978")
        self.assertEqual(reply["station"]["operator"], "M031978")

    def test_completed_ok_runs_step2_and_unlocks(self):
        scanned = self.station.scan("SN-OK")
        cycle_id = scanned["station"]["cycle_id"]
        with self.station.lock:
            self.station.state = "PASS_CONFIRM"
            self.station.result = "PASS"
            self.station.step_index = len(self.station.step_names)
            self.station.step_times = [123.0]
            self.station.completed_at = 123.0
        reply = self.station.confirm_result(cycle_id)
        self.assertTrue(reply["success"])
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check", "/result"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")


if __name__ == "__main__":
    unittest.main()
