"""Security and response-mapping checks for the MediaMTX WHEP proxy."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_proxy import register_stream_routes


class StreamProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = Path(self.temp.name) / "edge.ini"
        config.write_text("[stream]\nwhep_host=127.0.0.1\nwhep_port=8889\npath=smartcam\n", encoding="utf-8")
        self.app = Flask(__name__)
        register_stream_routes(self.app, config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_offer_is_rejected_before_upstream(self):
        self.assertEqual(self.client.post("/api/stream/whep", data=b"").status_code, 400)

    def test_invalid_session_is_not_routed(self):
        self.assertEqual(self.client.delete("/api/stream/whep/bad%2Fsession").status_code, 404)

    @patch("stream_proxy.http.client.HTTPConnection")
    def test_location_is_rewritten_to_same_origin(self, connection_type):
        upstream = Mock()
        upstream.status = 201
        upstream.read.return_value = b"answer"
        upstream.getheader.side_effect = lambda name: {
            "Content-Type": "application/sdp",
            "Location": "http://127.0.0.1:8889/smartcam/whep/session-1",
        }.get(name)
        connection_type.return_value.getresponse.return_value = upstream
        response = self.client.post(
            "/api/stream/whep", data=b"offer", content_type="application/sdp"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Location"], "/api/stream/whep/session-1")
        connection_type.assert_called_once_with("127.0.0.1", 8889, timeout=10)

    @patch("stream_proxy.http.client.HTTPConnection")
    def test_foreign_upstream_location_fails_closed(self, connection_type):
        upstream = Mock(status=201)
        upstream.read.return_value = b"answer"
        upstream.getheader.side_effect = lambda name: "https://evil.invalid/session" if name == "Location" else None
        connection_type.return_value.getresponse.return_value = upstream
        response = self.client.post("/api/stream/whep", data=b"offer")
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
