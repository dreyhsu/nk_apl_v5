"""Loopback-only test DMZ. This is not the production DMZ implementation."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockHandler(BaseHTTPRequestHandler):
    responses = {}
    guard = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def _reply(self, status, data):
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path not in {"/sn/check", "/result"}:
            return self._reply(404, {"error": "unknown endpoint", "mock": True})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("invalid body size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or any(not isinstance(body.get(k), str) or not body[k] for k in ("sn", "cycle_id", "request_id")):
                raise ValueError("missing correlation fields")
            if self.path == "/result" and body.get("result") != "PASS":
                raise ValueError("only PASS is supported")
            key = (self.path, body["request_id"])
            canonical = json.dumps(body, sort_keys=True)
            with self.guard:
                previous = self.responses.get(key)
                if previous and previous[0] != canonical:
                    return self._reply(409, {"error": "idempotency conflict", "mock": True})
                response = {"accepted": True, "mock": True, **body}
                if previous:
                    response = previous[1]
                else:
                    self.responses[key] = (canonical, response)
            return self._reply(200, response)
        except (ValueError, TypeError, KeyError):
            return self._reply(400, {"error": "invalid request", "mock": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print("MOCK DMZ / TEST ONLY at http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
