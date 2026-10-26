"""In-process stand-in for the Platform Model Router HTTP surface (plain HTTP on loopback).

Like the router it applies the pinned schema subset to every response_schema,
answers 401 for a wrong bearer token, stores every outcome it completes under
the Idempotency-Key and replays it (failures included, with
`Idempotent-Replayed: true`), answers 409 when a key returns with a different
body, and stores nothing for 429, 202, or a proxy-style reply without a router
error body.
"""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from tests.router_schema_validator import RouterError, canonical, validate_instance, validate_schema

TOKEN = "fake-router-token-0123456789abcdef"
MODEL = "bonsai2:27b-pq2_0"


class FakeRouter:
    def __init__(self):
        self.requests = []          # (method, path, headers, parsed body or None)
        self.script = []            # queued (status, payload) replies; payload: dict, bytes, or None
        self.output = {"ok": True}  # parsed_output of a default success
        self.model = MODEL
        self.ready = True
        self.delay = 0.0
        self.generations = 0
        self.token_file = None
        self._stored = {}
        self._lock = threading.Lock()
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                router._get(self)

            def do_POST(self):
                router._post(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def reply(self, status, payload=None):
        self.script.append((status, payload))

    @staticmethod
    def error(code):
        return {"error": {"code": code, "message": "sanitized",
                          "trace_id": "00000000-0000-0000-0000-000000000000"}}

    def succeeded(self, body, parsed):
        return {"task_id": body.get("task_id"), "status": "succeeded", "selected_model": self.model,
                "model_revision": "descriptor-sha256:" + "0" * 64, "content": canonical(parsed),
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cost_usd": 0,
                          "latency_ms": 1500, "finish_reason": "stop"},
                "routing_decision_id": "route-1", "trace_id": "00000000-0000-0000-0000-000000000000",
                "provider_activity_id": "activity-1", "parsed_output": parsed}

    @staticmethod
    def _send(handler, status, payload, headers=()):
        if isinstance(payload, bytes):
            data = payload
        elif payload is None:
            data = b""
        else:
            data = (canonical(payload) + "\n").encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Connection", "close")
            for name, value in headers:
                handler.send_header(name, value)
            handler.end_headers()
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The client gave up (timeout tests).

    def _get(self, handler):
        self.requests.append(("GET", handler.path, dict(handler.headers), None))
        if handler.path == "/readyz":
            self._send(handler, 200 if self.ready else 503, {"status": "ready" if self.ready else "unavailable"})
        else:
            self._send(handler, 404, self.error("MODEL_ENDPOINT_NOT_FOUND"))

    def _post(self, handler):
        raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        key = handler.headers.get("Idempotency-Key", "")
        self.requests.append(("POST", handler.path, dict(handler.headers), body))
        if handler.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(handler, 401, self.error("MODEL_UNAUTHORIZED"))
        try:
            validate_schema(body["response_schema"])
        except RouterError:
            return self._send(handler, 400, self.error("MODEL_TASK_INVALID"))
        digest = sha256(raw).hexdigest()
        with self._lock:
            stored = self._stored.get(key)
            if stored is not None:
                if stored[0] != digest:
                    return self._send(handler, 409, self.error("LAMAD_IDEMPOTENCY_CONFLICT"))
                return self._send(handler, stored[1], stored[2], [("Idempotent-Replayed", "true")])
            status, payload = self.script.pop(0) if self.script else (200, None)
        if self.delay:
            time.sleep(self.delay)
        if status == 200 and payload is None:
            self.generations += 1
            try:
                validate_instance(self.output, body["response_schema"])
                payload = self.succeeded(body, self.output)
            except RouterError:
                status, payload = 502, self.error("MODEL_OUTPUT_INVALID")
        if status not in (202, 429) and isinstance(payload, dict):
            with self._lock:
                self._stored[key] = (digest, status, payload)
        return self._send(handler, status, payload)


@pytest.fixture
def fake_router(tmp_path):
    router = FakeRouter()
    token = tmp_path / "router-token"
    token.write_text(TOKEN + "\n", encoding="ascii")
    token.chmod(0o600)
    router.token_file = str(token)
    yield router
    router.close()
