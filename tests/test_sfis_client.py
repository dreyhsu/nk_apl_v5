import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
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
            "step4_enabled": True,
            "line": "TEST", "station": "AI-CAM", "employee_no": "M031978",
        })
        self.payload = {"sn": "M0001000P153B52D01S", "cycle_id": "cycle-1", "request_id": "request-1",
                        "mcio_sn": "APL0012345"}

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

    def test_step1_accepts_real_ok_response(self):
        result, _ = self.send_with("/sn/check", {
            "success": True, "data": "OK1,REWORK=0 RETEST=1A", "message": "", "errorCode": None,
        })
        self.assertTrue(result["accepted"])

    def test_step1_rejects_wrong_sn_fail_response(self):
        with self.assertRaisesRegex(DMZError, "FAIL1,PASM1"):
            self.send_with("/sn/check", {
                "success": True, "data": "FAIL1,PASM1 ", "message": "", "errorCode": None,
            })

    def configured_step1_exceptions(self):
        config_path = Path(__file__).resolve().parents[1] / "config" / "apl_workflow.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))["dmz"]
        self.client = DMZClient(config)
        return config["step1_allowed_fail_responses"]

    def test_step1_accepts_configured_fail_responses(self):
        allowed = self.configured_step1_exceptions()
        self.assertEqual(allowed, [
            "FAIL1,Retest Rule(1A/2A/3A/4A) not found in SFIS!",
            'FAIL1,"PT" was not completed (Type=1A)',
        ])
        for data in allowed:
            with self.subTest(data=data):
                response = {"success": True, "data": " " + data + " \r\n", "errorCode": None}
                result, _ = self.send_with("/sn/check", response)
                self.assertTrue(result["accepted"])
                self.assertEqual(result["sfis_response"], response)

    def test_step1_exceptions_are_disabled_by_default(self):
        default_client = self.client
        for data in self.configured_step1_exceptions():
            with self.subTest(data=data):
                self.client = default_client
                with self.assertRaisesRegex(DMZError, "business validation failed"):
                    self.send_with("/sn/check", {"success": True, "data": data})

    def test_step1_exceptions_do_not_allow_other_failures(self):
        self.configured_step1_exceptions()
        for data in ['FAIL1,"PT" was not completed (Type=2A)', "FAIL1,PASM1", "FAIL1,HS_ASSY"]:
            with self.subTest(data=data), self.assertRaisesRegex(DMZError, "business validation failed"):
                self.send_with("/sn/check", {"success": True, "data": data})

    def test_step1_exceptions_do_not_override_api_errors(self):
        for data in self.configured_step1_exceptions():
            for success, error_code in [(False, None), (True, "E01")]:
                with self.subTest(data=data, success=success, error_code=error_code):
                    with self.assertRaisesRegex(DMZError, "SFIS rejected request"):
                        self.send_with("/sn/check", {
                            "success": success, "data": data, "errorCode": error_code,
                        })

    def test_step1_exceptions_never_apply_to_step4(self):
        for data in self.configured_step1_exceptions():
            with self.subTest(data=data), self.assertRaisesRegex(DMZError, "business validation failed"):
                self.send_with("/result", {"success": True, "data": data},
                               {**self.payload, "result": "PASS"})

    def test_step1_exceptions_require_a_list_of_fail_strings(self):
        for invalid in [None, "FAIL1,error", [None], [""], ["OK"]]:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "step1_allowed_fail_responses"):
                DMZClient({"step1_allowed_fail_responses": invalid})

    def test_step4_uses_same_confirmed_fields_and_normalizes_ack(self):
        payload = {**self.payload, "result": "PASS"}
        result, request = self.send_with("/result", {
            "success": True, "data": "OK", "message": "", "errorCode": None,
        }, payload)
        self.assertEqual(request.full_url, "http://10.0.28.125:8097/api/SFISAPI/Step4")
        body = json.loads(request.data)
        self.assertEqual(body["sn"], payload["sn"])
        # Step4 uses the curl casing: EmpNo (not empNo) and mciosn.
        self.assertEqual(body["EmpNo"], "M031978")
        self.assertNotIn("empNo", body)
        self.assertEqual(result["result"], "PASS")

    def test_step4_carries_the_mcio_card_serial_number(self):
        payload = {**self.payload, "result": "PASS"}
        _, request = self.send_with("/result", {
            "success": True, "data": "OK", "message": "", "errorCode": None,
        }, payload)
        self.assertEqual(json.loads(request.data)["mciosn"], "APL0012345")

    def test_step1_body_is_unchanged_by_the_mcio_card_serial_number(self):
        _, request = self.send_with("/sn/check", {
            "success": True, "data": "PASS", "message": "", "errorCode": None,
        })
        self.assertEqual(json.loads(request.data), {
            "line": "TEST", "station": "AI-CAM",
            "sn": "M0001000P153B52D01S", "empNo": "M031978",
        })

    def test_step4_without_an_mcio_card_serial_number_is_refused(self):
        payload = {**self.payload, "result": "PASS", "mcio_sn": ""}
        with patch.object(self.client.opener, "open") as opened:
            with self.assertRaisesRegex(DMZError, "MCIO card serial number is missing"):
                self.client.send("/result", payload)
        opened.assert_not_called()

    def test_step4_extra_fields_cannot_overwrite_the_mcio_card_serial_number(self):
        client = DMZClient({
            "mode": "http", "api_style": "sfis",
            "base_url": "http://10.0.28.125:8097/api/SFISAPI",
            "allow_insecure_http": True, "step4_enabled": True,
            "line": "TEST", "station": "AI-CAM", "employee_no": "M031978",
            "step4_extra_fields": {"mciosn": "SPOOFED"},
        })
        payload = {**self.payload, "result": "PASS"}
        with patch.object(client.opener, "open", return_value=FakeHTTPResponse({
            "success": True, "data": "OK", "message": "", "errorCode": None,
        })) as opened:
            client.send("/result", payload)
        self.assertEqual(json.loads(opened.call_args.args[0].data)["mciosn"], "APL0012345")

    def test_mcio_field_name_cannot_collide_with_a_documented_field(self):
        with self.assertRaisesRegex(ValueError, "step4_mcio_field"):
            DMZClient({"mode": "http", "api_style": "sfis", "step4_mcio_field": "sn"})

    def test_step4_is_blocked_without_explicit_test_switch(self):
        client = DMZClient({
            "mode": "http", "api_style": "sfis",
            "base_url": "http://10.0.28.125:8097/api/SFISAPI",
            "allow_insecure_http": True,
            "line": "TEST", "station": "AI-CAM", "employee_no": "M031978",
        })
        payload = {**self.payload, "result": "PASS"}
        with patch.object(client.opener, "open") as opened:
            with self.assertRaisesRegex(DMZError, "Step4 is disabled"):
                client.send("/result", payload)
        opened.assert_not_called()


class DMZBypassTests(unittest.TestCase):
    """The bypass switch answers Step1/Step4 locally without touching dmz.mode."""

    def client(self, **overrides):
        config = {
            "mode": "http", "api_style": "sfis",
            "base_url": "http://10.0.28.125:8097/api/SFISAPI",
            "allow_insecure_http": True,
            "line": "TEST", "station": "AI-CAM", "employee_no": "M031978",
        }
        config.update(overrides)
        return DMZClient(config)

    def setUp(self):
        self.payload = {"sn": "SN-BYPASS", "cycle_id": "cycle-1", "request_id": "request-1"}

    def test_status_reports_configured_bypass_without_hiding_the_real_mode(self):
        status = self.client(bypass_sfis=True).status()
        self.assertEqual(status["mode"], "http+bypass")
        self.assertTrue(status["configured"])
        self.assertTrue(status["step4_enabled"])
        self.assertTrue(status["bypass"])
        self.assertIn("BYPASS", status["label"])

    def test_both_steps_return_accepted_without_any_request(self):
        client = self.client(bypass_sfis=True, step4_enabled=False)
        step4_payload = {**self.payload, "result": "PASS"}
        with patch.object(client.opener, "open") as opened:
            step1 = client.send("/sn/check", self.payload)
            step4 = client.send("/result", step4_payload)
        opened.assert_not_called()
        DMZClient.validate_response("/sn/check", self.payload, step1)
        DMZClient.validate_response("/result", step4_payload, step4)
        self.assertTrue(step1["bypass"])
        self.assertEqual(step4["result"], "PASS")

    def test_bypass_can_be_forced_from_the_environment(self):
        with patch.dict("os.environ", {"APL_DMZ_BYPASS": "on"}):
            self.assertTrue(self.client().bypass)
        with patch.dict("os.environ", {"APL_DMZ_BYPASS": "off"}):
            self.assertFalse(self.client(bypass_sfis=True).bypass)

    def test_bypass_rejects_ambiguous_configuration(self):
        with self.assertRaisesRegex(ValueError, "dmz.bypass_sfis"):
            self.client(bypass_sfis="false")
        with patch.dict("os.environ", {"APL_DMZ_BYPASS": "maybe"}):
            with self.assertRaisesRegex(ValueError, "APL_DMZ_BYPASS"):
                self.client()

    def test_switch_off_keeps_the_existing_dmz_mode_behaviour(self):
        disabled = DMZClient({"mode": "disabled"})
        self.assertFalse(disabled.bypass)
        self.assertFalse(disabled.status()["configured"])
        with self.assertRaisesRegex(DMZError, "DMZ is disabled"):
            disabled.send("/sn/check", self.payload)
        live = self.client(step4_enabled=True)
        self.assertEqual(live.status()["mode"], "http")
        with patch.object(live.opener, "open",
                          return_value=FakeHTTPResponse({"success": True, "data": "PASS",
                                                         "message": "", "errorCode": None})) as opened:
            self.assertTrue(live.send("/sn/check", self.payload)["accepted"])
        opened.assert_called_once()


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
            "sn_prefix": "K2V5", "mcio_sn_prefix": "APL",
        }), encoding="utf-8")
        self.transport = GateTransport()
        self.station = APLStation(self.config, root / "data", transport=self.transport, matcher=object())
        self.station.set_operator("M031978")

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def start_cycle(self, sn="K2V5-001", mcio="APL-001"):
        """Step1 runs on the product scan; the MCIO scan then opens capture."""
        self.station.scan(sn)
        return self.station.scan(mcio)

    def test_product_scan_runs_step1_immediately(self):
        reply = self.station.scan("K2V5-001")
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_MCIO_SN")
        self.assertEqual(reply["station"]["mcio_sn"], "")
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])

    def test_mcio_scan_runs_step1_before_enabling_capture(self):
        reply = self.start_cycle()
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_CAPTURE")
        self.assertEqual(reply["station"]["mcio_sn"], "APL-001")
        self.assertEqual([call[0] for call in self.transport.calls], ["/sn/check"])

    def test_scan_blocked_without_operator(self):
        self.station.clear_operator()
        reply = self.station.scan("K2V5-001")
        self.assertFalse(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(self.transport.calls, [])

    def test_step1_failure_keeps_the_product_sn_for_a_rescan(self):
        self.transport.fail = True
        reply = self.station.scan("K2V5-001")
        self.assertFalse(reply["success"])
        self.assertEqual(reply["station"]["state"], "SN_CONFIRM")
        self.assertEqual(reply["station"]["sn"], "K2V5-001")
        self.assertEqual(reply["station"]["mcio_sn"], "")
        self.assertTrue(reply["station"]["step1_retry_required"])

    def test_no_emp_rejection_keeps_the_operator(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1, NO EMP"
        reply = self.station.scan("K2V5-001")
        self.assertFalse(reply["success"])
        self.assertEqual(self.station.operator, "M031978")
        self.assertEqual(reply["station"]["operator"], "M031978")
        self.assertEqual(reply["station"]["sn"], "K2V5-001")

    def test_other_business_failure_keeps_operator_signed_in(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,HS_ASSY"
        reply = self.station.scan("K2V5-001")
        self.assertFalse(reply["success"])
        self.assertEqual(self.station.operator, "M031978")
        self.assertEqual(reply["station"]["operator"], "M031978")
        self.assertEqual(reply["station"]["sn"], "K2V5-001")
        self.assertEqual(reply["station"]["state"], "SN_CONFIRM")

    def test_step4_payload_carries_both_serial_numbers(self):
        self.start_cycle()
        payload = self.station._payload(result=True)
        self.assertEqual(payload["sn"], "K2V5-001")
        self.assertEqual(payload["mcio_sn"], "APL-001")
        self.assertNotIn("mcio_sn", self.station._payload(result=False))

    def test_completed_ok_runs_step4_and_unlocks(self):
        scanned = self.start_cycle(mcio="APL-OK")
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
        self.assertEqual(self.transport.calls[-1][1]["mcio_sn"], "APL-OK")
        self.assertEqual(reply["station"]["state"], "WAITING_SN")


class SnapshotBypassCycleTests(unittest.TestCase):
    """A station with no DMZ link can run a full SN cycle when bypass is on."""

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
            "template_root": "templates",
            "dmz": {"mode": "http", "api_style": "sfis", "bypass_sfis": True,
                    "base_url": "http://10.0.28.125:8097/api/SFISAPI",
                    "allow_insecure_http": True, "step4_enabled": False,
                    "line": "TEST", "station": "AI-CAM"},
        }), encoding="utf-8")
        # No injected transport: the station builds a real DMZClient.
        self.station = APLStation(self.config, root / "data", matcher=object())
        self.station.set_operator("M031978")

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def test_step1_and_step4_pass_through_without_network(self):
        status = self.station.status()
        self.assertTrue(status["dmz"]["bypass"])
        self.assertTrue(status["dmz"]["configured"])
        with patch.object(self.station.transport.opener, "open") as opened:
            self.station.scan("SN-BYPASS")
            scanned = self.station.scan("MCIO-BYPASS")
            self.assertTrue(scanned["success"])
            self.assertEqual(scanned["station"]["state"], "WAITING_CAPTURE")
            cycle_id = scanned["station"]["cycle_id"]
            with self.station.lock:
                self.station.state = "PASS_CONFIRM"
                self.station.result = "PASS"
                self.station.step_index = len(self.station.step_names)
                self.station.step_times = [123.0]
                self.station.completed_at = 123.0
            reply = self.station.confirm_result(cycle_id)
        opened.assert_not_called()
        self.assertTrue(reply["success"])
        self.assertEqual(reply["station"]["state"], "WAITING_SN")
        self.assertEqual(reply["station"]["last_result"]["dmz_mode"], "http+bypass")


class SFISTraceTests(SnapshotSFISGateTests):
    """The dashboard console reads sfis_log; a rolled-back cycle must explain itself."""

    def phases(self):
        return [(item["step"], item["phase"]) for item in self.station.sfis_log()]

    def test_accepted_step1_is_traced_with_the_product_sn_and_empno(self):
        self.start_cycle()
        self.assertEqual(self.phases(), [("step1", "begin"), ("step1", "accepted")])
        entry = self.station.sfis_log()[0]
        # Step1 runs on the product SN, before the MCIO card exists.
        self.assertEqual((entry["sn"], entry["mcio_sn"], entry["emp_no"]),
                         ("K2V5-001", "", "M031978"))

    def test_rejected_step1_traces_the_retry_requirement_and_the_reason(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1,HS_ASSY"
        self.station.scan("K2V5-001")
        self.assertEqual(self.phases(),
                         [("step1", "begin"), ("step1", "failed"), ("step1", "retry_required")])
        retry = self.station.sfis_log()[-1]
        self.assertIn("HS_ASSY", retry["detail"])
        self.assertFalse(retry["operator_error"])
        self.assertEqual((retry["sn"], retry["mcio_sn"]), ("K2V5-001", ""))

    def test_operator_rejection_is_marked_in_the_trace(self):
        self.transport.fail_message = "SFIS business validation failed: FAIL1, NO EMP"
        self.station.scan("K2V5-001")
        self.assertTrue(self.station.sfis_log()[-1]["operator_error"])

    def test_log_cursor_only_returns_new_entries(self):
        self.start_cycle()
        latest = self.station.sfis_log()[-1]["seq"]
        self.assertEqual(self.station.sfis_log(latest), [])


class SFISWireTraceTests(SFISClientTests):
    def test_both_steps_print_success_and_rejected_api_bodies(self):
        for path, step in [("/sn/check", "STEP1"), ("/result", "STEP4")]:
            for data in ["PASS", "FAIL1,PASM1"]:
                with self.subTest(step=step, data=data):
                    seen = []
                    self.client.on_trace = seen.append
                    body = {"success": True, "data": data, "message": "", "errorCode": None}
                    output = io.StringIO()
                    with redirect_stdout(output):
                        if data.startswith("FAIL"):
                            with self.assertRaises(DMZError):
                                self.send_with(path, body, {**self.payload, "result": "PASS"})
                        else:
                            self.send_with(path, body, {**self.payload, "result": "PASS"})
                    self.assertEqual(seen[-1]["raw_response"], body)
                    self.assertIn(f"[SFIS {step}]", output.getvalue())
                    self.assertIn(json.dumps(body), output.getvalue())

    def test_terminal_response_is_printed_without_a_browser_hook(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.send_with("/sn/check", {"success": True, "data": "PASS"})
        self.assertIn('[SFIS STEP1] response', output.getvalue())
        self.assertIn('"data": "PASS"', output.getvalue())

    def test_http_error_body_is_kept_in_the_trace_and_console(self):
        seen = []
        self.client.on_trace = seen.append
        error = urllib.error.HTTPError(self.client.base_url, 500, "Server error", {},
                                      io.BytesIO(b'{"message":"SFIS unavailable"}'))
        output = io.StringIO()
        with patch.object(self.client.opener, "open", side_effect=error), redirect_stdout(output):
            with self.assertRaisesRegex(DMZError, "HTTPError 500"):
                self.client.send("/sn/check", self.payload)
        self.assertEqual(seen[-1]["phase"], "error")
        self.assertIn("SFIS unavailable", seen[-1]["raw_response"])
        self.assertIn("SFIS unavailable", output.getvalue())

    def test_the_trace_carries_the_real_url_body_and_answer(self):
        seen = []
        self.client.on_trace = seen.append
        self.send_with("/sn/check", {"success": True, "data": "OK1,REWORK=0",
                                     "message": "", "errorCode": None})
        self.assertEqual([item["phase"] for item in seen], ["request", "response"])
        self.assertEqual(seen[0]["url"], "http://10.0.28.125:8097/api/SFISAPI/Step1")
        self.assertEqual(seen[0]["request_body"]["empNo"], "M031978")
        self.assertEqual(seen[1]["response"]["sfis_response"]["data"], "OK1,REWORK=0")

    def test_a_business_failure_is_traced_as_an_error(self):
        seen = []
        self.client.on_trace = seen.append
        with self.assertRaises(DMZError):
            self.send_with("/sn/check", {"success": True, "data": "FAIL1,PASM1 ",
                                         "message": "", "errorCode": None})
        self.assertEqual(seen[-1]["phase"], "error")
        self.assertIn("FAIL1,PASM1", seen[-1]["detail"])

    def test_a_failing_trace_hook_never_breaks_a_request(self):
        def explode(_event):
            raise RuntimeError("diagnostics must not be load-bearing")
        self.client.on_trace = explode
        result, _ = self.send_with("/sn/check", {"success": True, "data": "PASS",
                                                 "message": "", "errorCode": None})
        self.assertTrue(result["accepted"])


if __name__ == "__main__":
    unittest.main()
