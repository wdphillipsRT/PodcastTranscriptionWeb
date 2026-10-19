"""The only learning/delivery input boundary fails closed, entirely offline."""

import ast
from dataclasses import FrozenInstanceError, fields
from hashlib import sha256
import importlib
import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from commercial_policy import LearningProjection, PromotionalProjection
from extensions import db
import models as m
from s3_utils import DEFAULT_MAX_BYTES, StorageIntegrityError, StorageTransientError
from tests.test_classification_service import env, add_item, finish
from transcripts import canonical_json


def gateway():
    assert importlib.util.find_spec("learning_projection") is not None, "filtered learning gateway missing"
    return importlib.import_module("learning_projection")


@pytest.fixture
def ready(env):
    texts = ("Editorial insight", "Paid advertiser private text", "Creator offer private text",
             "Other show private text", "Housekeeping private text", "Uncertain private text")
    env.item, env.transcript = add_item(env, texts=texts, external_id="gateway")
    labels = ("editorial", "paid_commercial", "self_promotion", "cross_promotion", "housekeeping", "uncertain")
    env.classifier.labels = {s.id: (label, 1.0) for s, label in zip(env.transcript.segments, labels)}
    identity, _ = finish(env)
    revision = db.session.get(m.ClassificationRevision, identity)
    env.keys = dict(canonical=env.item.segments_s3_key, policy=revision.policy_s3_key,
                    projection=revision.projection_s3_key, raw=env.item.transcript_s3_key)
    env.item_id, env.revision_id = env.item.id, identity
    env.storage.objects[env.keys["raw"]] = b"RAW TRANSCRIPT MUST NEVER BE READ"
    env.policy = json.loads(env.storage.objects[env.keys["policy"]])
    db.session.commit()
    env.storage.reads.clear()
    return env


def load(ready, **kwargs):
    return gateway().load_active_learning_projection(session=db.session, storage=ready.storage,
        item_id=ready.item_id, **kwargs)


def promo(ready, **kwargs):
    return gateway().load_promotional_projection(session=db.session, storage=ready.storage,
        item_id=ready.item_id, **{"labels": ("self_promotion", "cross_promotion"),
                                "purpose": "asp-50-marketing-research", **kwargs})


def corrupt_row(model, identity, **changes):
    db.session.execute(sa.update(model).where(model.id == identity).values(**changes))
    db.session.commit()


def replace_artifact(ready, kind, data):
    field = "policy_sha256" if kind == "policy" else "projection_sha256"
    data.pop(field, None)
    data[field] = sha256(canonical_json(data).encode()).hexdigest()
    key = ready.keys[kind].rsplit("/", 1)[0] + "/" + data[field] + ".json"
    ready.storage.objects[key] = canonical_json(data).encode()
    corrupt_row(m.ClassificationRevision, ready.revision_id,
                **{kind + "_s3_key": key, field: data[field]})
    ready.keys[kind] = key


def test_gateway_exists():
    assert callable(gateway().load_active_learning_projection)


def test_general_result_is_exact_editorial_canonical_and_immutable(ready):
    result = load(ready)
    assert isinstance(result, LearningProjection)
    assert result.schema == "learning-transcript-projection/v1"
    assert result.segments == ready.transcript.segments[:1]
    assert result.to_json().encode() == ready.storage.objects[ready.keys["projection"]]
    assert set(f.name for f in fields(result)) == set(LearningProjection.__dataclass_fields__)
    assert not hasattr(result, "__dict__")
    assert not any(hasattr(result, name) for name in ("storage", "policy", "transcript", "raw", "session"))
    with pytest.raises(FrozenInstanceError):
        result.item_id = 99
    with pytest.raises(FrozenInstanceError):
        result.segments[0].text = "changed"
    for excluded in ready.transcript.segments[1:]:
        assert excluded.text not in result.to_json()
        assert excluded.id not in result.to_json()
    assert {key for key, _ in ready.storage.reads} == {ready.keys[k] for k in ("canonical", "policy", "projection")}
    assert all(limit == DEFAULT_MAX_BYTES for _, limit in ready.storage.reads)


def test_citations_allow_only_editorial_ids_and_do_not_disclose_exclusions(ready):
    result = load(ready)
    assert result.assert_citable(result.segments[0].id) == result.segments[0]
    assert gateway().assert_citable(result, result.segments[0].id) == result.segments[0]
    messages = []
    for segment_id in [s.id for s in ready.transcript.segments[1:]] + ["unknown", None, [], True]:
        with pytest.raises(gateway().LearningProjectionUnavailable) as caught:
            result.assert_citable(segment_id)
        messages.append(str(caught.value))
    assert len(set(messages)) == 1
    assert "seg-" not in messages[0]


@pytest.mark.parametrize("state", ["pending", "retrying", "running", "failed", "superseded"])
def test_active_revision_must_be_completed(ready, state):
    corrupt_row(m.ClassificationRevision, ready.revision_id, state=state)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)
    assert not ready.storage.reads


@pytest.mark.parametrize("state", ["pending", "retrying", "running", "failed"])
def test_higher_revision_blocks_new_bundles_but_explicit_prior_inspection_is_allowed(ready, state):
    newer = ready.service.ensure_revision(ready.item_id, "manual_retry", idempotency_key="gateway-newer")
    corrupt_row(m.ClassificationRevision, newer, state=state)
    ready.storage.reads.clear()
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)
    assert not ready.storage.reads
    assert load(ready, require_latest=False).segments == ready.transcript.segments[:1]
    assert db.session.get(m.ContentItem, ready.item_id).active_classification_revision_id == ready.revision_id
    db.session.commit()


def test_superseded_higher_revision_does_not_block_valid_active_result(ready):
    newer = ready.service.ensure_revision(ready.item_id, "manual_retry", idempotency_key="superseded")
    corrupt_row(m.ClassificationRevision, newer, state="superseded")
    assert load(ready).segments == ready.transcript.segments[:1]


def test_nonactive_completed_higher_revision_blocks_new_construction(ready):
    first = db.session.get(m.ClassificationRevision, ready.revision_id)
    metadata = {name: getattr(first, name) for name in ("policy_s3_key", "policy_sha256",
        "projection_s3_key", "projection_sha256", "resolved_model", "completed_at")}
    db.session.commit()
    newer = ready.service.ensure_revision(ready.item_id, "manual_retry", idempotency_key="higher-completed")
    corrupt_row(m.ClassificationRevision, newer, state="completed", error_category=None, **metadata)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)


def test_higher_revision_for_another_item_does_not_block(ready):
    original = ready.item
    ready.item, _ = add_item(ready, external_id="unrelated")
    ready.service.ensure_revision(ready.item.id, "backfill")
    ready.item = original
    assert load(ready).segments == ready.transcript.segments[:1]


@pytest.mark.parametrize("argument,value", [("item_id", True), ("item_id", None),
    ("item_id", "2"), ("item_id", 0), ("item_id", 2**31),
    ("require_latest", None), ("require_latest", 1), ("require_latest", "false")])
def test_invalid_loader_arguments_fail_closed_without_storage(ready, argument, value):
    kwargs = {"session": db.session, "storage": ready.storage, "item_id": ready.item_id,
              argument: value}
    with pytest.raises(gateway().LearningProjectionUnavailable):
        gateway().load_active_learning_projection(**kwargs)
    assert not ready.storage.reads


def test_unknown_item_is_unavailable_without_storage(ready):
    with pytest.raises(gateway().LearningProjectionUnavailable):
        gateway().load_active_learning_projection(session=db.session, storage=ready.storage, item_id=999999)
    assert not ready.storage.reads


def test_empty_editorial_projection_is_valid_and_cites_nothing(ready):
    ready.classifier.labels = {s.id: ("paid_commercial", 1.0) for s in ready.transcript.segments}
    finish(ready, reason="manual_retry", idempotency_key="all-excluded")
    result = load(ready)
    assert result.segments == ()
    with pytest.raises(gateway().LearningProjectionUnavailable):
        result.assert_citable(ready.transcript.segments[0].id)


def test_active_pointer_cannot_reference_another_items_completed_revision(ready):
    original = ready.item
    ready.item, _ = add_item(ready, external_id="foreign-pointer")
    foreign_id, _ = finish(ready)
    ready.item = original
    corrupt_row(m.ContentItem, ready.item_id, active_classification_revision_id=foreign_id)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)
    assert ready.keys["raw"] not in [key for key, _ in ready.storage.reads]


def test_raw_text_artifact_is_not_a_prerequisite_for_general_loader(ready):
    del ready.storage.objects[ready.keys["raw"]]
    corrupt_row(m.ContentItem, ready.item_id, transcript_s3_key=None)
    assert load(ready).segments == ready.transcript.segments[:1]


@pytest.mark.parametrize("field,value", [("active_classification_revision_id", None),
    ("transcript_sha256", "f" * 64), ("segments_sha256", "f" * 64),
    ("segments_s3_key", None), ("status", "failed")])
def test_missing_or_stale_item_is_unavailable_without_raw_fallback(ready, field, value):
    corrupt_row(m.ContentItem, ready.item_id, **{field: value})
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)
    assert ready.keys["raw"] not in [key for key, _ in ready.storage.reads]


@pytest.mark.parametrize("field,value", [("source_id", "other-source"), ("item_id", "other-item"),
    ("transcript_sha256", "f" * 64), ("segments_sha256", "f" * 64),
    ("policy_s3_key", "wrong-key"), ("projection_s3_key", "wrong-key"),
    ("policy_sha256", "f" * 64), ("projection_sha256", "f" * 64),
    ("prompt_sha256", "f" * 64), ("source_rule_set_sha256", "f" * 64),
    ("override_set_sha256", "f" * 64), ("requested_model", "other-model"),
    ("resolved_model", "other-model"), ("classifier_cli_version", "other-version"),
    ("policy_version", "unsupported"), ("prompt_version", "unsupported"),
    ("included_segments", 999)])
def test_revision_identity_metadata_and_artifact_lineage_must_match(ready, field, value):
    if value == "other-source":
        value = ready.other.id
    if value == "other-item":
        value = add_item(ready, external_id="foreign")[0].id
    corrupt_row(m.ClassificationRevision, ready.revision_id, **{field: value})
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)
    assert ready.keys["raw"] not in [key for key, _ in ready.storage.reads]


@pytest.mark.parametrize("kind", ["canonical", "policy", "projection"])
@pytest.mark.parametrize("damage", ["missing", "bytes", "noncanonical", "invalid-json", "schema", "duplicate-key"])
def test_missing_corrupt_or_noncanonical_objects_fail_closed(ready, kind, damage):
    key = ready.keys[kind]
    original = ready.storage.objects[key]
    if damage == "missing":
        del ready.storage.objects[key]
    elif damage == "bytes":
        ready.storage.objects[key] = original.replace(b"editorial", b"uncertain", 1)
        if kind in {"canonical", "projection"}:
            ready.storage.objects[key] = original.replace(b"Editorial", b"Tampered", 1)
    elif damage == "noncanonical":
        ready.storage.objects[key] += b"\n"
    elif damage == "invalid-json":
        ready.storage.objects[key] = b"private invalid object"
    elif damage == "duplicate-key":
        ready.storage.objects[key] = b'{"schema":"duplicate",' + original[1:]
    else:
        data = json.loads(original)
        data["schema"] = "unsupported/v99"
        if kind == "canonical":
            ready.storage.objects[key] = canonical_json(data).encode()
        else:
            replace_artifact(ready, kind, data)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)
    assert ready.keys["raw"] not in [key for key, _ in ready.storage.reads]


@pytest.mark.parametrize("damage", ["text", "timing", "citation", "id", "hash", "excluded", "uncertain", "omission", "duplicate", "foreign-policy", "foreign-item", "foreign-source"])
def test_rehashed_projection_cannot_change_canonical_records_or_eligibility(ready, damage):
    data = json.loads(ready.storage.objects[ready.keys["projection"]])
    if damage in {"text", "citation", "id"}:
        data["segments"][0][damage] = "substituted"
    elif damage == "timing":
        data["segments"][0]["end"] += 1
    elif damage == "hash":
        data["segments"][0]["sha256"] = "f" * 64
    elif damage in {"excluded", "uncertain"}:
        raw = json.loads(ready.storage.objects[ready.keys["canonical"]])
        data["segments"].append(raw["segments"][1 if damage == "excluded" else -1])
    elif damage == "omission":
        data["segments"] = []
    elif damage == "duplicate":
        data["segments"] *= 2
    elif damage == "foreign-policy":
        data["policy_sha256"] = "f" * 64
    else:
        data["item_id" if damage == "foreign-item" else "source_id"] += 1
    replace_artifact(ready, "projection", data)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)


def test_policy_previous_hash_must_match_completed_previous_revision(ready):
    first_id = ready.revision_id
    ready.revision_id, _ = finish(ready, reason="manual_retry", idempotency_key="second-completed")
    revision = db.session.get(m.ClassificationRevision, ready.revision_id)
    ready.keys["policy"], ready.keys["projection"] = revision.policy_s3_key, revision.projection_s3_key
    db.session.commit()
    assert load(ready).segments == ready.transcript.segments[:1]
    corrupt_row(m.ClassificationRevision, first_id, policy_sha256="f" * 64)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)


@pytest.mark.parametrize("change", ["pointer", "transcript", "higher-pending", "higher-failed", "membership"])
def test_state_changed_during_storage_reads_is_rechecked(ready, change):
    original = ready.storage.get_bytes
    changed = False
    def racing_read(key, *, max_bytes):
        nonlocal changed
        raw = original(key, max_bytes=max_bytes)
        if not changed:
            changed = True
            if change == "pointer":
                corrupt_row(m.ContentItem, ready.item_id, active_classification_revision_id=None)
            elif change == "transcript":
                corrupt_row(m.ContentItem, ready.item_id, transcript_sha256="f" * 64)
            elif change == "membership":
                db.session.execute(sa.delete(m.SourceItem).where(m.SourceItem.item_id == ready.item_id))
                db.session.commit()
            else:
                identity = ready.service.ensure_revision(ready.item_id, "manual_retry", idempotency_key="racing")
                if change == "higher-failed":
                    corrupt_row(m.ClassificationRevision, identity, state="failed")
        return raw
    ready.storage.get_bytes = racing_read
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)


@pytest.mark.parametrize("error", [StorageIntegrityError, StorageTransientError])
def test_storage_errors_are_sanitized_and_produce_no_logs_or_database_writes(ready, error, caplog):
    def broken(key, *, max_bytes):
        raise error("private provider diagnostic s3://secret/raw?token=private")
    ready.storage.get_bytes = broken
    writes = []
    def inspect_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    sa.event.listen(db.engine, "before_cursor_execute", inspect_write)
    try:
        with pytest.raises(gateway().LearningProjectionUnavailable) as caught:
            load(ready)
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", inspect_write)
    assert str(caught.value) == "learning projection unavailable"
    assert caught.value.__suppress_context__
    assert "private" not in caplog.text and "s3://" not in caplog.text
    assert writes == []


def test_storage_returning_oversized_bytes_cannot_bypass_loader_limit(ready, monkeypatch):
    monkeypatch.setattr(gateway(), "DEFAULT_MAX_BYTES", 10)
    ready.storage.get_bytes = lambda key, *, max_bytes: b"x" * 11
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)


@pytest.mark.parametrize("raw", [None, "text instead of bytes", bytearray(b"bytes"), b""])
def test_storage_must_return_nonempty_bounded_bytes(ready, raw):
    ready.storage.get_bytes = lambda key, *, max_bytes: raw
    with pytest.raises(gateway().LearningProjectionUnavailable):
        load(ready)


def test_promotional_reconstruction_is_separate_immutable_and_never_citable(ready):
    result = promo(ready)
    assert type(result) is PromotionalProjection
    assert result.schema == "promotional-analysis-projection/v1"
    assert result.segments == ready.transcript.segments[2:4]
    assert result.purpose == "asp-50-marketing-research"
    assert {key for key, _ in ready.storage.reads} == {ready.keys[k] for k in ("canonical", "policy")}
    assert not hasattr(result, "assert_citable")
    with pytest.raises(FrozenInstanceError):
        result.purpose = "other"
    with pytest.raises(gateway().LearningProjectionUnavailable):
        gateway().assert_citable(result, result.segments[0].id)
    with pytest.raises(gateway().LearningProjectionUnavailable):
        type(load(ready)).assert_citable(result, result.segments[0].id)


@pytest.mark.parametrize("labels,purpose", [(("editorial",), "research"), (("paid_commercial",), "research"),
    (("housekeeping",), "research"), (("uncertain",), "research"), ((), "research"),
    (("self_promotion", "self_promotion"), "research"), ("self_promotion", "research"),
    (("self_promotion",), ""), (("self_promotion",), None), (("self_promotion",), " "),
    (("self_promotion",), "s3://private/key"), (("self_promotion",), "private\ntext")])
def test_promotional_labels_and_purpose_are_explicit_and_restricted(ready, labels, purpose):
    with pytest.raises(gateway().LearningProjectionUnavailable):
        promo(ready, labels=labels, purpose=purpose)
    assert not ready.storage.reads


def test_promotional_arguments_cannot_be_omitted(ready):
    with pytest.raises(TypeError):
        gateway().load_promotional_projection(session=db.session, storage=ready.storage, item_id=ready.item_id)


@pytest.mark.parametrize("kind", ["canonical", "policy"])
def test_promotional_loading_requires_both_canonical_and_policy(ready, kind):
    del ready.storage.objects[ready.keys[kind]]
    with pytest.raises(gateway().LearningProjectionUnavailable):
        promo(ready)


def test_promotional_loading_does_not_require_a_stored_promotional_or_learning_object(ready):
    del ready.storage.objects[ready.keys["projection"]]
    assert promo(ready, labels=("cross_promotion",)).segments == ready.transcript.segments[3:4]


def test_promotional_research_also_rejects_higher_failed_revision(ready):
    newer = ready.service.ensure_revision(ready.item_id, "manual_retry", idempotency_key="promo-newer")
    corrupt_row(m.ClassificationRevision, newer, state="failed")
    with pytest.raises(gateway().LearningProjectionUnavailable):
        promo(ready)


def test_loader_does_not_commit_or_flush_callers_pending_changes(ready):
    item = db.session.get(m.ContentItem, ready.item_id)
    item.title = "uncommitted caller edit"
    # This storage double deliberately permits an existing caller transaction.
    ready.storage.get_bytes = lambda key, *, max_bytes: ready.storage.objects[key]
    assert load(ready).segments == ready.transcript.segments[:1]
    assert item in db.session.dirty
    db.session.rollback()
    assert db.session.get(m.ContentItem, ready.item_id).title == "Episode"
    db.session.commit()


def forbidden_artifact_references(root):
    """Catch attribute and getattr/dictionary access, including model aliases."""
    modules = set(root.glob("learning_*.py"))
    for directory in ("publishers", "integrations"):
        modules.update((root / directory).rglob("*.py"))
    violations = []
    for module in sorted(modules):
        if module == root / "learning_projection.py":
            continue
        for node in ast.walk(ast.parse(module.read_text())):
            value = node.attr if isinstance(node, ast.Attribute) else node.value if isinstance(node, ast.Constant) else None
            if isinstance(value, str) and value in {"segments_s3_key", "transcript_s3_key"}:
                violations.append((str(module.relative_to(root)), node.lineno))
    return violations


def test_repository_learning_and_delivery_modules_use_only_gateway():
    root = Path(__file__).resolve().parents[1]
    assert (root / "learning_projection.py").is_file(), "gateway must exist before the contract can pass"
    assert forbidden_artifact_references(root) == []


@pytest.mark.parametrize("relative,source", [("learning_bundle.py", "value = item.segments_s3_key"),
    ("publishers/lamad.py", "value = ContentItem.transcript_s3_key"),
    ("integrations/nested/factory.py", 'value = getattr(item, "segments_s3_key")'),
    ("integrations/ebenezer.py", 'value = row["transcript_s3_key"]')])
def test_repository_scanner_detects_real_bypasses_and_ignores_approved_loader(tmp_path, relative, source):
    module = tmp_path / relative
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source)
    (tmp_path / "learning_projection.py").write_text("value = item.segments_s3_key")
    assert forbidden_artifact_references(tmp_path) == [(relative, 1)]
