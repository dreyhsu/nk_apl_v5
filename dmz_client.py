"""Explicit DMZ transport with correlated, retry-safe request identifiers."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class DMZError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DMZError("DMZ redirects are not allowed")


class DMZClient:
    def __init__(self, config=None):
        cfg = dict(config or {})
        self.mode = os.environ.get("APL_DMZ_MODE", cfg.get("mode", "disabled")).lower()
        self.base_url = os.environ.get("APL_DMZ_URL", cfg.get("base_url", "")).rstrip("/")
        self.timeout = max(0.1, min(60.0, float(cfg.get("timeout_seconds", 5))))
        self.allow_http = bool(cfg.get("allow_insecure_http", False))
        if self.mode not in {"disabled", "mock", "http"}:
            raise ValueError("dmz.mode must be disabled, mock, or http")
        if self.mode == "mock" and not self.base_url:
            self.base_url = "http://127.0.0.1:8791"
        self.opener = urllib.request.build_opener(_NoRedirect())

    def status(self):
        return {
            "mode": self.mode,
            "configured": self.mode != "disabled" and bool(self.base_url),
            "label": "MOCK / TEST ONLY" if self.mode == "mock" else self.mode.upper(),
        }

    def send(self, path, payload):
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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data,
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
