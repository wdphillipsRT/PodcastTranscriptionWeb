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

WHISPER_DEFAULTS = {
    "TRANSCRIPTION_PRIMARY": "amazon_transcribe",
    "FASTER_WHISPER_EXPECTED_VERSION": "1.2.1",
    "WHISPER_MODEL": "medium.en",
    "WHISPER_MODEL_REPOSITORY": "Systran/faster-whisper-medium.en",
    "WHISPER_MODEL_REVISION": "a29b04bd15381511a9af671baec01072039215e3",
    "WHISPER_COMPUTE_TYPE": "int8",
    "WHISPER_CPU_THREADS": 10,
    "WHISPER_MAX_ATTEMPTS": 3,
    "WHISPER_SOFT_TIME_LIMIT_SECONDS": 21600,
    "WHISPER_HARD_TIME_LIMIT_SECONDS": 21900,
    "WHISPER_MAX_AUDIO_DURATION_SECONDS": 21600,
}


@pytest.mark.parametrize("name,expected", WHISPER_DEFAULTS.items())
def test_whisper_defaults(valid_environment, name, expected):
    assert getattr(load_settings(valid_environment), name) == expected


@pytest.mark.parametrize("primary", ["amazon_transcribe", "faster_whisper"])
def test_transcription_primary_accepts_only_explicit_supported_providers(valid_environment, primary):
    valid_environment["TRANSCRIPTION_PRIMARY"] = primary
    assert load_settings(valid_environment).TRANSCRIPTION_PRIMARY == primary


@pytest.mark.parametrize("name,value", [
    ("TRANSCRIPTION_PRIMARY", "faster-whisper"),
    ("TRANSCRIPTION_PRIMARY", "Amazon_Transcribe"),
    ("TRANSCRIPTION_PRIMARY", "other"),
    ("FASTER_WHISPER_EXPECTED_VERSION", "1.2.0"),
    ("WHISPER_MODEL", "large-v3"),
    ("WHISPER_MODEL_REPOSITORY", "other/faster-whisper-medium.en"),
    ("WHISPER_MODEL_REVISION", "main"),
    ("WHISPER_MODEL_REVISION", "0" * 40),
    ("WHISPER_COMPUTE_TYPE", "float32"),
    ("WHISPER_CPU_THREADS", "11"),
    ("WHISPER_MAX_ATTEMPTS", "2"),
    ("WHISPER_MAX_ATTEMPTS", "4"),
    ("WHISPER_MAX_AUDIO_DURATION_SECONDS", "21601"),
])
def test_whisper_rejects_unapproved_settings(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("name", [name for name, default in WHISPER_DEFAULTS.items() if isinstance(default, str)])
def test_whisper_rejects_empty_identity_settings(valid_environment, name):
    valid_environment[name] = "   "
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("name", [name for name, default in WHISPER_DEFAULTS.items() if isinstance(default, int)])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many", ""])
def test_whisper_rejects_nonpositive_or_invalid_integers(valid_environment, name, value):
    valid_environment[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(valid_environment)


@pytest.mark.parametrize("threads", range(1, 11))
def test_whisper_accepts_bounded_cpu_threads(valid_environment, threads):
    valid_environment["WHISPER_CPU_THREADS"] = str(threads)
    assert load_settings(valid_environment).WHISPER_CPU_THREADS == threads


@pytest.mark.parametrize("soft,hard,duration,match", [
    ("21600", "21600", "21600", "WHISPER_HARD_TIME_LIMIT_SECONDS"),
    ("21600", "21599", "21600", "WHISPER_HARD_TIME_LIMIT_SECONDS"),
    ("100", "200", "201", "WHISPER_MAX_AUDIO_DURATION_SECONDS"),
])
def test_whisper_validates_timeout_relationships(valid_environment, soft, hard, duration, match):
    valid_environment.update(WHISPER_SOFT_TIME_LIMIT_SECONDS=soft,
                             WHISPER_HARD_TIME_LIMIT_SECONDS=hard,
                             WHISPER_MAX_AUDIO_DURATION_SECONDS=duration)
    with pytest.raises(ConfigError, match=match):
        load_settings(valid_environment)


def test_whisper_parses_valid_numeric_overrides(valid_environment):
    valid_environment.update(WHISPER_CPU_THREADS="1", WHISPER_MAX_ATTEMPTS="3",
                             WHISPER_SOFT_TIME_LIMIT_SECONDS="100",
                             WHISPER_HARD_TIME_LIMIT_SECONDS="200",
                             WHISPER_MAX_AUDIO_DURATION_SECONDS="200")
    settings = load_settings(valid_environment)
    assert (settings.WHISPER_CPU_THREADS, settings.WHISPER_MAX_ATTEMPTS,
            settings.WHISPER_SOFT_TIME_LIMIT_SECONDS, settings.WHISPER_HARD_TIME_LIMIT_SECONDS,
            settings.WHISPER_MAX_AUDIO_DURATION_SECONDS) == (1, 3, 100, 200, 200)


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
