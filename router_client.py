"""Native client for the Bezalel Platform Model Router (`POST /v1/model-tasks`).

Each call is one governed `structured_generation` task for the subject
`intelligence:podcast`: local only, `internal` data, temperature 0, a JSON
schema in the router's supported subset, and an `Idempotency-Key`. Transport is
mutual TLS to the router's Caddy listener plus a bearer token read from a file
on every call. The token never appears in argv, logs, or exceptions.

Failures become closed categories (`RouterError.category`):

    router_busy           429, 202 (same key still running), any 5xx other than
                          an output-validation 502 (including 503
                          MODEL_NO_ELIGIBLE_CANDIDATE: an open provider circuit or
                          an unavailable model), timeout, refused connection
    model_invalid_output  502 MODEL_OUTPUT_INVALID, or a 200 without usable output
    model_auth            401, 403, TLS handshake failure, unusable token file
    model_config          any other 4xx, or a request this client refuses to send,
                          including one too large for the route's context or
                          output budget (the router would store a 503
                          MODEL_NO_ELIGIBLE_CANDIDATE for it and replay it forever)
    model_mismatch        selected_model is not the expected model

The router stores every outcome it completes under the idempotency key and
replays it for the same key, including failures (503, 504, 502). `advance_key`
is False only when the router cannot have stored an outcome: 429, 202, a
transport failure, or a 5xx without a router error body (Caddy). The caller then
reuses the key, so a lost response is replayed rather than generated twice.
After any other failure the caller must use a new key.
"""

from dataclasses import dataclass
import json
import re
import uuid
from urllib.parse import urlsplit

import requests


PRINCIPAL = "intelligence:podcast"
CLASSIFICATION_ROUTE = "podcast.classification"
GUEST_ROUTE = "podcast.guests"
ROUTES = frozenset({CLASSIFICATION_ROUTE, GUEST_ROUTE})
CLASSIFICATION_CONTEXT_TOKENS = 32_768
CLASSIFICATION_MAX_OUTPUT_TOKENS = 8_192
GUEST_MAX_OUTPUT_TOKENS = 1_024
ROUTE_LIMITS = {  # (context tokens, max output tokens) of each route's one candidate (Plan 7a)
    CLASSIFICATION_ROUTE: (CLASSIFICATION_CONTEXT_TOKENS, CLASSIFICATION_MAX_OUTPUT_TOKENS),
    GUEST_ROUTE: (CLASSIFICATION_CONTEXT_TOKENS, GUEST_MAX_OUTPUT_TOKENS),
}
MAX_MESSAGE_CHARS = 131_072        # the router's validate_messages limit
MAX_REQUEST_BYTES = 1_048_576      # the router's default ROUTER_MAX_REQUEST_BYTES
MAX_RESPONSE_BYTES = 4_194_304
CONNECT_TIMEOUT_SECONDS = 10
READ_MARGIN_SECONDS = 30
READINESS_TIMEOUT_SECONDS = 10
CATEGORIES = {"router_busy": True, "model_invalid_output": True, "model_auth": False,
              "model_config": False, "model_mismatch": False}
_TASK_NAMESPACE = uuid.UUID("6f1c3f0e-5b8e-4f43-9a51-7c1d2e0b9a10")
_KEY = re.compile(r"[\x21-\x7e]{1,200}")
_TOKEN = re.compile(r"[A-Za-z0-9+/=._~-]{16,512}")
_SCHEMA_CONTRACT = ("Return exactly one JSON value that validates against the following "
                    "JSON Schema. Do not include Markdown, code fences, comments, or "
                    "explanatory text. JSON Schema: ")


class RouterError(RuntimeError):
    """Closed, payload-free error surface suitable for persistence."""

    def __init__(self, category, *, advance_key=True):
        self.category = category if category in CATEGORIES else "model_config"
        self.retryable = CATEGORIES[self.category]
        self.advance_key = bool(advance_key)
        super().__init__(self.category)


def _fail(category, *, advance_key=True):
    raise RouterError(category, advance_key=advance_key) from None


@dataclass(frozen=True, slots=True)
class RouterResult:
    parsed: object
    selected_model: str
    usage: dict
    replayed: bool = False


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def estimated_context_tokens(messages, schema, max_output_tokens):
    """The router's own context estimate: its schema contract joins the system message."""
    contract = _SCHEMA_CONTRACT + _canonical(schema)
    sent = list(messages)
    if sent and sent[0]["role"] == "system":
        sent[0] = {"role": "system", "content": f"{contract}\n\n{sent[0]['content']}"}
    else:
        sent.insert(0, {"role": "system", "content": contract})
    return max(1, sum(len(message["content"]) for message in sent) // 4 + len(sent) * 4) + max_output_tokens


def _reject_constant(_):
    raise ValueError("non-finite number")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json_object(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _read(response):
    body = bytearray()
    for chunk in response.iter_content(chunk_size=65_536):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
    return bytes(body)


def _error_code(body):
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _usage(value):
    usage = value if isinstance(value, dict) else {}
    return {name: usage[name] if type(usage.get(name)) is int and usage[name] >= 0 else 0
            for name in ("input_tokens", "output_tokens", "latency_ms")}


class RouterClient:
    """One router endpoint and credential set. Production URLs are https (config enforces it);
    tests use the plain-HTTP in-process fake router."""

    def __init__(self, base_url, *, token_file, expected_model, ca_file=None, cert_file=None,
                 key_file=None, read_timeout=None, session_factory=requests.Session):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("invalid router URL")
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.expected_model = expected_model
        self.verify = ca_file if ca_file else True
        self.cert = (cert_file, key_file) if cert_file and key_file else None
        self.read_timeout = read_timeout
        self.session_factory = session_factory

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.ROUTER_URL, token_file=settings.ROUTER_TOKEN_FILE,
                   expected_model=settings.ROUTER_EXPECTED_MODEL, ca_file=settings.ROUTER_CA_FILE,
                   cert_file=settings.ROUTER_CLIENT_CERT_FILE, key_file=settings.ROUTER_CLIENT_KEY_FILE)

    def _token(self):
        try:
            with open(self.token_file, encoding="ascii") as handle:
                token = handle.read(1024).strip()
        except (OSError, UnicodeError, TypeError, ValueError):
            _fail("model_auth")
        if not _TOKEN.fullmatch(token):
            _fail("model_auth")
        return token

    def token_available(self):
        try:
            self._token()
        except RouterError:
            return False
        return True

    def _session(self):
        session = self.session_factory()
        session.trust_env = False  # No proxies, netrc or CA overrides from the environment.
        return session

    def ready(self):
        """True only when GET /readyz answers 200 with status "ready" (mTLS, no bearer token).

        A 503 (for example Forge unavailable) or an unreachable router returns False
        within READINESS_TIMEOUT_SECONDS; it never hangs a startup check.
        """
        session = self._session()
        try:
            response = session.get(self.base_url + "/readyz", verify=self.verify, cert=self.cert,
                                   timeout=(CONNECT_TIMEOUT_SECONDS, READINESS_TIMEOUT_SECONDS),
                                   allow_redirects=False, stream=True)
            try:
                body = _json_object(_read(response)) if response.status_code == 200 else None
            finally:
                response.close()
        except (requests.RequestException, OSError, ValueError):
            return False
        finally:
            session.close()
        return isinstance(body, dict) and body.get("status") == "ready"

    def run(self, route, messages, schema, *, idempotency_key, prompt_version, max_output_tokens, deadline):
        if (route not in ROUTES or not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key)
                or not isinstance(prompt_version, str) or not 0 < len(prompt_version) <= 200
                or type(max_output_tokens) is not int or not 16 <= max_output_tokens <= 8192
                or type(deadline) is not int or not 5 <= deadline <= 900
                or not isinstance(schema, dict) or not isinstance(messages, list) or not messages
                or any(not isinstance(message, dict) or set(message) != {"role", "content"}
                       or not isinstance(message["content"], str) for message in messages)
                or sum(len(message["content"]) for message in messages) > MAX_MESSAGE_CHARS):
            _fail("model_config")
        body = {"task_id": str(uuid.uuid5(_TASK_NAMESPACE, idempotency_key)),
                "task_type": "structured_generation", "route": route, "principal": PRINCIPAL,
                "messages": messages, "prompt_version": prompt_version,
                "requirements": {"local_only": True, "max_latency_ms": deadline * 1000, "max_cost_usd": 0,
                                 "minimum_context_tokens": 2048, "required_modalities": ["text"],
                                 "data_classification": "internal"},
                "generation": {"temperature": 0, "max_output_tokens": max_output_tokens},
                "response_schema": schema}
        try:
            data = _canonical(body).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            _fail("model_config")
        if len(data) > MAX_REQUEST_BYTES:
            _fail("model_config")
        context_limit, output_limit = ROUTE_LIMITS[route]
        if (max_output_tokens > output_limit
                or estimated_context_tokens(messages, schema, max_output_tokens) > context_limit):
            _fail("model_config")  # The router's _select would never find a candidate.
        headers = {"Authorization": "Bearer " + self._token(), "Idempotency-Key": idempotency_key,
                   "Content-Type": "application/json", "Accept": "application/json"}
        timeout = (CONNECT_TIMEOUT_SECONDS, self.read_timeout or deadline + READ_MARGIN_SECONDS)
        session = self._session()
        try:
            try:
                response = session.post(self.base_url + "/v1/model-tasks", data=data, headers=headers,
                                        timeout=timeout, verify=self.verify, cert=self.cert,
                                        allow_redirects=False, stream=True)
            except requests.exceptions.SSLError:
                _fail("model_auth")
            except (requests.Timeout, requests.ConnectionError):
                _fail("router_busy", advance_key=False)
            except (requests.RequestException, OSError, ValueError):
                _fail("model_config")
            try:
                status = response.status_code
                raw = _read(response)
            except (requests.RequestException, OSError):
                _fail("router_busy", advance_key=False)
            except ValueError:
                _fail("model_invalid_output")
            finally:
                response.close()
            return self._outcome(status, _json_object(raw), response.headers)
        finally:
            session.close()

    def _outcome(self, status, body, headers):
        if status == 200:
            if (not isinstance(body, dict) or body.get("status") != "succeeded"
                    or not isinstance(body.get("selected_model"), str) or "parsed_output" not in body):
                _fail("model_invalid_output")
            if body["selected_model"] != self.expected_model:
                _fail("model_mismatch")
            return RouterResult(body["parsed_output"], body["selected_model"], _usage(body.get("usage")),
                                headers.get("Idempotent-Replayed") == "true")
        code = _error_code(body)
        if status in (202, 429):
            _fail("router_busy", advance_key=False)
        if status in (401, 403):
            _fail("model_auth")
        if status == 502 and code == "MODEL_OUTPUT_INVALID":
            _fail("model_invalid_output")
        if 500 <= status <= 599:
            _fail("router_busy", advance_key=code is not None)
        _fail("model_config")
