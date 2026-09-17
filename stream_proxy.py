"""Same-origin WHEP signaling proxy for the local MediaMTX instance."""
from __future__ import annotations

import configparser
import http.client
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, jsonify, request


_SESSION_RE = re.compile(r"^[A-Za-z0-9._~-]{1,160}$")
_MAX_SIGNAL_BODY = 1_048_576
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _settings(config_path: Path):
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8-sig")
    section = parser["stream"] if parser.has_section("stream") else {}
    host = os.environ.get("APL_MEDIAMTX_HOST", section.get("whep_host", "127.0.0.1")).strip()
    port = int(os.environ.get("APL_MEDIAMTX_WHEP_PORT", section.get("whep_port", "8889")))
    stream = os.environ.get("APL_MEDIAMTX_PATH", section.get("path", "smartcam")).strip()
    if host not in _ALLOWED_HOSTS:
        raise ValueError("MediaMTX WHEP proxy host must be loopback")
    if not 1 <= port <= 65535 or not _SESSION_RE.fullmatch(stream):
        raise ValueError("Invalid MediaMTX WHEP port or path")
    return host, port, stream


def register_stream_routes(app, config_path):
    """Register a strict proxy for POST/PATCH/DELETE WHEP signaling only."""
    host, port, stream = _settings(Path(config_path))
    marker = f"/{stream}/whep"

    def proxy(method, suffix=""):
        if suffix and not _SESSION_RE.fullmatch(suffix):
            return jsonify({"success": False, "error": "Invalid WHEP session"}), 404
        body = request.get_data(cache=False, as_text=False)
        if len(body) > _MAX_SIGNAL_BODY:
            return jsonify({"success": False, "error": "WHEP body too large"}), 413
        if method == "POST" and not body:
            return jsonify({"success": False, "error": "SDP offer is required"}), 400
        content_type = request.headers.get("Content-Type", "application/sdp")
        connection = http.client.HTTPConnection(host, port, timeout=10)
        try:
            target = marker + (("/" + suffix) if suffix else "")
            connection.request(method, target, body=body, headers={"Content-Type": content_type})
            upstream = connection.getresponse()
            response_body = upstream.read(_MAX_SIGNAL_BODY + 1)
            if len(response_body) > _MAX_SIGNAL_BODY:
                return jsonify({"success": False, "error": "MediaMTX response too large"}), 502
            headers = {}
            upstream_type = upstream.getheader("Content-Type")
            if upstream_type:
                headers["Content-Type"] = upstream_type
            location = upstream.getheader("Location")
            if location:
                path = urlsplit(location).path
                if not path.startswith(marker + "/"):
                    return jsonify({"success": False, "error": "Invalid MediaMTX session location"}), 502
                session = path[len(marker) + 1:]
                if not _SESSION_RE.fullmatch(session):
                    return jsonify({"success": False, "error": "Invalid MediaMTX session identifier"}), 502
                headers["Location"] = "/api/stream/whep/" + session
            return Response(response_body, status=upstream.status, headers=headers)
        except (OSError, http.client.HTTPException) as exc:
            return jsonify({"success": False, "error": "MediaMTX WHEP is unavailable", "detail": str(exc)}), 502
        finally:
            connection.close()

    app.add_url_rule("/api/stream/whep", "apl_whep_create", lambda: proxy("POST"), methods=["POST"])
    app.add_url_rule(
        "/api/stream/whep/<session>", "apl_whep_session",
        lambda session: proxy(request.method, session), methods=["PATCH", "DELETE"],
    )
