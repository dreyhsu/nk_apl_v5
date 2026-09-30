"""Explicit SFIS/DMZ transport with correlated, retry-safe requests."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class DMZError(RuntimeError):
    pass


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def sfis_enabled(config=None):
    """Master switch for Shopfloor (SFIS) traffic.

    An absent key means enabled, so an existing production config keeps its
    behaviour. This is distinct from dmz.mode == "disabled", which fails loud on
    every call; this switch lets the station drop the transport entirely and
    complete cycles locally, for a SmartCam with no Shopfloor network.
    """
    value = dict(config or {}).get("sfis_enabled", True)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("dmz.sfis_enabled must be a boolean (true/false)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DMZError("DMZ redirects are not allowed")


class DMZClient:
    def __init__(self, config=None):
        cfg = dict(config or {})
        self.enabled = sfis_enabled(cfg)
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
        if self.mode not in {"disabled", "mock", "http"}:
            raise ValueError("dmz.mode must be disabled, mock, or http")
        if self.mode == "mock" and not self.base_url:
            self.base_url = "http://127.0.0.1:8791"
        self.opener = urllib.request.build_opener(_NoRedirect())

    def status(self):
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

    def send(self, path, payload):
        if not self.enabled:
            raise DMZError("Shopfloor Server calls are disabled by configuration (dmz.sfis_enabled=false)")
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
