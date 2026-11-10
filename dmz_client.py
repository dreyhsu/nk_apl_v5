"""Explicit SFIS/DMZ transport with correlated, retry-safe requests."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class DMZError(RuntimeError):
    pass


def _truthy(env_value, config_value):
    if not isinstance(config_value, bool):
        raise ValueError("dmz.bypass_sfis must be a boolean (true/false)")
    if env_value is None or not str(env_value).strip():
        return config_value
    raw = str(env_value).strip().lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    raise ValueError("APL_DMZ_BYPASS must be on or off")


def bypass_enabled(config):
    """Read the bypass switch the way DMZClient does, without building one.

    A station running without a transport (browser video test) still needs to
    know whether SFIS is bypassed, and the env override must behave identically.
    """
    return _truthy(os.environ.get("APL_DMZ_BYPASS"), dict(config or {}).get("bypass_sfis", False))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DMZError("DMZ redirects are not allowed")


class DMZClient:
    def __init__(self, config=None):
        cfg = dict(config or {})
        if "sfis_enabled" in cfg:
            raise ValueError("dmz.sfis_enabled is obsolete; explicitly migrate to dmz.bypass_sfis")
        self.mode = os.environ.get("APL_DMZ_MODE", cfg.get("mode", "disabled")).lower()
        self.base_url = os.environ.get("APL_DMZ_URL", cfg.get("base_url", "")).rstrip("/")
        self.timeout = max(0.1, min(60.0, float(cfg.get("timeout_seconds", 5))))
        self.allow_http = bool(cfg.get("allow_insecure_http", False))
        self.api_style = str(cfg.get("api_style", "legacy")).strip().lower()
        if self.api_style not in {"legacy", "sfis"}:
            raise ValueError("dmz.api_style must be legacy or sfis")
        self.line = str(os.environ.get("APL_SFIS_LINE", cfg.get("line", ""))).strip()
        self.station = str(os.environ.get("APL_SFIS_STATION", cfg.get("station", ""))).strip()
        self.employee_no = str(os.environ.get("APL_SFIS_EMP_NO", cfg.get("employee_no", ""))).strip()
        self.step1_path = str(cfg.get("step1_path", "/Step1")).strip()
        step1_allowed = cfg.get("step1_allowed_fail_responses", [])
        if not isinstance(step1_allowed, list) or any(
            not isinstance(value, str) or not value.strip().upper().startswith("FAIL")
            for value in step1_allowed
        ):
            raise ValueError("dmz.step1_allowed_fail_responses must be a list of FAIL response strings")
        self.step1_allowed_fail_responses = {value.strip() for value in step1_allowed}
        self.step4_path = str(cfg.get("step4_path", "/Step4")).strip()
        self.step4_enabled = bool(cfg.get("step4_enabled", False))
        self.step4_extra_fields = dict(cfg.get("step4_extra_fields") or {})
        self.mcio_field = str(cfg.get("step4_mcio_field", "mciosn")).strip()
        if not self.mcio_field or self.mcio_field in {"line", "station", "sn", "empNo"}:
            raise ValueError("dmz.step4_mcio_field must be a field name that does not collide with line, station, sn, or empNo")
        if self.mode not in {"disabled", "mock", "http"}:
            raise ValueError("dmz.mode must be disabled, mock, or http")
        # Engineering switch for stations without a DMZ link: Step1/Step4 answer
        # OK locally so the whole SN cycle can be exercised. It never changes
        # what dmz.mode means; it only short-circuits ahead of it.
        self.bypass = bypass_enabled(cfg)
        if self.mode == "mock" and not self.base_url:
            self.base_url = "http://127.0.0.1:8791"
        self.opener = urllib.request.build_opener(_NoRedirect())
        # Optional diagnostics sink. The station sets this so the dashboard
        # console can show the real Step1/Step4 URL, body and answer. It never
        # changes what is sent and a failing hook never fails a request.
        self.on_trace = None

    def _emit(self, event):
        if event.get("phase") in {"response", "error", "bypass"}:
            try:
                # Log before invoking the optional browser trace hook, including
                # API bodies rejected by validation and calls without a hook.
                answer = event.get("raw_response", event.get("response"))
                print(f"[SFIS {event['step'].upper()}] {event['phase']} "
                      + json.dumps({"url": event.get("url", ""), "response": answer,
                                    "detail": event.get("detail", "")}, ensure_ascii=False),
                      flush=True)
            except Exception:
                pass
        hook = self.on_trace
        if hook is None:
            return
        try:
            hook(event)
        except Exception:
            pass

    def status(self):
        if self.bypass:
            # The "+bypass" suffix keeps _same_transport() from letting a cycle
            # that started against the real SFIS be finished by a bypassed
            # Step4, and records the difference in the cycle history.
            return {"mode": self.mode + "+bypass", "configured": True,
                    "step4_enabled": True, "bypass": True,
                    "label": f"SFIS BYPASS - TEST ONLY ({self.mode})"}
        # The employee number is supplied per request by the signed-in operator,
        # so station readiness no longer depends on a static config value.
        sfis_ready = self.api_style != "sfis" or all(
            (self.line, self.station, self.step1_path, self.step4_path)
        )
        return {
            "mode": self.mode,
            "configured": self.mode != "disabled" and bool(self.base_url) and sfis_ready,
            "step4_enabled": self.step4_enabled if self.api_style == "sfis" else True,
            "label": "MOCK / TEST ONLY" if self.mode == "mock" else "SFIS" if self.api_style == "sfis" else self.mode.upper(),
        }

    def _wire_request(self, path, payload):
        if self.api_style != "sfis":
            return path, payload
        mapping = {"/sn/check": self.step1_path, "/result": self.step4_path}
        if path not in mapping:
            raise DMZError("Unsupported SFIS operation")
        if path == "/result" and not self.step4_enabled:
            raise DMZError("SFIS Step4 is disabled by configuration")
        emp_no = str(payload.get("emp_no") or self.employee_no).strip()
        if not all((self.line, self.station, emp_no)):
            raise DMZError("SFIS line, station, and employee number must be configured")
        emp_field = "EmpNo" if path == "/result" else "empNo"
        body = {
            "line": self.line,
            "station": self.station,
            "sn": payload["sn"],
            emp_field: emp_no,
        }
        if path == "/result":
            body.update(self.step4_extra_fields)
            mcio = str(payload.get("mcio_sn") or "").strip()
            if not mcio:
                raise DMZError("MCIO card serial number is missing for SFIS Step4")
            # After the extras: configuration must never clobber the real serial.
            body[self.mcio_field] = mcio
        return mapping[path], body

    def _normalize_sfis_response(self, path, payload, response):
        if not isinstance(response, dict):
            raise DMZError("SFIS response must be a JSON object")
        data = response.get("data")
        error_code = response.get("errorCode")
        if response.get("success") is not True or error_code not in (None, ""):
            detail = response.get("message") or error_code or "request rejected"
            raise DMZError("SFIS rejected request: " + str(detail))
        if isinstance(data, str) and data.strip().upper().startswith("FAIL"):
            # Only explicitly listed Step1 business failures are allowed. Keep
            # Step4 and the success/errorCode checks above strict.
            allowed = path == "/sn/check" and data.strip() in self.step1_allowed_fail_responses
            if not allowed:
                raise DMZError("SFIS business validation failed: " + data.strip())
        normalized = {
            "accepted": True,
            "sn": payload["sn"],
            "cycle_id": payload["cycle_id"],
            "request_id": payload["request_id"],
            "sfis_response": response,
        }
        if path == "/result":
            normalized["result"] = payload.get("result")
        return normalized

    @staticmethod
    def _bypass_response(path, payload):
        reply = {"accepted": True, "bypass": True,
                 "sn": payload["sn"], "cycle_id": payload["cycle_id"],
                 "request_id": payload["request_id"]}
        if path == "/result":
            reply["result"] = payload.get("result")
        return reply

    def send(self, path, payload):
        step = "step4" if path == "/result" else "step1"
        if self.bypass:
            reply = self._bypass_response(path, payload)
            self._emit({"step": step, "phase": "bypass", "url": "", "request_body": None,
                        "response": reply, "detail": "SFIS bypass is on; nothing was sent"})
            return reply
        context = {"step": step, "url": "", "request_body": None}
        try:
            result = self._send_http(path, payload, context)
        except Exception as exc:
            self._emit({**context, "phase": "error", "detail": str(exc)})
            raise
        self._emit({**context, "phase": "response", "response": result})
        return result

    def _send_http(self, path, payload, context):
        if self.mode == "disabled":
            raise DMZError("DMZ is disabled. Configure production HTTP or explicit mock mode.")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise DMZError("Invalid DMZ base URL")
        if parsed.query or parsed.fragment:
            raise DMZError("DMZ base URL must not contain query or fragment")
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if self.mode == "mock" and not local:
            raise DMZError("Mock DMZ must use loopback")
        if parsed.scheme != "https" and not local and not self.allow_http:
            raise DMZError("Remote DMZ requires HTTPS unless allow_insecure_http is explicitly enabled")
        wire_path, wire_payload = self._wire_request(path, payload)
        context["url"] = self.base_url + wire_path
        context["request_body"] = wire_payload
        context["timeout_seconds"] = self.timeout
        self._emit({**context, "phase": "request"})
        data = json.dumps(wire_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + wire_path, data=data,
            headers={"Content-Type": "application/json", "Idempotency-Key": payload["request_id"]},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise DMZError("DMZ response exceeds limit")
                context["raw_response"] = raw.decode("utf-8", errors="replace")
                result = json.loads(raw)
                context["raw_response"] = result
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read(65537)
            context["raw_response"] = raw[:65536].decode("utf-8", errors="replace")
            if len(raw) > 65536:
                context["raw_response"] += " [truncated]"
            raise DMZError("DMZ request failed: HTTPError " + str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DMZError("DMZ request failed: " + type(exc).__name__) from exc
        if self.api_style == "sfis":
            result = self._normalize_sfis_response(path, payload, result)
        self.validate_response(path, payload, result)
        if self.mode == "http" and result.get("mock") is True:
            raise DMZError("Production DMZ mode rejected a mock response")
        return result

    @staticmethod
    def validate_response(path, payload, response):
        if not isinstance(response, dict) or response.get("accepted") is not True:
            raise DMZError("DMZ did not accept this request")
        for field in ("sn", "cycle_id", "request_id"):
            if response.get(field) != payload.get(field):
                raise DMZError("DMZ response correlation mismatch: " + field)
        if path == "/result" and response.get("result") != "PASS":
            raise DMZError("DMZ result acknowledgment does not confirm PASS")
