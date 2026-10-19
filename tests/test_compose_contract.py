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
SERVICE_NAMES = {"postgres", "redis", "migrate", "web", "worker", "beat"}


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
    for name in ("web", "worker", "beat"):
        assert services[name]["depends_on"] == {**migrate["depends_on"], "migrate": {"condition": "service_completed_successfully"}}
        assert services[name]["restart"] == "unless-stopped"
        assert services[name]["cap_drop"] == ["ALL"]
        assert services[name]["env_file"] == [".env"]
        assert not services[name].get("volumes")


def test_commands_named_queues_beat_and_only_bounded_worker_tmpfs(compose):
    services = compose["services"]
    worker = services.get("worker", {})
    assert worker.get("entrypoint") == ["/app/scripts/start-worker.sh"]
    command = worker["command"]
    assert command[:4] == ["celery", "-A", "tasks.celery_app", "worker"]
    assert set(command[command.index("--queues") + 1].split(",")) == {"default", "discovery", "downloads", "transcription"}
    assert worker["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=768m,uid=1000,gid=1000,mode=1770"]
    for name, service in services.items():
        if name != "worker":
            assert not service.get("tmpfs")
    beat = services["beat"]
    assert beat["command"][:4] == ["celery", "-A", "tasks.celery_app", "beat"]
    assert beat["entrypoint"] == ["/app/scripts/start-beat.sh"]
    assert all("SYSTEM_CHECK_SERVICES" not in service.get("environment", {}) for service in services.values())
    assert services["web"]["entrypoint"] == ["/app/scripts/start-web.sh"]
    assert services["web"]["command"][-1] == "app:create_app()"


def test_deployment_has_no_prohibited_services_credentials_or_host_mounts(compose):
    rendered = repr(compose).lower()
    for forbidden in ("stripe", "mailgun", "heroku", "sqs", "cloudflared", "public_registration", "/home/", "~/.aws", "docker.sock"):
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


def test_dockerignore_excludes_sensitive_and_local_material_but_ships_app_assets():
    path = ROOT / ".dockerignore"
    assert path.exists(), "image build context must exclude local secrets"
    patterns = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
    def excluded(name):
        return any(fnmatch.fnmatch(name, pattern) or name.startswith(pattern.rstrip("/") + "/") for pattern in patterns)
    for name in (".env", ".env.prod", ".aws/credentials", "credentials.json", "secret.pem", ".git/config", ".worktrees/other/app.py", ".venv/lib/pkg.py", "tests/test_auth.py", ".pytest_cache/v/cache", "__pycache__/app.pyc", "data/database.sqlite", ".superpowers/sdd/task.md"):
        assert excluded(name), name
    for name in ("migrations/env.py", "migrations/versions/0001_media_foundation.py", "templates/index.html", "static/app.css", "scripts/start-web.sh", "scripts/start-worker.sh", "scripts/start-beat.sh"):
        assert not excluded(name), name
