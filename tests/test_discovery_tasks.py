import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from tests.test_discovery_service import store, service, Adapter, descriptor
from ingestion_types import DiscoveryPage
from models import ContentItem


def test_celery_configuration_is_json_utc_bounded_and_routes_named_queues():
    config = importlib.import_module("celeryconfig")
    assert config.task_serializer == config.result_serializer == "json"
    assert config.accept_content == ["json"] and config.enable_utc and config.timezone == "UTC"
    assert config.task_acks_late and config.task_reject_on_worker_lost
    assert config.worker_prefetch_multiplier == 1 and config.broker_connection_retry_on_startup
    assert 0 < config.task_soft_time_limit < config.task_time_limit
    assert config.result_expires > 0
    assert {q.name for q in config.task_queues} == {"default", "discovery", "downloads", "transcription",
                                                    "classification", "knowledge"}
    for name in ("classify_item", "reconcile_classifications", "scan_source_rule"):
        assert config.task_routes["tasks." + name]["queue"] == "classification"
    assert config.beat_schedule["classification-reconciliation"]["task"] == "tasks.reconcile_classifications"
    assert config.task_routes["tasks.discover_source"]["queue"] == "discovery"
    assert config.task_routes["tasks.process_item"]["queue"] == "downloads"
    assert any(value["task"] == "tasks.check_all_sources" and value["schedule"] == 3600 for value in config.beat_schedule.values())


def test_task_wrappers_execute_in_existing_context_and_pass_only_ids(store, monkeypatch):
    tasks = importlib.import_module("tasks")
    source = store()
    svc = service(Adapter([DiscoveryPage((descriptor(1),))]))
    monkeypatch.setattr(tasks, "get_discovery_service", lambda: svc)
    result = tasks.discover_source.run(source.id)
    assert type(result) is int and ContentItem.query.count() == 1
    assert json.loads(json.dumps(result)) == result
    for name in ("discover_source", "continue_discovery", "finalize_discovery", "run_backfill", "cancel_backfill", "check_all_sources"):
        assert getattr(tasks, name).name == "tasks." + name


@pytest.mark.parametrize("argument", ["1", True, {}, 0, -1, None])
def test_task_wrappers_reject_non_integer_or_nonpositive_ids(store, argument):
    tasks = importlib.import_module("tasks")
    with pytest.raises(ValueError):
        tasks.continue_discovery.run(argument)


def test_dispatch_messages_only_contain_integer_ids_and_stable_task_identity(monkeypatch):
    tasks = importlib.import_module("tasks")
    sent = []
    monkeypatch.setattr(tasks.celery_app, "send_task", lambda *args, **kwargs: sent.append((args, kwargs)))
    tasks.dispatch_task("process_item", 42, task_id="process-item-42-youtube_captions")
    assert sent == [(("tasks.process_item",), {"args": [42], "task_id": "process-item-42-youtube_captions"})]


def test_worker_import_never_loads_app_settings_or_connects_to_services():
    script = textwrap.dedent("""\
        import importlib
        import sys
        import socket
        from unittest.mock import patch
        from flask import Flask
        from sqlalchemy.engine import Engine
        import config
        import dotenv
        def forbidden(*args, **kwargs):
            raise AssertionError('Task import must not construct apps or connect services')
        with patch.object(Flask, '__init__', forbidden), \\
             patch.object(config, 'load_settings', forbidden), \\
             patch.object(dotenv, 'load_dotenv', forbidden), \\
             patch.object(socket.socket, 'connect', forbidden), \\
             patch.object(Engine, 'connect', forbidden):
            tasks = importlib.import_module('tasks')
            assert 'router_classifier' not in sys.modules and 'router_client' not in sys.modules
            importlib.import_module('discovery_service')
            assert tasks.celery_app.main == 'podcast_transcription'
    """)
    environment = dict(os.environ)
    for name in ("SECRET_KEY", "DATABASE_URL", "REDIS_URL", "AWS_REGION", "AWS_S3_BUCKET_NAME", "YOUTUBE_API_KEY"):
        environment.pop(name, None)
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
