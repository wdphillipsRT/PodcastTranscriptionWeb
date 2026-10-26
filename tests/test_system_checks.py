"""Readiness is private, bounded and independent of liveness."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import logging
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

COMPONENTS = ("database", "redis", "s3", "transcribe", "youtube", "yt_dlp", "deno", "worker", "beat")


@pytest.fixture
def checks():
    return importlib.import_module("system_checks")


def test_readiness_is_registered(app):
    assert any(rule.rule == "/readyz" for rule in app.url_map.iter_rules())


def test_readiness_route_exists_and_only_exposes_fixed_statuses(app, checks, monkeypatch):
    monkeypatch.setattr(checks, "run_checks", lambda config, services=COMPONENTS: {
        **dict.fromkeys(COMPONENTS, "ok"), "database": "failed"})
    response = app.test_client().get("/readyz")
    assert response.status_code == 503
    assert response.json == {**dict.fromkeys(COMPONENTS, "ok"), "database": "failed"}
    assert response.headers["Cache-Control"] == "no-store"


def test_liveness_does_not_invoke_checks(app, checks, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("liveness must not access dependencies")
    monkeypatch.setattr(checks, "run_checks", forbidden)
    assert app.test_client().get("/healthz").json == {"status": "ok"}


def test_readiness_cache_expires_after_fifteen_seconds(app, checks, monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(checks.time, "monotonic", lambda: clock[0])
    def run(config, services=COMPONENTS):
        calls.append(1)
        return dict.fromkeys(COMPONENTS, "ok" if len(calls) == 1 else "failed")
    monkeypatch.setattr(checks, "run_checks", run)
    client = app.test_client()
    assert client.get("/readyz").status_code == 200
    clock[0] = 114.99
    assert client.get("/readyz").status_code == 200
    clock[0] = 115.0
    assert client.get("/readyz").status_code == 503
    assert len(calls) == 2


def test_concurrent_readiness_has_one_probe_per_cache_window(app, checks, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(config, services=COMPONENTS):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return dict.fromkeys(COMPONENTS, "ok")
    monkeypatch.setattr(checks, "run_checks", run)
    def request():
        with app.test_client() as client:
            return client.get("/readyz").status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(request) for _ in range(6)]
        assert entered.wait(timeout=3)
        release.set()
        assert [future.result(timeout=3) for future in futures] == [200] * 6
    assert len(calls) == 1


def test_readiness_caches_are_per_application(app_factory, checks, monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "run_checks", lambda *args: calls.append(1) or dict.fromkeys(COMPONENTS, "ok"))
    for _ in range(2):
        assert app_factory().test_client().get("/readyz").status_code == 200
    assert len(calls) == 2


def test_cli_runs_only_selected_checks_and_failure_is_safe(app, checks, monkeypatch):
    calls = []
    def probe(component, config):
        calls.append(component)
        if component == "s3":
            raise RuntimeError("secret https://internal.invalid/version-123")
        return True
    monkeypatch.setattr(checks, "_run_isolated", probe)
    result = app.test_cli_runner().invoke(args=["system", "check", "--services", "database,s3"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"database": "ok", "s3": "failed"}
    assert calls == ["database", "s3"]


@pytest.mark.parametrize("selection", ["", "database,", "database,database", "Database", "all", "redis,unknown", "redis;echo secret"])
def test_cli_rejects_nonallowlisted_or_ambiguous_selection(app, checks, monkeypatch, selection):
    calls = []
    monkeypatch.setattr(checks, "_run_isolated", lambda *args: calls.append(args))
    result = app.test_cli_runner().invoke(args=["system", "check", "--services", selection])
    assert result.exit_code == 2
    assert calls == []
    assert result.output.endswith("Error: Select unique components: " + ",".join(COMPONENTS + ("claude_absent", "router")) + "\n")


def test_cli_all_components_success(app, checks, monkeypatch):
    monkeypatch.setattr(checks, "_run_isolated", lambda *args: True)
    result = app.test_cli_runner().invoke(args=["system", "check"])
    assert result.exit_code == 0
    assert json.loads(result.output) == dict.fromkeys(COMPONENTS, "ok")


def test_database_requires_connectivity_and_exact_migration_head(checks, tmp_path):
    from sqlalchemy import create_engine, text
    url = "sqlite:///" + str(tmp_path / "readiness.sqlite")
    config = {"SQLALCHEMY_DATABASE_URI": url}
    assert checks._check_database(config) is False
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('outdated')"))
    assert checks._check_database(config) is False
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0001_media_foundation'"))
    assert checks._check_database(config) is False
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0002_commercial_classification'"))
    assert checks._check_database(config) is False
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0003_knowledge_export'"))
    assert checks._check_database(config) is False
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0004_router_classification'"))
    assert checks._check_database(config) is True
    engine.dispose()


def test_postgres_probe_bounds_connection_and_statement_timeouts(checks, monkeypatch):
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = MagicMock()
    create = MagicMock(return_value=engine)
    monkeypatch.setattr(checks, "create_engine", create)
    context = MagicMock()
    context.configure.return_value.get_current_heads.return_value = ("0002_commercial_classification",)
    monkeypatch.setattr(checks, "MigrationContext", context)
    checks._check_database({"SQLALCHEMY_DATABASE_URI": "postgresql://name:secret@db/app"})
    options = create.call_args.kwargs["connect_args"]
    assert 0 < options["connect_timeout"] <= 5
    assert "statement_timeout=" in options["options"]
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("pong", [True, False])
def test_redis_ping_is_bounded(checks, monkeypatch, pong):
    client = MagicMock()
    client.ping.return_value = pong
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(checks.redis.Redis, "from_url", factory)
    assert checks._check_redis({"REDIS_URL": "redis://internal/0"}) is pong
    assert 0 < factory.call_args.kwargs["socket_connect_timeout"] <= 5
    assert 0 < factory.call_args.kwargs["socket_timeout"] <= 5
    client.close.assert_called_once()


@pytest.mark.parametrize("region,expected", [("us-east-1", True), ("us-west-2", False), (None, False)])
def test_s3_probes_only_exact_bucket_and_region(checks, monkeypatch, region, expected):
    client = MagicMock()
    client.head_bucket.return_value = {"BucketRegion": region}
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(checks.boto3, "client", factory)
    config = {"AWS_REGION": "us-east-1", "AWS_S3_BUCKET_NAME": "podcast-transcriptions-web-2025"}
    assert checks._check_s3(config) is expected
    client.head_bucket.assert_called_once_with(Bucket="podcast-transcriptions-web-2025")
    assert factory.call_args.kwargs["region_name"] == "us-east-1"
    sdk = factory.call_args.kwargs["config"]
    assert 0 < sdk.connect_timeout <= 5 and 0 < sdk.read_timeout <= 5
    assert sdk.retries["total_max_attempts"] == 1
    client.head_bucket.reset_mock()
    assert checks._check_s3({**config, "AWS_S3_BUCKET_NAME": "different"}) is False
    client.head_bucket.assert_not_called()


def test_transcribe_uses_authenticated_minimal_list(checks, monkeypatch):
    client = MagicMock()
    client.list_transcription_jobs.return_value = {"TranscriptionJobSummaries": []}
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(checks.boto3, "client", factory)
    assert checks._check_transcribe({"AWS_REGION": "us-east-1"}) is True
    client.list_transcription_jobs.assert_called_once_with(MaxResults=1)
    assert factory.call_args.args == ("transcribe",)
    assert factory.call_args.kwargs["config"].signature_version != "unsigned"


@pytest.mark.parametrize("service", ["s3", "transcribe"])
@pytest.mark.parametrize("override", ["global_environment", "service_environment", "global_shared_config", "service_shared_config", "explicit_application_config"])
def test_aws_readiness_ignores_endpoint_overrides_without_network_or_secret_output(
        checks, monkeypatch, tmp_path, capsys, caplog, service, override):
    from botocore.httpsession import URLLib3Session

    marker = "endpoint-override-secret"
    caplog.set_level(logging.DEBUG, logger="botocore")
    hostile = "https://" + marker + ".invalid"
    for name in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL_TRANSCRIBE",
                 "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_USE_FIPS_ENDPOINT", "AWS_USE_DUALSTACK_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "AWS_ACCESS_KEY_ID": "readiness-test-key", "AWS_SECRET_ACCESS_KEY": "readiness-test-secret",
        "AWS_SESSION_TOKEN": "readiness-test-session-token", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "false",
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "absent-credentials"),
    }.items():
        monkeypatch.setenv(name, value)
    shared = "[default]\nregion = us-east-1\nignore_configured_endpoint_urls = false\n"
    config = {"AWS_REGION": "us-west-2"}
    if override == "global_environment":
        monkeypatch.setenv("AWS_ENDPOINT_URL", hostile)
    elif override == "service_environment":
        monkeypatch.setenv("AWS_ENDPOINT_URL_" + service.upper(), hostile)
    elif override == "global_shared_config":
        shared += "endpoint_url = " + hostile + "\n"
    elif override == "service_shared_config":
        shared += "services = hostile-services\n\n[services hostile-services]\n" + service + " =\n  endpoint_url = " + hostile + "\n"
    else:
        config.update(endpoint_url=hostile, AWS_ENDPOINT_URL=hostile,
                      **{"AWS_ENDPOINT_URL_" + service.upper(): hostile})
    (tmp_path / "aws-config").write_text(shared)

    def no_network(*args, **kwargs):
        raise AssertionError("endpoint resolution must not use the network")
    monkeypatch.setattr(URLLib3Session, "send", no_network)
    # A fresh real SDK session exercises environment/shared-config precedence;
    # client construction and endpoint resolution perform no service requests.
    session = checks.boto3.Session()
    supplied = []
    def create_client(service_name, **kwargs):
        supplied.append(kwargs["config"])
        return session.client(service_name, **kwargs)
    monkeypatch.setattr(checks.boto3, "client", create_client)
    client = checks._aws_client(service, config)
    try:
        assert client.meta.endpoint_url == "https://" + service + ".us-west-2.amazonaws.com"
        # Botocore consumes this option during construction and does not retain
        # it on the reconstructed client.meta.config object.
        assert supplied[0].ignore_configured_endpoint_urls is True
        credentials = client._request_signer._credentials.get_frozen_credentials()
        assert (credentials.access_key, credentials.secret_key, credentials.token) == (
            "readiness-test-key", "readiness-test-secret", "readiness-test-session-token")
    finally:
        client.close()
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    assert marker not in caplog.text
    assert "readiness-test-secret" not in caplog.text
    assert "readiness-test-session-token" not in caplog.text


@pytest.mark.parametrize("status,body,expected", [
    (200, {"kind": "youtube#videoListResponse", "etag": "list-etag", "items": [
        {"kind": "youtube#video", "etag": "video-etag", "id": "jNQXAC9IVRw"}]}, True),
    (403, {"error": {"message": "secret"}}, False),
    (200, {"error": {"message": "invalid key"}}, False),
    (200, {"kind": "youtube#videoListResponse", "items": []}, False),
    (302, {}, False),
])
def test_youtube_checks_fixed_public_video_key_without_search(checks, monkeypatch, status, body, expected):
    response = MagicMock(status_code=status)
    response.iter_content.return_value = [json.dumps(body).encode()]
    session = MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(checks.requests, "Session", lambda: session)
    assert checks._check_youtube({"YOUTUBE_API_KEY": "synthetic-secret"}) is expected
    url = session.get.call_args.args[0]
    kwargs = session.get.call_args.kwargs
    assert url == "https://www.googleapis.com/youtube/v3/videos"
    assert kwargs["params"] == {"part": "id", "id": "jNQXAC9IVRw", "key": "synthetic-secret"}
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert all(0 < value <= 5 for value in kwargs["timeout"])
    response.close.assert_called_once()
    session.close.assert_called_once()


@pytest.mark.parametrize("component,output,expected", [
    ("yt_dlp", "2026.08.19\n", True), ("yt_dlp", "2026.08.18\n", False),
    ("yt_dlp", "2026.08.19.post1\n", False),
    ("deno", "deno 2.9.7 (stable, release, x86_64-unknown-linux-gnu)\nv8 14\ntypescript 5\n", True),
    ("deno", "deno 2.9.70\n", False), ("deno", "deno 2.9.6\n", False),
])
def test_binary_versions_are_exact_bounded_and_not_config_overridable(checks, monkeypatch, component, output, expected):
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=output))
    monkeypatch.setattr(checks.subprocess, "run", run)
    assert getattr(checks, "_check_" + component)({"YTDLP_EXPECTED_VERSION": "bogus", "DENO_EXPECTED_VERSION": "bogus"}) is expected
    assert run.call_args.args[0] == ["yt-dlp" if component == "yt_dlp" else "deno", "--version"]
    assert 0 < run.call_args.kwargs["timeout"] <= 5
    assert not run.call_args.kwargs.get("shell", False)


@pytest.mark.parametrize("responses,expected", [([], False), ([{"worker@media": {"ok": "pong"}}], True), ([{"worker@media": {"error": "secret"}}], False)])
def test_worker_requires_a_pong_with_bounded_broker(checks, monkeypatch, responses, expected):
    celery = MagicMock()
    celery.control.ping.return_value = responses
    factory = MagicMock(return_value=celery)
    monkeypatch.setattr(checks, "Celery", factory)
    assert checks._check_worker({"REDIS_URL": "redis://internal/0"}) is expected
    assert 0 < celery.control.ping.call_args.kwargs["timeout"] <= 5
    assert 0 < celery.conf.update.call_args.kwargs["broker_connection_timeout"] <= 5
    celery.close.assert_called_once()


def test_beat_checks_required_periodic_discovery_schedule(checks, monkeypatch):
    import celeryconfig
    assert checks._check_beat({}) is True
    monkeypatch.setattr(celeryconfig, "beat_schedule", {})
    assert checks._check_beat({}) is False
    monkeypatch.setattr(celeryconfig, "beat_schedule", {"invalid": {"task": "tasks.check_all_sources", "schedule": 0}})
    assert checks._check_beat({}) is False


def test_isolated_check_has_hard_deadline_and_no_credentials_in_argv(checks, monkeypatch):
    process = MagicMock(pid=12345)
    process.communicate.return_value = (None, None)
    process.returncode = 0
    factory = MagicMock(return_value=process)
    monkeypatch.setattr(checks.subprocess, "Popen", factory)
    config = {"YOUTUBE_API_KEY": "synthetic-secret", "IRRELEVANT_SECRET": "omit-me"}
    assert checks._run_isolated("youtube", config) is True
    args, kwargs = factory.call_args
    assert "synthetic-secret" not in repr(args)
    assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    payload = json.loads(process.communicate.call_args.kwargs["input"])
    assert payload == {"YOUTUBE_API_KEY": "synthetic-secret"}
    assert 0 < process.communicate.call_args.kwargs["timeout"] <= 10


def test_timed_out_probe_kills_and_reaps_process_group(checks, monkeypatch):
    process = MagicMock(pid=12345)
    process.communicate.side_effect = [subprocess.TimeoutExpired("safe", 8), (None, None)]
    monkeypatch.setattr(checks.subprocess, "Popen", lambda *args, **kwargs: process)
    kill = MagicMock()
    monkeypatch.setattr(checks.os, "killpg", kill)
    assert checks._run_isolated("s3", {}) is False
    kill.assert_called_once_with(12345, checks.signal.SIGKILL)
    assert process.communicate.call_count == 2


def test_real_isolated_nonnetwork_probe_and_invalid_selection(checks):
    assert checks._run_isolated("beat", {}) is True
    with pytest.raises(ValueError):
        checks.run_checks({}, ("unknown",))


def test_youtube_response_size_is_bounded(checks, monkeypatch):
    response = MagicMock(status_code=200)
    response.iter_content.return_value = [b" " * 16385]
    session = MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(checks.requests, "Session", lambda: session)
    assert checks._check_youtube({"YOUTUBE_API_KEY": "test-key"}) is False


@pytest.mark.parametrize("component", COMPONENTS)
def test_any_dependency_error_has_only_fixed_public_status(checks, monkeypatch, component):
    def failed(*args):
        raise RuntimeError("secret https://internal.invalid version-123")
    monkeypatch.setattr(checks, "_run_isolated", failed)
    assert checks.run_checks({}, (component,)) == {component: "failed"}


from tests.router_fake import fake_router  # noqa: E402,F401


def test_claude_absent_rejects_any_claude_variable_cli_or_package(checks, monkeypatch, tmp_path):
    package = tmp_path / "anthropic"
    monkeypatch.setattr(checks, "_CLAUDE_PACKAGE", package)
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    monkeypatch.setattr(checks.os, "environ", {"PATH": "/usr/bin"})
    assert checks._check_claude_absent({}) is True
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDECODE", "claude_model"):
        monkeypatch.setattr(checks.os, "environ", {"PATH": "/usr/bin", name: "1"})
        assert checks._check_claude_absent({}) is False, name
    monkeypatch.setattr(checks.os, "environ", {"PATH": "/usr/bin"})
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None)
    assert checks._check_claude_absent({}) is False
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    package.mkdir()
    assert checks._check_claude_absent({}) is False


def test_router_probe_needs_readiness_and_a_usable_token(checks, fake_router, tmp_path):  # noqa: F811
    config = {"ROUTER_URL": fake_router.url, "ROUTER_TOKEN_FILE": fake_router.token_file, "ROUTER_CA_FILE": "",
              "ROUTER_CLIENT_CERT_FILE": "", "ROUTER_CLIENT_KEY_FILE": ""}
    assert checks._check_router(config) is True
    assert checks._check_router({**config, "ROUTER_TOKEN_FILE": str(tmp_path / "missing")}) is False
    fake_router.ready = False  # /readyz 503, e.g. Forge unavailable
    assert checks._check_router(config) is False


def test_classifier_components_are_selectable_but_not_part_of_web_readiness(app, checks, monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "_run_isolated", lambda component, config: calls.append(component) or True)
    result = app.test_cli_runner().invoke(args=["system", "check", "--services", "database,redis,s3,claude_absent,router"])
    assert result.exit_code == 0, result.output
    assert calls == ["database", "redis", "s3", "claude_absent", "router"]
    assert "router" not in checks.COMPONENTS and "claude_absent" not in checks.COMPONENTS


def test_router_probe_payload_carries_paths_never_secrets(checks, monkeypatch):
    process = MagicMock(pid=1)
    process.communicate.return_value = (None, None)
    process.returncode = 0
    monkeypatch.setattr(checks.subprocess, "Popen", MagicMock(return_value=process))
    paths = {"ROUTER_URL": "https://router.invalid:8443", "ROUTER_TOKEN_FILE": "/router/token",
             "ROUTER_CA_FILE": "/router/router-ca.crt", "ROUTER_CLIENT_CERT_FILE": "/router/client.crt",
             "ROUTER_CLIENT_KEY_FILE": "/router/client.key"}
    assert checks._run_isolated("router", {**paths, "SECRET_KEY": "omit-me"}) is True
    assert json.loads(process.communicate.call_args.kwargs["input"]) == paths
