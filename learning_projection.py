"""Sole fail-closed transcript gateway for future learning and delivery code.

Only canonical editorial segments leave the general gateway. Promotional
research uses a separately typed, explicitly purposed reconstruction. Neither
API publishes anything or exposes storage, canonical transcripts, or policies.
"""

from dataclasses import dataclass, fields
import re

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from commercial_policy import (
    LearningProjection, PromotionalProjection, SegmentPolicy, PolicyValidationError,
    build_promotional_projection, _promotional_labels, _purpose,
)
from models import ContentItem, ContentSource, SourceItem, ClassificationRevision
from s3_utils import DEFAULT_MAX_BYTES, StorageError, validate_artifact_key
from transcripts import parse_canonical_transcript, TranscriptValidationError


class LearningProjectionUnavailable(RuntimeError):
    """Closed public failure; contains no segment IDs or provider diagnostics."""

    def __init__(self):
        super().__init__("learning projection unavailable")


def _unavailable():
    raise LearningProjectionUnavailable() from None


@dataclass(frozen=True, slots=True)
class CitableLearningProjection(LearningProjection):
    """Validated editorial DTO with exactly the canonical projection fields."""

    def assert_citable(self, segment_id):
        return assert_citable(self, segment_id)


def assert_citable(projection: CitableLearningProjection, segment_id):
    """Return a canonical editorial record, rejecting every other input alike.

    The explicit runtime type check also rejects PromotionalProjection when
    this function (or the unbound DTO method) is called directly.
    """
    if type(projection) is CitableLearningProjection and isinstance(segment_id, str):
        for segment in projection.segments:
            if segment.id == segment_id:
                return segment
    _unavailable()


_ITEM_FIELDS = ("id", "admin_id", "platform", "external_id", "status",
    "transcript_sha256", "segments_sha256", "segments_s3_key", "active_classification_revision_id")
_TOTAL_FIELDS = ("included_segments", "excluded_segments", "uncertain_segments",
    "included_milliseconds", "excluded_milliseconds", "uncertain_milliseconds", "review_required")
_REVISION_FIELDS = ("id", "item_id", "source_id", "previous_revision_id", "revision", "state",
    "transcript_sha256", "segments_sha256", "policy_version", "prompt_version", "prompt_sha256",
    "requested_model", "resolved_model", "classifier_cli_version", "source_rule_set_sha256",
    "override_set_sha256", "policy_s3_key", "policy_sha256", "projection_s3_key", "projection_sha256",
    "completed_at", "error_category", *_TOTAL_FIELDS)
_VALIDATION_ERRORS = (StorageError, SQLAlchemyError, PolicyValidationError,
    TranscriptValidationError, ValueError, TypeError, KeyError, OverflowError, RecursionError)


def _row(reader, model, names, identity, *, lock=False):
    statement = sa.select(*(getattr(model, name) for name in names)).where(model.id == identity)
    if lock:
        statement = statement.with_for_update()
    row = reader.execute(statement).mappings().one_or_none()
    if row is None:
        _unavailable()
    return dict(row)


def _snapshot(session, item_id, require_latest):
    # A dedicated read session never commits/flushes the caller's unit of work.
    # Close its transaction (and release its short item lock) before S3 reads.
    with Session(bind=session.get_bind()) as reader:
        item = _row(reader, ContentItem, _ITEM_FIELDS, item_id, lock=True)
        if item["status"] != "completed" or item["active_classification_revision_id"] is None:
            _unavailable()
        revision = _row(reader, ClassificationRevision, _REVISION_FIELDS,
                        item["active_classification_revision_id"])
        if (revision["item_id"] != item_id or revision["state"] != "completed"
                or revision["completed_at"] is None or revision["error_category"] is not None
                or revision["transcript_sha256"] != item["transcript_sha256"]
                or revision["segments_sha256"] != item["segments_sha256"]):
            _unavailable()
        membership = reader.scalar(sa.select(SourceItem.id).join(ContentSource,
            ContentSource.id == SourceItem.source_id).where(SourceItem.item_id == item_id,
            SourceItem.source_id == revision["source_id"], ContentSource.admin_id == item["admin_id"]))
        if membership is None:
            _unavailable()
        if require_latest and reader.scalar(sa.select(ClassificationRevision.id).where(
                ClassificationRevision.item_id == item_id,
                ClassificationRevision.revision > revision["revision"],
                ClassificationRevision.state != "superseded").limit(1)) is not None:
            _unavailable()
        previous = None
        if revision["previous_revision_id"] is not None:
            previous = _row(reader, ClassificationRevision,
                ("id", "item_id", "state", "revision", "policy_sha256"), revision["previous_revision_id"])
            if (previous["item_id"] != item_id or previous["state"] != "completed"
                    or previous["revision"] >= revision["revision"]):
                _unavailable()
        return item, revision, previous


def _key(item, kind, digest):
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        _unavailable()
    return validate_artifact_key(f"items/{item['platform']}/{item['external_id']}/{kind}/{digest}.json")


def _bytes(storage, key):
    raw = storage.get_bytes(key, max_bytes=DEFAULT_MAX_BYTES)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= DEFAULT_MAX_BYTES:
        _unavailable()
    return raw


def _inputs(session, storage, item_id, require_latest):
    if type(item_id) is not int or not 0 < item_id < 2**31 or type(require_latest) is not bool:
        _unavailable()
    snapshot = _snapshot(session, item_id, require_latest)
    item, revision, previous = snapshot
    if (item["segments_s3_key"] != _key(item, "transcripts", item["transcript_sha256"])
            or revision["policy_s3_key"] != _key(item, "policy", revision["policy_sha256"])
            or revision["projection_s3_key"] != _key(item, "learning", revision["projection_sha256"])):
        _unavailable()
    transcript = parse_canonical_transcript(_bytes(storage, item["segments_s3_key"]),
        expected_item_id=item_id, expected_transcript_sha256=item["transcript_sha256"],
        expected_segments_sha256=item["segments_sha256"])
    if ((transcript.platform, transcript.external_id, transcript.source_id)
            != (item["platform"], item["external_id"], revision["source_id"])):
        _unavailable()
    raw = _bytes(storage, revision["policy_s3_key"])
    policy = SegmentPolicy.from_json(raw, transcript=transcript)
    if raw != policy.to_json().encode("utf-8"):
        _unavailable()
    expected = {
        "policy_sha256": policy.policy_sha256, "policy_version": policy.policy_version,
        "source_id": policy.source_id, "source_rule_set_sha256": policy.source_rule_set_sha256,
        "override_set_sha256": policy.override_set_sha256,
        "prompt_version": policy.classifier.prompt_version, "prompt_sha256": policy.classifier.prompt_sha256,
        "requested_model": policy.classifier.requested_model, "resolved_model": policy.classifier.resolved_model,
        "classifier_cli_version": policy.classifier.cli_version,
        **{name: getattr(policy, name) for name in _TOTAL_FIELDS},
    }
    if (any(revision[name] != value for name, value in expected.items())
            or policy.previous_policy_sha256 != (previous["policy_sha256"] if previous else None)):
        _unavailable()
    return snapshot, transcript, policy


def _still_active(session, item_id, require_latest, snapshot):
    if _snapshot(session, item_id, require_latest) != snapshot:
        _unavailable()


def load_active_learning_projection(*, session, storage, item_id, require_latest=True) -> CitableLearningProjection:
    """Load exact editorial records from the current completed revision.

    The default blocks new bundle construction during any newer unfinished or
    failed revision. ``require_latest=False`` permits explicit inspection of the
    still-active prior result; it never bypasses artifact or identity checks.
    Inputs must already be committed. No caller transaction is committed.
    """
    try:
        snapshot, transcript, policy = _inputs(session, storage, item_id, require_latest)
        revision = snapshot[1]
        raw = _bytes(storage, revision["projection_s3_key"])
        projection = LearningProjection.from_json(raw, transcript=transcript, policy=policy)
        if raw != projection.to_json().encode("utf-8") or projection.projection_sha256 != revision["projection_sha256"]:
            _unavailable()
        result = CitableLearningProjection(**{field.name: getattr(projection, field.name) for field in fields(projection)})
        _still_active(session, item_id, require_latest, snapshot)
        return result
    except _VALIDATION_ERRORS:
        _unavailable()


def load_promotional_projection(*, session, storage, item_id, labels, purpose) -> PromotionalProjection:
    """Reconstruct explicitly purposed self/cross promotion research on demand.

    No promotional artifact is stored or read. The canonical transcript and
    active policy are required; latest-revision gating always applies.
    """
    try:
        # Policy owns the canonical validation semantics for these arguments.
        labels = _promotional_labels(labels)
        _purpose(purpose)
        snapshot, transcript, policy = _inputs(session, storage, item_id, True)
        result = build_promotional_projection(transcript, policy, labels=labels, purpose=purpose)
        _still_active(session, item_id, True, snapshot)
        return result
    except _VALIDATION_ERRORS:
        _unavailable()
