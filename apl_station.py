"""Durable SN-gated APL station. Network calls never hold the camera lock."""

import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from apl_matching import APLMatcher, StableStateFilter, UNKNOWN
from dmz_client import DMZClient
from inspection_store import InspectionStore
from workflow_view import STEP_LABELS


class APLStation:
    def __init__(self, config_path: Path, data_dir: Path, *, transport=None, matcher=None, local_mode=None, test_mode=False):
        self.config_path = Path(config_path).resolve()
        config_bytes = self.config_path.read_bytes()
        self.config = json.loads(config_bytes)
        self.inspection_mode = str(self.config.get("inspection_mode", "legacy_workflow"))
        self.snapshot_mode = self.inspection_mode == "single_snapshot_cover_card"
        self.target_state = str(self.config.get("target_state", "50_COVER_CLOSED_CARD_PRESENT"))
        self.config_fingerprint = hashlib.sha256(config_bytes).hexdigest()
        template_hash = hashlib.sha256()
        template_root = (self.config_path.parent / self.config.get("template_root", "../templates")).resolve()
        for sample in sorted(template_root.rglob("*")):
            if sample.is_file() and sample.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
                template_hash.update(str(sample.relative_to(template_root)).replace("\\", "/").encode())
                template_hash.update(sample.read_bytes())
        self.template_fingerprint = template_hash.hexdigest()
        self.step_names = [self.target_state] if self.snapshot_mode else list(self.config["steps"])
        expected = ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED", "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"]
        if not self.snapshot_mode and self.step_names != expected:
            raise ValueError("APL requires the five ordered fixture and 50-series steps")
        self.ng_states = set(self.config.get("ng_states", ["30_PRODUCT_LOADED", "30_COVER_CLOSED"]))
        self.removal_state = self.config.get("removal_state", "EMPTY_FIXTURE")
        self.lock = threading.RLock()
        self.frame_lock = threading.Lock()
        self.closed = False
        self.generation = 0
        self.request_token = None
        self.matcher = matcher
        self.matcher_error = ""
        self.storage_error = ""
        self.filter = StableStateFilter(self.config.get("stable_frames", 2))
        self.local_mode = (transport is None and self.config.get('station_mode') == 'local') if local_mode is None else local_mode
        self.test_mode = bool(test_mode)
        self.transport = None if self.local_mode else transport or DMZClient(self.config.get("dmz"))
        self.state = "WAITING_SN"
        self.cycle_id = ""
        self.sn = ""
        self.source = ""
        self.step_index = 0
        self.step_times = []
        self.sn_request_id = ""
        self.report_request_id = ""
        self.last_error = ""
        self.last_result = None
        self.result = None
        self.created_at = None
        self.started_at = None
        self.completed_at = None
        self.cycle_profile = None
        self.cycle_fingerprints = None
        self.cycle_station_id = None
        self.matching = self._empty_matching()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.data_dir / "station.sqlite3"), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cycles (
                cycle_id TEXT PRIMARY KEY, sn TEXT NOT NULL, source TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                sn_request_id TEXT NOT NULL, report_request_id TEXT NOT NULL,
                step_index INTEGER NOT NULL, detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL,
                ts REAL NOT NULL, event TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                request_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, response_json TEXT
            );
        """)
        self.evidence = InspectionStore(self.data_dir / "evidence", self.db)
        self._recover()

    def _empty_matching(self):
        return {"raw": UNKNOWN, "stable": UNKNOWN, "box": None,
                "roi": list(self.config.get("search_roi", [751, 254, 431, 588])),
                "class_scores": {}, "reason": "waiting", "score": None, "gap": None}

    def _recover(self):
        last = self.db.execute("SELECT * FROM cycles WHERE state IN ('COMPLETE','RESET','TERMINATED') ORDER BY updated_at DESC LIMIT 1").fetchone()
        if last:
            detail = json.loads(last["detail_json"])
            self.last_result = detail.get("last_result")
        rows = self.db.execute("SELECT * FROM cycles WHERE state NOT IN ('COMPLETE','RESET','TERMINATED') ORDER BY created_at").fetchall()
        if len(rows) > 1:
            raise RuntimeError("Multiple unresolved station cycles require database recovery")
        if not rows:
            return
        row = rows[0]
        self.cycle_id, self.sn, self.source = row["cycle_id"], row["sn"], row["source"]
        self.sn_request_id, self.report_request_id = row["sn_request_id"], row["report_request_id"]
        self.step_index, self.result, self.created_at = row["step_index"], row["result"], row["created_at"]
        detail = json.loads(row["detail_json"])
        self.step_times = detail.get("step_times", [])
        self.started_at = detail.get("started_at")
        self.completed_at = detail.get("completed_at")
        self.cycle_profile = detail.get("dmz_profile") or {"mode": "unknown", "endpoint_hash": "", "label": "UNKNOWN RECOVERY"}
        self.cycle_fingerprints = detail.get("fingerprints", {})
        self.cycle_station_id = detail.get("station_id", self.config.get("station_id", "NK_APL_50"))
        self.matching = detail.get("matching") or self._empty_matching()
        if self.snapshot_mode and row["state"] in {"WAITING_CAPTURE", "NOT_MATCHED", "PASS_CONFIRM"}:
            self.state = row["state"]
            self.last_error = "Recovered active inspection. The SN remains locked."
        elif self.result == "PASS" and row["state"] in {"RESULT_CONFIRM", "REPORTING"}:
            self.state = "RESULT_CONFIRM"
            self.last_error = "Recovered completed PASS; confirm to save the result." if self.local_mode else "Recovered unacknowledged PASS; confirm to retry the same report ID."
        elif self.result == "NG" and row["state"] == "NG":
            self.state = "NG"
            self.last_error = "Recovered 30-series NG; remove the product to clear the alarm."
        else:
            self.state = "RECOVERY_REQUIRED"
            self.last_error = "Previous cycle was interrupted; reset and scan again."
        self._save("RECOVERED", {"previous_state": row["state"]})

    def _save(self, event, payload=None, *, state_override=None, outbox=None):
        try:
            return self._write_save(event, payload, state_override=state_override, outbox=outbox)
        except Exception:
            self.storage_error = "Station journal could not be committed; restore storage and restart."
            self.last_error = self.storage_error
            self.state = "RECOVERY_REQUIRED"
            self.request_token = None
            self.generation += 1
            raise

    def _write_save(self, event, payload=None, *, state_override=None, outbox=None):
        now = time.time()
        detail = json.dumps({"step_times": self.step_times, "last_error": self.last_error,
                             "last_result": self.last_result, "started_at": self.started_at,
                             "completed_at": self.completed_at, "dmz_profile": self.cycle_profile,
                             "fingerprints": self.cycle_fingerprints, "station_id": self.cycle_station_id,
                             "matching": self.matching}, ensure_ascii=False)
        with self.db:
            self.db.execute("""INSERT INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id) DO UPDATE SET source=excluded.source,
                state=excluded.state,result=excluded.result,
                updated_at=excluded.updated_at,step_index=excluded.step_index,detail_json=excluded.detail_json""",
                (self.cycle_id, self.sn, self.source, state_override or self.state, self.result,
                 self.created_at, now, self.sn_request_id, self.report_request_id, self.step_index, detail))
            self.db.execute("INSERT INTO events(cycle_id,ts,event,payload_json) VALUES (?,?,?,?)",
                            (self.cycle_id, now, event, json.dumps(payload or {}, ensure_ascii=False)))
            if outbox == "pending":
                body = json.dumps(self._payload(result=True), sort_keys=True)
                self.db.execute("""INSERT INTO outbox VALUES (?,?,?,'PENDING',1,?,NULL)
                    ON CONFLICT(request_id) DO UPDATE SET attempts=attempts+1,updated_at=excluded.updated_at""",
                    (self.report_request_id, self.cycle_id, body, now))
            elif isinstance(outbox, dict):
                self.db.execute("UPDATE outbox SET status='ACKED',updated_at=?,response_json=? WHERE request_id=?",
                                (now, json.dumps(outbox), self.report_request_id))

    def _payload(self, result=False):
        body = {"sn": self.sn, "cycle_id": self.cycle_id,
                "request_id": self.report_request_id if result else self.sn_request_id,
                "station_id": str(self.cycle_station_id or self.config.get("station_id", "NK_APL_50"))}
        if result:
            body["result"] = "PASS"
            body["started_at"] = self.started_at
            body["completed_at"] = self.completed_at
            body["steps"] = [{"name": name, "completed_at": stamp} for name, stamp in zip(self.step_names, self.step_times)]
            body["fingerprints"] = copy.deepcopy(self.cycle_fingerprints)
        return body

    def _transport_profile(self):
        if self.test_mode:
            return {'mode': 'local', 'configured': False,
                    'label': 'BROWSER TEST - SIFI DISABLED',
                    'endpoint_hash': 'local-video'}
        if self.local_mode:
            configured = not self.snapshot_mode
            return {'mode': 'local', 'configured': configured,
                    'label': 'SIFI NOT CONFIGURED' if self.snapshot_mode else 'LOCAL',
                    'endpoint_hash': 'local-station'}
        status = self.transport.status() if hasattr(self.transport, "status") else {"mode": "test", "configured": True, "label": "TEST TRANSPORT"}
        endpoint = str(getattr(self.transport, "base_url", "injected-test-transport"))
        return {**status, "endpoint_hash": hashlib.sha256(endpoint.encode()).hexdigest()}

    def _same_transport(self):
        current = self._transport_profile()
        return self.cycle_profile is None or all(current.get(key) == self.cycle_profile.get(key) for key in ("mode", "endpoint_hash"))

    def _clear_matching(self):
        self.generation += 1
        self.filter.reset()
        self.matching = self._empty_matching()

    def _clear_cycle(self):
        self._clear_matching()
        self.state = "WAITING_SN"
        self.cycle_id = self.sn = self.source = ""
        self.sn_request_id = self.report_request_id = ""
        self.step_index = 0
        self.step_times = []
        self.created_at = None
        self.started_at = self.completed_at = None
        self.cycle_profile = self.cycle_fingerprints = None
        self.cycle_station_id = None
        self.result = None
        self.request_token = None

    def _snapshot(self):
        if self.snapshot_mode:
            return self._snapshot_single()
        steps = [{"id": i+1, "step_no": i+1, "name": name, "state": name,
                  "label": STEP_LABELS.get(name, name),
                  "required": True, "status": "DONE" if i < self.step_index else "PENDING",
                  "completed_at": self.step_times[i] if i < len(self.step_times) else None}
                 for i, name in enumerate(self.step_names)]
        alarm_active = self.state in {"NG", "RECOVERY_REQUIRED"}
        complete = self.state in {"RESULT_CONFIRM", "REPORTING"} and self.result == "PASS"
        sop = {"enabled": True, "session_id": self.cycle_id,
               "state": "ALARM" if alarm_active else "COMPLETE" if complete else "RUNNING" if self.state == "RUNNING" else "WAITING",
               "complete": complete, "steps_complete": self.step_index == len(steps),
               "steps": steps, "done_count": self.step_index, "total_required": len(steps),
               "progress_pct": self.step_index / len(steps) * 100,
               "current_step_index": self.step_index if self.step_index < len(steps) else None,
               "current_step_id": self.step_index + 1 if self.state == 'RUNNING' and self.step_index < len(steps) else None,
               "current_step_name": self.step_names[self.step_index] if self.step_index < len(steps) else "",
               "alarm": {"active": alarm_active, "code": "30_SERIES" if self.state == "NG" else "INTERRUPTED" if alarm_active else "",
                         "message": self.last_error if alarm_active else ""}}
        current_dmz = self._transport_profile()
        dmz = dict(self.cycle_profile or current_dmz)
        dmz["configuration_matches_cycle"] = self._same_transport()
        dmz["configured"] = bool(current_dmz.get("configured")) and dmz["configuration_matches_cycle"]
        dmz["configured_mode"] = current_dmz.get("mode")
        return copy.deepcopy({"state": self.state, "cycle_id": self.cycle_id, "sn": self.sn, "source": self.source,
                              "local_mode": self.local_mode, "test_mode": self.test_mode,
                              "step_index": self.step_index, "total_steps": len(steps), "steps": steps,
                              "matching": self.matching, "sop": sop, "dmz": dmz, "last_error": self.last_error,
                              "last_result": self.last_result, "result": self.result,
                              "storage_error": self.storage_error, "started_at": self.started_at,
                              "completed_at": self.completed_at,
                              "request_in_flight": self.request_token is not None,
                              "can_confirm_sn": self.state == "SN_CONFIRM" and self.request_token is None and dmz.get("configured", False),
                              "can_confirm_result": self.state == "RESULT_CONFIRM" and self.request_token is None and dmz["configured"] and not self.storage_error,
                              "can_reset": self.state in {"SN_CONFIRM", "RUNNING", "RECOVERY_REQUIRED"} and self.request_token is None})

    def _snapshot_single(self):
        passed = self.state in {"PASS_CONFIRM", "UPLOADING"} and self.result == "PASS"
        active = bool(self.cycle_id)
        attempt_count = 0
        if active:
            attempt_count = int(self.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE cycle_id=?", (self.cycle_id,)
            ).fetchone()[0])
        step = {"id": 1, "step_no": 1, "name": self.target_state,
                "state": self.target_state, "label": "Closed cover with MCIO card",
                "required": True, "status": "DONE" if passed else "CURRENT" if active else "PENDING",
                "completed_at": self.completed_at if passed else None}
        alarm_active = self.state == "RECOVERY_REQUIRED"
        sop = {"enabled": True, "session_id": self.cycle_id,
               "state": "ALARM" if alarm_active else "COMPLETE" if passed else "RUNNING" if active else "WAITING",
               "complete": passed, "steps_complete": passed, "steps": [step],
               "done_count": 1 if passed else 0, "total_required": 1,
               "progress_pct": 100.0 if passed else 0.0,
               "current_step_index": 0 if active and not passed else None,
               "current_step_id": 1 if active and not passed else None,
               "current_step_name": self.target_state if active and not passed else "",
               "alarm": {"active": alarm_active, "code": "INTERRUPTED" if alarm_active else "",
                         "message": self.last_error if alarm_active else ""}}
        current_transport = self._transport_profile()
        transport = dict(self.cycle_profile or current_transport)
        transport["configuration_matches_cycle"] = self._same_transport()
        transport["configured"] = bool(current_transport.get("configured")) and transport["configuration_matches_cycle"]
        transport["configured_mode"] = current_transport.get("mode")
        can_terminate = self.state in {"WAITING_CAPTURE", "NOT_MATCHED", "PASS_CONFIRM", "RECOVERY_REQUIRED"} and self.request_token is None
        return copy.deepcopy({
            "inspection_mode": self.inspection_mode, "state": self.state,
            "cycle_id": self.cycle_id, "sn": self.sn, "source": self.source,
            "local_mode": self.local_mode, "test_mode": self.test_mode,
            "step_index": 1 if passed else 0, "total_steps": 1, "steps": [step],
            "matching": self.matching, "sop": sop, "dmz": transport,
            "last_error": self.last_error, "last_result": self.last_result,
            "result": self.result, "storage_error": self.storage_error,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "request_in_flight": self.request_token is not None,
            "attempt_count": attempt_count,
            "can_capture": self.state in {"WAITING_CAPTURE", "NOT_MATCHED"} and self.request_token is None and not self.storage_error,
            "can_confirm_sn": False,
            "can_confirm_result": self.state == "PASS_CONFIRM" and self.request_token is None and transport.get("configured", False) and not self.storage_error,
            "can_terminate": can_terminate,
            "can_reset": can_terminate,
        })

    def status(self):
        with self.lock:
            return self._snapshot()

    def set_test_mode(self, enabled, source='browser_video'):
        with self.lock:
            enabled = bool(enabled)
            if self.cycle_id:
                if not enabled or self.state not in {'WAITING_CAPTURE', 'NOT_MATCHED'}:
                    raise ValueError('Test mode can only be enabled before snapshot inspection')
                self.test_mode = True
                self.source = str(source or 'browser_video')[:40]
                self.cycle_profile = self._transport_profile()
                self._save('VIDEO_TEST_ATTACHED', {'source': self.source})
            else:
                if self.state != 'WAITING_SN':
                    raise ValueError('Finish this inspection before changing test mode')
                self.test_mode = enabled
            return self._snapshot()

    def _reply(self, error=None):
        value = {"success": error is None, "station": self._snapshot()}
        if error:
            value["error"] = str(error)
        return value

    def _check_cycle(self, cycle_id):
        if self.closed:
            raise ValueError("Station is closed")
        if self.storage_error:
            raise ValueError(self.storage_error)
        if not cycle_id or cycle_id != self.cycle_id:
            raise ValueError("Cycle ID is stale or missing")

    def scan(self, sn, source="web"):
        if not isinstance(sn, str):
            raise ValueError("SN must be text")
        sn = sn.strip()
        maximum = max(1, int(self.config.get("sn_max_length", 128)))
        if not sn or len(sn) > maximum or any(ord(c) < 33 or ord(c) > 126 for c in sn):
            raise ValueError("SN must contain printable ASCII characters without spaces")
        with self.lock:
            if self.closed:
                return self._reply("Station is closed")
            if self.storage_error:
                return self._reply(self.storage_error)
            if not self.snapshot_mode and self.state == "SN_CONFIRM" and self.sn == sn:
                return self._reply()
            if self.state != "WAITING_SN":
                return self._reply("The current SN is locked. Upload successfully or terminate the inspection before scanning another SN")
            self.cycle_id = uuid.uuid4().hex
            self.sn_request_id, self.report_request_id = uuid.uuid4().hex, uuid.uuid4().hex
            self.sn, self.source = sn, str(source)[:40]
            self.created_at = time.time()
            self.started_at = time.time() if self.snapshot_mode else None
            self.completed_at = None
            self.cycle_profile = self._transport_profile()
            self.cycle_fingerprints = {"config_sha256": self.config_fingerprint, "templates_sha256": self.template_fingerprint}
            self.cycle_station_id = str(self.config.get("station_id", "NK_APL_50"))
            self.state = "WAITING_CAPTURE" if self.snapshot_mode else "SN_CONFIRM"
            self.result = None
            self.step_index, self.step_times = 0, []
            self.last_error = ""
            self._clear_matching()
            self._save("SN_SCANNED_AND_STARTED" if self.snapshot_mode else "SN_SCANNED",
                       {"source": self.source})
            return self._reply()

    def confirm_sn(self, cycle_id):
        if self.snapshot_mode:
            return self._reply("This mode starts automatically after scanning")
        return self._confirm(cycle_id, result=False)

    def confirm_result(self, cycle_id):
        return self._confirm(cycle_id, result=True)

    def _confirm(self, cycle_id, result):
        expected = "PASS_CONFIRM" if result and self.snapshot_mode else "RESULT_CONFIRM" if result else "SN_CONFIRM"
        pending = "UPLOADING" if result and self.snapshot_mode else "REPORTING" if result else "WAITING_DMZ"
        path = "/result" if result else "/sn/check"
        with self.lock:
            self._check_cycle(cycle_id)
            if not self._same_transport():
                return self._reply("DMZ mode or endpoint differs from this cycle; restore its original DMZ configuration")
            if self.state != expected or self.request_token is not None:
                return self._reply("This action is not available in the current state")
            if result and (self.result != "PASS" or self.step_index != len(self.step_names)):
                return self._reply("Only a completed PASS can be reported")
            if result and self.test_mode:
                return self._reply("Browser test data cannot be uploaded to SIFI")
            if result and self.snapshot_mode and self.local_mode:
                return self._reply("SIFI not configured")
            if self.local_mode:
                self.last_error = ''
                if result:
                    self.last_result = {'cycle_id': self.cycle_id, 'sn': self.sn, 'result': 'PASS',
                                        'reported': False, 'saved_at': time.time(), 'mode': 'local',
                                        'test_mode': self.test_mode, 'step_times': list(self.step_times)}
                    self._save('PASS_SAVED_LOCALLY', state_override='COMPLETE')
                    self._clear_cycle()
                else:
                    self.state = 'RUNNING'
                    self.started_at = time.time()
                    self._clear_matching()
                    self._save('SN_CONFIRMED_LOCALLY')
                return self._reply()
            payload = self._payload(result=result)
            profile = dict(self.cycle_profile)
            token = uuid.uuid4().hex
            self.request_token = token
            self.state, self.last_error = pending, ""
            self._save("REPORT_REQUESTED" if result else "SN_CHECK_REQUESTED", outbox="pending" if result else None)
        error, response = None, None
        try:
            response = self.transport.send(path, payload)
            DMZClient.validate_response(path, payload, response)
            if profile.get("mode") == "http" and response.get("mock") is True:
                raise ValueError("Production DMZ mode rejected a mock response")
        except Exception as exc:
            error = str(exc)
        with self.lock:
            if self.closed or self.cycle_id != cycle_id or self.state != pending or self.request_token != token:
                return self._reply("Late DMZ response ignored because the cycle changed")
            self.request_token = None
            if error:
                self.state, self.last_error = expected, error
                self._save("REPORT_RETRY_REQUIRED" if result else "SN_CHECK_FAILED", {"error": error})
                return self._reply(error)
            if result:
                self.last_result = {"cycle_id": self.cycle_id, "sn": self.sn, "result": "PASS", "reported": True,
                                    "reported_at": time.time(), "request_id": self.report_request_id,
                                    "dmz_mode": self._snapshot()["dmz"]["mode"], "step_times": list(self.step_times)}
                self._save("PASS_ACKNOWLEDGED", state_override="COMPLETE", outbox=response)
                self._clear_cycle()
            else:
                self.state = "RUNNING"
                self.started_at = time.time()
                self._clear_matching()
                self._save("SN_ACCEPTED")
            return self._reply()

    def reset(self, cycle_id):
        if self.snapshot_mode:
            return self.terminate(cycle_id)
        with self.lock:
            self._check_cycle(cycle_id)
            if self.request_token is not None:
                return self._reply("A DMZ request is in progress")
            if self.state in {"RESULT_CONFIRM", "REPORTING"} or self.result == "PASS":
                return self._reply("Unacknowledged PASS cannot be discarded; retry result confirmation")
            if self.state == "NG":
                return self._reply("Remove the 30-series product; a stable empty fixture clears NG")
            if self.state not in {"SN_CONFIRM", "RUNNING", "RECOVERY_REQUIRED"}:
                return self._reply("Reset is unavailable in this state")
            self._finish_without_report("OPERATOR_RESET")
            return self._reply()

    def terminate(self, cycle_id):
        with self.lock:
            self._check_cycle(cycle_id)
            if not self.snapshot_mode:
                return self._reply("Terminate Inspection is only available in snapshot mode")
            if self.request_token is not None or self.state in {"JUDGING", "UPLOADING"}:
                return self._reply("Wait for the current operation to finish before terminating")
            if self.state not in {"WAITING_CAPTURE", "NOT_MATCHED", "PASS_CONFIRM", "RECOVERY_REQUIRED"}:
                return self._reply("Terminate Inspection is unavailable in the current state")
            self._finish_without_report("OPERATOR_SELECTED_NO_UPLOAD")
            return self._reply()

    def _finish_without_report(self, reason):
        if self.snapshot_mode:
            self.last_result = {
                "cycle_id": self.cycle_id, "sn": self.sn,
                "visual_result": "OK" if self.result == "PASS" else self.result or "NOT_INSPECTED",
                "cycle_status": "TERMINATED", "upload_status": "NOT_SENT_BY_OPERATOR",
                "reported": False, "reason": reason, "finished_at": time.time(),
            }
            self._save(reason, state_override="TERMINATED")
        else:
            self.last_result = {"cycle_id": self.cycle_id, "sn": self.sn, "result": self.result or "ABORTED",
                                "reported": False, "reason": reason, "finished_at": time.time(), "step_times": list(self.step_times)}
            self._save(reason, state_override="RESET")
        self._clear_cycle()
        self.last_error = ""

    def camera_fault(self, reason):
        with self.lock:
            if self.closed or self.storage_error:
                return self._reply(self.storage_error or "Station is closed")
            self._clear_matching()
            active_states = {"WAITING_CAPTURE", "NOT_MATCHED", "JUDGING"} if self.snapshot_mode else {"RUNNING", "WAITING_DMZ"}
            if self.state in active_states:
                self.state, self.result = "RECOVERY_REQUIRED", "INTERRUPTED"
                self.request_token = None
                self.last_error = str(reason)[:500]
                self._save("CAMERA_INTERRUPTED", {"reason": self.last_error})
            return self._reply()

    def _apply_match_locked(self, match):
        self.matcher_error = ""
        raw = str(match.get("raw", UNKNOWN))
        stable = self.filter.update(raw)
        self.matching = {**match, "stable": stable}
        if self.state == "NG":
            if stable == self.removal_state:
                self._finish_without_report("30_REMOVED_EMPTY_FIXTURE")
        elif self.state == "RUNNING":
            if stable in self.ng_states:
                self.state, self.result = "NG", "NG"
                self.last_error = "30-series product detected: " + stable
                self._save("30_SERIES_NG", {"matched_state": stable})
            elif self.step_index < len(self.step_names) and stable == self.step_names[self.step_index]:
                self.step_times.append(time.time())
                self.step_index += 1
                if self.step_index == len(self.step_names):
                    self.state, self.result = "RESULT_CONFIRM", "PASS"
                    self.completed_at = self.step_times[-1]
                self._save("STEP_COMPLETED", {"step": stable, "step_index": self.step_index})

    def _frame_result(self, frame, snapshot):
        vis = self.matcher.draw(frame, snapshot) if hasattr(self.matcher, "draw") else frame
        accepted = snapshot["matching"]["raw"]
        results = [{"label": name, "score": score, "pass": name == accepted,
                    "matched": name == accepted}
                   for name, score in snapshot["matching"].get("class_scores", {}).items()]
        return vis, snapshot["sop"]["complete"], results, [], snapshot["sop"]

    def process_frame(self, frame):
        with self.lock:
            token = self.generation
        with self.frame_lock:
            with self.lock:
                if self.closed:
                    return frame, False, [], [], self._snapshot()["sop"]
            try:
                if self.matcher is None:
                    self.matcher = APLMatcher(self.config, self.config_path)
                if self.snapshot_mode:
                    with self.lock:
                        snapshot = self._snapshot()
                    return self._frame_result(frame, snapshot)
                match = self.matcher.evaluate(frame)
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
                    self.matcher_error = str(exc)
                self.camera_fault("Template matching unavailable: " + str(exc))
                return frame, False, [], [], self.status()["sop"]
            with self.lock:
                if token == self.generation and not self.closed:
                    self._apply_match_locked(match)
                snapshot = self._snapshot()
            return self._frame_result(frame, snapshot)

    def process_matches(self, frame, matches):
        """Apply precomputed video matches in order, then draw the current station state."""
        if self.snapshot_mode:
            return self.process_frame(frame)
        with self.lock:
            token = self.generation
        with self.frame_lock:
            with self.lock:
                if self.closed:
                    return frame, False, [], [], self._snapshot()["sop"]
                for match in matches:
                    if token != self.generation:
                        break
                    self._apply_match_locked(match)
                snapshot = self._snapshot()
            return self._frame_result(frame, snapshot)

    def inspect_snapshot(self, frame, source=None, quality=92):
        with self.lock:
            self._check_cycle(self.cycle_id)
            if not self.snapshot_mode:
                return self._reply("Capture & Inspect is only available in snapshot mode")
            if self.state not in {"WAITING_CAPTURE", "NOT_MATCHED"} or self.request_token is not None:
                return self._reply("Capture & Inspect is unavailable in the current state")
            token = self.generation
            cycle_id, sn = self.cycle_id, self.sn
            self.state, self.last_error = "JUDGING", ""
            self._save("INSPECTION_REQUESTED", {"source": dict(source or {})})
        with self.frame_lock:
            try:
                if self.matcher is None:
                    self.matcher = APLMatcher(self.config, self.config_path)
                match = self.matcher.evaluate_snapshot(frame)
            except Exception as exc:
                with self.lock:
                    if self.cycle_id == cycle_id:
                        self.state = "RECOVERY_REQUIRED"
                        self.last_error = "Template matching unavailable: " + str(exc)
                        self._save("INSPECTION_FAILED", {"error": self.last_error})
                    return self._reply(self.last_error)
            with self.lock:
                if self.closed or token != self.generation or self.cycle_id != cycle_id or self.state != "JUDGING":
                    return self._reply("Inspection result was discarded because the cycle changed")
                accepted = bool(match.get("accepted"))
                self.matching = match
                self.result = "PASS" if accepted else "NOT_MATCHED"
                self.state = "PASS_CONFIRM" if accepted else "NOT_MATCHED"
                self.step_index = 1 if accepted else 0
                self.completed_at = time.time() if accepted else None
                self.step_times = [self.completed_at] if accepted else []
                snapshot = self._snapshot()
                result_frame = self.matcher.draw(frame, snapshot)
                try:
                    attempt = self.evidence.save_attempt(
                        cycle_id=cycle_id, sn=sn,
                        visual_result="OK" if accepted else "NOT_MATCHED",
                        raw_frame=frame, result_frame=result_frame, matching=match,
                        source=source, quality=quality,
                    )
                    self._save("INSPECTION_OK" if accepted else "INSPECTION_NOT_MATCHED",
                               {"attempt_id": attempt["attempt_id"], "reason": match.get("reason")})
                except Exception as exc:
                    self.state, self.result = "RECOVERY_REQUIRED", "INTERRUPTED"
                    self.last_error = "Inspection evidence could not be saved: " + str(exc)
                    self._save("EVIDENCE_SAVE_FAILED", {"error": self.last_error})
                    return self._reply(self.last_error)
                reply = self._reply()
                reply["attempt"] = attempt
                return reply

    def list_history(self, **filters):
        with self.lock:
            return self.evidence.list_cycles(**filters)

    def history_detail(self, cycle_id):
        with self.lock:
            return self.evidence.cycle_detail(str(cycle_id))

    def resolve_evidence(self, relative_path):
        return self.evidence.resolve(relative_path)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.generation += 1
                self.request_token = None
                self.db.close()
