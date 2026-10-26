"""In-process stand-in for the Platform Model Router HTTP surface (plain HTTP on loopback).

It follows the order of the router's http.py `do_POST` and service.py `execute`
(bezalel-deploy 4bcf4ae): 401 for a wrong bearer token; 404 off
`/v1/model-tasks`; 400 `LAMAD_IDEMPOTENCY_REQUIRED` for a missing, empty,
over-200-character or non-printable (outside 33..126) Idempotency-Key; 415, 411,
413 or 400 `MODEL_TASK_INVALID` for a bad Content-Type, Content-Length or body;
400 for a task without `messages`, `generation.max_output_tokens` (16..8192) or a
`response_schema` in the pinned subset. None of those is stored. Then, under the
key: 409 when it returns with a different body, the stored outcome replayed
(failures included, with `Idempotent-Replayed: true`), or 202 `in_progress`
(also `Idempotent-Replayed: true`) while the same key is still running. A task
whose `estimate_tokens(provider_messages(...)) + max_output_tokens` exceeds
`context_tokens`, or whose `max_output_tokens` exceeds `max_output_tokens`, gets
a stored 503 `MODEL_NO_ELIGIBLE_CANDIDATE` like the router's `_select`/`_fail`,
without generating or using a scripted reply. Scripted replies of 429, 202, or
a proxy-style body without a router error are not stored. Only the task fields
the fake uses are checked; the rest of `normalize_model_task` is not modelled,
and neither is TLS.
"""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit
import uuid

import pytest

from tests.router_schema_validator import (RouterError, canonical, estimate_tokens, provider_messages,
                                           validate_instance, validate_schema)

TOKEN = "fake-router-token-0123456789abcdef"
MODEL = "bonsai2:27b-pq2_0"
TRACE_ID = "00000000-0000-0000-0000-000000000000"
MAX_REQUEST_BYTES = 1_048_576  # the router's default ROUTER_MAX_REQUEST_BYTES


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
        self.context_tokens = 32_768    # the podcast.classification candidate's context
        self.max_output_tokens = 8_192  # the candidate's output budget
        self._stored = {}               # key -> (body sha256, status or None while running, payload or invocation id)
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
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
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
                          "trace_id": TRACE_ID}}

    def succeeded(self, body, parsed):
        return {"task_id": body.get("task_id"), "status": "succeeded", "selected_model": self.model,
                "model_revision": "descriptor-sha256:" + "0" * 64, "content": canonical(parsed),
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cost_usd": 0,
                          "latency_ms": 1500, "finish_reason": "stop"},
                "routing_decision_id": "route-1", "trace_id": TRACE_ID,
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

    @staticmethod
    def _key_is_valid(key):
        return bool(key) and len(key) <= 200 and all(33 <= ord(character) <= 126 for character in key)

    def _task(self, body):
        """The task fields the fake uses, checked like normalize_model_task; returns (messages, max_output_tokens)."""
        if not isinstance(body, dict):
            raise RouterError("MODEL_TASK_INVALID", "Request body must be a JSON object")
        messages = body.get("messages")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 64
                or any(not isinstance(message, dict) or not isinstance(message.get("role"), str)
                       or not isinstance(message.get("content"), str) for message in messages)):
            raise RouterError("MODEL_TASK_INVALID", "messages must contain 1-64 entries")
        generation = body.get("generation")
        tokens = generation.get("max_output_tokens") if isinstance(generation, dict) else None
        if type(tokens) is not int or not 16 <= tokens <= 8192:
            raise RouterError("MODEL_TASK_INVALID", "max_output_tokens is out of range")
        if body.get("response_schema") is None:
            raise RouterError("MODEL_OUTPUT_SCHEMA_REQUIRED", "This route requires a response schema")
        validate_schema(body["response_schema"])
        return messages, tokens

    def _post(self, handler):
        raw, length_error = None, None
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            length_error = 411
        else:
            if 2 <= length <= MAX_REQUEST_BYTES:
                raw = handler.rfile.read(length)
            else:
                length_error = 413
        try:
            body = json.loads(raw) if raw is not None else None
        except (ValueError, UnicodeDecodeError):
            body = None
        key = handler.headers.get("Idempotency-Key", "")
        self.requests.append(("POST", handler.path, dict(handler.headers), body))
        if handler.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(handler, 401, self.error("MODEL_UNAUTHORIZED"))
        if urlsplit(handler.path).path != "/v1/model-tasks":
            return self._send(handler, 404, self.error("MODEL_ENDPOINT_NOT_FOUND"))
        if not self._key_is_valid(key):
            return self._send(handler, 400, self.error("LAMAD_IDEMPOTENCY_REQUIRED"))
        content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return self._send(handler, 415, self.error("MODEL_TASK_INVALID"))
        if length_error is not None:
            return self._send(handler, length_error, self.error("MODEL_TASK_INVALID"))
        try:
            messages, max_output_tokens = self._task(body)
        except RouterError as error:
            return self._send(handler, error.status, self.error(error.code))
        context = estimate_tokens(provider_messages(messages, body["response_schema"])) + max_output_tokens
        eligible = context <= self.context_tokens and max_output_tokens <= self.max_output_tokens
        digest = sha256(raw).hexdigest()
        with self._lock:
            stored = self._stored.get(key)
            if stored is not None:
                if stored[0] != digest:
                    return self._send(handler, 409, self.error("LAMAD_IDEMPOTENCY_CONFLICT"))
                if stored[1] is None:
                    return self._send(handler, 202, {"status": "in_progress", "invocation_id": stored[2],
                                                     "trace_id": TRACE_ID}, [("Idempotent-Replayed", "true")])
                return self._send(handler, stored[1], stored[2], [("Idempotent-Replayed", "true")])
            if not eligible:
                status, payload = 503, self.error("MODEL_NO_ELIGIBLE_CANDIDATE")
                self._stored[key] = (digest, status, payload)
            else:
                self._stored[key] = (digest, None, f"inv-{uuid.uuid4().hex}")
                status, payload = self.script.pop(0) if self.script else (200, None)
                if status == 200 and payload is None:
                    self.generations += 1
        if not eligible:
            return self._send(handler, status, payload)
        if self.delay:
            time.sleep(self.delay)
        if status == 200 and payload is None:
            try:
                validate_instance(self.output, body["response_schema"])
                payload = self.succeeded(body, self.output)
            except RouterError:
                status, payload = 502, self.error("MODEL_OUTPUT_INVALID")
        with self._lock:
            if status not in (202, 429) and isinstance(payload, dict):
                self._stored[key] = (digest, status, payload)
            else:
                del self._stored[key]
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
