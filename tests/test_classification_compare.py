"""Information-only comparison: fixed sample across sources, per-segment agreement, nothing stored."""

import json

import pytest
import sqlalchemy as sa

from classification_compare import compare_models, select_sample
from extensions import db
import models as m
from router_classifier import ClassifierError
from tests.test_classification_service import add_item, finish
from tests.test_classification_tasks import env, worker  # noqa: F401


def classified(env, external_id, source=None):  # noqa: F811
    env.item, env.transcript = add_item(env, external_id=external_id, source=source)
    revision_id, _ = finish(env)
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == revision_id)
                       .values(requested_model="claude-sonnet-5-5"))
    db.session.commit()
    return env.item.id


def invoke(worker, *args):  # noqa: F811
    return worker.app.test_cli_runner().invoke(args=["classification", *args])


def compare(env, **options):  # noqa: F811
    arguments = {"sample": 5, "seed": 1, "max_blocks": 4, "sleep": lambda seconds: None, "monotonic": lambda: 0.0,
                 **options}
    return compare_models(session=db.session, service=env.service, **arguments)


def test_the_sample_is_fixed_by_seed_and_spread_across_sources(worker):  # noqa: F811
    env = worker.env
    feed = [classified(env, f"feed-{n}") for n in range(3)]
    other = [classified(env, f"other-{n}", source=env.other) for n in range(2)]
    env.item, env.transcript = add_item(env, external_id="router-era")
    finish(env)  # Classified by the router: never sampled.
    picked = select_sample(db.session, sample=4, seed=7)
    assert picked == select_sample(db.session, sample=4, seed=7)
    assert len(picked) == 4 and set(picked) <= set(feed + other)
    assert len(set(picked) & set(other)) == 2 and len(set(picked) & set(feed)) == 2


def test_the_report_compares_labels_per_segment_and_stores_nothing(worker):  # noqa: F811
    env = worker.env
    classified(env, "compare-one")
    env.classifier.labels = {env.transcript.segments[1].id: ("paid_commercial", 0.99)}
    counts = (m.ClassificationRevision.query.count(), m.ClassificationBlock.query.count(), len(env.storage.puts))
    writes, statements = [], []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    ticks = iter([0.0, 2.5])
    sa.event.listen(db.engine, "before_cursor_execute", record)
    try:
        report = compare(env, monotonic=lambda: next(ticks))
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", record)
    assert statements and writes == []
    assert (m.ClassificationRevision.query.count(), m.ClassificationBlock.query.count(), len(env.storage.puts)) == counts
    total = report["total"]
    assert (report["selected"], total["items"], total["blocks"], total["segments"]) == (1, 1, 1, 3)
    assert total["agreement"]["editorial"] == {"agree": 2, "total": 3}
    assert total["raw_agreement"]["editorial"] == {"agree": 2, "total": 3}
    assert (total["editorial_to_excluded"], total["excluded_to_editorial"], total["model_uncertain_share"]) == (1, 0, 0.0)
    assert total["seconds_per_call"] == {"calls": 1, "mean": 2.5, "max": 2.5}
    assert report["episodes"][0]["seconds_per_call"] == {"calls": 1, "mean": 2.5, "max": 2.5}
    assert env.classifier.keys[-1].startswith("podcast.classification:compare-")
    assert ":rev-" not in env.classifier.keys[-1]
    rendered = json.dumps(report)
    assert "insight" not in rendered and "promo code" not in rendered


def test_agreement_is_reported_before_and_after_the_confidence_threshold(worker):  # noqa: F811
    env = worker.env
    classified(env, "near-miss")
    # A near-miss advert: the model says paid_commercial, but below the exclusion threshold.
    env.classifier.labels = {env.transcript.segments[1].id: ("paid_commercial", 0.10)}
    total = compare(env)["total"]
    assert total["raw_agreement"]["editorial"] == {"agree": 2, "total": 3}
    assert total["agreement"]["editorial"] == {"agree": 2, "total": 3}
    assert (total["raw_editorial_to_excluded"], total["editorial_to_excluded"]) == (1, 0)
    assert (total["raw_model_uncertain"], total["model_uncertain"], total["threshold_to_uncertain"]) == (0, 1, 1)
    assert total["model_uncertain_share"] == round(1 / 3, 4)


def test_a_busy_router_is_retried_with_back_off_on_one_key_then_the_episode_is_skipped(worker):  # noqa: F811
    env = worker.env
    classified(env, "busy-one")
    env.classifier.error = ClassifierError("router_busy", advance_key=False)
    env.classifier.calls.clear(), env.classifier.keys.clear()
    waits = []
    report = compare(env, sample=1, sleep=waits.append)
    assert waits == [30, 60, 120, 240, 480]
    assert len(set(env.classifier.keys)) == 1, "a busy retry must replay the same key"
    assert len({id(window) for window, _ in env.classifier.calls}) == 1, "retries resend the frozen inputs"
    assert (report["total"]["busy_skipped"], report["total"]["items"], report["total"]["blocks"]) == (1, 0, 0)
    assert report["episodes"][0]["skipped"] == "router_busy"


def test_a_busy_outcome_the_router_stored_advances_the_compare_key(worker):  # noqa: F811
    env = worker.env
    classified(env, "busy-stored")
    calls = []

    def fail_once():
        if calls:
            env.classifier.error = None
        calls.append(1)
    env.classifier.error = ClassifierError("router_busy", reason="router_failure", advance_key=True)
    env.classifier.on_call = fail_once
    report = compare(env, sample=1)
    assert report["total"]["blocks"] == 1 and len(calls) == 2
    first, second = env.classifier.keys[-2:]
    assert first.endswith(":g0") and second.endswith(":g1") and first[:-3] == second[:-3]


def test_invalid_model_output_is_counted_and_the_run_continues(worker):  # noqa: F811
    env = worker.env
    classified(env, "invalid-one")
    env.classifier.error = ClassifierError("model_invalid_output")
    total = compare(env, sample=1)["total"]
    assert (total["invalid_output"], total["blocks"], total["items"]) == (1, 0, 1)


def test_the_command_prints_json_and_stops_on_a_permanent_router_error(worker):  # noqa: F811
    env = worker.env
    classified(env, "cli-one")
    result = invoke(worker, "compare", "--sample", "1", "--max-blocks", "1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["total"]["items"] == 1
    env.classifier.error = ClassifierError("model_auth")
    result = invoke(worker, "compare", "--sample", "1")
    assert result.exit_code != 0 and "Comparison stopped: model_auth" in result.output


@pytest.mark.parametrize("arguments", [("--sample", "0"), ("--sample", "101"), ("--max-blocks", "51")])
def test_bounds_are_validated(worker, arguments):  # noqa: F811
    result = invoke(worker, "compare", *arguments)
    assert result.exit_code != 0 and not worker.env.classifier.calls
