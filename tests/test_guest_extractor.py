"""Router guest extraction and its call-limited batch runner; never a live router call."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ConfigError, load_settings
from extensions import db
from integrations.knowledge.people import guest_input_sha256
import models as m
from router_client import (CONNECT_TIMEOUT_SECONDS, GUEST_MAX_OUTPUT_TOKENS, READ_MARGIN_SECONDS, ROUTE_LIMITS,
                           RouterClient, RouterError, estimated_context_tokens)
from tests.router_fake import MODEL, FakeRouter, fake_router  # noqa: F401
from tests.router_schema_validator import validate_schema
from tests.test_classification_service import add_item, env, finish  # noqa: F401
from tests.test_config import valid_environment  # noqa: F401
import guest_extractor


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
TITLE = "Habits with James Clear"
DESCRIPTION = "<p>James Clear and <b>Ada Host</b> discuss habits. Sponsored by Example Corp.</p>"


@pytest.fixture
def run(fake_router, valid_environment):  # noqa: F811
    settings = load_settings({**valid_environment, "GUEST_EXTRACTION_ENABLED": "true"})
    extractor = guest_extractor.RouterGuestExtractor(settings, client=RouterClient(
        fake_router.url, token_file=fake_router.token_file, expected_model=MODEL))

    def invoke(guests=None, **arguments):
        if guests is not None:
            fake_router.output = {"guests": guests}
        values = dict(show="People Show", hosts=("Ada Host",), title=TITLE, description=DESCRIPTION)
        values.update(arguments)
        return extractor.extract(**values)
    return invoke, fake_router


def test_extraction_sends_one_guest_task_and_keeps_verbatim_names(run):
    invoke, router = run
    result = invoke([{"name": "James Clear", "confidence": 0.92}, {"name": "Invented Person", "confidence": 1}],
                    request_key="podcast.guests:item-1:abc:20261022T120000Z")
    assert result == guest_extractor.GuestResult((("James Clear", 0.92),), 0)
    _, _, headers, body = router.requests[-1]
    assert headers["Idempotency-Key"] == "podcast.guests:item-1:abc:20261022T120000Z"
    assert (body["route"], body["prompt_version"], body["generation"]["max_output_tokens"],
            body["requirements"]["max_latency_ms"]) == ("podcast.guests", "episode-guest-extraction/v1", 1024, 120_000)
    assert body["messages"][0] == {"role": "system", "content": guest_extractor.SYSTEM_PROMPT}
    assert json.loads(body["messages"][1]["content"]) == {
        "schema": "episode-guest-extraction-input/v1", "untrusted_quoted_data": True, "show": "People Show",
        "known_hosts": ["Ada Host"], "title": TITLE,
        "description": "James Clear and Ada Host discuss habits. Sponsored by Example Corp."}


def test_the_guest_schema_is_in_the_router_subset():
    validate_schema(guest_extractor.OUTPUT_SCHEMA)


def test_only_names_found_verbatim_in_the_input_are_kept(run):
    invoke, _ = run
    result = invoke([{"name": "james  clear", "confidence": 0.9}, {"name": "Invented Person", "confidence": 1},
                     {"name": "James Clear", "confidence": 0.5}, {"name": "!!!", "confidence": 0.7},
                     {"name": "Example Corp", "confidence": 0.123}])
    assert result.guests == (("James Clear", 0.9), ("Example Corp", 0.12))


def test_names_match_whole_words_within_the_title_or_description(run):
    invoke, _ = run
    result = invoke([{"name": "Ann", "confidence": 0.9}, {"name": "Clear Discuss", "confidence": 0.9},
                     {"name": "jane doe", "confidence": 0.8}, {"name": "james clear", "confidence": 0.7}],
                    title="Annual habits with James Clear",
                    description="Discuss habits with Ｊａｎｅ Ｄｏｅ.")
    assert result.guests == (("Jane Doe", 0.8), ("James Clear", 0.7))


def test_long_descriptions_are_truncated_before_the_call(run):
    invoke, router = run
    invoke([], description="word " * 5000)
    sent = json.loads(router.requests[-1][3]["messages"][1]["content"])["description"]
    assert len(sent) == guest_extractor.MAX_DESCRIPTION_CHARS


def test_the_largest_possible_input_fits_the_guest_route(run):
    """Bounded show, title, description and hosts always fit podcast.guests (32,768 context, 1,024 output),
    even when every character is JSON-escaped, so no item is refused as too large."""
    invoke, router = run
    escaped = "\x01"
    invoke([], show=escaped * 1000, hosts=tuple(f"{index}" + escaped * 299 for index in range(12)),
           title=escaped * 2000, description=escaped * 20_000)
    body = router.requests[-1][3]
    estimate = estimated_context_tokens(body["messages"], body["response_schema"],
                                        body["generation"]["max_output_tokens"])
    assert body["generation"]["max_output_tokens"] == GUEST_MAX_OUTPUT_TOKENS
    assert estimate <= ROUTE_LIMITS["podcast.guests"][0] - 4096


@pytest.mark.parametrize("setup, category", [
    (lambda router: setattr(router, "model", "other:1b"), "model_mismatch"),
    (lambda router: router.reply(502, FakeRouter.error("MODEL_OUTPUT_INVALID")), "model_invalid_output"),
    (lambda router: router.reply(429, FakeRouter.error("MODEL_RATE_LIMITED")), "router_busy"),
    (lambda router: router.reply(200, router.succeeded({}, {"guests": [{"name": "James Clear"}]})), "model_invalid_output"),
    (lambda router: router.reply(200, router.succeeded({}, {"guests": [{"name": "James Clear", "confidence": 2}]})), "model_invalid_output"),
    (lambda router: router.reply(200, router.succeeded({}, {"guests": [{"name": "J C", "confidence": 0.5}] * 11})), "model_invalid_output"),
    (lambda router: router.reply(200, router.succeeded({}, ["not", "an", "object"])), "model_invalid_output"),
])
def test_invalid_results_and_router_failures_fail_closed(run, setup, category):
    invoke, router = run
    router.output = {"guests": []}
    setup(router)
    with pytest.raises(RouterError) as caught:
        invoke()
    assert caught.value.category == category


def test_a_router_failure_body_keeps_its_reason(run):
    invoke, router = run
    router.reply(504, FakeRouter.error("MODEL_TIMEOUT"))
    with pytest.raises(RouterError) as caught:
        invoke([])
    assert (caught.value.category, caught.value.reason) == ("router_busy", "router_failure")


class _BrokenClient:
    expected_model = MODEL

    def run(self, *arguments, **keywords):
        raise OSError("/router/token: secret detail")


def test_failures_carry_no_exception_chain(valid_environment, monkeypatch):  # noqa: F811
    settings = load_settings({**valid_environment, "GUEST_EXTRACTION_ENABLED": "true"})
    extractor = guest_extractor.RouterGuestExtractor(settings, client=_BrokenClient())
    with pytest.raises(RouterError) as caught:
        extractor.extract(show="Show", hosts=(), title=TITLE, description=DESCRIPTION)
    assert caught.value.category == "model_config"
    assert caught.value.__cause__ is None and caught.value.__context__ is None

    def broken(*arguments, **keywords):
        raise ValueError("secret detail")
    monkeypatch.setattr(guest_extractor, "person_names", broken)
    with pytest.raises(RouterError) as caught:
        guest_extractor._result({"guests": [{"name": "James Clear", "confidence": 0.5}]}, ())
    assert caught.value.category == "model_invalid_output"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_guest_settings_defaults_and_validation(valid_environment):  # noqa: F811
    settings = load_settings(valid_environment)
    assert settings.GUEST_EXTRACTION_ENABLED is False
    assert (settings.GUEST_EXTRACTION_MAX_CALLS_PER_DAY, settings.GUEST_EXTRACTION_MAX_ITEMS_PER_RUN,
            settings.GUEST_EXTRACTION_TIMEOUT_SECONDS) == (200, 25, 120)
    for name, value in (("GUEST_EXTRACTION_ENABLED", "yes"), ("GUEST_EXTRACTION_MAX_ITEMS_PER_RUN", "101"),
                        ("GUEST_EXTRACTION_TIMEOUT_SECONDS", "601"), ("GUEST_EXTRACTION_TIMEOUT_SECONDS", "4"),
                        ("GUEST_EXTRACTION_INTERVAL_SECONDS", "3599"), ("GUEST_EXTRACTION_MAX_CALLS_PER_DAY", "2401")):
        with pytest.raises(ConfigError, match=name):
            load_settings({**valid_environment, name: value})


class FakeExtractor:
    def __init__(self):
        self.calls, self.keys, self.error, self.errors = [], [], None, []

    def extract(self, *, show, hosts, title, description, request_key=None):
        assert not db.session().in_transaction(), "Router call held a database transaction"
        self.calls.append((show, hosts, title, description))
        self.keys.append(request_key)
        error = self.errors.pop(0) if self.errors else self.error
        if error:
            raise error
        return guest_extractor.GuestResult((("James Clear", 0.92),))


@pytest.fixture
def ready(env):  # noqa: F811
    finish(env)
    env.item.title, env.item.description = TITLE, DESCRIPTION
    env.source.feed_hosts = ["Ada Host"]
    db.session.commit()
    env.extractor = FakeExtractor()
    env.guest_settings = replace(env.settings, GUEST_EXTRACTION_ENABLED=True)
    return env


def extract(ready, *, clock=lambda: NOW, monotonic=lambda: 0.0, **changes):
    return guest_extractor.extract_pending_guests(session=db.session, extractor=ready.extractor,
        settings=replace(ready.guest_settings, **changes), clock=clock, monotonic=monotonic)


def newer_item(ready):
    """A newer candidate, extracted before the fixture's item (newest first)."""
    other, _ = add_item(ready, external_id="newer")
    other.title, other.description = TITLE, DESCRIPTION
    db.session.commit()
    revision_id = ready.service.ensure_revision(other.id, "backfill")
    for ordinal in ready.service.prepare_revision(revision_id).block_ordinals:
        ready.service.run_block(revision_id, ordinal)
    ready.service.finalize_revision(revision_id)
    return other


def recorded(item, *, created_at, state="completed"):
    db.session.add(m.GuestExtraction(item_id=item.id, input_sha256="f" * 64, state=state, guests=[],
                                     model=MODEL, cost_microusd=0, created_at=created_at))
    db.session.commit()


def test_extraction_runs_once_per_input_and_records_the_router_model(ready):
    assert extract(ready) == {"scanned": 1, "extracted": 1, "failed": 0, "stopped": None}
    row = m.GuestExtraction.query.one()
    assert (row.state, row.guests, row.cost_microusd, row.model) == (
        "completed", [{"name": "James Clear", "confidence": 0.92}], 0, MODEL)
    assert row.input_sha256 == guest_input_sha256(ready.item)
    assert ready.extractor.calls == [("Feed", ("Ada Host",), TITLE, DESCRIPTION)]
    assert ready.extractor.keys == [f"podcast.guests:item-{ready.item.id}:{row.input_sha256[:16]}:20261022T120000Z"]
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


def test_sources_whose_hosts_cannot_be_exported_make_no_call(ready):
    ready.source.feed_hosts = ["!!!", "—"]
    db.session.commit()
    assert extract(ready)["scanned"] == 0 and not ready.extractor.calls
    ready.source.admin_hosts = ["Ada Host"]
    db.session.commit()
    assert extract(ready)["extracted"] == 1


def test_the_daily_call_limit_counts_todays_calls_and_stops_before_the_call(ready):
    other, _ = add_item(ready, external_id="other")
    recorded(other, created_at=NOW.replace(hour=1))
    assert extract(ready, GUEST_EXTRACTION_MAX_CALLS_PER_DAY=1)["stopped"] == "daily_limit"
    assert not ready.extractor.calls
    assert extract(ready, GUEST_EXTRACTION_MAX_CALLS_PER_DAY=2)["extracted"] == 1


def test_calls_from_earlier_days_do_not_count(ready):
    other, _ = add_item(ready, external_id="yesterday")
    recorded(other, created_at=NOW.replace(day=21))
    assert extract(ready, GUEST_EXTRACTION_MAX_CALLS_PER_DAY=1)["extracted"] == 1


@pytest.mark.parametrize("category", ["model_auth", "model_config", "model_mismatch"])
def test_environmental_failures_stop_the_run_unrecorded(ready, category):
    newer_item(ready)
    ready.extractor.error = RouterError(category)
    assert extract(ready) == {"scanned": 1, "extracted": 0, "failed": 0, "stopped": category}
    assert m.GuestExtraction.query.count() == 0 and len(ready.extractor.calls) == 1


def test_a_busy_router_stops_the_run_unrecorded_and_the_item_waits_for_the_next_run(ready):
    newer_item(ready)
    ready.extractor.errors = [RouterError("router_busy", advance_key=False)]
    assert extract(ready) == {"scanned": 1, "extracted": 0, "failed": 0, "stopped": "router_busy"}
    assert m.GuestExtraction.query.count() == 0
    assert extract(ready) == {"scanned": 2, "extracted": 2, "failed": 0, "stopped": None}


def test_a_router_failure_is_recorded_and_stops_the_run_so_the_next_run_moves_on(ready):
    """A provider timeout (504 MODEL_TIMEOUT) would recur for the newest item and block every run."""
    newer = newer_item(ready)
    ready.extractor.errors = [RouterError("router_busy", reason="router_failure")]
    assert extract(ready) == {"scanned": 1, "extracted": 0, "failed": 1, "stopped": "router_busy"}
    failed = m.GuestExtraction.query.one()
    assert (failed.item_id, failed.state, failed.error_category, failed.cost_microusd, failed.model) == (
        newer.id, "failed", "router_busy", 0, MODEL)
    assert extract(ready) == {"scanned": 1, "extracted": 1, "failed": 0, "stopped": None}
    assert m.GuestExtraction.query.filter_by(item_id=ready.item.id).one().state == "completed"


@pytest.mark.parametrize("reason", ["in_progress", "rate_limited", "timeout", "transport", "proxy", "no_candidate"])
def test_other_busy_reasons_stay_unrecorded(ready, reason):
    newer_item(ready)
    ready.extractor.errors = [RouterError("router_busy", reason=reason, advance_key=False)]
    assert extract(ready)["stopped"] == "router_busy"
    assert m.GuestExtraction.query.count() == 0


def test_invalid_output_is_recorded_once_and_the_run_continues(ready):
    newer = newer_item(ready)
    ready.extractor.errors = [RouterError("model_invalid_output")]
    assert extract(ready) == {"scanned": 2, "extracted": 1, "failed": 1, "stopped": None}
    failed = m.GuestExtraction.query.filter_by(item_id=newer.id).one()
    assert (failed.state, failed.error_category, failed.cost_microusd, failed.model) == (
        "failed", "model_invalid_output", 0, MODEL)
    assert extract(ready)["scanned"] == 0 and len(ready.extractor.calls) == 2


def test_todays_calls_are_reread_per_call_and_rows_are_stamped_when_recorded(ready):
    newer_item(ready)
    late = datetime(2026, 10, 22, 23, 59, 59, tzinfo=timezone.utc)
    early = datetime(2026, 10, 23, 0, 0, 1, tzinfo=timezone.utc)
    other, _ = add_item(ready, external_id="counted")
    recorded(other, created_at=late.replace(hour=1))
    clock = lambda: late if not ready.extractor.calls else early  # noqa: E731
    assert extract(ready, clock=clock, GUEST_EXTRACTION_MAX_CALLS_PER_DAY=2)["extracted"] == 2
    stamps = {row.created_at for row in m.GuestExtraction.query.filter(m.GuestExtraction.item_id != other.id)}
    assert stamps == {early}


def test_no_call_starts_that_could_outlast_the_run_deadline(ready):
    """A call can take its deadline plus the client's connect timeout and read margin."""
    newer_item(ready)
    worst = 60 + CONNECT_TIMEOUT_SECONDS + READ_MARGIN_SECONDS
    last_start = guest_extractor.RUN_DEADLINE_SECONDS - worst
    monotonic = lambda: 0.0 if not ready.extractor.calls else last_start + 1.0  # noqa: E731
    assert extract(ready, monotonic=monotonic, GUEST_EXTRACTION_TIMEOUT_SECONDS=60) == {
        "scanned": 1, "extracted": 1, "failed": 0, "stopped": "run_deadline"}
    assert extract(ready, monotonic=lambda: float(last_start), GUEST_EXTRACTION_TIMEOUT_SECONDS=60)["extracted"] == 1


def test_the_longest_allowed_call_still_fits_one_run(ready):
    assert 600 + CONNECT_TIMEOUT_SECONDS + READ_MARGIN_SECONDS <= guest_extractor.RUN_DEADLINE_SECONDS
    assert extract(ready, GUEST_EXTRACTION_TIMEOUT_SECONDS=600)["extracted"] == 1


class _Connection:
    def __init__(self, acquired):
        self.acquired, self.statements, self.closed = acquired, [], False

    def execute(self, statement, parameters=None):
        self.statements.append((str(statement), parameters))
        return SimpleNamespace(scalar=lambda: self.acquired)

    def commit(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("acquired", [True, False])
def test_postgres_runs_hold_a_session_advisory_lock(ready, monkeypatch, acquired):
    connection = _Connection(acquired)
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection)
    monkeypatch.setattr(db.session, "get_bind", lambda *args, **kwargs: engine)
    counts = extract(ready)
    statements = [text for text, _ in connection.statements]
    assert "pg_try_advisory_lock" in statements[0] and connection.closed
    if acquired:
        assert counts["extracted"] == 1 and "pg_advisory_unlock" in statements[1]
        assert connection.statements[0][1] == connection.statements[1][1]
    else:
        assert counts == {"scanned": 0, "extracted": 0, "failed": 0, "stopped": "busy"}
        assert len(statements) == 1 and not ready.extractor.calls


def test_task_runs_on_the_classifier_queue_with_the_app_settings(ready, monkeypatch):
    from flask import current_app
    import tasks
    monkeypatch.setattr(tasks, "get_guest_extractor", lambda: ready.extractor)
    current_app.config["GUEST_EXTRACTION_ENABLED"] = True
    assert tasks.extract_guests.run()["extracted"] == 1
    assert tasks.extract_guests.queue == "classification"


def test_the_production_factory_builds_a_router_extractor(app):
    import tasks
    with app.app_context():
        extractor = tasks.get_guest_extractor()
    assert isinstance(extractor, guest_extractor.RouterGuestExtractor)
    assert extractor.client.expected_model == MODEL and extractor.client.token_file == "/router/token"


@pytest.mark.parametrize("enabled, expected", [("true", True), ("false", False)])
def test_hourly_guest_extraction_is_scheduled_only_when_enabled(enabled, expected):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    script = ("import celeryconfig, json; entry = celeryconfig.beat_schedule.get('guest-extraction');"
              "print(json.dumps([entry, celeryconfig.task_routes['tasks.extract_guests']]))")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "GUEST_EXTRACTION_ENABLED": enabled,
             "GUEST_EXTRACTION_INTERVAL_SECONDS": "7200"})
    entry, route = json.loads(result.stdout)
    assert route == {"queue": "classification"}
    assert entry == ({"task": "tasks.extract_guests", "schedule": 7200.0} if expected else None)


@pytest.mark.parametrize("interval", ["1800", "3599", "abc", "3600.5", ""])
def test_beat_refuses_an_unvalidated_guest_interval(interval):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", "import celeryconfig"], cwd=root, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "GUEST_EXTRACTION_ENABLED": "true",
             "GUEST_EXTRACTION_INTERVAL_SECONDS": interval})
    assert result.returncode != 0 and "GUEST_EXTRACTION_INTERVAL_SECONDS" in result.stderr
