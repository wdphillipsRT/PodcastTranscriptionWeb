"""Durable classification records contain decisions by reference, never text."""

import importlib

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from extensions import db


@pytest.fixture
def schema(app):
    m = importlib.import_module("models")
    for name in ("ClassificationRevision", "ClassificationBlock", "SegmentOverride",
                 "SourceClassificationRule", "ClassificationSweep"):
        assert hasattr(m, name), f"models must expose {name}"
    with app.app_context():
        db.create_all()
        yield m
        db.session.remove()
        db.drop_all()


def graph(m):
    owner = m.Admin(email="owner@example.com", password_hash="hash")
    db.session.add(owner)
    db.session.flush()
    source = m.ContentSource(admin_id=owner.id, source_type="podcast_rss", external_id="feed",
                             canonical_url="https://example.com/feed", name="Feed")
    item = m.ContentItem(admin_id=owner.id, platform="podcast", item_type="podcast_episode",
                         external_id="episode", canonical_url="https://example.com/episode", title="Episode")
    db.session.add_all([source, item])
    db.session.flush()
    db.session.add(m.SourceItem(source_id=source.id, item_id=item.id))
    db.session.flush()
    return owner, source, item


def revision_values(source, item, **changes):
    values = dict(item_id=item.id, source_id=source.id, revision=1, request_reason="transcript_completed",
                  transcript_sha256="a" * 64, segments_sha256="b" * 64,
                  input_identity_sha256="c" * 64, idempotency_key="automatic:" + "c" * 64,
                  policy_version="commercial-segment-policy/v1", prompt_version="commercial-segment-classifier/v1",
                  prompt_sha256="d" * 64, requested_model="claude-sonnet", resolved_model="claude-sonnet-20260101",
                  classifier_cli_version="1.0", source_rule_set_sha256="e" * 64, override_set_sha256="f" * 64)
    values.update(changes)
    return values


def completed(m):
    return dict(state="completed", policy_s3_key="policy.json", policy_sha256="1" * 64,
                projection_s3_key="projection.json", projection_sha256="2" * 64, completed_at=m.utc_now())


def test_revision_defaults_and_explicit_retry_with_same_identity(schema):
    _, source, item = graph(schema)
    first = schema.ClassificationRevision(**revision_values(source, item))
    db.session.add(first)
    db.session.flush()
    retry = schema.ClassificationRevision(**revision_values(source, item, revision=2,
        request_reason="manual_retry", idempotency_key=f"manual_retry:{first.id}", previous_revision_id=first.id))
    db.session.add(retry)
    db.session.commit()
    assert first.state == "pending" and first.attempts == 0
    assert retry.previous_revision is first
    assert first.item is item and first.source is source
    assert first.review_required is False and first.total_cost_microusd == 0
    assert first.created_at.tzinfo is not None


@pytest.mark.parametrize("changes", [dict(revision=0), dict(attempts=-1), dict(total_cost_microusd=-1),
    dict(included_segments=-1), dict(excluded_segments=-1), dict(uncertain_segments=-1),
    dict(included_milliseconds=-1), dict(excluded_milliseconds=-1), dict(uncertain_milliseconds=-1),
    dict(state="unknown"), dict(request_reason="unknown"), dict(state="completed"),
    dict(classifier_cli_version=None)])
def test_revision_database_rejects_invalid_values(schema, changes):
    _, source, item = graph(schema)
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.ClassificationRevision).values(**revision_values(source, item, **changes)))


@pytest.mark.parametrize("field", ["policy_s3_key", "policy_sha256", "projection_s3_key", "projection_sha256", "completed_at", "resolved_model"])
def test_completed_revision_requires_both_artifacts_and_time(schema, field):
    _, source, item = graph(schema)
    values = completed(schema)
    values[field] = None
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.ClassificationRevision).values(**revision_values(source, item, **values)))


def test_completed_revision_rejects_error_category(schema):
    _, source, item = graph(schema)
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.ClassificationRevision).values(**revision_values(
            source, item, **completed(schema), error_category="classifier_failed")))


def test_pending_revision_can_await_resolved_model(schema):
    _, source, item = graph(schema)
    revision = schema.ClassificationRevision(**revision_values(source, item, resolved_model=None))
    db.session.add(revision)
    db.session.commit()
    assert revision.resolved_model is None


@pytest.mark.parametrize("kind", ["revision", "idempotency"])
def test_revision_uniqueness(schema, kind):
    _, source, item = graph(schema)
    db.session.add(schema.ClassificationRevision(**revision_values(source, item)))
    db.session.flush()
    changes = dict(idempotency_key="different") if kind == "revision" else dict(revision=2)
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.ClassificationRevision).values(**revision_values(source, item, **changes)))


@pytest.mark.parametrize("field,value", [("policy_s3_key", "replacement"), ("state", "failed"),
    ("attempts", 3), ("included_segments", 5), ("requested_model", "different"), ("error_category", "failed")])
def test_completed_revision_is_immutable_but_selectable(schema, field, value):
    _, source, item = graph(schema)
    revision = schema.ClassificationRevision(**revision_values(source, item, **completed(schema)))
    db.session.add(revision)
    db.session.commit()
    db.session.expire_all()
    item.active_classification_revision = revision
    db.session.commit()
    assert item.active_classification_revision_id == revision.id
    setattr(revision, field, value)
    with pytest.raises(ValueError, match="immutable"):
        db.session.commit()


def test_revision_immutability_check_locks_row_before_update(schema):
    _, source, item = graph(schema)
    revision = schema.ClassificationRevision(**revision_values(source, item))
    db.session.add(revision)
    db.session.commit()
    statements = []

    def capture(connection, statement, _multiparams, _params, _execution_options):
        statements.append((statement, connection.get_transaction()))

    sa.event.listen(db.engine, "before_execute", capture)
    try:
        revision.attempts = 1
        db.session.flush()
    finally:
        sa.event.remove(db.engine, "before_execute", capture)

    state_reads = [(index, statement, transaction) for index, (statement, transaction) in enumerate(statements)
        if isinstance(statement, sa.sql.Select) and list(statement.selected_columns.keys()) == ["state"]
        and statement.get_final_froms() == [schema.ClassificationRevision.__table__]]
    assert len(state_reads) == 1
    read_index, state_query, transaction = state_reads[0]
    update_index, update_transaction = next((index, transaction)
        for index, (statement, transaction) in enumerate(statements)
        if isinstance(statement, sa.sql.Update) and statement.table.name == "classification_revisions")
    assert read_index < update_index
    assert transaction is update_transaction and transaction.is_active
    # Compile the real listener query for PostgreSQL; SQLite cannot establish
    # PostgreSQL row-lock semantics and deliberately omits FOR UPDATE itself.
    assert str(state_query.compile(dialect=postgresql.dialect())).endswith("FOR UPDATE")
    assert "FOR UPDATE" not in str(state_query.compile(dialect=sqlite.dialect()))


def block_values(revision, **changes):
    values = dict(revision_id=revision.id, ordinal=1, target_first_segment_id="seg-000001-" + "a" * 16,
                  target_last_segment_id="seg-000080-" + "b" * 16, input_sha256="a" * 64)
    values.update(changes)
    return values


@pytest.mark.parametrize("changes", [dict(ordinal=0), dict(attempts=-1), dict(cost_microusd=-1),
    dict(state="retrying"), dict(state="completed"),
    dict(state="completed", decision_s3_key="decision.json", completed_at=None),
    dict(state="completed", decision_sha256="a" * 64, completed_at=None)])
def test_block_database_constraints(schema, changes):
    _, source, item = graph(schema)
    revision = schema.ClassificationRevision(**revision_values(source, item))
    db.session.add(revision)
    db.session.flush()
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.ClassificationBlock).values(**block_values(revision, **changes)))


def test_blocks_have_unique_ordinals_and_completed_decision(schema):
    _, source, item = graph(schema)
    revision = schema.ClassificationRevision(**revision_values(source, item))
    db.session.add(revision)
    db.session.flush()
    block = schema.ClassificationBlock(**block_values(revision, state="completed",
        decision_s3_key="decision.json", decision_sha256="a" * 64, completed_at=schema.utc_now()))
    db.session.add(block)
    db.session.flush()
    assert block.revision is revision and revision.blocks == [block]
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.ClassificationBlock).values(**block_values(revision)))


def test_new_tables_have_no_transcript_or_unbounded_payload_columns(schema):
    for name in ("classification_revisions", "classification_blocks", "segment_overrides",
                 "source_classification_rules", "classification_sweeps"):
        for column in db.metadata.tables[name].columns:
            assert not isinstance(column.type, (sa.Text, sa.JSON))
            assert column.name not in {"transcript", "text", "output", "error_message", "provider_error"}


def test_exact_closed_state_vocabularies(schema):
    assert schema.CLASSIFICATION_STATES == ("pending", "running", "retrying", "completed", "failed", "superseded")
    assert schema.CLASSIFICATION_REASONS == ("transcript_completed", "backfill", "override", "policy_upgrade", "source_rule", "manual_retry", "live_smoke")
    assert schema.RULE_SCAN_STATES == ("pending", "running", "completed", "failed")
    assert schema.CLASSIFICATION_BLOCK_STATES == ("pending", "running", "completed", "failed", "superseded")


def override_values(owner, source, item, revision, **changes):
    values = dict(item_id=item.id, source_id=source.id, admin_id=owner.id, policy_revision_id=revision.id,
        transcript_sha256="a" * 64, first_segment_id="seg-000001-" + "a" * 16,
        last_segment_id="seg-000080-" + "b" * 16, label="paid_commercial", range_fingerprint_sha256="b" * 64)
    values.update(changes)
    return values


def rule_values(source, override, **changes):
    values = dict(source_id=source.id, originating_override_id=override.id,
                  fingerprint_sha256="b" * 64, fingerprint_version="commercial-fingerprint/v1", label="paid_commercial")
    values.update(changes)
    return values


def correction_graph(m):
    owner, source, item = graph(m)
    revision = m.ClassificationRevision(**revision_values(source, item))
    db.session.add(revision)
    db.session.flush()
    override = m.SegmentOverride(**override_values(owner, source, item, revision))
    db.session.add(override)
    db.session.flush()
    rule = m.SourceClassificationRule(**rule_values(source, override))
    db.session.add(rule)
    db.session.flush()
    sweep = m.ClassificationSweep(source_id=source.id, rule_id=rule.id, cursor_item_id=item.id)
    db.session.add(sweep)
    db.session.flush()
    return owner, source, item, revision, override, rule, sweep


def test_correction_and_sweep_relationships_are_durable(schema):
    owner, source, item, revision, override, rule, sweep = correction_graph(schema)
    correction = schema.SegmentOverride(**override_values(owner, source, item, revision,
        label="editorial", supersedes_id=override.id))
    successor = schema.SourceClassificationRule(**rule_values(source, override,
        label="editorial", supersedes_id=rule.id))
    db.session.add_all([correction, successor])
    db.session.commit()
    db.session.expire_all()
    assert override.item is item and override.source is source and override.admin is owner
    assert override.policy_revision is revision and correction.supersedes is override
    assert rule.source is source and rule.originating_override is override and successor.supersedes is rule
    assert sweep.source is source and sweep.rule is rule and sweep.cursor_item_id == item.id
    assert sweep.state == "pending" and sweep.items_scanned == sweep.items_queued == 0
    assert override.created_at.tzinfo is not None and rule.created_at.tzinfo is not None


@pytest.mark.parametrize("kind,field", [("revision", "item_id"), ("revision", "source_id"),
    ("revision", "previous_revision_id"), ("block", "revision_id"),
    ("override", "item_id"), ("override", "source_id"), ("override", "admin_id"),
    ("override", "policy_revision_id"), ("override", "supersedes_id"),
    ("rule", "source_id"), ("rule", "originating_override_id"), ("rule", "supersedes_id"),
    ("sweep", "source_id"), ("sweep", "rule_id"),
    ("item", "active_classification_revision_id")])
def test_classification_foreign_keys_reject_missing_targets(schema, kind, field):
    owner, source, item, revision, override, rule, _ = correction_graph(schema)
    table, values = {
        "revision": (schema.ClassificationRevision, revision_values(source, item, revision=2, idempotency_key="new")),
        "block": (schema.ClassificationBlock, block_values(revision)),
        "override": (schema.SegmentOverride, override_values(owner, source, item, revision)),
        "rule": (schema.SourceClassificationRule, rule_values(source, override, label="editorial")),
        "sweep": (schema.ClassificationSweep, dict(source_id=source.id, rule_id=rule.id)),
        "item": (schema.ContentItem, {}),
    }[kind]
    with pytest.raises(IntegrityError):
        if kind == "item":
            db.session.execute(sa.update(table).where(table.id == item.id).values({field: 99999}))
        else:
            db.session.execute(sa.insert(table).values(**{**values, field: 99999}))


@pytest.mark.parametrize("kind,changes", [("override", {"label": "unknown"}),
    ("rule", {"label": "unknown"}), ("sweep", {"state": "superseded"}),
    ("sweep", {"items_scanned": -1}), ("sweep", {"items_queued": -1}),
    ("sweep", {"rule_id": None}), ("sweep", {"cursor_item_id": 0})])
def test_corrections_and_sweeps_reject_invalid_scalars(schema, kind, changes):
    owner, source, item, revision, override, rule, _ = correction_graph(schema)
    table, values = {
        "override": (schema.SegmentOverride, override_values(owner, source, item, revision)),
        "rule": (schema.SourceClassificationRule, rule_values(source, override, label="editorial")),
        "sweep": (schema.ClassificationSweep, dict(source_id=source.id, rule_id=rule.id)),
    }[kind]
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(table).values(**{**values, **changes}))


def test_rules_have_unique_source_version_fingerprint_label(schema):
    _, source, _, _, override, _, _ = correction_graph(schema)
    with pytest.raises(IntegrityError):
        db.session.execute(sa.insert(schema.SourceClassificationRule).values(**rule_values(source, override)))


def test_classification_delete_behaviors(schema):
    _, _, item, revision, override, rule, sweep = correction_graph(schema)
    db.session.add(schema.ClassificationBlock(**block_values(revision)))
    for field, value in completed(schema).items():
        setattr(revision, field, value)
    db.session.flush()
    item.active_classification_revision = revision
    db.session.commit()
    revision_id, override_id, rule_id, sweep_id = revision.id, override.id, rule.id, sweep.id
    db.session.execute(sa.delete(schema.ClassificationRevision).where(schema.ClassificationRevision.id == revision_id))
    db.session.commit()
    db.session.expire_all()
    assert item.active_classification_revision_id is None
    assert override.policy_revision_id is None
    assert db.session.query(schema.ClassificationBlock).count() == 0
    db.session.execute(sa.delete(schema.SegmentOverride).where(schema.SegmentOverride.id == override_id))
    db.session.commit()
    db.session.expire_all()
    assert rule.originating_override_id is None
    db.session.execute(sa.delete(schema.SourceClassificationRule).where(schema.SourceClassificationRule.id == rule_id))
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(schema.ClassificationSweep, sweep_id) is None


@pytest.mark.parametrize("kind", ["source", "item", "admin"])
def test_classification_owner_deletions_cascade(schema, kind):
    owner, source, item, revision, _, _, _ = correction_graph(schema)
    db.session.add(schema.ClassificationBlock(**block_values(revision)))
    db.session.commit()
    table, identifier = {"source": (schema.ContentSource, source.id),
        "item": (schema.ContentItem, item.id), "admin": (schema.Admin, owner.id)}[kind]
    db.session.execute(sa.delete(table).where(table.id == identifier))
    db.session.commit()
    for table in (schema.ClassificationRevision, schema.ClassificationBlock, schema.SegmentOverride):
        assert db.session.query(table).count() == 0
    if kind in {"source", "admin"}:
        assert db.session.query(schema.SourceClassificationRule).count() == 0
        assert db.session.query(schema.ClassificationSweep).count() == 0


def test_sweep_cursor_survives_item_deletion(schema):
    _, _, item, _, _, _, sweep = correction_graph(schema)
    cursor = item.id
    db.session.commit()
    db.session.execute(sa.delete(schema.ContentItem).where(schema.ContentItem.id == cursor))
    db.session.commit()
    db.session.expire_all()
    assert sweep.cursor_item_id == cursor
    assert not schema.ClassificationSweep.__table__.c.cursor_item_id.foreign_keys
