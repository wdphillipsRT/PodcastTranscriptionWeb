import os
from decimal import Decimal

import pytest

from config import ConfigError, Settings, load_settings


@pytest.fixture
def valid_environment():
    return {
        "SECRET_KEY": "test-only-key-do-not-use-in-production-" + "s" * 32,
        "DATABASE_URL": "postgresql+psycopg2://podcasts:test@postgres:5432/podcasts",
        "REDIS_URL": "redis://redis:6379/0",
        "AWS_REGION": "us-east-1",
        "AWS_S3_BUCKET_NAME": "podcast-transcriptions-web-2025",
        "YOUTUBE_API_KEY": "test-only-youtube-key",
    }


NUMERIC_DEFAULTS = {
    "MAX_CONTENT_LENGTH": 1_048_576,
    "MAX_AUDIO_BYTES": 536_870_912,
    "RSS_MAX_BYTES": 10_485_760,
    "YOUTUBE_CAPTION_MAX_BYTES": 52_428_800,
    "HTTP_CONNECT_TIMEOUT_SECONDS": 10,
    "HTTP_READ_TIMEOUT_SECONDS": 60,
    "PODCAST_INITIAL_ITEM_LIMIT": 25,
    "YOUTUBE_INITIAL_ITEM_LIMIT": 25,
    "DISCOVERY_INTERVAL_SECONDS": 3600,
    "YOUTUBE_MAX_PAGES_PER_TASK": 5,
    "YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK": 100,
    "BACKFILL_REQUEST_MAX_ITEMS": 5000,
    "YTDLP_TIMEOUT_SECONDS": 1800,
    "TRANSCRIBE_POLL_SECONDS": 10,
    "TRANSCRIBE_TIMEOUT_SECONDS": 14_400,
    "CLAUDE_CLASSIFIER_TIMEOUT_SECONDS": 180,
    "CLAUDE_CLASSIFIER_MAX_REVISION_SECONDS": 7200,
    "CLAUDE_CLASSIFIER_MAX_OUTPUT_BYTES": 2_097_152,
    "CLASSIFICATION_RECONCILE_INTERVAL_SECONDS": 300,
    "CLASSIFICATION_RECONCILE_BATCH_SIZE": 100,
}


def test_load_settings_returns_flask_compatible_settings(valid_environment):
    settings = load_settings(valid_environment)
    assert isinstance(settings, Settings)
    for name, value in valid_environment.items():
        assert getattr(settings, name) == value
    assert settings.SQLALCHEMY_DATABASE_URI == valid_environment["DATABASE_URL"]
    assert settings.SQLALCHEMY_TRACK_MODIFICATIONS is False


@pytest.mark.parametrize("name,expected", NUMERIC_DEFAULTS.items())
def test_numeric_defaults(valid_environment, name, expected):
    assert getattr(load_settings(valid_environment), name) == expected


def test_caption_and_runtime_version_defaults(valid_environment):
    settings = load_settings(valid_environment)
    assert settings.YOUTUBE_PREFERRED_CAPTION_LANGUAGES == ("en.*", "en")
    assert settings.YTDLP_EXPECTED_VERSION == "2026.08.19"
    assert settings.DENO_EXPECTED_VERSION == "2.9.7"


@pytest.mark.parametrize("name", ["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "AWS_REGION", "AWS_S3_BUCKET_NAME", "YOUTUBE_API_KEY"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_rejects_missing_required_values(valid_environment, name, value):
    if value is None:
        valid_environment.pop(name)
    else:
        valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("secret", ["dev", "secret", "changeme", "change-me", "your-secret-key", "your-secret-key-here", "dev-secret-key", "dev-key-change-in-production"])
def test_rejects_known_development_secrets(valid_environment, secret):
    valid_environment["SECRET_KEY"] = secret
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_settings(valid_environment)


def test_rejects_other_buckets(valid_environment):
    valid_environment["AWS_S3_BUCKET_NAME"] = "wrong-bucket"
    with pytest.raises(ConfigError, match="podcast-transcriptions-web-2025"):
        load_settings(valid_environment)


@pytest.mark.parametrize("name,value", [("DATABASE_URL", "not-a-url"), ("DATABASE_URL", "https://postgres/db"), ("REDIS_URL", "redis://"), ("REDIS_URL", "https://redis:6379"), ("RATELIMIT_STORAGE_URI", "not-a-url")])
def test_rejects_invalid_service_urls(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("name", NUMERIC_DEFAULTS)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many", ""])
def test_rejects_nonpositive_or_invalid_integers(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


def test_numeric_overrides_are_parsed(valid_environment):
    valid_environment.update({name: "17" for name in NUMERIC_DEFAULTS})
    settings = load_settings(valid_environment)
    assert all(getattr(settings, name) == 17 for name in NUMERIC_DEFAULTS)


def test_secure_cookie_defaults(valid_environment):
    settings = load_settings(valid_environment)
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.REMEMBER_COOKIE_SECURE is True
    assert settings.REMEMBER_COOKIE_HTTPONLY is True
    assert settings.REMEMBER_COOKIE_SAMESITE == "Lax"


@pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), ("false", False), ("FALSE", False)])
def test_secure_cookie_can_be_configured_explicitly(valid_environment, value, expected):
    valid_environment["SESSION_COOKIE_SECURE"] = value
    settings = load_settings(valid_environment)
    assert settings.SESSION_COOKIE_SECURE is expected
    assert settings.REMEMBER_COOKIE_SECURE is expected


@pytest.mark.parametrize("value", ["yes", "1", "", "maybe"])
def test_rejects_invalid_boolean(valid_environment, value):
    valid_environment["SESSION_COOKIE_SECURE"] = value
    with pytest.raises(ConfigError, match="SESSION_COOKIE_SECURE"):
        load_settings(valid_environment)


def test_backfill_task_cannot_exceed_request_bound(valid_environment):
    valid_environment["YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK"] = "5001"
    with pytest.raises(ConfigError, match="YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK"):
        load_settings(valid_environment)


def test_caption_languages_can_be_configured(valid_environment):
    valid_environment["YOUTUBE_PREFERRED_CAPTION_LANGUAGES"] = " fr.*, fr , en "
    assert load_settings(valid_environment).YOUTUBE_PREFERRED_CAPTION_LANGUAGES == ("fr.*", "fr", "en")


@pytest.mark.parametrize("name", ["YOUTUBE_PREFERRED_CAPTION_LANGUAGES", "YTDLP_EXPECTED_VERSION", "DENO_EXPECTED_VERSION"])
def test_rejects_empty_runtime_settings(valid_environment, name):
    valid_environment[name] = "  "
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


def test_rate_limit_storage_uses_separate_redis_database(valid_environment):
    valid_environment["REDIS_URL"] = "rediss://user:password@redis:6379/7?ssl_cert_reqs=required"
    settings = load_settings(valid_environment)
    assert settings.RATELIMIT_STORAGE_URI == "rediss://user:password@redis:6379/1?ssl_cert_reqs=required"
    assert settings.RATELIMIT_LOGIN == "5 per minute"
    assert settings.TRANSCRIBE_LANGUAGE_CODE == "en-US"


def test_rate_limit_storage_can_be_overridden(valid_environment):
    valid_environment["RATELIMIT_STORAGE_URI"] = "redis://limiter:6379/3"
    assert load_settings(valid_environment).RATELIMIT_STORAGE_URI == "redis://limiter:6379/3"


def test_injected_mapping_is_isolated_from_process_environment(valid_environment, monkeypatch):
    monkeypatch.setenv("YTDLP_TIMEOUT_SECONDS", "0")
    before = dict(os.environ)
    provided = dict(valid_environment)
    assert load_settings(valid_environment).YTDLP_TIMEOUT_SECONDS == 1800
    assert valid_environment == provided
    assert dict(os.environ) == before


def test_load_settings_reads_process_environment_by_default(valid_environment, monkeypatch):
    monkeypatch.setattr(os, "environ", valid_environment)
    assert load_settings().SECRET_KEY == valid_environment["SECRET_KEY"]


def test_classifier_defaults(valid_environment):
    settings = load_settings(valid_environment)
    assert settings.CLAUDE_CLASSIFIER_MODEL == "claude-sonnet-5-5"
    assert settings.CLAUDE_EXPECTED_VERSION == "2.1.303"
    assert settings.CLAUDE_CLASSIFIER_MAX_BUDGET_USD == Decimal("0.50")
    assert settings.CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD == Decimal("5.00")
    assert settings.CLASSIFICATION_ENABLED is False


@pytest.mark.parametrize("name", ["CLAUDE_CLASSIFIER_MAX_BUDGET_USD", "CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD"])
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity", "", "secret", "1_0", "1e-2", "+1", "01", ".5", "1.", "١"])
def test_classifier_rejects_invalid_budgets(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("name,value", [
    ("CLAUDE_CLASSIFIER_MODEL", "sonnet"), ("CLAUDE_CLASSIFIER_MODEL", "claude-sonnet-latest"),
    ("CLAUDE_CLASSIFIER_MODEL", "claude-sonnet-5"), ("CLAUDE_CLASSIFIER_MODEL", "claude-latest-5-5"),
    ("CLAUDE_EXPECTED_VERSION", "2.1"), ("CLAUDE_EXPECTED_VERSION", "v2.1.303"),
    ("CLAUDE_CLASSIFIER_MAX_OUTPUT_BYTES", "2097153"),
    ("CLASSIFICATION_RECONCILE_BATCH_SIZE", "501"),
    ("CLASSIFICATION_ENABLED", "yes"), ("CLASSIFICATION_ENABLED", "1"),
    ("CLASSIFICATION_ENABLED", ""),
])
def test_classifier_rejects_invalid_settings(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), ("false", False), ("FALSE", False)])
def test_classifier_boolean(valid_environment, value, expected):
    valid_environment["CLASSIFICATION_ENABLED"] = value
    assert load_settings(valid_environment).CLASSIFICATION_ENABLED is expected


def test_classifier_accepts_pinned_snapshot_and_decimal_budget(valid_environment):
    valid_environment.update(CLAUDE_CLASSIFIER_MODEL="claude-sonnet-5-5-20261019",
                             CLAUDE_EXPECTED_VERSION="12.34.567", CLAUDE_CLASSIFIER_MAX_BUDGET_USD="0.123456789")
    settings = load_settings(valid_environment)
    assert settings.CLAUDE_CLASSIFIER_MODEL == "claude-sonnet-5-5-20261019"
    assert settings.CLAUDE_EXPECTED_VERSION == "12.34.567"
    assert settings.CLAUDE_CLASSIFIER_MAX_BUDGET_USD == Decimal("0.123456789")


@pytest.mark.parametrize("name,value,match", [
    ("CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD", "0.49", "CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD"),
    ("CLAUDE_CLASSIFIER_MAX_BUDGET_USD", "5.01", "CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD"),
    ("CLAUDE_CLASSIFIER_MAX_REVISION_SECONDS", "179", "CLAUDE_CLASSIFIER_MAX_REVISION_SECONDS"),
    ("CLAUDE_CLASSIFIER_TIMEOUT_SECONDS", "7201", "CLAUDE_CLASSIFIER_MAX_REVISION_SECONDS"),
])
def test_classifier_revision_limits_cover_one_call(valid_environment, name, value, match):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=match):
        load_settings(valid_environment)


def test_router_defaults(valid_environment):
    settings = load_settings(valid_environment)
    assert (settings.ROUTER_URL, settings.ROUTER_EXPECTED_MODEL) == ("https://192.168.68.200:18443", "bonsai2:27b-pq2_0")
    assert (settings.ROUTER_TOKEN_FILE, settings.ROUTER_CA_FILE, settings.ROUTER_CLIENT_CERT_FILE,
            settings.ROUTER_CLIENT_KEY_FILE) == ("/router/token", "/router/router-ca.crt", "/router/client.crt",
                                                 "/router/client.key")
    assert (settings.CLASSIFIER_TIMEOUT_SECONDS, settings.CLASSIFIER_MAX_REVISION_SECONDS,
            settings.CLASSIFIER_MAX_OUTPUT_BYTES, settings.GUEST_EXTRACTION_MAX_CALLS_PER_DAY) == (
        540, 14_400, 2_097_152, 200)


def test_router_url_trailing_slash_is_dropped(valid_environment):
    valid_environment["ROUTER_URL"] = "https://router.internal:8443/"
    assert load_settings(valid_environment).ROUTER_URL == "https://router.internal:8443"


@pytest.mark.parametrize("value", ["https://router.internal", "https://192.168.68.200:18443",
                                   "https://[::1]:8443", "https://[fd00::1]", "https://[::ffff:192.168.0.1]/"])
def test_router_url_accepts_host_and_bracketed_ipv6_origins(valid_environment, value):
    valid_environment["ROUTER_URL"] = value
    assert load_settings(valid_environment).ROUTER_URL == value.rstrip("/")


@pytest.mark.parametrize("name,value", [
    ("ROUTER_URL", "http://192.168.68.200:18443"), ("ROUTER_URL", "https://user:secret@192.168.68.200:18443"),
    ("ROUTER_URL", "https://192.168.68.200:18443/v1"), ("ROUTER_URL", "https://192.168.68.200:18443?x=1"),
    ("ROUTER_URL", "https://192.168.68.200:99999"), ("ROUTER_URL", ""),
    ("ROUTER_URL", "https://host?"), ("ROUTER_URL", "https://host#"), ("ROUTER_URL", "https://ho st"),
    ("ROUTER_URL", "https://[::1"), ("ROUTER_URL", "https://host\t:8443"), ("ROUTER_URL", "https://host;x"),
    ("ROUTER_URL", "https://host:"), ("ROUTER_URL", "https://host:/"), ("ROUTER_URL", "https://host:8443:1"),
    ("ROUTER_URL", "https://ho[st]"), ("ROUTER_URL", "https://[::1]x"), ("ROUTER_URL", "https://host:123456"),
    ("ROUTER_EXPECTED_MODEL", "bonsai2"), ("ROUTER_EXPECTED_MODEL", "bonsai2:latest"),
    ("ROUTER_EXPECTED_MODEL", "Bonsai2:27b"),
    ("ROUTER_TOKEN_FILE", "router/token"), ("ROUTER_CA_FILE", "/router/../etc/ca.crt"),
    ("ROUTER_CLIENT_CERT_FILE", ""), ("ROUTER_CLIENT_KEY_FILE", "relative.key"),
    ("CLASSIFIER_TIMEOUT_SECONDS", "4"), ("CLASSIFIER_TIMEOUT_SECONDS", "901"),
    ("CLASSIFIER_MAX_REVISION_SECONDS", "539"), ("CLASSIFIER_MAX_OUTPUT_BYTES", "2097153"),
    ("GUEST_EXTRACTION_MAX_CALLS_PER_DAY", "2401"), ("GUEST_EXTRACTION_MAX_CALLS_PER_DAY", "0"),
])
def test_router_settings_are_validated(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)
