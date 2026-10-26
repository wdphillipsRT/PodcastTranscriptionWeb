"""Restricted, budgeted Claude guest extraction; never a live Claude call."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import subprocess

import pytest
import sqlalchemy as sa

from claude_classifier import ClassifierError
from config import ConfigError, Settings, load_settings
from extensions import db
from integrations.knowledge.people import guest_input_sha256
import models as m
from tests.test_claude_classifier import PipeChild
from tests.test_classification_service import add_item, env, finish  # noqa: F401
from tests.test_config import valid_environment  # noqa: F401
from transcripts import canonical_json
import guest_extractor


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
TITLE = "Habits with James Clear"
DESCRIPTION = "<p>James Clear and <b>Ada Host</b> discuss habits. Sponsored by Example Corp.</p>"


@pytest.fixture
def settings():
    return Settings("test-secret", "postgresql://db/app", "postgresql://db/app", "redis://redis/0",
                    "redis://redis/1", "us-east-1", "podcast-transcriptions-web-2025", "test-key",
                    GUEST_EXTRACTION_ENABLED=True)


def reply(guests, *, cost=.004, model="claude-sonnet-5-5"):
    return json.dumps({"structured_output": {"guests": guests}, "total_cost_usd": cost,
                       "modelUsage": {model: {}}, "result": "secret raw telemetry"}).encode()


@pytest.fixture
def run(tmp_path, monkeypatch, settings):
    calls, children = [], []
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(tmp_path),
                   "AWS_SECRET_ACCESS_KEY": "secret", "HOME": "/secret"}
    monkeypatch.setattr(os, "killpg", lambda pid, sig: [child.killed.set() for child in children])

    def invoke(output, *, configuration=None, **arguments):
        def factory(argv, **kwargs):
            calls.append((argv, kwargs))
            child = PipeChild(output)
            children.append(child)
            return child
        extractor = guest_extractor.ClaudeGuestExtractor(configuration or settings, environment=environment,
                                                         popen_factory=factory)
        values = dict(show="People Show", hosts=("Ada Host",), title=TITLE, description=DESCRIPTION)
        values.update(arguments)
        return extractor.extract(**values)

    yield invoke, calls, children, environment
    for child in children:
        child.killed.set()
        child.thread.join(2)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def test_invocation_is_restricted_and_input_is_an_untrusted_envelope(run, settings):
    invoke, calls, children, environment = run
    result = invoke(reply([{"name": "James Clear", "confidence": 0.92}]))
    argv, kwargs = calls[0]
    assert argv == ["claude", "--restricted", "--safe-mode", "-p", "--tools", "", "--disallowedTools", "mcp__*",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--no-chrome", "--disable-slash-commands",
        "--no-session-persistence", "--permission-prompts", "none", "--max-turns", "1", "--output-format", "json",
        "--json-schema", canonical_json(guest_extractor.OUTPUT_SCHEMA), "--model", "claude-sonnet-5-5",
        "--max-budget-usd", "0.05", "--system-prompt", guest_extractor.SYSTEM_PROMPT]
    assert kwargs["shell"] is False and kwargs["start_new_session"] is True
    assert set(kwargs["env"]) == {"PATH", "LANG", "CLAUDE_CONFIG_DIR", "DISABLE_UPDATES",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_POLICY_SKILLS",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"}
    assert json.loads(bytes(children[0].input)) == {
        "schema": "episode-guest-extraction-input/v1", "untrusted_quoted_data": True, "show": "People Show",
        "known_hosts": ["Ada Host"], "title": TITLE,
        "description": "James Clear and Ada Host discuss habits. Sponsored by Example Corp."}
    assert result.guests == (("James Clear", 0.92),) and result.cost_microusd == 4000
    assert "secret raw telemetry" not in repr(result)


def test_only_names_found_verbatim_in_the_input_are_kept(run):
    invoke, *_ = run
    result = invoke(reply([{"name": "james  clear", "confidence": 0.9}, {"name": "Invented Person", "confidence": 1},
                           {"name": "James Clear", "confidence": 0.5}, {"name": "!!!", "confidence": 0.7},
                           {"name": "Example Corp", "confidence": 0.123}]))
    assert result.guests == (("james clear", 0.9), ("Example Corp", 0.12))


def test_long_descriptions_are_truncated_before_the_call(run):
    invoke, _, children, _ = run
    invoke(reply([]), description="word " * 5000)
    sent = json.loads(bytes(children[0].input))["description"]
    assert len(sent) == guest_extractor.MAX_DESCRIPTION_CHARS


@pytest.mark.parametrize("output, category", [
    (reply([], model="claude-opus-5-5"), "model_mismatch"),
    (reply([], cost=0.06), "claude_invalid_output"),
    (reply([{"name": "James Clear"}]), "claude_invalid_output"),
    (reply([{"name": "James Clear", "confidence": 2}]), "claude_invalid_output"),
    (reply([{"name": "James Clear", "confidence": 0.5}] * 11), "claude_invalid_output"),
    (b"not json", "claude_invalid_output"),
])
def test_invalid_results_fail_closed(run, output, category):
    invoke, *_ = run
    with pytest.raises(ClassifierError) as caught:
        invoke(output)
    assert caught.value.category == category


def test_guest_settings_defaults_and_validation(valid_environment):  # noqa: F811
    settings = load_settings(valid_environment)
    assert settings.GUEST_EXTRACTION_ENABLED is False
    assert settings.CLAUDE_GUEST_MODEL == "claude-sonnet-5-5"
    assert settings.GUEST_EXTRACTION_MAX_BUDGET_USD == Decimal("0.05")
    assert settings.GUEST_EXTRACTION_DAILY_BUDGET_USD == Decimal("1.00")
    assert settings.GUEST_EXTRACTION_MAX_ITEMS_PER_RUN == 25
    for name, value in (("GUEST_EXTRACTION_ENABLED", "yes"), ("CLAUDE_GUEST_MODEL", "haiku"),
                        ("GUEST_EXTRACTION_MAX_BUDGET_USD", "0"), ("GUEST_EXTRACTION_DAILY_BUDGET_USD", "0.01"),
                        ("GUEST_EXTRACTION_MAX_ITEMS_PER_RUN", "101"), ("GUEST_EXTRACTION_TIMEOUT_SECONDS", "601")):
        with pytest.raises(ConfigError, match=name if name != "GUEST_EXTRACTION_DAILY_BUDGET_USD" else "DAILY"):
            load_settings({**valid_environment, name: value})


class FakeExtractor:
    def __init__(self):
        self.calls, self.error, self.cost = [], None, 4000

    def extract(self, *, show, hosts, title, description):
        assert not db.session().in_transaction(), "Claude call held a database transaction"
        self.calls.append((show, hosts, title, description))
        if self.error:
            raise self.error
        return guest_extractor.GuestResult((("James Clear", 0.92),), self.cost)


@pytest.fixture
def ready(env):  # noqa: F811
    finish(env)
    env.item.title, env.item.description = TITLE, DESCRIPTION
    env.source.feed_hosts = ["Ada Host"]
    db.session.commit()
    env.extractor = FakeExtractor()
    env.guest_settings = replace(env.settings, GUEST_EXTRACTION_ENABLED=True)
    return env


def extract(ready, **changes):
    return guest_extractor.extract_pending_guests(session=db.session, extractor=ready.extractor,
        settings=replace(ready.guest_settings, **changes), now=NOW)


def test_extraction_runs_once_per_input_and_records_cost(ready):
    assert extract(ready) == {"scanned": 1, "extracted": 1, "failed": 0, "stopped": None}
    row = m.GuestExtraction.query.one()
    assert (row.state, row.guests, row.cost_microusd, row.model) == (
        "completed", [{"name": "James Clear", "confidence": 0.92}], 4000, "claude-sonnet-5-5")
    assert row.input_sha256 == guest_input_sha256(ready.item)
    assert ready.extractor.calls == [("Feed", ("Ada Host",), TITLE, DESCRIPTION)]
    assert extract(ready)["scanned"] == 0
    ready.item.title = "Renamed episode with James Clear"
    db.session.commit()
    assert extract(ready)["extracted"] == 1 and len(ready.extractor.calls) == 2


def test_disabled_or_unneeded_items_make_no_call(ready):
    assert extract(ready, GUEST_EXTRACTION_ENABLED=False)["stopped"] == "disabled"
    ready.item.feed_guests = ["Feed Guest"]
    db.session.commit()
    assert extract(ready)["scanned"] == 0
    ready.item.feed_guests, ready.item.admin_guests = None, []
    db.session.commit()
    assert extract(ready)["scanned"] == 0
    ready.item.admin_guests, ready.source.feed_hosts = None, None
    db.session.commit()
    assert extract(ready)["scanned"] == 0
    assert not ready.extractor.calls


def test_daily_budget_counts_todays_spend_and_stops_before_the_call(ready):
    other, _ = add_item(ready, external_id="other")
    db.session.add(m.GuestExtraction(item_id=other.id, input_sha256="f" * 64, state="completed", guests=[],
        model="claude-sonnet-5-5", cost_microusd=970_000, created_at=NOW.replace(hour=1)))
    db.session.commit()
    assert extract(ready)["stopped"] == "daily_budget" and not ready.extractor.calls
    assert extract(ready, GUEST_EXTRACTION_DAILY_BUDGET_USD=Decimal("1.03"))["extracted"] == 1


def test_item_specific_failures_are_recorded_and_transient_failures_stop(ready):
    ready.extractor.error = ClassifierError("claude_timeout")
    assert extract(ready)["stopped"] == "claude_timeout"
    assert m.GuestExtraction.query.count() == 0
    ready.extractor.error = ClassifierError("claude_invalid_output")
    assert extract(ready)["failed"] == 1
    row = m.GuestExtraction.query.one()
    assert (row.state, row.error_category, row.cost_microusd) == ("failed", "claude_invalid_output", 50_000)
    assert extract(ready)["scanned"] == 0


def test_task_runs_on_the_classifier_queue_with_the_app_settings(ready, monkeypatch):
    from flask import current_app
    import tasks
    monkeypatch.setattr(tasks, "get_guest_extractor", lambda: ready.extractor)
    current_app.config["GUEST_EXTRACTION_ENABLED"] = True
    assert tasks.extract_guests.run()["extracted"] == 1
    assert tasks.extract_guests.queue == "classification"


@pytest.mark.parametrize("enabled, expected", [("true", True), ("false", False)])
def test_hourly_guest_extraction_is_scheduled_only_when_enabled(enabled, expected):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    script = ("import celeryconfig, json; entry = celeryconfig.beat_schedule.get('guest-extraction');"
              "print(json.dumps([entry, celeryconfig.task_routes['tasks.extract_guests']]))")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "GUEST_EXTRACTION_ENABLED": enabled,
             "GUEST_EXTRACTION_INTERVAL_SECONDS": "1800"})
    entry, route = json.loads(result.stdout)
    assert route == {"queue": "classification"}
    assert entry == ({"task": "tasks.extract_guests", "schedule": 1800.0} if expected else None)
