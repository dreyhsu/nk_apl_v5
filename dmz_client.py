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
        self.step2_path = str(cfg.get("step2_path", "/Step2")).strip()
        self.step2_enabled = bool(cfg.get("step2_enabled", False))
        self.step2_extra_fields = dict(cfg.get("step2_extra_fields") or {})
        self.mcio_field = str(cfg.get("step2_mcio_field", "mcioSn")).strip()
        if not self.mcio_field or self.mcio_field in {"line", "station", "sn", "empNo"}:
            raise ValueError("dmz.step2_mcio_field must be a field name that does not collide with line, station, sn, or empNo")
        if self.mode not in {"disabled", "mock", "http"}:
            raise ValueError("dmz.mode must be disabled, mock, or http")
        # Engineering switch for stations without a DMZ link: Step1/Step2 answer
        # OK locally so the whole SN cycle can be exercised. It never changes
        # what dmz.mode means; it only short-circuits ahead of it.
        self.bypass = bypass_enabled(cfg)
        if self.mode == "mock" and not self.base_url:
            self.base_url = "http://127.0.0.1:8791"
        self.opener = urllib.request.build_opener(_NoRedirect())

    def status(self):
        if self.bypass:
            # The "+bypass" suffix keeps _same_transport() from letting a cycle
            # that started against the real SFIS be finished by a bypassed
            # Step2, and records the difference in the cycle history.
            return {"mode": self.mode + "+bypass", "configured": True,
                    "step2_enabled": True, "bypass": True,
                    "label": f"SFIS BYPASS - TEST ONLY ({self.mode})"}
        # The employee number is supplied per request by the signed-in operator,
        # so station readiness no longer depends on a static config value.
        sfis_ready = self.api_style != "sfis" or all(
            (self.line, self.station, self.step1_path, self.step2_path)
        )
        return {
            "mode": self.mode,
            "configured": self.mode != "disabled" and bool(self.base_url) and sfis_ready,
            "step2_enabled": self.step2_enabled if self.api_style == "sfis" else True,
            "label": "MOCK / TEST ONLY" if self.mode == "mock" else "SFIS" if self.api_style == "sfis" else self.mode.upper(),
        }

    def _wire_request(self, path, payload):
        if self.api_style != "sfis":
            return path, payload
        mapping = {"/sn/check": self.step1_path, "/result": self.step2_path}
        if path not in mapping:
            raise DMZError("Unsupported SFIS operation")
        if path == "/result" and not self.step2_enabled:
            raise DMZError("SFIS Step2 is disabled by configuration")
        emp_no = str(payload.get("emp_no") or self.employee_no).strip()
        if not all((self.line, self.station, emp_no)):
            raise DMZError("SFIS line, station, and employee number must be configured")
        body = {
            "line": self.line,
            "station": self.station,
            "sn": payload["sn"],
            "empNo": emp_no,
        }
        if path == "/result":
            body.update(self.step2_extra_fields)
            mcio = str(payload.get("mcio_sn") or "").strip()
            if not mcio:
                raise DMZError("MCIO card serial number is missing for SFIS Step2")
            # After the extras: configuration must never clobber the real serial.
            body[self.mcio_field] = mcio
        return mapping[path], body

    @staticmethod
    def _normalize_sfis_response(path, payload, response):
        if not isinstance(response, dict):
            raise DMZError("SFIS response must be a JSON object")
        data = response.get("data")
        error_code = response.get("errorCode")
        if response.get("success") is not True or error_code not in (None, ""):
            detail = response.get("message") or error_code or "request rejected"
            raise DMZError("SFIS rejected request: " + str(detail))
        if isinstance(data, str) and data.strip().upper().startswith("FAIL"):
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
        if self.bypass:
            return self._bypass_response(path, payload)
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
                result = json.loads(raw)
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
