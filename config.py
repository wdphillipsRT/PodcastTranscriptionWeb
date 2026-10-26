"""Validated environment settings shared by web and worker processes."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """A deployment setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    SECRET_KEY: str
    DATABASE_URL: str
    SQLALCHEMY_DATABASE_URI: str
    REDIS_URL: str
    RATELIMIT_STORAGE_URI: str
    AWS_REGION: str
    AWS_S3_BUCKET_NAME: str
    YOUTUBE_API_KEY: str
    SESSION_COOKIE_SECURE: bool = True
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"
    REMEMBER_COOKIE_SECURE: bool = True
    REMEMBER_COOKIE_HTTPONLY: bool = True
    REMEMBER_COOKIE_SAMESITE: str = "Lax"
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    MAX_CONTENT_LENGTH: int = 1_048_576
    MAX_AUDIO_BYTES: int = 536_870_912
    RSS_MAX_BYTES: int = 10_485_760
    YOUTUBE_CAPTION_MAX_BYTES: int = 52_428_800
    HTTP_CONNECT_TIMEOUT_SECONDS: int = 10
    HTTP_READ_TIMEOUT_SECONDS: int = 60
    PODCAST_INITIAL_ITEM_LIMIT: int = 25
    YOUTUBE_INITIAL_ITEM_LIMIT: int = 25
    DISCOVERY_INTERVAL_SECONDS: int = 3600
    YOUTUBE_MAX_PAGES_PER_TASK: int = 5
    YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK: int = 100
    BACKFILL_REQUEST_MAX_ITEMS: int = 5000
    YOUTUBE_PREFERRED_CAPTION_LANGUAGES: tuple[str, ...] = ("en.*", "en")
    YTDLP_EXPECTED_VERSION: str = "2026.08.19"
    DENO_EXPECTED_VERSION: str = "2.9.7"
    YTDLP_TIMEOUT_SECONDS: int = 1800
    TRANSCRIBE_POLL_SECONDS: int = 10
    TRANSCRIBE_TIMEOUT_SECONDS: int = 14_400
    TRANSCRIBE_LANGUAGE_CODE: str = "en-US"
    RATELIMIT_DEFAULT: str = ""
    RATELIMIT_LOGIN: str = "5 per minute"
    LOG_LEVEL: str = "INFO"
    ROUTER_URL: str = "https://192.168.68.200:18443"
    ROUTER_EXPECTED_MODEL: str = "bonsai2:27b-pq2_0"
    ROUTER_TOKEN_FILE: str = "/router/token"
    ROUTER_CA_FILE: str = "/router/router-ca.crt"
    ROUTER_CLIENT_CERT_FILE: str = "/router/client.crt"
    ROUTER_CLIENT_KEY_FILE: str = "/router/client.key"
    CLASSIFIER_TIMEOUT_SECONDS: int = 540
    CLASSIFIER_MAX_REVISION_SECONDS: int = 14_400
    CLASSIFIER_MAX_OUTPUT_BYTES: int = 2_097_152
    CLASSIFICATION_ENABLED: bool = False
    CLASSIFICATION_RECONCILE_INTERVAL_SECONDS: int = 300
    CLASSIFICATION_RECONCILE_BATCH_SIZE: int = 100
    GUEST_EXTRACTION_ENABLED: bool = False
    GUEST_EXTRACTION_TIMEOUT_SECONDS: int = 120
    GUEST_EXTRACTION_MAX_ITEMS_PER_RUN: int = 25
    GUEST_EXTRACTION_INTERVAL_SECONDS: int = 3600
    GUEST_EXTRACTION_MAX_CALLS_PER_DAY: int = 200
    KNOWLEDGE_EXPORT_ENABLED: bool = False
    KNOWLEDGE_EXPORT_INBOX: str = "/spool/podcast/inbox"
    KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN: int = 5
    KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH: int = 10_000


_REQUIRED = (
    "SECRET_KEY", "DATABASE_URL", "REDIS_URL", "AWS_REGION",
    "AWS_S3_BUCKET_NAME", "YOUTUBE_API_KEY",
)
_DEVELOPMENT_SECRETS = {
    "dev", "secret", "changeme", "change-me", "change-me-in-production",
    "your-secret-key", "your-secret-key-here", "dev-secret", "dev-secret-key",
    "development", "development-secret", "development-secret-key",
    "dev-key-change-in-production",
}
# Settings retired with the Claude CLI. Present in the environment, they stop startup, so a
# stale .env cannot leave a Claude model or a dollar budget configured that nothing enforces.
RETIRED_SETTINGS = (
    "CLAUDE_CLASSIFIER_MODEL", "CLAUDE_EXPECTED_VERSION", "CLAUDE_CLASSIFIER_TIMEOUT_SECONDS",
    "CLAUDE_CLASSIFIER_MAX_BUDGET_USD", "CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD",
    "CLAUDE_CLASSIFIER_MAX_REVISION_SECONDS", "CLAUDE_CLASSIFIER_MAX_OUTPUT_BYTES", "CLAUDE_GUEST_MODEL",
    "CLAUDE_CONFIG_HOST_PATH", "GUEST_EXTRACTION_MAX_BUDGET_USD", "GUEST_EXTRACTION_DAILY_BUDGET_USD",
)


def _nonempty(environment: Mapping[str, str], name: str, default: str = "") -> str:
    value = environment.get(name, default).strip()
    if not value:
        raise ConfigError(f"{name} must be set and nonempty")
    return value


def _service_url(value: str, name: str, schemes: set[str]) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in schemes or not parsed.hostname:
            raise ValueError
        # Validate a supplied port without exposing credentials in the error.
        parsed.port
    except ValueError:
        raise ConfigError(f"{name} must be a valid service URL") from None
    return value


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    """Load settings without merging or mutating an explicitly supplied mapping.

    Tests can inject synthetic credentials. The same validation applies to every
    caller, so test execution never silently weakens production configuration.
    """
    environment = os.environ if environment is None else environment
    retired = sorted(name for name in RETIRED_SETTINGS if name in environment)
    if retired:
        raise ConfigError("Retired settings must be removed from the environment: " + ", ".join(retired))
    values = {name: _nonempty(environment, name) for name in _REQUIRED}
    if values["SECRET_KEY"].lower() in _DEVELOPMENT_SECRETS:
        raise ConfigError("SECRET_KEY must not be a known development secret")
    if values["AWS_S3_BUCKET_NAME"] != "podcast-transcriptions-web-2025":
        raise ConfigError("AWS_S3_BUCKET_NAME must be podcast-transcriptions-web-2025")
    values["DATABASE_URL"] = _service_url(
        values["DATABASE_URL"], "DATABASE_URL", {"postgresql", "postgresql+psycopg2"}
    )
    values["SQLALCHEMY_DATABASE_URI"] = values["DATABASE_URL"]
    values["REDIS_URL"] = _service_url(values["REDIS_URL"], "REDIS_URL", {"redis", "rediss"})
    redis = urlsplit(values["REDIS_URL"])
    default_limiter_url = urlunsplit(redis._replace(path="/1"))
    values["RATELIMIT_STORAGE_URI"] = _service_url(
        _nonempty(environment, "RATELIMIT_STORAGE_URI", default_limiter_url),
        "RATELIMIT_STORAGE_URI", {"redis", "rediss"},
    )

    for field in fields(Settings):
        if field.type is int:
            raw = environment.get(field.name, str(field.default)).strip()
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{field.name} must be a positive integer") from None
            if value <= 0:
                raise ConfigError(f"{field.name} must be a positive integer")
            values[field.name] = value

    if values["YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK"] > values["BACKFILL_REQUEST_MAX_ITEMS"]:
        raise ConfigError("YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK must not exceed BACKFILL_REQUEST_MAX_ITEMS")

    if values["CLASSIFICATION_RECONCILE_BATCH_SIZE"] > 500:
        raise ConfigError("CLASSIFICATION_RECONCILE_BATCH_SIZE must not exceed 500")
    if values["GUEST_EXTRACTION_MAX_ITEMS_PER_RUN"] > 100:
        raise ConfigError("GUEST_EXTRACTION_MAX_ITEMS_PER_RUN must not exceed 100")
    if not 5 <= values["GUEST_EXTRACTION_TIMEOUT_SECONDS"] <= 600:
        raise ConfigError("GUEST_EXTRACTION_TIMEOUT_SECONDS must be between 5 and 600")
    if values["GUEST_EXTRACTION_INTERVAL_SECONDS"] < 3600:
        raise ConfigError("GUEST_EXTRACTION_INTERVAL_SECONDS must be at least 3600")
    router_url = _nonempty(environment, "ROUTER_URL", Settings.ROUTER_URL)
    origin_error = "ROUTER_URL must be an https origin without credentials or a path"
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    if not re.fullmatch(rf"https://(?:{label}(?:\.{label})*|\[[0-9A-Fa-f:.]+\])(?::\d{{1,5}})?/?", router_url):
        raise ConfigError(origin_error)  # Also rejects '?', '#', whitespace and userinfo.
    try:
        parsed = urlsplit(router_url)
        port_valid = parsed.port is None or parsed.port > 0
    except ValueError:  # e.g. an unclosed IPv6 bracket or an out-of-range port
        raise ConfigError(origin_error) from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not port_valid):
        raise ConfigError(origin_error)
    values["ROUTER_URL"] = router_url.rstrip("/")
    model = _nonempty(environment, "ROUTER_EXPECTED_MODEL", Settings.ROUTER_EXPECTED_MODEL)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}:(?!latest$)[a-z0-9][a-z0-9._-]{0,63}", model):
        raise ConfigError("ROUTER_EXPECTED_MODEL must be a pinned model tag")
    values["ROUTER_EXPECTED_MODEL"] = model
    for name in ("ROUTER_TOKEN_FILE", "ROUTER_CA_FILE", "ROUTER_CLIENT_CERT_FILE", "ROUTER_CLIENT_KEY_FILE"):
        path = _nonempty(environment, name, getattr(Settings, name))
        if not path.startswith("/") or "\x00" in path or "/../" in path + "/":
            raise ConfigError(f"{name} must be an absolute path")
        values[name] = path
    if not 5 <= values["CLASSIFIER_TIMEOUT_SECONDS"] <= 900:
        raise ConfigError("CLASSIFIER_TIMEOUT_SECONDS must be between 5 and 900")
    if values["CLASSIFIER_MAX_REVISION_SECONDS"] < values["CLASSIFIER_TIMEOUT_SECONDS"]:
        raise ConfigError("CLASSIFIER_MAX_REVISION_SECONDS must cover the per-call timeout")
    if values["CLASSIFIER_MAX_OUTPUT_BYTES"] > 2_097_152:
        raise ConfigError("CLASSIFIER_MAX_OUTPUT_BYTES must not exceed 2097152")
    if values["GUEST_EXTRACTION_MAX_CALLS_PER_DAY"] > 2400:
        raise ConfigError("GUEST_EXTRACTION_MAX_CALLS_PER_DAY must not exceed 2400")
    enabled = environment.get("CLASSIFICATION_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise ConfigError("CLASSIFICATION_ENABLED must be true or false")
    values["CLASSIFICATION_ENABLED"] = enabled == "true"
    enabled = environment.get("GUEST_EXTRACTION_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise ConfigError("GUEST_EXTRACTION_ENABLED must be true or false")
    values["GUEST_EXTRACTION_ENABLED"] = enabled == "true"
    enabled = environment.get("KNOWLEDGE_EXPORT_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise ConfigError("KNOWLEDGE_EXPORT_ENABLED must be true or false")
    values["KNOWLEDGE_EXPORT_ENABLED"] = enabled == "true"
    inbox = _nonempty(environment, "KNOWLEDGE_EXPORT_INBOX", Settings.KNOWLEDGE_EXPORT_INBOX)
    if not inbox.startswith("/") or "\x00" in inbox or "/../" in inbox + "/":
        raise ConfigError("KNOWLEDGE_EXPORT_INBOX must be an absolute path")
    values["KNOWLEDGE_EXPORT_INBOX"] = inbox
    if values["KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH"] > 10_000:
        raise ConfigError("KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH must not exceed 10000")
    if values["KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN"] > 999:
        raise ConfigError("KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN must not exceed 999")

    secure = environment.get("SESSION_COOKIE_SECURE", "true").strip().lower()
    if secure not in {"true", "false"}:
        raise ConfigError("SESSION_COOKIE_SECURE must be true or false")
    values["SESSION_COOKIE_SECURE"] = secure == "true"
    values["REMEMBER_COOKIE_SECURE"] = secure == "true"
    languages = _nonempty(environment, "YOUTUBE_PREFERRED_CAPTION_LANGUAGES", "en.*,en")
    values["YOUTUBE_PREFERRED_CAPTION_LANGUAGES"] = tuple(part.strip() for part in languages.split(",") if part.strip())
    if not values["YOUTUBE_PREFERRED_CAPTION_LANGUAGES"]:
        raise ConfigError("YOUTUBE_PREFERRED_CAPTION_LANGUAGES must contain a language")
    for name in ("YTDLP_EXPECTED_VERSION", "DENO_EXPECTED_VERSION", "TRANSCRIBE_LANGUAGE_CODE", "RATELIMIT_LOGIN", "LOG_LEVEL"):
        values[name] = _nonempty(environment, name, getattr(Settings, name))
    values["RATELIMIT_DEFAULT"] = environment.get("RATELIMIT_DEFAULT", "").strip()
    return Settings(**values)
