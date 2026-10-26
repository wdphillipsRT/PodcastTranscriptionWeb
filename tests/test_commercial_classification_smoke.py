"""Offline acceptance at the implemented pre-bundle/pre-publisher boundaries."""

from collections import Counter
from hashlib import sha256
import json
import logging
import socket
from time import monotonic

import pytest

from commercial_policy import SegmentPolicy, PromotionalProjection
from extensions import db
from learning_projection import (LearningProjectionUnavailable, assert_citable,
    load_active_learning_projection, load_promotional_projection)
import models as m
import tasks
from router_classifier import ClassifierError
from tests.test_classification_service import env  # shared offline storage/classifier boundaries
from transcripts import normalize_vtt


EVENT_FIELDS = {"item_id", "source_id", "revision_id", "policy_version",
    "stage_duration_ms", "label_counts", "duration_ratios", "review_required",
    "retry_category", "rule_match_counts"}
TEXTS = ("Synthetic lesson introduction", "Synthetic sponsor purchase offer",
    "Synthetic creator course offer", "Synthetic neighboring show offer",
    "Synthetic subscription reminder", "Synthetic ambiguous transition",
    "Synthetic teaching call to action", "Synthetic closing lesson")
LABELS = ("editorial", "paid_commercial", "self_promotion", "cross_promotion",
    "housekeeping", "uncertain", "editorial", "editorial")


@pytest.fixture
def acceptance(env, app, monkeypatch, caplog):
    from web import routes
    def forbidden(*args, **kwargs):
        pytest.fail("provider/media/network boundary invoked")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for method in ("get_youtube_media_client", "get_processing_http_session"):
        monkeypatch.setattr(tasks, method, forbidden)
    import episode_fetcher, transcribe
    for target, method in ((episode_fetcher, "download_podcast_audio"),
            (episode_fetcher, "reacquire_podcast_media"), (transcribe, "transcribe_audio_from_s3")):
        monkeypatch.setattr(target, method, forbidden)
    monkeypatch.setattr(tasks, "get_classification_service", lambda: env.service)
    monkeypatch.setattr(tasks, "get_processing_storage", lambda: env.storage)
    monkeypatch.setattr(routes, "get_s3_manager", lambda *args: env.storage)
    app.config["CLASSIFICATION_ENABLED"] = True
    # The shared fixture's seed item is outside this acceptance run's backlog.
    env.item.status = "failed"
    db.session.commit()
    env.sent = []
    def send(name, *, args=None, **options):
        assert not db.session().in_transaction(), "broker publication before commit"
        if name == "tasks.classify_item":
            row = db.session.get(m.ClassificationRevision, args[0])
            assert row.error_category == "dispatch_pending"
            item_id = row.item_id
            db.session.commit()
            with tasks._item_lock(item_id) as acquired:
                assert acquired, "broker publication before lock release"
        env.sent.append((name, args, options))
    monkeypatch.setattr(tasks.celery_app, "send_task", send)
    env.client = app.test_client()
    with env.client.session_transaction() as session:
        session["_user_id"], session["_fresh"] = str(env.owner.id), True
    caplog.set_level(logging.INFO, logger="tasks")
    return env


def canonical(env, platform, *, source=None, suffix="main", texts=TEXTS):
    source = source or env.source
    if platform == "youtube" and source is env.source:
        source = env.other
        source.source_type = "youtube_channel"
    item = m.ContentItem(admin_id=env.owner.id, platform=platform,
        item_type="podcast_episode" if platform == "podcast" else "youtube_video",
        external_id=f"{platform}-{suffix}", canonical_url="https://synthetic.invalid/URL_SECRET",
        title="Synthetic acceptance item", status="completed")
    db.session.add(item)
    db.session.flush()
    if platform == "youtube":
        item.external_id = f"syn{item.id:08d}"
    def stamp(seconds):
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}.000"
    vtt = "WEBVTT\n\n" + "\n\n".join(
        f"{stamp(i * 2)} --> {stamp(i * 2 + 1)}\n{text}" for i, text in enumerate(texts))
    transcript = normalize_vtt(vtt, platform=platform, external_id=item.external_id,
        item_id=item.id, source_id=source.id, language="en", tool_version="synthetic",
        origin="creator_caption", provider_version="synthetic", canonical_url=item.canonical_url)
    raw = transcript.to_json().encode()
    item.transcript_sha256, item.segments_sha256 = transcript.transcript_sha256, sha256(raw).hexdigest()
    prefix = f"items/{platform}/{item.external_id}/transcripts/{item.transcript_sha256}"
    item.segments_s3_key, item.transcript_s3_key = prefix + ".json", prefix + ".txt"
    env.storage.objects[item.segments_s3_key] = raw
    env.storage.objects[item.transcript_s3_key] = transcript.to_text().encode()
    db.session.add(m.SourceItem(item_id=item.id, source_id=source.id))
    db.session.commit()
    return item, transcript


def final_hook(env, item):
    # Enter the real final-stage hook with an existing canonical completed item.
    # Acquisition/normalization already happened and are deliberately out of scope.
    result = tasks._ProcessingPipeline(tasks.process_item)._complete(item)
    assert result and result[0] == "classification"
    row = db.session.get(m.ClassificationRevision, result[1].revision_id)
    assert row.request_reason == "transcript_completed" and row.error_category == "dispatch_pending"
    db.session.commit()
    tasks._publish_classification(result[1])
    return result[1].revision_id


def drain(env):
    while env.sent:
        name, args, options = env.sent.pop(0)
        assert name in {"tasks.classify_item", "tasks.scan_source_rule"}
        getattr(tasks, name.removeprefix("tasks.")).run(*args)


def load(env, item_id):
    db.session.commit()
    return load_active_learning_projection(session=db.session, storage=env.storage, item_id=item_id)


def policy(env, revision_id, transcript):
    row = db.session.get(m.ClassificationRevision, revision_id)
    return SegmentPolicy.from_json(env.storage.objects[row.policy_s3_key], transcript=transcript)


def assert_safe_events(env, caplog, *, required):
    records = [r for r in caplog.records if r.name == "tasks" and hasattr(r, "commercial_event")]
    assert required <= {r.msg for r in records}, "missing structured classification observations"
    for record in records:
        event = record.commercial_event
        assert set(event) <= EVENT_FIELDS
        assert {"item_id", "source_id", "revision_id", "policy_version", "stage_duration_ms"} <= set(event)
        assert type(event["stage_duration_ms"]) is int and event["stage_duration_ms"] >= 0
        assert record.args == () and not record.exc_info and not record.stack_info
    logged = caplog.text + json.dumps([r.commercial_event for r in records])
    private = [*TEXTS, "URL_SECRET", "CREDENTIAL_SECRET", "PRIVATE_DIAGNOSTIC",
        "https://", "items/", "requested_model", "prompt_sha256", "seg-"]
    private += [env.item.transcript_sha256, *env.storage.objects]
    assert all(value not in logged for value in private)
    return records


@pytest.mark.parametrize("platform", ["podcast", "youtube"])
def test_completed_item_to_review_correction_and_filtered_consumers(acceptance, platform, caplog):
    env = acceptance
    item, transcript = canonical(env, platform)
    env.classifier.labels = {s.id: (label, 1.0) for s, label in zip(transcript.segments, LABELS)}
    original = {key: env.storage.objects[key] for key in (item.segments_s3_key, item.transcript_s3_key)}
    identity = (item.transcript_sha256, item.segments_sha256, transcript.segments)
    first = final_hook(env, item)
    drain(env)
    first_row = db.session.get(m.ClassificationRevision, first)
    old_keys = (first_row.policy_s3_key, first_row.projection_s3_key)
    old_artifacts = {key: env.storage.objects[key] for key in old_keys}
    result = load(env, item.id)
    positions = {s.id: i for i, s in enumerate(transcript.segments)}
    covered = [s.id for r in policy(env, first, transcript).ranges
        for s in transcript.segments[positions[r.first_segment_id]:positions[r.last_segment_id] + 1]]
    assert Counter(covered) == Counter(s.id for s in transcript.segments)  # invariant 2
    assert result.segments == tuple(s for s, label in zip(transcript.segments, LABELS) if label == "editorial")
    for segment, label in zip(transcript.segments, LABELS):
        if label == "editorial":
            assert result.assert_citable(segment.id) == segment  # invariant 3
        else:
            assert segment.id not in result.to_json() and segment.text not in result.to_json()  # invariant 4
            with pytest.raises(LearningProjectionUnavailable):
                result.assert_citable(segment.id)
    item_id = item.id
    db.session.commit()
    promo = load_promotional_projection(session=db.session, storage=env.storage, item_id=item_id,
        labels=("self_promotion", "cross_promotion"), purpose="synthetic-research")
    assert type(promo) is PromotionalProjection
    with pytest.raises(LearningProjectionUnavailable):
        assert_citable(promo, promo.segments[0].id)  # invariant 5
    page = env.client.get(f"/items/{item.id}/classification")
    assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
    assert all(text in page.text for text in TEXTS)
    payload = dict(active_revision_id=first, first_segment_id=transcript.segments[1].id,
        last_segment_id=transcript.segments[1].id, label="editorial")
    response = env.client.post(f"/api/items/{item.id}/classification/overrides", json=payload)
    assert response.status_code == 202
    second = response.json["revision_id"]
    with pytest.raises(LearningProjectionUnavailable):
        load(env, item.id)  # no stale revision for new downstream work
    drain(env)
    assert db.session.get(m.ContentItem, item.id).active_classification_revision_id == second
    newer = db.session.get(m.ClassificationRevision, second)
    assert newer.previous_revision_id == first and newer.policy_s3_key != old_keys[0]
    assert newer.projection_s3_key != old_keys[1]
    assert all(env.storage.objects[key] == raw for key, raw in old_artifacts.items())  # invariant 7
    for _consumer in ("Lamad", "Ebenezer", "Agent Factory"):
        # These are future-consumer contracts, not a claim that publishers exist.
        for _retry in range(2):
            current = load(env, item.id)
            assert current.policy_sha256 == newer.policy_sha256  # invariant 8 boundary
            assert transcript.segments[1] in current.segments
    revisions, calls = m.ClassificationRevision.query.count(), len(env.classifier.calls)
    for revision_id in (first, second):
        for phase, ordinal in (("prepare", None), ("block", 1), ("finalize", None)):
            tasks.classify_item.run(revision_id, phase, ordinal)
    replay = env.client.post(f"/api/items/{item.id}/classification/overrides", json=payload)
    assert replay.status_code == 409  # stale owner form cannot create another revision
    drain(env)
    assert m.ClassificationRevision.query.count() == revisions and len(env.classifier.calls) == calls
    assert all(env.storage.objects[key] == raw for key, raw in original.items())  # invariant 1
    assert (item.transcript_sha256, item.segments_sha256, transcript.segments) == identity
    events = assert_safe_events(env, caplog, required={"commercial_classification_prepare",
        "commercial_classification_block", "commercial_classification_finalize"})
    completed = [r.commercial_event for r in events if "label_counts" in r.commercial_event]
    assert completed and completed[0]["label_counts"] == {"editorial": 3, "excluded": 4, "uncertain": 1}
    assert completed[0]["duration_ratios"] == {"included": .375, "excluded": .5, "uncertain": .125}
    assert completed[0]["review_required"] is True


def test_crash_between_committed_block_and_publish_resumes_without_double_cost(acceptance, monkeypatch, caplog):
    env = acceptance
    item, transcript = canonical(env, "podcast", suffix="crash", texts=tuple(f"Synthetic lesson {i}" for i in range(85)))
    revision_id = final_hook(env, item)
    name, args, _ = env.sent.pop(0)
    tasks.classify_item.run(*args)
    _, block_args, _ = env.sent.pop(0)
    class WorkerDeath(BaseException):
        pass
    def die(*args, **kwargs):
        raise WorkerDeath()
    with monkeypatch.context() as patch:
        patch.setattr(tasks, "_publish_classification", die)
        with pytest.raises(WorkerDeath):
            tasks.classify_item.run(*block_args)
    assert len(env.classifier.calls) == 1
    assert db.session.get(m.ClassificationRevision, revision_id).total_cost_microusd == 25
    tasks.reconcile_classifications.run()
    tasks.classify_item.run(*block_args)  # duplicate old delivery races recovery
    drain(env)
    row = db.session.get(m.ClassificationRevision, revision_id)
    assert row.state == "completed" and row.total_cost_microusd == 50 and row.attempts == 0
    assert len(env.classifier.calls) == 2 and m.ClassificationBlock.query.filter_by(revision_id=revision_id).count() == 2
    assert len(load(env, item.id).segments) == 85
    assert m.ClassificationRevision.query.filter_by(item_id=item.id).count() == 1
    assert_safe_events(env, caplog, required={"commercial_classification_finalize"})


def test_learned_rule_changes_only_matching_source_through_real_sweep(acceptance, caplog):
    env = acceptance
    item, transcript = canonical(env, "podcast", suffix="rule-origin", texts=(TEXTS[1],))
    first = final_hook(env, item)
    drain(env)
    response = env.client.post(f"/api/items/{item.id}/classification/overrides", json=dict(
        active_revision_id=first, first_segment_id=transcript.segments[0].id,
        last_segment_id=transcript.segments[0].id, label="paid_commercial"))
    assert response.status_code == 202
    same, same_transcript = canonical(env, "podcast", suffix="same", texts=(TEXTS[1],))
    foreign, foreign_transcript = canonical(env, "youtube", suffix="foreign", texts=(TEXTS[1],))
    foreign_revision = final_hook(env, foreign)
    drain(env)
    same_revision = db.session.get(m.ContentItem, same.id).active_classification_revision_id
    assert same_revision and not load(env, same.id).segments
    same_policy = policy(env, same_revision, same_transcript)
    assert same_policy.ranges[0].decision_source == "source_rule"
    assert load(env, foreign.id).segments == foreign_transcript.segments  # invariant 6
    assert all(not r.matched_rule_ids for r in policy(env, foreign_revision, foreign_transcript).ranges)
    records = assert_safe_events(env, caplog, required={"commercial_classification_source_rule"})
    sweep_events = [r.commercial_event for r in records if "rule_match_counts" in r.commercial_event]
    assert sweep_events and sweep_events[-1]["rule_match_counts"]["matched"] >= 1


@pytest.mark.parametrize("failure", ["classifier", "policy", "projection"])
def test_failures_never_return_canonical_content(acceptance, failure, caplog):
    env = acceptance
    item, transcript = canonical(env, "youtube", suffix="failure")
    revision_id = final_hook(env, item)
    if failure == "classifier":
        # A provider-controlled diagnostic is never an event field or traceback.
        error = ClassifierError("model_auth")
        error.args = ("PRIVATE_DIAGNOSTIC CREDENTIAL_SECRET https://synthetic.invalid",)
        env.classifier.error = error
    drain(env)
    row = db.session.get(m.ClassificationRevision, revision_id)
    if failure != "classifier":
        env.storage.objects[getattr(row, failure + "_s3_key")] = b"PRIVATE_DIAGNOSTIC CREDENTIAL_SECRET"
    env.storage.reads.clear()
    with pytest.raises(LearningProjectionUnavailable, match="^learning projection unavailable$"):
        load(env, item.id)  # invariant 9
    response = env.client.get(f"/items/{item.id}/classification")
    assert response.status_code in (200, 503)
    if failure != "projection":
        assert all(text not in response.text for text in TEXTS)
    # The authorized review page intentionally reconstructs from canonical +
    # valid policy; it is not a general-learning projection consumer.
    assert item.transcript_s3_key not in [key for key, _ in env.storage.reads]
    required = {"commercial_classification_failed"} if failure == "classifier" else {"commercial_classification_finalize"}
    records = assert_safe_events(env, caplog, required=required)
    if failure == "classifier":
        assert any(r.commercial_event.get("retry_category") == "model_auth" for r in records)


def test_observation_encoder_omits_untrusted_values_and_extra_count_fields(acceptance, caplog):
    from classification_service import FinalizationResult
    started = monotonic()
    # Even a future caller constructing a malformed service DTO cannot turn a
    # numeric observation into transcript/provider-controlled log content.
    malformed = FinalizationResult(1, "completed", "CREDENTIAL_SECRET", 0, 0, 0, 0, 0, False)
    tasks._observe_classification("finalize", (1, 1, 1), started, result=malformed,
        retry_category="PRIVATE_DIAGNOSTIC", rule_match_counts={"scanned": 2, "matched": 1,
            "body": "CREDENTIAL_SECRET", "url": "https://synthetic.invalid"})
    event = caplog.records[-1].commercial_event
    assert "label_counts" not in event and "retry_category" not in event
    assert event["rule_match_counts"] == {"scanned": 2, "matched": 1}
    assert "CREDENTIAL_SECRET" not in json.dumps(event)
    before = len(caplog.records)
    tasks._observe_classification("PRIVATE_DIAGNOSTIC", (1, 1, 1), started)
    tasks._observe_classification("prepare", (True, 1, 1), started)
    assert len(caplog.records) == before


def test_log_sink_failure_cannot_fail_a_durable_classification_checkpoint(acceptance, monkeypatch):
    env = acceptance
    item, _ = canonical(env, "podcast", suffix="log-failure")
    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_DIAGNOSTIC")
    monkeypatch.setattr(logging.getLogger("tasks"), "info", broken)
    revision_id = final_hook(env, item)
    drain(env)
    assert db.session.get(m.ClassificationRevision, revision_id).state == "completed"
    assert len(load(env, item.id).segments) == len(TEXTS)
