"""Parse deployment contracts without depending on live provider credentials."""
import fnmatch
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICE_NAMES = {"postgres", "redis", "migrate", "web", "worker", "beat", "classifier"}


@pytest.fixture
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_exact_services_and_loopback_web_only(compose):
    services = compose["services"]
    assert set(services) == SERVICE_NAMES
    assert services["web"]["ports"] == ["127.0.0.1:8005:8005"]
    for name, service in services.items():
        if name != "web":
            assert not service.get("ports")
        assert not service.get("privileged")
        assert service.get("network_mode") != "host"
        assert "no-new-privileges:true" in service["security_opt"]
        assert all("docker.sock" not in str(volume) for volume in service.get("volumes", []))


def test_official_versioned_db_images_persistence_and_health(compose):
    services = compose["services"]
    assert re.fullmatch(r"postgres:\d+\.\d+-alpine", services.get("postgres", {}).get("image", ""))
    assert re.fullmatch(r"redis:\d+\.\d+\.\d+-alpine", services["redis"]["image"])
    assert "postgres_data" in compose["volumes"]
    assert "postgres_data:/var/lib/postgresql/data" in services["postgres"]["volumes"]
    for name in ("postgres", "redis"):
        assert services[name]["healthcheck"]["timeout"] == "5s"
        assert services[name]["restart"] == "unless-stopped"


def test_one_shot_migration_and_app_dependency_order(compose):
    services = compose["services"]
    migrate = services.get("migrate", {})
    assert migrate.get("command") == ["flask", "--app", "app:create_app", "db", "upgrade"]
    assert migrate["restart"] == "no"
    assert migrate["depends_on"] == {"postgres": {"condition": "service_healthy"}, "redis": {"condition": "service_healthy"}}
    for name in ("web", "worker", "beat", "classifier"):
        assert services[name]["depends_on"] == {**migrate["depends_on"], "migrate": {"condition": "service_completed_successfully"}}
        assert services[name]["restart"] == "unless-stopped"
        assert services[name]["cap_drop"] == ["ALL"]
        assert services[name]["env_file"] == [".env"]
        if name != "classifier":
            assert not services[name].get("volumes")


def test_commands_named_queues_beat_and_only_bounded_worker_tmpfs(compose):
    services = compose["services"]
    worker = services.get("worker", {})
    assert worker.get("entrypoint") == ["/app/scripts/start-worker.sh"]
    command = worker["command"]
    assert command[:4] == ["celery", "-A", "tasks.celery_app", "worker"]
    assert command[command.index("--queues") + 1] == "default,discovery,downloads,transcription"
    assert worker["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=768m,uid=1000,gid=1000,mode=1770"]
    for name, service in services.items():
        if name not in {"worker", "classifier"}:
            assert not service.get("tmpfs")
    beat = services["beat"]
    assert beat["command"][:4] == ["celery", "-A", "tasks.celery_app", "beat"]
    assert beat["entrypoint"] == ["/app/scripts/start-beat.sh"]
    assert all("SYSTEM_CHECK_SERVICES" not in service.get("environment", {}) for service in services.values())
    assert services["web"]["entrypoint"] == ["/app/scripts/start-web.sh"]
    assert services["web"]["command"][-1] == "app:create_app()"


def test_classifier_has_exclusive_credentials_queue_and_bounded_tmpfs(compose):
    services = compose["services"]
    classifier = services.get("classifier", {})
    assert classifier.get("entrypoint") == ["/app/scripts/start-classifier.sh"]
    assert classifier["command"] == [
        "celery", "-A", "tasks.celery_app", "worker", "--loglevel=info",
        "--queues", "classification", "--concurrency", "1", "--hostname", "classifier@%h",
    ]
    assert classifier["environment"] == {
        "CLAUDE_CONFIG_DIR": "/claude-config", "DISABLE_UPDATES": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    assert classifier["volumes"] == ["${CLAUDE_CONFIG_HOST_PATH:-./.runtime/claude}:/claude-config"]
    assert classifier["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=1770"]
    for name, service in services.items():
        if name != "classifier":
            assert "classification" not in repr(service.get("command", []))
            assert not service.get("environment", {}).get("CLAUDE_CONFIG_DIR")
            assert "claude" not in repr(service.get("volumes", [])).lower()
            assert "classifier" not in service.get("depends_on", {})


def test_deployment_has_no_prohibited_services_credentials_or_host_mounts(compose):
    rendered = repr(compose).lower()
    for forbidden in ("stripe", "mailgun", "heroku", "sqs", "cloudflare", "tunnel_token", "public_registration", "/home/", "~/.aws", "docker.sock"):
        assert forbidden not in rendered
    for service in compose["services"].values():
        for key in service.get("environment", {}):
            assert key not in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    example = (ROOT / ".env.example").read_text()
    assert "AWS_PROFILE=" not in example
    assert "mounted profile" not in example
    assert re.search(r"^AWS_ACCESS_KEY_ID=$", example, re.M)
    assert re.search(r"^AWS_SECRET_ACCESS_KEY=$", example, re.M)


def test_image_uses_exact_deno_stage_nonroot_owned_copy_and_minimal_apt():
    dockerfile = (ROOT / "Dockerfile").read_text()
    lines = [line.strip() for line in dockerfile.splitlines() if line.strip() and not line.startswith("#")]
    assert "FROM denoland/deno:bin-2.9.7 AS deno" in lines
    assert "FROM python:3.11-slim" in lines
    assert "COPY --from=deno /deno /usr/local/bin/deno" in lines
    assert "USER 1000:1000" in lines
    assert "COPY --chown=1000:1000 . ." in lines
    logical = dockerfile.replace("\\\n", " ")
    packages = re.search(r"apt-get install\s+-y\s+--no-install-recommends\s+(.+?)\s+&&", logical)
    assert packages and set(shlex.split(packages[1])) == {"ca-certificates", "ffmpeg"}
    assert re.search(r"groupadd\s+--gid\s+1000", dockerfile)
    assert re.search(r"useradd\s+--uid\s+1000\s+--gid\s+1000", dockerfile)
    assert "--no-cache-dir" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "build-essential" not in dockerfile and "curl" not in dockerfile


def test_image_copies_only_pinned_claude_and_node_into_final_nonroot_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM node:22-bookworm-slim AS claude" in dockerfile
    builder, runtime = dockerfile.split("FROM python:3.11-slim", 1)
    assert re.search(r"npm install --global @anthropic-ai/claude-code@2\.1\.303(?:\s|$)", builder)
    assert "COPY --from=claude /usr/local/bin/node /usr/local/bin/node" in runtime
    assert "COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai/claude-code /usr/local/lib/node_modules/@anthropic-ai/claude-code" in runtime
    assert "ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude" in runtime
    assert not re.search(r"\b(?:npm|npx|corepack)\b", runtime)
    assert "DISABLE_UPDATES=1" in runtime
    assert [line for line in runtime.splitlines() if line.startswith("USER ")] == ["USER 1000:1000"]


def test_direct_python_runtime_and_dev_requirements_are_pinned():
    for filename in ("requirements.txt", "requirements-dev.txt"):
        lines = (ROOT / filename).read_text().splitlines()
        dependencies = [line for line in lines if line.strip() and not line.startswith(("#", "-r "))]
        assert dependencies and all(re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.+!-]+", line) for line in dependencies)
    assert "yt-dlp[default]==2026.8.19" in (ROOT / "requirements.txt").read_text().splitlines()
    assert "PyYAML==6.0.3" in (ROOT / "requirements-dev.txt").read_text().splitlines()


@pytest.mark.parametrize("script,selection", [
    ("start-web.sh", "database,redis,s3,transcribe,youtube,yt_dlp,deno"),
    ("start-worker.sh", "database,redis,s3,transcribe,youtube,yt_dlp,deno"),
    ("start-beat.sh", "database,redis,beat"),
])
def test_startup_scripts_fail_closed_and_preserve_exec_arguments(script, selection, tmp_path):
    path = ROOT / "scripts" / script
    assert path.exists(), "startup checks must exist"
    content = path.read_text()
    assert content.startswith("#!/bin/sh\nset -eu\n")
    assert 'exec "$@"' in content
    assert "eval " not in content and "set -x" not in content
    assert os.access(path, os.X_OK)
    assert subprocess.run(["sh", "-n", str(path)], capture_output=True).returncode == 0
    # A fake Flask executable records selection and permits/fails the guard;
    # the final process prints literal argv to prove no shell re-evaluation.
    fake = tmp_path / "flask"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\nexit "${GUARD_EXIT:-0}"\n')
    fake.chmod(0o755)
    environment = {**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"]}
    arguments = [str(path), "/bin/printf", "%s", "literal $(echo secret)"]
    result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "--app app:create_app system check --services " + selection
    assert result.stdout.endswith("literal $(echo secret)")
    result = subprocess.run(arguments, env={**environment, "GUARD_EXIT": "1"}, capture_output=True, text=True, timeout=3)
    assert result.returncode == 1 and "literal" not in result.stdout
    for hostile in ("beat", "database,redis,beat", "", "redis; echo injected-secret", "s3", "unknown"):
        result = subprocess.run(arguments, env={**environment, "SYSTEM_CHECK_SERVICES": hostile}, capture_output=True, text=True, timeout=3)
        assert result.returncode == 0
        assert result.stdout.splitlines()[0] == "--app app:create_app system check --services " + selection
        assert result.stdout.endswith("literal $(echo secret)")
    assert "SYSTEM_CHECK_SERVICES" not in content


def test_worker_startup_pins_app_pythonpath_for_prefork_lazy_imports(tmp_path):
    """Celery's console loader must not leave child imports dependent on cwd."""
    script = ROOT / "scripts" / "start-worker.sh"
    fake = tmp_path / "flask"
    fake.write_text('#!/bin/sh\nprintf "guard:%s\\n" "$PYTHONPATH"\n')
    fake.chmod(0o755)
    probe = tmp_path / "probe"
    probe.write_text('#!/bin/sh\nprintf "child:%s\\n" "$PYTHONPATH"\n')
    probe.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        "PYTHONPATH": "/tmp/untrusted-shadow-path",
    }

    result = subprocess.run(
        [str(script), str(probe)], env=environment,
        capture_output=True, text=True, timeout=3,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["guard:/app", "child:/app"]


@pytest.fixture
def classifier_startup(tmp_path):
    path = ROOT / "scripts" / "start-classifier.sh"
    for name, body in {
        "claude": '''case "$*" in
  --version) printf 'version:%s\\n' "$PYTHONPATH" >> "$STARTUP_EVENTS"
    printf '%s\\n' "$FAKE_VERSION"; printf 'private-version-diagnostic\\n' >&2
    exit "${VERSION_EXIT:-0}" ;;
  'auth status') printf 'auth:%s\\n' "$PYTHONPATH" >> "$STARTUP_EVENTS"
    printf 'private-auth-stdout\\n'; printf 'private-auth-stderr\\n' >&2
    exit "${AUTH_EXIT:-0}" ;;
  *) exit 99 ;;
esac
''',
        "flask": '''printf 'flask:%s:%s\\n' "$PYTHONPATH" "$*" >> "$STARTUP_EVENTS"
exit "${GUARD_EXIT:-0}"
''',
        "celery": '''printf 'celery:%s\\n' "$PYTHONPATH" >> "$STARTUP_EVENTS"
printf '%s\\n' "$@"
exit "${CELERY_EXIT:-0}"
''',
    }.items():
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\n" + body)
        executable.chmod(0o755)
    events = tmp_path / "events"
    environment = {
        **os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        "PYTHONPATH": "/tmp/untrusted-shadow-path", "STARTUP_EVENTS": str(events),
        "FAKE_VERSION": "2.1.303 (Claude Code)", "CLAUDE_EXPECTED_VERSION": "2.1.303",
        "AUTH_EXIT": "0", "VERSION_EXIT": "0", "GUARD_EXIT": "0", "CELERY_EXIT": "0",
    }
    return path, environment, events


def run_classifier_startup(fixture, **overrides):
    path, environment, events = fixture
    assert path.is_file(), "classifier startup guard must exist"
    assert os.access(path, os.X_OK)
    content = path.read_text()
    assert content.startswith("#!/bin/sh\nset -eu\n")
    assert 'exec "$@"' in content and "eval " not in content and "set -x" not in content
    assert subprocess.run(["sh", "-n", str(path)], capture_output=True).returncode == 0
    result = subprocess.run(
        [str(path), "celery", "literal $(echo injected-secret)", "argument with spaces"],
        env={**environment, **overrides}, capture_output=True, text=True, timeout=3,
    )
    assert "private-" not in result.stdout + result.stderr
    return result, events.read_text().splitlines()


@pytest.mark.parametrize("expected", ["2.1.303", "12.34.567"])
def test_classifier_startup_checks_in_order_pins_pythonpath_and_executes_literal_argv(classifier_startup, expected):
    result, events = run_classifier_startup(
        classifier_startup, CLAUDE_EXPECTED_VERSION=expected,
        FAKE_VERSION=expected + " (Claude Code)", CELERY_EXIT="17",
        SYSTEM_CHECK_SERVICES="redis; echo injected-secret",
    )
    assert result.returncode == 17
    assert events == ["version:/app", "auth:/app",
                      "flask:/app:--app app:create_app system check --services database,redis,s3", "celery:/app"]
    assert result.stdout == "literal $(echo injected-secret)\nargument with spaces\n"


@pytest.mark.parametrize("overrides,events", [
    ({"FAKE_VERSION": "2.1.302 (Claude Code)"}, ["version:/app"]),
    ({"FAKE_VERSION": "2.1.303 (Claude Code) extra"}, ["version:/app"]),
    ({"VERSION_EXIT": "1"}, ["version:/app"]),
    ({"CLAUDE_EXPECTED_VERSION": "$(echo injected-secret)"}, ["version:/app"]),
    ({"AUTH_EXIT": "1"}, ["version:/app", "auth:/app"]),
    ({"GUARD_EXIT": "1"}, ["version:/app", "auth:/app",
                            "flask:/app:--app app:create_app system check --services database,redis,s3"]),
])
def test_classifier_failures_stop_before_celery_without_auth_or_version_output(classifier_startup, overrides, events):
    result, actual = run_classifier_startup(classifier_startup, **overrides)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "injected-secret" not in result.stderr
    assert actual == events


def test_web_readiness_remains_independent_of_claude_auth(app, monkeypatch):
    import system_checks

    components = ("database", "redis", "s3", "transcribe", "youtube", "yt_dlp", "deno", "worker", "beat")
    called = []
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(system_checks, "_run_isolated", lambda component, config: called.append(component) or True)
    response = app.test_client().get("/readyz")
    assert response.status_code == 200
    assert response.json == dict.fromkeys(components, "ok")
    assert called == list(components)


def test_dockerignore_excludes_sensitive_and_local_material_but_ships_app_assets():
    path = ROOT / ".dockerignore"
    assert path.exists(), "image build context must exclude local secrets"
    patterns = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
    def excluded(name):
        return any(fnmatch.fnmatch(name, pattern) or name.startswith(pattern.rstrip("/") + "/") for pattern in patterns)
    for name in (".env", ".env.prod", ".aws/credentials", ".runtime/claude/config.json", "credentials.json", "secret.pem", ".git/config", ".worktrees/other/app.py", ".venv/lib/pkg.py", "tests/test_auth.py", ".pytest_cache/v/cache", "__pycache__/app.pyc", "data/database.sqlite", ".superpowers/sdd/task.md"):
        assert excluded(name), name
    for name in ("migrations/env.py", "migrations/versions/0001_media_foundation.py", "templates/index.html", "static/app.css", "scripts/start-web.sh", "scripts/start-worker.sh", "scripts/start-beat.sh", "scripts/start-classifier.sh"):
        assert not excluded(name), name
