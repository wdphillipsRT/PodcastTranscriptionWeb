"""Bounded deployment probes with a deliberately small public result surface.

Each probe runs in a disposable process: socket timeouts alone cannot bound
DNS, SDK credential discovery or broker reconnects. Configuration travels over
stdin, never argv; child output is discarded, including third-party logging.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
import boto3
from celery import Celery
import click
from flask import current_app
from flask.cli import with_appcontext
import redis
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from aws_clients import create_aws_client

COMPONENTS = ("database", "redis", "s3", "transcribe", "youtube", "yt_dlp", "deno", "worker", "beat")
# Selectable for the classifier's startup guard only; web readiness never probes the router.
CLASSIFIER_COMPONENTS = ("claude_absent", "router")
ALL_COMPONENTS = COMPONENTS + CLASSIFIER_COMPONENTS
PROBE_TIMEOUT_SECONDS = 8
# Only the router probe: beyond the router client's 10 s connect timeout. The web /readyz
# runs its probes serially, so the shared limit stays short.
ROUTER_PROBE_TIMEOUT_SECONDS = 12
CACHE_SECONDS = 15
# Router entries are a URL and file paths; the probe child reads the files, never this process.
_CONFIG_KEYS = ("SQLALCHEMY_DATABASE_URI", "REDIS_URL", "AWS_REGION", "AWS_S3_BUCKET_NAME", "YOUTUBE_API_KEY",
                "ROUTER_URL", "ROUTER_TOKEN_FILE", "ROUTER_CA_FILE", "ROUTER_CLIENT_CERT_FILE",
                "ROUTER_CLIENT_KEY_FILE")
_CLAUDE_PACKAGE = Path("/usr/local/lib/node_modules/@anthropic-ai")
_ROOT = Path(__file__).resolve().parent


def _check_database(config):
    url = config["SQLALCHEMY_DATABASE_URI"]
    connect_args = {} if url.startswith("sqlite:") else {
        "connect_timeout": 3, "options": "-c statement_timeout=3000 -c lock_timeout=3000"}
    engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(connection).get_current_heads())
            alembic = Config(str(_ROOT / "migrations" / "alembic.ini"))
            heads = set(ScriptDirectory.from_config(alembic).get_heads())
            return bool(heads) and current == heads
    finally:
        engine.dispose()


def _check_redis(config):
    client = redis.Redis.from_url(config["REDIS_URL"], socket_connect_timeout=2,
                                  socket_timeout=2, retry_on_timeout=False)
    try:
        return client.ping() is True
    finally:
        client.close()


def _aws_client(service, config):
    return create_aws_client(service, config["AWS_REGION"], connect_timeout=2, read_timeout=2)


def _check_s3(config):
    if config["AWS_S3_BUCKET_NAME"] != "podcast-transcriptions-web-2025":
        return False
    client = _aws_client("s3", config)
    try:
        result = client.head_bucket(Bucket="podcast-transcriptions-web-2025")
        return result.get("BucketRegion") == config["AWS_REGION"]
    finally:
        client.close()


def _check_transcribe(config):
    client = _aws_client("transcribe", config)
    try:
        result = client.list_transcription_jobs(MaxResults=1)
        return isinstance(result.get("TranscriptionJobSummaries"), list)
    finally:
        client.close()


def _check_youtube(config):
    # videos.list costs one unit. A fixed public video avoids search costs,
    # user-controlled URLs, redirects, cookies and OAuth/account credentials.
    from youtube_discovery import _public_api_auth, _safe_transport_logging
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        with _safe_transport_logging(config["YOUTUBE_API_KEY"]):
            response = session.get("https://www.googleapis.com/youtube/v3/videos",
                params={"part": "id", "id": "jNQXAC9IVRw", "key": config["YOUTUBE_API_KEY"]},
                auth=_public_api_auth, stream=True, allow_redirects=False, timeout=(2, 2))
            if response.status_code != 200:
                return False
            body = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                body.extend(chunk)
                if len(body) > 16384:
                    return False
            result = json.loads(body)
            items = result.get("items") if isinstance(result, dict) else None
            return (isinstance(result, dict) and result.get("kind") == "youtube#videoListResponse"
                    and isinstance(items, list) and len(items) == 1
                    and isinstance(items[0], dict) and items[0].get("id") == "jNQXAC9IVRw")
    finally:
        if response is not None:
            response.close()
        session.close()


def _check_yt_dlp(_config):
    result = subprocess.run(["yt-dlp", "--version"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=3, check=False)
    return result.returncode == 0 and result.stdout.strip() == "2026.08.19"


def _check_deno(_config):
    result = subprocess.run(["deno", "--version"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=3, check=False)
    first_line = result.stdout.splitlines()[0].split() if result.stdout else []
    return result.returncode == 0 and first_line[:2] == ["deno", "2.9.7"]


def _check_worker(config):
    client = Celery("readiness", broker=config["REDIS_URL"])
    client.conf.update(broker_connection_timeout=2, broker_connection_retry=False,
        broker_connection_retry_on_startup=False, broker_transport_options={
            "socket_connect_timeout": 2, "socket_timeout": 2, "max_retries": 0},
        task_serializer="json", accept_content=["json"], result_accept_content=["json"])
    try:
        replies = client.control.ping(timeout=2)
        return any(isinstance(reply, dict) and any(
            isinstance(value, dict) and value.get("ok") == "pong" for value in reply.values())
            for reply in replies)
    finally:
        client.close()


def _check_beat(_config):
    from celeryconfig import beat_schedule
    return any(isinstance(entry, dict) and entry.get("task") == "tasks.check_all_sources"
               and type(entry.get("schedule")) in {int, float} and entry["schedule"] == 3600
               for entry in beat_schedule.values())


def _check_claude_absent(_config):
    """Claude was removed: no CLAUDE* variable, no claude executable, no installed package."""
    if any(name.upper().startswith("CLAUDE") for name in os.environ):
        return False
    return shutil.which("claude") is None and not _CLAUDE_PACKAGE.exists()


def _check_router(config):
    """The router answers /readyz as ready over mTLS (bounded; a 503 is a fast failure) and the token file is usable."""
    from router_client import RouterClient
    client = RouterClient(config["ROUTER_URL"], token_file=config["ROUTER_TOKEN_FILE"], expected_model="unused:0",
                          ca_file=config["ROUTER_CA_FILE"], cert_file=config["ROUTER_CLIENT_CERT_FILE"],
                          key_file=config["ROUTER_CLIENT_KEY_FILE"])
    return client.token_available() and client.ready()


def _run_isolated(component, config):
    if component not in ALL_COMPONENTS:
        raise ValueError("Invalid component selection")
    payload = {name: config[name] for name in _CONFIG_KEYS if name in config}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--probe", component],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    try:
        process.communicate(input=json.dumps(payload),
                            timeout=ROUTER_PROBE_TIMEOUT_SECONDS if component == "router" else PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Also terminate any credential helper/tool subprocess, then reap.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=2)
        return False
    return process.returncode == 0


def run_checks(config, services=COMPONENTS):
    if not services or len(set(services)) != len(services) or any(name not in ALL_COMPONENTS for name in services):
        raise ValueError("Invalid component selection")
    results = {}
    for component in services:
        try:
            healthy = _run_isolated(component, config) is True
        except Exception:
            healthy = False
        results[component] = "ok" if healthy else "failed"
    return results


class ReadinessCache:
    """An app-local cache; the lock prevents duplicate concurrent refreshes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._expires = 0.0
        self._results = None

    def get(self, config):
        with self._lock:
            if self._results is None or time.monotonic() >= self._expires:
                results = run_checks(config)
                self._results = {name: "ok" if results.get(name) == "ok" else "failed" for name in COMPONENTS}
                self._expires = time.monotonic() + CACHE_SECONDS
            return dict(self._results)


@click.group("system")
def system_cli():
    """Check deployment dependencies without exposing provider details."""


@system_cli.command("check")
@click.option("--services", default=",".join(COMPONENTS), help="Comma-separated component names.")
@with_appcontext
def check_command(services):
    selected = tuple(services.split(","))
    if not selected or len(set(selected)) != len(selected) or any(name not in ALL_COMPONENTS for name in selected):
        raise click.UsageError("Select unique components: " + ",".join(ALL_COMPONENTS))
    results = run_checks(current_app.config, selected)
    click.echo(json.dumps(results, sort_keys=True))
    if "failed" in results.values():
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    # This internal child entrypoint never prints exceptions or tool output.
    try:
        valid = len(sys.argv) == 3 and sys.argv[1] == "--probe" and sys.argv[2] in ALL_COMPONENTS
        if not valid:
            raise ValueError("Invalid component selection")
        configuration = json.loads(sys.stdin.read(65536))
        passed = globals()["_check_" + sys.argv[2]](configuration) is True
    except Exception:
        passed = False
    sys.exit(0 if passed else 1)
