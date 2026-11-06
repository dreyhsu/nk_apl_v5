"""Durable SN-gated APL station. Network calls never hold the camera lock."""

import collections
import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from apl_matching import APLMatcher, StableStateFilter, UNKNOWN
from dmz_client import DMZClient, bypass_enabled
from inspection_store import InspectionStore
from light_tower import validated_light_tower
from workflow_view import STEP_LABELS


class APLStation:
    def __init__(self, config_path: Path, data_dir: Path, *, transport=None, matcher=None, local_mode=None, test_mode=False):
        self.config_path = Path(config_path).resolve()
        config_bytes = self.config_path.read_bytes()
        self.config = json.loads(config_bytes)
        dmz_config = self.config.get("dmz") or {}
        if "sfis_enabled" in dmz_config:
            raise ValueError("dmz.sfis_enabled is obsolete; explicitly migrate to dmz.bypass_sfis")
        if not isinstance(dmz_config.get("bypass_sfis", False), bool):
            raise ValueError("dmz.bypass_sfis must be a boolean (true/false)")
        # Bypass means nothing ever reaches SFIS, so it also decides whether a
        # transport-less station (browser video test) may close its own cycle.
        self.bypass_sfis = bypass_enabled(dmz_config)
        self.inspection_mode = str(self.config.get("inspection_mode", "legacy_workflow"))
        self.snapshot_mode = self.inspection_mode == "single_snapshot_cover_card"
        # Label prefixes let the station tell the right label at the wrong step
        # apart from a scanner misread, for both the HID and the browser path.
        self.sn_prefix = self._validated_prefix("sn_prefix", "sn_max_length")
        self.mcio_sn_prefix = self._validated_prefix("mcio_sn_prefix", "mcio_sn_max_length")
        # Exact label lengths (0 = any length); snapshot mode only, like the prefixes.
        self.sn_length = self._validated_length("sn_length", "sn_max_length")
        self.mcio_sn_length = self._validated_length("mcio_sn_length", "mcio_sn_max_length")
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
        self.auto_capture = self._validated_auto_capture()
        # Driven by the camera runtime; validated here so a bad block fails at startup.
        self.light_tower = validated_light_tower(self.config.get("light_tower"))
        self.local_mode = (transport is None and self.config.get('station_mode') == 'local') if local_mode is None else local_mode
        self.test_mode = bool(test_mode)
        self.transport = None if self.local_mode else transport or DMZClient(dmz_config)
        # Operator-visible SFIS diagnostics. The dashboard reads this ring over
        # /api/apl/sfis-log and prints it in the browser console, so Step1 and
        # Step4 can be debugged from F12 without a shell on the station.
        self.sfis_lock = threading.Lock()
        self.sfis_events = collections.deque(maxlen=60)
        self.sfis_seq = 0
        if self.transport is not None and hasattr(self.transport, "on_trace"):
            self.transport.on_trace = self._record_sfis
        self.state = "WAITING_SN"
        self.cycle_id = ""
        self.sn = ""
        self.mcio_sn = ""
        self.operator = ""
        self.cycle_operator = None
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
        # Live probe scores and countdown for the overlay only. Never journaled,
        # never saved as evidence, never part of _snapshot() - see
        # set_preview_matching.
        self.preview_matching = None
        self.preview_auto = None
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

    def _validated_prefix(self, key, max_key, default_max=128):
        value = self.config.get(key, "")
        if not isinstance(value, str):
            raise ValueError(key + " must be text")
        value = value.strip()
        if not value:
            return ""
        if any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError(key + " must contain printable ASCII characters without spaces")
        if len(value) >= max(1, int(self.config.get(max_key, default_max))):
            raise ValueError(key + " must be shorter than " + max_key)
        return value

    def _validated_length(self, key, max_key, default_max=128):
        value = self.config.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(key + " must be a whole number (0 = any length)")
        if not 0 <= value <= max(1, int(self.config.get(max_key, default_max))):
            raise ValueError(key + " must be between 0 and " + max_key)
        return value

    def _validated_auto_capture(self):
        """Continuous template matching settings. Everything defaults to off so a
        config without the block keeps the operator-triggered Capture & Inspect."""
        raw = self.config.get("auto_capture") or {}
        if not isinstance(raw, dict):
            raise ValueError("auto_capture must be an object")
        unknown = set(raw) - {"enabled", "fps", "required_hits", "timeout_sec", "timeout_action"}
        if unknown:
            raise ValueError("auto_capture has unsupported keys: " + ", ".join(sorted(unknown)))
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("auto_capture.enabled must be a boolean (true/false)")
        # A legacy-workflow station is not refused here: the same config file is
        # reused with inspection_mode overridden, and the probe's own arming check
        # reports 'not_snapshot_mode' before it does any matching work.
        fps = self._validated_number("auto_capture.fps", raw.get("fps", 5.0), 0.1, 30.0)
        hits = raw.get("required_hits", self.config.get("stable_frames", 2))
        if isinstance(hits, bool) or not isinstance(hits, int):
            raise ValueError("auto_capture.required_hits must be a whole number")
        if not 1 <= hits <= 30:
            raise ValueError("auto_capture.required_hits must be between 1 and 30")
        timeout = raw.get("timeout_sec", 0.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("auto_capture.timeout_sec must be a number")
        timeout = float(timeout)
        # Zero means the probe never gives up on its own.
        if timeout != 0.0 and not 1.0 <= timeout <= 600.0:
            raise ValueError("auto_capture.timeout_sec must be 0 or between 1 and 600 seconds")
        action = raw.get("timeout_action", "not_matched")
        if action not in {"not_matched", "pass"}:
            raise ValueError("auto_capture.timeout_action must be 'not_matched' or 'pass'")
        if action == "pass" and timeout == 0.0:
            raise ValueError("auto_capture.timeout_action 'pass' needs a non-zero timeout_sec")
        return {"enabled": enabled, "fps": fps, "required_hits": hits,
                "timeout_sec": timeout, "timeout_action": action}

    @staticmethod
    def _validated_number(key, value, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(key + " must be a number")
        value = float(value)
        if not low <= value <= high:
            raise ValueError("%s must be between %g and %g" % (key, low, high))
        return value

    def set_preview_matching(self, match, auto=None):
        """Publish one live probe result for the overlay, with the automatic-capture
        countdown that goes beside it. Both reach draw() through _frame_result only,
        so self.matching - which feeds the journal detail, the evidence JSON and
        attempts.matching_json - is never touched."""
        with self.lock:
            self.preview_matching = dict(match) if match else None
            self.preview_auto = dict(auto) if auto else None

    def _serial_prefix(self, which):
        # Prefix rules are a snapshot-mode operator aid; legacy serials stay free-form.
        if not self.snapshot_mode:
            return ""
        return self.sn_prefix if which == "sn" else self.mcio_sn_prefix

    def _serial_length(self, which):
        if not self.snapshot_mode:
            return 0
        return self.sn_length if which == "sn" else self.mcio_sn_length

    def _wrong_length_error(self, field, length, instruction, value):
        return ("Wrong label: the " + field + " must be " + str(length) + " characters. "
                + instruction + ". Scanned: " + str(value)[:32] + " (" + str(len(value)) + " chars)")

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
        self.cycle_operator = detail.get("cycle_operator")
        self.mcio_sn = detail.get("mcio_sn", "")
        if self.cycle_operator and not self.operator:
            self.operator = self.cycle_operator
        self.matching = detail.get("matching") or self._empty_matching()
        if self.snapshot_mode and row["state"] in {"SN_CONFIRM", "WAITING_DMZ"}:
            # Step1 was either never answered or was refused: the product SN is
            # valid and nothing was captured, so reopen as the sticky retry
            # state. Restoring last_error is what makes the retry discriminator
            # (and therefore can_terminate / active_scan_field) correct at boot.
            self.state = "SN_CONFIRM"
            self.last_error = ("Recovered a product SN whose SFIS Step1 did not complete."
                               " Scan the same product serial number again to retry Step1,"
                               " or terminate this inspection.")
        elif self.snapshot_mode and row["state"] == "WAITING_MCIO_SN":
            self.state = row["state"]
            self.last_error = "Recovered a product SN waiting for the MCIO card serial number. Scan the MCIO card serial number or terminate this inspection."
        elif self.snapshot_mode and row["state"] in {"WAITING_CAPTURE", "NOT_MATCHED", "PASS_CONFIRM"}:
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
                             "cycle_operator": self.cycle_operator, "matching": self.matching,
                             "mcio_sn": self.mcio_sn}, ensure_ascii=False)
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
                "station_id": str(self.cycle_station_id or self.config.get("station_id", "NK_APL_50")),
                "emp_no": self.cycle_operator or ""}
        if result:
            body["mcio_sn"] = self.mcio_sn
            body["result"] = "PASS"
            body["started_at"] = self.started_at
            body["completed_at"] = self.completed_at
            body["steps"] = [{"name": name, "completed_at": stamp} for name, stamp in zip(self.step_names, self.step_times)]
            body["fingerprints"] = copy.deepcopy(self.cycle_fingerprints)
        return body

    def _transport_profile(self):
        if self.test_mode:
            if self.bypass_sfis:
                # Nothing is sent in either case, so blocking the close here would
                # only stop the operator rehearsing the full loop on a PC.
                return {'mode': 'local+bypass', 'configured': True,
                        'step4_enabled': True, 'bypass': True,
                        'label': 'BROWSER TEST - SFIS BYPASS',
                        'endpoint_hash': 'local-video'}
            return {'mode': 'local', 'configured': False,
                    'label': 'BROWSER TEST - SFIS DISABLED',
                    'endpoint_hash': 'local-video'}
        if self.local_mode:
            configured = not self.snapshot_mode
            return {'mode': 'local', 'configured': configured,
                    'label': 'SFIS NOT CONFIGURED' if self.snapshot_mode else 'LOCAL',
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
        self.mcio_sn = ""
        self.cycle_operator = None
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
                              "operator": self.operator, "cycle_operator": self.cycle_operator,
                              "operator_required": self._operator_required(),
                              "local_mode": self.local_mode, "test_mode": self.test_mode,
                              "step_index": self.step_index, "total_steps": len(steps), "steps": steps,
                              "matching": self.matching, "sop": sop, "dmz": dmz, "last_error": self.last_error,
                              "last_result": self.last_result, "result": self.result,
                              "storage_error": self.storage_error, "started_at": self.started_at,
                              "completed_at": self.completed_at,
                              "request_in_flight": self.request_token is not None,
                              "can_confirm_sn": self.state == "SN_CONFIRM" and self.request_token is None and dmz.get("configured", False),
                              "can_confirm_result": self.state == "RESULT_CONFIRM" and self.request_token is None and dmz["configured"] and dmz.get("step4_enabled", True) and not self.storage_error,
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
        # SN_CONFIRM is momentary on a passing Step1 but sticky on a failed one;
        # the sticky state must be terminable or a refused Step1 is a dead end.
        step1_retry = self._step1_retry_locked()
        can_terminate = self.state in {"SN_CONFIRM", "WAITING_MCIO_SN", "WAITING_CAPTURE", "NOT_MATCHED", "PASS_CONFIRM", "RECOVERY_REQUIRED"} and self.request_token is None
        return copy.deepcopy({
            "inspection_mode": self.inspection_mode, "state": self.state,
            "cycle_id": self.cycle_id, "sn": self.sn, "source": self.source,
            "mcio_sn": self.mcio_sn, "sn_prefix": self._serial_prefix("sn"),
            "mcio_sn_prefix": self._serial_prefix("mcio"),
            "sn_length": self._serial_length("sn"),
            "mcio_sn_length": self._serial_length("mcio"),
            "active_scan_field": "sn" if self.state == "WAITING_SN" or step1_retry else "mcio_sn" if self.state == "WAITING_MCIO_SN" else "",
            "step1_retry_required": step1_retry,
            "operator": self.operator, "cycle_operator": self.cycle_operator,
            "operator_required": self._operator_required(),
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
            "can_confirm_result": self.state == "PASS_CONFIRM" and self.request_token is None and transport.get("configured", False) and transport.get("step4_enabled", True) and not self.storage_error,
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
                # SN_CONFIRM is deliberately excluded: attaching a browser video
                # rewrites cycle_profile, which would make _same_transport() fail
                # and the retried Step1 reject permanently as a profile mismatch.
                if not enabled or self.state not in {'WAITING_MCIO_SN', 'WAITING_CAPTURE', 'NOT_MATCHED'}:
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

    def _record_sfis(self, event):
        """Append one Step1/Step4 diagnostic entry. Never raises, never blocks
        on the station lock: the transport calls this from the request thread."""
        entry = dict(event or {})
        with self.sfis_lock:
            self.sfis_seq += 1
            entry["seq"] = self.sfis_seq
            entry["ts"] = time.time()
            entry.setdefault("step", "")
            entry.setdefault("phase", "")
            entry.setdefault("cycle_id", self.cycle_id)
            entry.setdefault("sn", self.sn)
            entry.setdefault("mcio_sn", self.mcio_sn)
            entry.setdefault("emp_no", self.cycle_operator or self.operator or "")
            entry.setdefault("station_state", self.state)
            try:
                json.dumps(entry, ensure_ascii=False, default=str)
            except Exception:
                entry = {k: str(v) for k, v in entry.items()}
            self.sfis_events.append(entry)

    def sfis_log(self, after=0):
        after = int(after or 0)
        with self.sfis_lock:
            return [copy.deepcopy(item) for item in self.sfis_events if item["seq"] > after]

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

    def _operator_required(self):
        # An operator must sign in before any snapshot-mode SN, including tests.
        return self.snapshot_mode

    def _is_operator_error(self, text):
        markers = self.config.get("dmz", {}).get("operator_error_markers") or ["NO EMP"]
        haystack = str(text or "").upper()
        return any(str(marker).upper() in haystack for marker in markers)

    def _step1_retry_locked(self):
        # The sticky state a refused Step1 leaves the cycle in: the product SN
        # and cycle are kept, last_error names the reason, no request is in
        # flight. _accept_sn_locked clears last_error before scan() releases the
        # lock, so this is false inside the lock-release window and a concurrent
        # retry or operator change in that window is refused rather than torn.
        return (self.snapshot_mode and self.state == "SN_CONFIRM"
                and bool(self.last_error) and self.request_token is None)

    def set_operator(self, emp_no):
        if not isinstance(emp_no, str):
            raise ValueError("Employee number must be text")
        emp_no = emp_no.strip()
        maximum = max(1, int(self.config.get("emp_no_max_length", 32)))
        if not emp_no or len(emp_no) > maximum or any(ord(c) < 33 or ord(c) > 126 for c in emp_no):
            raise ValueError("Employee number must contain printable ASCII characters without spaces")
        with self.lock:
            if self.closed:
                return self._reply("Station is closed")
            if self.storage_error:
                return self._reply(self.storage_error)
            # The employee number is never locked: an operator must be able to
            # correct it in any state, above all after a NO EMP Step1 rejection.
            self.operator = emp_no
            if self.cycle_id:
                # _payload reads cycle_operator, so a mid-cycle change (e.g. a
                # Step1 retry) carries the corrected number on the next SFIS call.
                previous = self.cycle_operator
                self.cycle_operator = emp_no
                self._save("OPERATOR_CHANGED", {"from": previous or "", "to": emp_no})
            return self._reply()

    def clear_operator(self):
        with self.lock:
            if self.closed:
                return self._reply("Station is closed")
            if self.storage_error:
                return self._reply(self.storage_error)
            self.operator = ""
            if self.cycle_id:
                previous = self.cycle_operator
                self.cycle_operator = None
                self._save("OPERATOR_CHANGED", {"from": previous or "", "to": ""})
            return self._reply()

    def _clean_serial(self, value, label, max_key, default_max=128):
        if not isinstance(value, str):
            raise ValueError(label + " must be text")
        value = value.strip()
        maximum = max(1, int(self.config.get(max_key, default_max)))
        if not value or len(value) > maximum or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError(label + " must contain printable ASCII characters without spaces")
        return value

    def _wrong_label_error(self, field, prefix, instruction, value):
        return ("Wrong label: the " + field + " must start with " + prefix + ". "
                + instruction + ". Scanned: " + str(value)[:32])

    def _reject_scan_locked(self, message, event=None):
        # A wrong label is an operator mistake at a known step, not malformed
        # input: it must reach the dashboard even when the HID scanner sent it,
        # so it is a reply plus last_error instead of a raised ValueError.
        self.last_error = message
        if event and self.cycle_id:
            self._save(event, {"error": message})
        return self._reply(message)

    def scan(self, sn, source="web"):
        # The HID scanner is grabbed exclusively and calls straight into here, so
        # the station itself decides whether a scan is the product or MCIO serial.
        sn = self._clean_serial(sn, "SN", "sn_max_length")
        gate_cycle = None
        with self.lock:
            if self.closed:
                return self._reply("Station is closed")
            if self.storage_error:
                return self._reply(self.storage_error)
            if self.snapshot_mode and self.state == "WAITING_MCIO_SN":
                reply, gate_cycle = self._accept_mcio_locked(sn, source)
            else:
                reply, gate_cycle = self._accept_sn_locked(sn, source)
        if gate_cycle is None:
            return reply
        return self._gate_step1(gate_cycle)

    def _accept_sn_locked(self, sn, source):
        if not self.snapshot_mode and self.state == "SN_CONFIRM" and self.sn == sn:
            return self._reply(), None
        if self._step1_retry_locked() and self.sn == sn:
            # Step1 was refused for this product SN and the cycle was kept. A
            # re-scan of the same label is the retry trigger: mint a fresh
            # request id (the retry may now carry a different empNo, so SFIS
            # must not dedupe it against the first attempt) and re-send Step1.
            # This must precede the duplicate guard below, which would otherwise
            # swallow the re-scan as a silent success.
            if self._operator_required() and not self.operator:
                # Switch operator clears the empNo; a HID scan that arrives
                # before the new one is signed in would only earn another NO EMP.
                return self._reply("Enter an operator employee number before scanning"), None
            self.sn_request_id = uuid.uuid4().hex
            self.last_error = ""
            self._save("SN_RESCANNED_FOR_STEP1_RETRY",
                       {"source": str(source)[:40], "request_id": self.sn_request_id})
            return self._reply(), self.cycle_id
        if self.snapshot_mode and self.cycle_id and sn in {self.sn, self.mcio_sn}:
            # A scanner that fires the same label twice must not raise the
            # locked-SN alarm; the step has simply already been completed.
            return self._reply(), None
        if self.state != "WAITING_SN":
            return self._reply("The current SN is locked. Upload successfully or terminate the inspection before scanning another SN"), None
        if self._operator_required() and not self.operator:
            return self._reply("Enter an operator employee number before scanning"), None
        prefix = self._serial_prefix("sn")
        if prefix and not sn.upper().startswith(prefix.upper()):
            # No cycle exists yet, so this rejection must not journal a row.
            return self._reject_scan_locked(self._wrong_label_error(
                "product serial number", prefix, "Scan the product serial number label", sn)), None
        length = self._serial_length("sn")
        if length and len(sn) != length:
            return self._reject_scan_locked(self._wrong_length_error(
                "product serial number", length, "Scan the product serial number label", sn)), None
        self.cycle_id = uuid.uuid4().hex
        self.sn_request_id, self.report_request_id = uuid.uuid4().hex, uuid.uuid4().hex
        self.sn, self.source = sn, str(source)[:40]
        self.mcio_sn = ""
        self.cycle_operator = self.operator or None
        self.created_at = time.time()
        self.started_at = time.time() if self.snapshot_mode else None
        self.completed_at = None
        self.cycle_profile = self._transport_profile()
        self.cycle_fingerprints = {"config_sha256": self.config_fingerprint, "templates_sha256": self.template_fingerprint}
        self.cycle_station_id = str(self.config.get("station_id", "NK_APL_50"))
        # Step1 fires on the product SN, before the MCIO card is scanned: its
        # payload never carried the MCIO serial, so there is no reason to make
        # the operator install a card before the unit is validated.
        auto_sfis_check = self.snapshot_mode and not self.local_mode and not self.test_mode
        self.state = ("SN_CONFIRM" if auto_sfis_check else "WAITING_MCIO_SN") if self.snapshot_mode else "SN_CONFIRM"
        self.result = None
        self.step_index, self.step_times = 0, []
        self.last_error = ""
        self._clear_matching()
        self._save("SN_SCANNED_AND_STARTED" if self.snapshot_mode else "SN_SCANNED",
                   {"source": self.source})
        return self._reply(), (self.cycle_id if auto_sfis_check else None)

    def _accept_mcio_locked(self, value, source):
        if self.request_token is not None:
            return self._reply("Wait for the current operation to finish"), None
        value = self._clean_serial(value, "MCIO card serial number", "mcio_sn_max_length")
        prefix = self._serial_prefix("mcio")
        if prefix and not value.upper().startswith(prefix.upper()):
            return self._reject_scan_locked(self._wrong_label_error(
                "MCIO card serial number", prefix, "Scan the MCIO card label", value),
                "MCIO_SN_REJECTED"), None
        length = self._serial_length("mcio")
        if length and len(value) != length:
            return self._reject_scan_locked(self._wrong_length_error(
                "MCIO card serial number", length, "Scan the MCIO card label", value),
                "MCIO_SN_REJECTED"), None
        self.mcio_sn = value
        self.last_error = ""
        # Step1 already passed on the product SN; the MCIO scan only opens capture.
        self.state = "WAITING_CAPTURE"
        self._save("MCIO_SN_SCANNED", {"source": str(source)[:40]})
        return self._reply(), None

    def _gate_step1(self, cycle_id):
        result = self._confirm(cycle_id, result=False)
        if result.get("success"):
            return result
        # Step1 is a pre-inspection gate. A rejection or connection failure keeps
        # the product SN and cycle: the operator fixes the cause (e.g. a wrong
        # employee number) and re-scans the same product SN to retry. _confirm
        # has already returned the station to SN_CONFIRM with last_error set and
        # journaled SN_CHECK_FAILED, so this only refines the operator-facing
        # message and traces the retry requirement — no extra _save.
        with self.lock:
            if self.cycle_id == cycle_id and self.state == "SN_CONFIRM":
                error = self.last_error or result.get("error") or "SFIS Step1 failed"
                operator_error = self._is_operator_error(error)
                if operator_error:
                    self.last_error = ("Employee number rejected by SFIS. Select Switch operator,"
                                       " enter the correct employee number, then scan the same product"
                                       " serial number again. SFIS said: " + error)
                else:
                    self.last_error = error
                self._record_sfis({"step": "step1", "phase": "retry_required", "detail": error,
                                   "operator_error": operator_error,
                                   "hint": "The product serial number is kept. Scan the same product"
                                           " serial number again to retry Step1, or terminate this"
                                           " inspection."
                                           + (" Change the employee number first." if operator_error else "")})
                result = self._reply(self.last_error)
        return result

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
        step_name = "step4" if result else "step1"
        with self.lock:
            self._check_cycle(cycle_id)
            if not self._same_transport():
                return self._reply("DMZ mode or endpoint differs from this cycle; restore its original DMZ configuration")
            if self.state != expected or self.request_token is not None:
                return self._reply("This action is not available in the current state")
            if result and (self.result != "PASS" or self.step_index != len(self.step_names)):
                return self._reply("Only a completed PASS can be reported")
            if result and self.test_mode and not self.bypass_sfis:
                return self._reply("Browser test data cannot be uploaded to SFIS")
            if result and self.snapshot_mode and self.local_mode and not self.bypass_sfis:
                return self._reply("SFIS not configured")
            if result and self._transport_profile().get("step4_enabled") is False:
                return self._reply("SFIS Step4 is disabled by configuration")
            if self.local_mode:
                self.last_error = ''
                self._record_sfis({"step": step_name, "phase": "local",
                                   "detail": "Station is in local mode; SFIS was not contacted"})
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
            self._save("REPORT_REQUESTED" if result else "SN_CHECK_REQUESTED",
                       {"request_id": payload["request_id"]}, outbox="pending" if result else None)
            self._record_sfis({"step": step_name, "phase": "begin", "path": path,
                               "request_id": payload["request_id"],
                               "transport_mode": profile.get("mode"),
                               "bypass": bool(profile.get("bypass"))})
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
                self._record_sfis({"step": step_name, "phase": "ignored",
                                   "detail": "Late DMZ answer ignored because the cycle changed"})
                return self._reply("Late DMZ response ignored because the cycle changed")
            self.request_token = None
            if error:
                self.state, self.last_error = expected, error
                self._save("REPORT_RETRY_REQUIRED" if result else "SN_CHECK_FAILED", {"error": error})
                self._record_sfis({"step": step_name, "phase": "failed", "detail": error,
                                   "station_state": self.state})
                return self._reply(error)
            self._record_sfis({"step": step_name, "phase": "accepted",
                               "response": response if isinstance(response, dict) else None})
            if result:
                self.last_result = {"cycle_id": self.cycle_id, "sn": self.sn, "result": "PASS", "reported": True,
                                    "reported_at": time.time(), "request_id": self.report_request_id,
                                    "dmz_mode": self._snapshot()["dmz"]["mode"], "step_times": list(self.step_times)}
                self._save("PASS_ACKNOWLEDGED", state_override="COMPLETE", outbox=response)
                self._clear_cycle()
            else:
                # Snapshot mode validates on the product SN, so a passed Step1
                # advances to the MCIO scan rather than straight to capture.
                self.state = "WAITING_MCIO_SN" if self.snapshot_mode else "RUNNING"
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
            if self.state not in {"SN_CONFIRM", "WAITING_MCIO_SN", "WAITING_CAPTURE", "NOT_MATCHED", "PASS_CONFIRM", "RECOVERY_REQUIRED"}:
                return self._reply("Terminate Inspection is unavailable in the current state")
            self._finish_without_report("OPERATOR_SELECTED_NO_UPLOAD")
            return self._reply()

    def _finish_without_report(self, reason):
        if self.snapshot_mode:
            self.last_result = {
                "cycle_id": self.cycle_id, "sn": self.sn, "mcio_sn": self.mcio_sn,
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
            # SN_CONFIRM/WAITING_DMZ (the Step1 gate) are pre-capture with no
            # outbox row, so a lost camera there kills the cycle like the MCIO
            # step already does; a late DMZ answer is still discarded by the
            # cycle/token guard in _confirm.
            active_states = {"SN_CONFIRM", "WAITING_DMZ", "WAITING_MCIO_SN", "WAITING_CAPTURE", "NOT_MATCHED", "JUDGING"} if self.snapshot_mode else {"RUNNING", "WAITING_DMZ"}
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
        # While the automatic probe is running the station has no committed match
        # yet, so the overlay would freeze on UNKNOWN. Draw the live probe scores
        # instead, on this throwaway copy of the snapshot only.
        if (self.snapshot_mode and self.preview_matching
                and snapshot.get("state") in {"WAITING_CAPTURE", "NOT_MATCHED"}):
            snapshot = {**snapshot, "matching": self.preview_matching,
                        "auto_capture": self.preview_auto or {}}
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

    def inspect_snapshot(self, frame, source=None, quality=92, cancelled=None,
                         timeout_pass=False):
        with self.lock:
            if cancelled is not None and cancelled():
                return self._reply('Camera stopped before inspection')
            self._check_cycle(self.cycle_id)
            if not self.snapshot_mode:
                return self._reply("Capture & Inspect is only available in snapshot mode")
            if self.state not in {"WAITING_CAPTURE", "NOT_MATCHED"} or self.request_token is not None:
                return self._reply("Capture & Inspect is unavailable in the current state")
            token = self.generation
            cycle_id, sn, mcio_sn = self.cycle_id, self.sn, self.mcio_sn
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
                if (self.closed or token != self.generation or self.cycle_id != cycle_id
                        or self.state != "JUDGING" or (cancelled is not None and cancelled())):
                    return self._reply("Inspection result was discarded because the cycle changed")
                accepted = bool(match.get("accepted"))
                # auto_capture.timeout_action='pass' closes the unit when the probe
                # ran out of time. The real matcher verdict is kept verbatim and
                # flagged, so neither the journal, the evidence nor the history can
                # be read as a genuine visual match.
                auto_pass = bool(timeout_pass) and not accepted
                if auto_pass:
                    match = {**match, "timeout_auto_pass": True}
                passed = accepted or auto_pass
                self.matching = match
                self.result = "PASS" if passed else "NOT_MATCHED"
                self.state = "PASS_CONFIRM" if passed else "NOT_MATCHED"
                self.step_index = 1 if passed else 0
                self.completed_at = time.time() if passed else None
                self.step_times = [self.completed_at] if passed else []
                snapshot = self._snapshot()
                result_frame = self.matcher.draw(frame, snapshot)
                try:
                    attempt = self.evidence.save_attempt(
                        cycle_id=cycle_id, sn=sn, mcio_sn=mcio_sn,
                        visual_result="OK" if accepted else "TIMEOUT_PASS" if auto_pass else "NOT_MATCHED",
                        raw_frame=frame, result_frame=result_frame, matching=match,
                        source=source, quality=quality,
                    )
                    self._save("INSPECTION_OK" if accepted else "INSPECTION_TIMEOUT_PASS" if auto_pass
                               else "INSPECTION_NOT_MATCHED",
                               {"attempt_id": attempt["attempt_id"], "reason": match.get("reason"),
                                "timeout_auto_pass": auto_pass})
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
