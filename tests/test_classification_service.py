"""Durable classifier orchestration with real SQLite and immutable in-memory S3."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import router_classifier
from router_classifier import BlockDecision, ClassifierResult, ClassifierError, expected_provenance
from commercial_policy import (ClassifierProvenance, PROMPT_VERSION, SegmentPolicy,
                               LearningProjection)
from commercial_signals import fingerprint_range
from config import load_settings
from extensions import db
import models as m
from s3_utils import StorageIntegrityError, StorageTransientError, DEFAULT_MAX_BYTES
from transcripts import canonical_json, normalize_vtt


class MemoryStorage:
    def __init__(self):
        self.objects, self.reads, self.puts = {}, [], []
        self.on_put = None

    def get_bytes(self, key, *, max_bytes=DEFAULT_MAX_BYTES):
        assert not db.session().in_transaction(), "S3 read held a database transaction"
        self.reads.append((key, max_bytes))
        if key not in self.objects or len(self.objects[key]) > max_bytes:
            raise StorageIntegrityError("invalid artifact")
        return self.objects[key]

    def put_content_addressed(self, content, key, content_type="application/json", *, address_sha256=None):
        assert not db.session().in_transaction(), "S3 write held a database transaction"
        assert key.rsplit("/", 1)[1] == (address_sha256 or sha256(content).hexdigest()) + ".json"
        if key in self.objects and self.objects[key] != content:
            raise StorageIntegrityError("immutable conflict")
        self.objects[key] = content
        self.puts.append((key, content))
        if self.on_put:
            self.on_put(key)
        return key


class FakeClassifier:
    def __init__(self, settings):
        self.calls = []
        self.keys = []
        self.settings = settings
        self.labels = {}
        self.cost = 25
        self.error = None
        self.on_call = None

    def classify(self, window, *, examples=(), request_key=None):
        assert not db.session().in_transaction(), "Router call held a database transaction"
        self.calls.append((window, examples))
        self.keys.append(request_key)
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        decisions = []
        for segment in window.target_segments:
            label, confidence = self.labels.get(segment.id, ("editorial", 1.0))
            decisions.append(BlockDecision(segment.id, segment.id, label, confidence, ("substantive_content",)))
        return ClassifierResult(tuple(decisions), self.cost, expected_provenance(self.settings))


def add_item(env, *, source=None, texts=("First editorial insight", "Buy now with promo code", "Final editorial insight"), external_id=None):
    source = source or env.source
    item = m.ContentItem(admin_id=env.owner.id, platform="podcast", item_type="podcast_episode",
        external_id=external_id or "episode", canonical_url="https://example.com/episode", title="Episode", status="completed")
    db.session.add(item)
    db.session.flush()
    def stamp(number):
        return f"{number // 3600:02}:{number // 60 % 60:02}:{number % 60:02}.000"
    vtt = "WEBVTT\n\n" + "\n\n".join(f"{stamp(i * 2)} --> {stamp(i * 2 + 1)}\n{text}" for i, text in enumerate(texts))
    transcript = normalize_vtt(vtt, platform=item.platform, external_id=item.external_id,
        item_id=item.id, source_id=source.id, language="en", tool_version="test",
        origin="creator_caption", provider_version="test", canonical_url=item.canonical_url)
    raw = transcript.to_json().encode()
    item.transcript_sha256 = transcript.transcript_sha256
    item.segments_sha256 = sha256(raw).hexdigest()
    item.segments_s3_key = f"items/podcast/{item.external_id}/transcripts/{item.transcript_sha256}.json"
    item.transcript_s3_key = f"items/podcast/{item.external_id}/transcripts/{item.transcript_sha256}.txt"
    db.session.add(m.SourceItem(item_id=item.id, source_id=source.id))
    env.storage.objects[item.segments_s3_key] = raw
    db.session.commit()
    return item, transcript


@pytest.fixture
def env(app):
    if importlib.util.find_spec("classification_service") is None:
        pytest.skip("service contract test reports missing implementation")
    module = importlib.import_module("classification_service")
    with app.app_context():
        db.create_all()
        owner = m.Admin(email="owner@example.com", password_hash="hash")
        db.session.add(owner)
        db.session.flush()
        source = m.ContentSource(admin_id=owner.id, source_type="podcast_rss", external_id="feed",
            canonical_url="https://example.com/feed", name="Feed")
        other = m.ContentSource(admin_id=owner.id, source_type="podcast_rss", external_id="other",
            canonical_url="https://example.com/other", name="Other")
        db.session.add_all([source, other])
        db.session.commit()
        settings = load_settings()
        storage = MemoryStorage()
        classifier = FakeClassifier(settings)
        value = SimpleNamespace(module=module, owner=owner, source=source, other=other,
            storage=storage, classifier=classifier, settings=settings)
        value.item, value.transcript = add_item(value)
        value.service = module.ClassificationService(session=db.session, storage=storage,
            classifier=classifier, settings=settings)
        yield value
        db.session.remove()
        db.drop_all()


def override(env, first, last=None, *, label="paid_commercial", item=None, transcript=None, source=None):
    item, transcript, source = item or env.item, transcript or env.transcript, source or env.source
    segments = transcript.segments[first:(last if last is not None else first) + 1]
    row = m.SegmentOverride(item_id=item.id, source_id=source.id, admin_id=env.owner.id,
        transcript_sha256=transcript.transcript_sha256, first_segment_id=segments[0].id,
        last_segment_id=segments[-1].id, label=label, range_fingerprint_sha256=fingerprint_range(segments))
    db.session.add(row)
    db.session.commit()
    return row


def rule(env, row, *, source=None, label=None, supersedes_id=None):
    result = m.SourceClassificationRule(source_id=(source or env.source).id,
        originating_override_id=row.id, fingerprint_sha256=row.range_fingerprint_sha256,
        label=label or row.label, fingerprint_version="commercial-range-fingerprint/v1", supersedes_id=supersedes_id)
    db.session.add(result)
    db.session.commit()
    return result


def finish(env, *, reason="backfill", idempotency_key=None):
    revision_id = env.service.ensure_revision(env.item.id, reason, idempotency_key=idempotency_key)
    preparation = env.service.prepare_revision(revision_id)
    for ordinal in preparation.block_ordinals:
        env.service.run_block(revision_id, ordinal)
    result = env.service.finalize_revision(revision_id)
    return revision_id, result


def artifacts(env, revision_id):
    revision = db.session.get(m.ClassificationRevision, revision_id)
    policy = SegmentPolicy.from_json(env.storage.objects[revision.policy_s3_key], transcript=env.transcript)
    projection = LearningProjection.from_json(env.storage.objects[revision.projection_s3_key], transcript=env.transcript, policy=policy)
    return revision, policy, projection


def test_service_contract_is_available():
    assert importlib.util.find_spec("classification_service") is not None, "durable classification service missing"


def test_revision_creation_is_durable_idempotent_and_has_stable_dispatch(env):
    item_id = env.item.id
    first = env.service.ensure_revision(item_id, "backfill")
    assert first == env.service.ensure_revision(item_id, "backfill")
    revision = db.session.get(m.ClassificationRevision, first)
    assert revision.revision == 1 and revision.previous_revision_id is None
    assert revision.error_category == "dispatch_pending" and revision.state == "pending"
    assert revision.celery_task_id == f"classify-{item_id}-{env.transcript.transcript_sha256[:16]}-r1-prepare"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(m.ClassificationRevision)) == 1
    second = env.service.ensure_revision(item_id, "manual_retry", idempotency_key="manual_retry:1")
    assert second != first
    assert db.session.get(m.ClassificationRevision, second).revision == 2


@pytest.mark.parametrize("key", ["", "with space", "private/locator", "é", "a" * 256, True])
def test_idempotency_keys_reject_unsafe_values(env, key):
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.ensure_revision(env.item.id, "backfill", idempotency_key=key)
    assert caught.value.category == "invalid_policy"


@pytest.mark.parametrize("change", ["key", "bytes", "hash", "membership", "status", "missing_text_key"])
def test_canonical_identity_fails_closed_before_creating_revision(env, change):
    item = env.item
    if change == "key":
        item.segments_s3_key = item.segments_s3_key.replace("episode", "foreign")
    elif change == "bytes":
        env.storage.objects[item.segments_s3_key] += b" "
    elif change == "hash":
        item.segments_sha256 = "a" * 64
    elif change == "membership":
        db.session.execute(sa.delete(m.SourceItem).where(m.SourceItem.item_id == item.id))
    elif change == "status":
        item.status = "failed"
    else:
        item.transcript_s3_key = None
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.ensure_revision(item.id, "backfill")
    assert caught.value.category == "invalid_canonical"
    assert not db.session.scalar(sa.select(sa.func.count()).select_from(m.ClassificationRevision))


def test_blocks_checkpoint_exact_requested_window_and_replay_without_calling_again(env):
    env.item, env.transcript = add_item(env, external_id="long", texts=tuple(f"Editorial line {i}" for i in range(161)))
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    prep = env.service.prepare_revision(revision_id)
    assert prep.block_ordinals == (1, 2, 3)
    assert prep.next_dispatch.phase == "block" and prep.next_dispatch.block_ordinal == 1
    assert prep.next_dispatch.task_id.endswith("-r1-b1")
    first = env.service.run_block(revision_id, 1)
    assert first.total_cost_microusd == 25 and first.next_dispatch.block_ordinal == 2
    assert env.service.run_block(revision_id, 1) == first
    assert len(env.classifier.calls) == 1
    assert env.service.prepare_revision(revision_id).next_dispatch.block_ordinal == 2
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.finalize_revision(revision_id)
    env.service.run_block(revision_id, 2)
    last = env.service.run_block(revision_id, 3)
    assert last.next_dispatch.phase == "finalize" and last.next_dispatch.task_id.endswith("-r1-finalize")
    assert env.service.finalize_revision(revision_id).included_segments == 161


def test_policy_projection_publish_after_both_uploads_and_replay_is_deterministic(env):
    env.classifier.labels[env.transcript.segments[1].id] = ("paid_commercial", .99)
    env.classifier.labels[env.transcript.segments[2].id] = ("editorial", .5)
    revision_id, result = finish(env)
    revision, policy, projection = artifacts(env, revision_id)
    assert (result.included_segments, result.excluded_segments, result.uncertain_segments) == (1, 1, 1)
    assert (result.included_milliseconds, result.excluded_milliseconds, result.uncertain_milliseconds) == (1000, 1000, 1000)
    assert result.review_required and policy.ranges[-1].decision_source == "uncertain_threshold"
    assert projection.segments == env.transcript.segments[:1]
    assert revision.policy_s3_key.endswith(f"/policy/{policy.policy_sha256}.json")
    assert revision.projection_s3_key.endswith(f"/learning/{projection.projection_sha256}.json")
    assert db.session.get(m.ContentItem, env.item.id).active_classification_revision_id == revision_id
    stored = dict(env.storage.objects)
    assert env.service.finalize_revision(revision_id) == result
    assert env.storage.objects == stored and len(env.classifier.calls) == 1
    public = canonical_json(asdict(result))
    assert "sha256" not in public and "s3_key" not in public and "text" not in public
    block_bytes = next(value for key, value in env.storage.objects.items() if "/classification/" in key)
    block = json.loads(block_bytes)
    assert set(block) == {"schema", "revision_id", "block_ordinal", "input_sha256", "decisions", "classifier", "cost_microusd"}
    assert "text" not in block_bytes.decode()


def test_latest_override_wins_rule_conflicts_are_uncertain_and_foreign_rules_do_not_apply(env):
    example_item, example = add_item(env, external_id="example", texts=tuple(s.text for s in env.transcript.segments))
    sponsor = override(env, 1, item=example_item, transcript=example)
    first_rule = rule(env, sponsor)
    editorial = rule(env, sponsor, label="editorial")
    foreign = override(env, 2, item=example_item, transcript=example, source=env.other)
    rule(env, foreign, source=env.other)
    override(env, 0, label="paid_commercial")
    newest = override(env, 0, label="editorial")
    revision_id, result = finish(env)
    _, policy, _ = artifacts(env, revision_id)
    assert policy.ranges[0].override_id == newest.id and policy.ranges[0].label == "editorial"
    assert policy.ranges[1].label == "uncertain" and policy.ranges[1].reason_codes == ("ambiguous_boundary",)
    assert policy.ranges[1].matched_rule_ids == (first_rule.id, editorial.id)
    assert len(env.classifier.calls) == 1
    window, examples = env.classifier.calls[0]
    assert tuple(s.id for s in window.target_segments) == (env.transcript.segments[2].id,)
    assert all(s.id != example.segments[2].id for e in examples for s in e.segments)


def test_exact_only_revision_skips_claude_and_coalesces_metadata(env):
    env.item, env.transcript = add_item(env, external_id="exact", texts=("First insight", "Second insight", "Third insight"))
    row = override(env, 0, 2, label="editorial")
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    prep = env.service.prepare_revision(revision_id)
    assert prep.block_ordinals == () and prep.next_dispatch.phase == "finalize"
    result = env.service.finalize_revision(revision_id)
    _, policy, _ = artifacts(env, revision_id)
    assert result.included_segments == 3 and not env.classifier.calls
    assert len(policy.ranges) == 1 and policy.ranges[0].override_id == row.id
    assert policy.processing_milliseconds == 0


def test_examples_use_only_reviewed_ranges_chunk_and_cap_newest_first(env):
    example_item, example = add_item(env, external_id="examples", texts=tuple(f"Example line {i}" for i in range(170)))
    override(env, 0, 169, item=example_item, transcript=example)
    newest = override(env, 160, 169, label="editorial", item=example_item, transcript=example)
    finish(env)
    examples = env.classifier.calls[0][1]
    assert examples[0].label == newest.label and examples[0].segments == example.segments[160:170]
    assert [len(e.segments) for e in examples] == [10, 80, 80, 10]
    assert all(len(e.segments) <= 80 for e in examples)


def test_effective_examples_and_rules_change_identity_but_superseded_boundary_does_not(env):
    old = override(env, 0)
    newest = override(env, 0, label="editorial")
    first = env.service.ensure_revision(env.item.id, "backfill")
    old.label = "housekeeping"
    db.session.commit()
    assert env.service.ensure_revision(env.item.id, "backfill") == first
    example_item, example = add_item(env, external_id="new-example", texts=("Reviewed text",))
    override(env, 0, item=example_item, transcript=example)
    assert env.service.ensure_revision(env.item.id, "backfill") != first


def test_fresh_running_block_is_not_paid_twice(env):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    def race():
        with pytest.raises(env.module.ClassificationServiceError) as caught:
            env.service.run_block(revision_id, 1)
        assert caught.value.category == "worker_timeout"
    env.classifier.on_call = race
    env.service.run_block(revision_id, 1)
    assert len(env.classifier.calls) == 1


@pytest.mark.parametrize("changed", ["transcript", "new_revision", "pointer", "rules"])
def test_final_activation_cas_preserves_previous_revision_when_inputs_change(env, changed):
    previous_id, _ = finish(env)
    revision_id = env.service.ensure_revision(env.item.id, "manual_retry", idempotency_key="retry:1")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    seen = []
    def race(key):
        item = db.session.get(m.ContentItem, env.item.id)
        seen.append((key, item.active_classification_revision_id))
        assert item.active_classification_revision_id == previous_id
        db.session.commit()
        if "/learning/" not in key:
            return
        if changed == "transcript":
            item.transcript_sha256 = "a" * 64
        elif changed == "pointer":
            item.active_classification_revision_id = None
        elif changed == "rules":
            override(env, 0)
        else:
            env.service.ensure_revision(item.id, "manual_retry", idempotency_key="retry:2")
        db.session.commit()
    env.storage.on_put = race
    result = env.service.finalize_revision(revision_id)
    assert result.state == "superseded"
    assert [key.split("/")[3] for key, _ in seen] == ["policy", "learning"]
    item = db.session.get(m.ContentItem, env.item.id)
    assert item.active_classification_revision_id == (None if changed == "pointer" else previous_id)


@pytest.mark.parametrize("corruption", ["extra", "duplicate", "coverage", "cost", "provenance"])
def test_hash_valid_block_artifacts_are_strictly_reparsed(env, corruption):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    payload = json.loads(env.storage.objects[block.decision_s3_key])
    if corruption == "extra":
        payload["text"] = "SECRET"
    elif corruption == "coverage":
        payload["decisions"] = payload["decisions"][:-1]
    elif corruption == "cost":
        payload["cost_microusd"] += 1
    elif corruption == "provenance":
        payload["classifier"]["resolved_model"] = "wrong-model"
    raw = canonical_json(payload).encode()
    if corruption == "duplicate":
        raw = b'{"schema":"bad",' + raw[1:]
    block.decision_sha256 = sha256(raw).hexdigest()
    block.decision_s3_key = f"items/podcast/episode/classification/{block.decision_sha256}.json"
    env.storage.objects[block.decision_s3_key] = raw
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.finalize_revision(revision_id)
    assert caught.value.category == "invalid_policy"
    assert db.session.get(m.ContentItem, env.item.id).active_classification_revision_id is None


def _long_revision(env):
    env.item, env.transcript = add_item(env, external_id="long", texts=tuple(f"Line {i}" for i in range(81)))
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    return revision_id


def test_generation_time_limit_stops_before_the_next_call(env):
    revision_id = _long_revision(env)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(
        m.ClassificationBlock.revision_id == revision_id, m.ClassificationBlock.ordinal == 1))
    block.started_at = block.completed_at - timedelta(
        seconds=env.settings.CLASSIFIER_MAX_REVISION_SECONDS - env.settings.CLASSIFIER_TIMEOUT_SECONDS + 1)
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.run_block(revision_id, 2)
    assert caught.value.category == "revision_budget_exhausted"
    assert len(env.classifier.calls) == 1


def test_waiting_for_a_busy_router_does_not_count_against_the_generation_limit(env):
    revision_id = _long_revision(env)
    revision = db.session.get(m.ClassificationRevision, revision_id)
    revision.started_at = m.utc_now() - timedelta(seconds=10 * env.settings.CLASSIFIER_MAX_REVISION_SECONDS)
    db.session.commit()
    assert env.service.run_block(revision_id, 2).state == "completed"
    assert len(env.classifier.calls) == 2


def test_block_calls_carry_a_stable_router_key(env):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    assert env.classifier.keys == [f"podcast.classification:rev-{revision_id}:block-1:g0"]


def test_model_decisions_are_recorded_as_the_model_decision_source(env):
    revision_id, _ = finish(env)
    _, policy, _ = artifacts(env, revision_id)
    assert {entry.decision_source for entry in policy.ranges} == {"model"}
    assert policy.classifier == expected_provenance(env.settings)


@pytest.mark.parametrize("error,category", [(ClassifierError("model_invalid_output"), "model_invalid_output"),
    (StorageTransientError("SECRET body"), "storage_transient"), (RuntimeError("SECRET body"), "model_config")])
def test_failure_persistence_contains_only_closed_categories(env, error, category):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.classifier.error = error
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.run_block(revision_id, 1)
    assert str(caught.value) == category
    revision = db.session.get(m.ClassificationRevision, revision_id)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    assert revision.error_category == block.error_category == category
    assert "SECRET" not in str(revision.__dict__) and "SECRET" not in str(block.__dict__)


def test_deleted_origin_rule_is_inert_and_existing_bad_origin_fails_closed(env):
    first = env.service.ensure_revision(env.item.id, "backfill")
    inert = m.SourceClassificationRule(source_id=env.source.id, originating_override_id=None,
        fingerprint_sha256="a" * 64, label="paid_commercial", fingerprint_version="commercial-range-fingerprint/v1")
    db.session.add(inert)
    db.session.commit()
    assert env.service.ensure_revision(env.item.id, "backfill") == first
    origin = override(env, 1)
    learned = rule(env, origin)
    origin.range_fingerprint_sha256 = "b" * 64
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.ensure_revision(env.item.id, "backfill")
    assert caught.value.category == "invalid_policy"


def test_foreign_existing_rule_origin_fails_closed(env):
    other_item, other_transcript = add_item(env, source=env.other, external_id="other")
    origin = override(env, 0, item=other_item, transcript=other_transcript, source=env.other)
    rule(env, origin)
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.ensure_revision(env.item.id, "backfill")
    assert caught.value.category == "invalid_policy"


def test_superseded_rule_is_not_matched_and_same_label_overlaps_keep_all_ids(env):
    example_item, example = add_item(env, external_id="example", texts=tuple(s.text for s in env.transcript.segments))
    first_origin = override(env, 0, 1, item=example_item, transcript=example)
    first = rule(env, first_origin)
    replacement = rule(env, first_origin, label="editorial", supersedes_id=first.id)
    second_origin = override(env, 1, 2, item=example_item, transcript=example)
    second = rule(env, second_origin, label="editorial")
    revision_id, result = finish(env)
    _, policy, _ = artifacts(env, revision_id)
    assert result.included_segments == 3 and not env.classifier.calls
    assert policy.ranges[1].matched_rule_ids == (replacement.id, second.id)
    assert all(first.id not in entry.matched_rule_ids for entry in policy.ranges)


def test_twenty_example_cap_does_not_load_unselected_artifacts(env):
    transcripts = []
    for i in range(22):
        item, transcript = add_item(env, external_id=f"example-{i}", texts=(f"Source example {i}",))
        override(env, 0, item=item, transcript=transcript)
        transcripts.append(transcript)
    finish(env)
    examples = env.classifier.calls[0][1]
    assert len(examples) == 20
    assert [example.segments for example in examples] == [transcript.segments for transcript in reversed(transcripts[2:])]
    assert not any("/example-0/" in key or "/example-1/" in key for key, _ in env.storage.reads)


def test_input_change_during_classification_supersedes_without_activation(env):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.classifier.on_call = lambda: override(env, 0)
    result = env.service.run_block(revision_id, 1)
    assert result.state == "superseded" and result.next_dispatch is None
    assert db.session.get(m.ContentItem, env.item.id).active_classification_revision_id is None


def test_finalization_database_failure_leaves_only_reusable_orphans(env, monkeypatch):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    original_commit = db.session.commit
    def fail_once():
        monkeypatch.setattr(db.session, "commit", original_commit)
        raise sa.exc.OperationalError("SECRET SQL", {}, Exception("SECRET body"))
    def after_upload(key):
        if "/learning/" in key:
            monkeypatch.setattr(db.session, "commit", fail_once)
    env.storage.on_put = after_upload
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.finalize_revision(revision_id)
    assert "SECRET" not in str(caught.value)
    assert db.session.get(m.ContentItem, env.item.id).active_classification_revision_id is None
    stored = dict(env.storage.objects)
    env.storage.on_put = None
    assert env.service.finalize_revision(revision_id).state == "completed"
    assert env.storage.objects == stored and len(env.classifier.calls) == 1


def test_projection_upload_failure_preserves_previous_active_revision(env):
    previous_id, _ = finish(env)
    revision_id = env.service.ensure_revision(env.item.id, "manual_retry", idempotency_key="retry:1")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    def failure(key):
        if "/learning/" in key:
            raise StorageTransientError("SECRET transport")
    env.storage.on_put = failure
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.finalize_revision(revision_id)
    assert caught.value.category == "storage_transient"
    assert db.session.get(m.ContentItem, env.item.id).active_classification_revision_id == previous_id
    env.storage.on_put = None
    assert env.service.finalize_revision(revision_id).state == "completed"


def test_block_limit_is_enforced_during_preparation(env, monkeypatch):
    env.item, env.transcript = add_item(env, external_id="many", texts=tuple(f"Line {i}" for i in range(81)))
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    monkeypatch.setattr(env.module, "MAX_BLOCKS_PER_REVISION", 1)
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.prepare_revision(revision_id)
    assert caught.value.category == "revision_budget_exhausted" and not env.classifier.calls


def test_unknown_or_out_of_order_checkpoint_does_not_poison_revision(env):
    env.item, env.transcript = add_item(env, external_id="ordered", texts=tuple(f"Line {i}" for i in range(81)))
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    for ordinal in (2, 3):
        with pytest.raises(env.module.ClassificationServiceError):
            env.service.run_block(revision_id, ordinal)
    assert db.session.get(m.ClassificationRevision, revision_id).state == "running"
    assert not env.classifier.calls


def test_classification_module_imports_no_media_discovery_or_worker_clients(env):
    import ast
    from pathlib import Path
    tree = ast.parse(Path(env.module.__file__).read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert not imports & {"tasks", "youtube", "rss", "media_download", "transcription", "amazon_transcribe", "boto3"}


def test_duplicate_running_delivery_cannot_fail_the_owning_checkpoint(env, monkeypatch):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    block.state, block.started_at, block.heartbeat_at = "running", m.utc_now(), m.utc_now()
    db.session.commit()
    def unavailable(*args, **kwargs):
        raise StorageTransientError("SECRET")
    monkeypatch.setattr(env.storage, "get_bytes", unavailable)
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.run_block(revision_id, 1)
    assert caught.value.category == "worker_timeout"
    db.session.refresh(block)
    assert block.state == "running" and block.error_category is None


def test_late_failure_cannot_fail_a_reclaimed_block(env):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    def reclaim():
        block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
        block.attempts += 1
        block.started_at = m.utc_now()
        db.session.commit()
    env.classifier.on_call = reclaim
    env.classifier.error = ClassifierError("model_invalid_output")
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.run_block(revision_id, 1)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    assert block.state == "running" and block.attempts == 2 and block.error_category is None
    assert db.session.get(m.ClassificationRevision, revision_id).state == "running"


def test_exact_canonical_key_error_has_closed_service_category(env):
    env.item.external_id = "unsafe/path"
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.ensure_revision(env.item.id, "backfill")
    assert caught.value.category == "invalid_canonical"


def test_supplied_idempotency_key_replays_the_original_request_after_new_examples(env):
    first = env.service.ensure_revision(env.item.id, "manual_retry", idempotency_key="manual:request")
    override(env, 0)
    assert env.service.ensure_revision(env.item.id, "manual_retry", idempotency_key="manual:request") == first


def test_exact_oversized_segments_succeed_without_supplying_invalid_examples(env):
    env.item, env.transcript = add_item(env, external_id="oversized-exact", texts=("x" * 40001,))
    override(env, 0, label="editorial")
    _, result = finish(env)
    assert result.included_segments == 1 and not env.classifier.calls


def test_unresolved_oversized_segment_fails_with_closed_category(env):
    env.item, env.transcript = add_item(env, external_id="oversized-unresolved", texts=("x" * 40001,))
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.prepare_revision(revision_id)
    assert caught.value.category == "invalid_policy" and not env.classifier.calls
    assert db.session.get(m.ClassificationRevision, revision_id).error_category == "invalid_policy"


def test_example_chunks_split_around_unrepresentable_segments(env):
    item, transcript = add_item(env, external_id="oversized-example", texts=("Before", "x" * 40001, "After"))
    override(env, 0, 2, item=item, transcript=transcript)
    finish(env)
    assert [example.segments for example in env.classifier.calls[0][1]] == [transcript.segments[:1], transcript.segments[2:]]


def test_final_sql_compare_and_set_loses_safely(env):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.service.run_block(revision_id, 1)
    item_id = env.item.id
    raced = []
    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if not raced and statement.startswith("UPDATE content_items SET") and "active_classification_revision_id" in statement:
            raced.append(True)
            connection.execute(sa.update(m.ContentItem).where(m.ContentItem.id == item_id).values(transcript_sha256="a" * 64))
    sa.event.listen(db.engine, "before_cursor_execute", before_execute)
    try:
        result = env.service.finalize_revision(revision_id)
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", before_execute)
    assert raced and result.state == "superseded"
    assert db.session.get(m.ContentItem, item_id).active_classification_revision_id is None


def test_postgres_mutation_phases_request_transaction_scoped_item_lock(env, monkeypatch):
    original = db.session.execute
    calls = []
    def execute(statement, params=None, **kwargs):
        if isinstance(statement, sa.sql.elements.TextClause) and "pg_advisory_xact_lock" in statement.text:
            calls.append(params)
            return None
        return original(statement, params, **kwargs)
    monkeypatch.setattr(db.engine.dialect, "name", "postgresql")
    monkeypatch.setattr(db.session, "execute", execute)
    finish(env)
    assert len(calls) >= 8
    assert all(call == {"namespace": 0x434C5346, "item_id": env.item.id} for call in calls)


def test_example_loading_does_not_retain_whole_unrelated_transcripts(env, monkeypatch):
    import weakref
    for ordinal in range(6):
        item, transcript = add_item(env, external_id=f"bounded-{ordinal}", texts=(f"Reviewed {ordinal}",))
        override(env, 0, item=item, transcript=transcript)
    parser = env.module.parse_canonical_transcript
    references, peaks = [], []
    class TrackedCanonical:
        def __init__(self, value):
            self.value = value
        def __getattr__(self, key):
            return getattr(self.value, key)
    def parse(*args, **kwargs):
        transcript = TrackedCanonical(parser(*args, **kwargs))
        references.append(weakref.ref(transcript))
        peaks.append(sum(reference() is not None for reference in references))
        return transcript
    monkeypatch.setattr(env.module, "parse_canonical_transcript", parse)
    env.service.ensure_revision(env.item.id, "backfill")
    assert max(peaks) <= 3, "keep current canonical plus one recent example, not every source transcript"


@pytest.mark.parametrize("field,value", [("revision", 2), ("idempotency_key", "changed:request")])
def test_revision_identity_cannot_change_while_claude_runs(env, field, value):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    def change():
        revision = db.session.get(m.ClassificationRevision, revision_id)
        setattr(revision, field, value)
        db.session.commit()
    env.classifier.on_call = change
    result = env.service.run_block(revision_id, 1)
    assert result.state == "superseded" and result.next_dispatch is None


def test_prepared_request_is_opaque_immutable_metadata_and_ends_transactions(env):
    example_item, example = add_item(env, external_id="request-example")
    rule(env, override(env, 0, item=example_item, transcript=example))
    item_id = env.item.id
    request = env.service.prepare_revision_request(item_id)
    assert type(request) is env.module.PreparedRevisionRequest
    assert not db.session().in_transaction()
    assert len(env.storage.reads) == 2 and not env.classifier.calls
    assert not hasattr(request, "__dict__")
    with pytest.raises(FrozenInstanceError):
        request._item_id = item_id + 1
    representation = repr(request) + str(asdict(request))
    for private in (env.transcript.segments[0].text, "https://", "items/podcast/", "SECRET"):
        assert private not in representation
    assert "PreparedRevisionRequest" in repr(request)


@pytest.mark.parametrize("status", [status for status in m.ITEM_STATUSES if status != "completed"])
@pytest.mark.parametrize("allow_precompletion", [False, True])
def test_prepared_request_only_permits_completed_or_explicit_transcribing(env, status, allow_precompletion):
    item_id = env.item.id
    env.item.status = status
    db.session.commit()
    if allow_precompletion and status == "transcribing":
        env.service.prepare_revision_request(item_id, allow_precompletion=True)
        assert not db.session().in_transaction()
    else:
        with pytest.raises(env.module.ClassificationServiceError) as caught:
            env.service.prepare_revision_request(item_id, allow_precompletion=allow_precompletion)
        assert caught.value.category == "invalid_canonical"
        assert not env.storage.reads


@pytest.mark.parametrize("complete_first", [False, True])
def test_prepared_request_atomically_completes_item_without_external_io(env, monkeypatch, complete_first):
    item_id = env.item.id
    env.item.status = "transcribing"
    db.session.commit()
    request = env.service.prepare_revision_request(item_id, allow_precompletion=True)
    def forbidden(*args, **kwargs):
        pytest.fail("prepared insertion crossed an external or transaction ownership boundary")
    with monkeypatch.context() as patch:
        for target, method in ((env.storage, "get_bytes"), (env.storage, "put_content_addressed"),
                (env.classifier, "classify"), (db.session, "commit"), (db.session, "rollback")):
            patch.setattr(target, method, forbidden)
        with db.session.begin():
            item = db.session.get(m.ContentItem, item_id)
            if complete_first:
                item.status = "completed"
            revision_id = env.service.ensure_revision(item_id, "transcript_completed", prepared_request=request, commit=False)
            assert db.session.get(m.ClassificationRevision, revision_id).state == "pending"
            assert env.service.ensure_revision(item_id, "transcript_completed", prepared_request=request, commit=False) == revision_id
            item.status = "completed"
    revision = db.session.get(m.ClassificationRevision, revision_id)
    assert revision.celery_task_id == f"classify-{item_id}-{env.transcript.transcript_sha256[:16]}-r1-prepare"
    assert env.service.prepare_revision(revision_id).state == "running"
    assert env.service.ensure_revision(item_id, "transcript_completed") == revision_id


def test_prepared_request_insertion_rollback_keeps_item_transcribing(env):
    item_id = env.item.id
    env.item.status = "transcribing"
    db.session.commit()
    request = env.service.prepare_revision_request(item_id, allow_precompletion=True)
    with pytest.raises(RuntimeError, match="force caller rollback"):
        with db.session.begin():
            revision_id = env.service.ensure_revision(item_id, "transcript_completed", prepared_request=request, commit=False)
            assert db.session.get(m.ClassificationRevision, revision_id) is not None
            db.session.get(m.ContentItem, item_id).status = "completed"
            db.session.flush()
            raise RuntimeError("force caller rollback")
    assert db.session.get(m.ContentItem, item_id).status == "transcribing"
    assert not db.session.scalar(sa.select(sa.func.count()).select_from(m.ClassificationRevision))


def test_caller_transaction_requires_a_prepared_request_without_io_or_transaction_control(env, monkeypatch):
    item_id = env.item.id
    db.session.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("invalid caller request performed I/O or controlled the transaction")
    for target, method in ((env.storage, "get_bytes"), (db.session, "commit"), (db.session, "rollback")):
        monkeypatch.setattr(target, method, forbidden)
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.ensure_revision(item_id, "backfill", commit=False)
    assert not db.session().in_transaction()


@pytest.mark.parametrize("change", ["transcript_s3_key", "segments_s3_key", "transcript_sha256",
    "segments_sha256", "platform", "external_id", "membership", "override", "rule",
    "example", "example_identity", "origin", "model", "cli", "prompt", "policy", "lineage", "status"])
def test_prepared_request_rechecks_all_database_and_configuration_identity(env, change, monkeypatch):
    item_id = env.item.id
    example_item, example = add_item(env, external_id="stale-example")
    origin = override(env, 0, item=example_item, transcript=example)
    active_rule = rule(env, origin)
    previous_id = env.service.ensure_revision(item_id, "backfill")
    request = env.service.prepare_revision_request(item_id)
    if change in {"transcript_s3_key", "segments_s3_key"}:
        setattr(env.item, change, "items/podcast/changed/transcripts/" + "a" * 64 + ".json")
    elif change in {"transcript_sha256", "segments_sha256"}:
        setattr(env.item, change, "a" * 64)
    elif change in {"platform", "external_id"}:
        setattr(env.item, change, "youtube" if change == "platform" else "changed")
        if change == "platform":
            env.item.item_type = "youtube_video"
    elif change == "membership":
        db.session.execute(sa.delete(m.SourceItem).where(m.SourceItem.item_id == item_id))
    elif change == "override":
        override(env, 1)
    elif change == "rule":
        active_rule.label = "editorial"
    elif change == "example":
        override(env, 1, item=example_item, transcript=example)
    elif change == "example_identity":
        example_item.external_id = "changed"
    elif change == "origin":
        origin.range_fingerprint_sha256 = "a" * 64
    elif change == "model":
        monkeypatch.setattr(env.service, "settings", replace(env.settings, ROUTER_EXPECTED_MODEL="changed:1"))
    elif change == "cli":
        monkeypatch.setattr(router_classifier, "CLASSIFICATION_ROUTE", "podcast.changed")
    elif change == "prompt":
        monkeypatch.setattr(router_classifier, "SYSTEM_PROMPT", "changed")
    elif change == "policy":
        monkeypatch.setattr(env.module, "POLICY_VERSION", "changed")
    elif change == "lineage":
        env.item.active_classification_revision_id = previous_id
    else:
        env.item.status = "transcribing"
    db.session.commit()
    reads = len(env.storage.reads)
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.ensure_revision(item_id, "manual_retry", prepared_request=request, commit=False)
    assert db.session().in_transaction()
    db.session.rollback()
    assert len(env.storage.reads) == reads and not env.classifier.calls
    assert db.session.scalar(sa.select(sa.func.count()).select_from(m.ClassificationRevision)) == 1


@pytest.mark.parametrize("kind", ["wrong_type", "cross_item", "forged", "mutated"])
def test_invalid_prepared_requests_fail_closed(env, kind):
    item_id = env.item.id
    other, _ = add_item(env, external_id="other-request")
    other_id = other.id
    request = env.service.prepare_revision_request(item_id)
    if kind == "wrong_type":
        request = object()
    elif kind == "cross_item":
        item_id = other_id
    elif kind == "forged":
        request = object.__new__(env.module.PreparedRevisionRequest)
    else:
        object.__setattr__(request, "_item_id", other_id)
        item_id = other_id
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.ensure_revision(item_id, "backfill", prepared_request=request, commit=False)
    db.session.rollback()
    assert not db.session.scalar(sa.select(sa.func.count()).select_from(m.ClassificationRevision))


def test_prepared_database_failure_leaves_rollback_to_caller(env, monkeypatch):
    item_id = env.item.id
    request = env.service.prepare_revision_request(item_id)
    def invalid_row(mapper, connection, target):
        target.idempotency_key = None
    def forbidden(*args, **kwargs):
        pytest.fail("service took ownership of the caller transaction")
    sa.event.listen(m.ClassificationRevision, "before_insert", invalid_row)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db.session, "commit", forbidden)
            patch.setattr(db.session, "rollback", forbidden)
            with pytest.raises(sa.exc.IntegrityError):
                env.service.ensure_revision(item_id, "backfill", prepared_request=request, commit=False)
    finally:
        sa.event.remove(m.ClassificationRevision, "before_insert", invalid_row)
    db.session.rollback()
    assert not db.session.scalar(sa.select(sa.func.count()).select_from(m.ClassificationRevision))


def test_prepared_request_explicit_replay_and_committing_mode_remain_compatible(env):
    item_id = env.item.id
    first = env.service.ensure_revision(item_id, "manual_retry", idempotency_key="prepared:explicit")
    override(env, 0)
    request = env.service.prepare_revision_request(item_id)
    assert env.service.ensure_revision(item_id, "manual_retry", idempotency_key="prepared:explicit", prepared_request=request) == first
    assert not db.session().in_transaction()


def test_a_busy_router_keeps_the_block_pending_and_its_key(env):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.classifier.error = ClassifierError("router_busy", advance_key=False)
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.run_block(revision_id, 1)
    assert (caught.value.category, caught.value.retryable, caught.value.advance_key) == ("router_busy", True, False)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    assert (block.state, block.error_category, block.request_generation) == ("pending", "router_busy", 0)
    revision = db.session.get(m.ClassificationRevision, revision_id)
    assert (revision.state, revision.error_category) == ("retrying", "router_busy")


@pytest.mark.parametrize("seconds_ago, generation", [(-30, 0), (1, 1)])
def test_a_key_left_in_progress_advances_only_once_it_is_stuck(env, seconds_ago, generation):
    from classification_service import IN_PROGRESS_SLACK_SECONDS
    from router_client import READ_MARGIN_SECONDS
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.classifier.error = ClassifierError("router_busy", reason="in_progress", advance_key=False)
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.run_block(revision_id, 1)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    assert (block.request_generation, block.request_generation_started_at is not None) == (0, True)
    stuck_after = env.settings.CLASSIFIER_TIMEOUT_SECONDS + READ_MARGIN_SECONDS + IN_PROGRESS_SLACK_SECONDS
    block.request_generation_started_at = m.utc_now() - timedelta(seconds=stuck_after + seconds_ago)
    db.session.commit()
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.run_block(revision_id, 1)
    assert caught.value.category == "router_busy"
    db.session.refresh(block)
    assert (block.state, block.request_generation) == ("pending", generation)
    assert (block.request_generation_started_at is None) is (generation == 1)


@pytest.mark.parametrize("error, generation, state", [
    (ClassifierError("router_busy"), 1, "pending"),
    (ClassifierError("model_invalid_output"), 1, "failed"),
    (ClassifierError("model_invalid_output", advance_key=False), 0, "failed"),
])
def test_stored_router_outcomes_advance_the_block_key(env, error, generation, state):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.classifier.error = error
    with pytest.raises(env.module.ClassificationServiceError):
        env.service.run_block(revision_id, 1)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    assert (block.request_generation, block.state) == (generation, state)


@pytest.mark.parametrize("reason, retryable, generation, revision_state", [
    ("conflict", True, 1, "retrying"),   # the key's stored body differs (a setting changed): new key, retry
    ("rejected", False, 1, "failed"),    # any other 4xx is a client bug
    (None, False, 1, "failed"),
])
def test_only_a_key_conflict_makes_model_config_retryable(env, reason, retryable, generation, revision_state):
    revision_id = env.service.ensure_revision(env.item.id, "backfill")
    env.service.prepare_revision(revision_id)
    env.classifier.error = ClassifierError("model_config", reason=reason)
    with pytest.raises(env.module.ClassificationServiceError) as caught:
        env.service.run_block(revision_id, 1)
    assert (caught.value.category, caught.value.retryable, caught.value.reason) == ("model_config", retryable, reason)
    block = db.session.scalar(sa.select(m.ClassificationBlock).where(m.ClassificationBlock.revision_id == revision_id))
    assert (block.state, block.error_category, block.request_generation) == ("failed", "model_config", generation)
    revision = db.session.get(m.ClassificationRevision, revision_id)
    assert (revision.state, revision.error_category) == (revision_state, "model_config")
