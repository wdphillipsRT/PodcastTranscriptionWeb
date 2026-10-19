"""Validated environment settings shared by web and worker processes."""

import os
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
