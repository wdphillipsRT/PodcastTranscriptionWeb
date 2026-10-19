"""Durable classification checkpoints; external I/O never holds database locks.

This module returns committed dispatch instructions. Worker lifetime locks,
broker publication, retries, and stale-claim reclamation belong to the worker.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import hmac
import json
import math
import re
import secrets

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from claude_classifier import (BlockDecision, ClassifierExample, ClassifierError,
                               ClassifierResult, SYSTEM_PROMPT)
from commercial_policy import (ClassificationRange, ClassifierProvenance,
    LearningProjection, SegmentPolicy, POLICY_VERSION, PROMPT_VERSION, LABELS,
    REASON_CODES, SIGNAL_CODES, MAX_BLOCKS_PER_REVISION, MAX_BLOCK_TEXT_CHARS,
    TARGET_SEGMENTS_PER_BLOCK, build_segment_policy, build_learning_projection)
from commercial_signals import (CommercialSignalError, ExactRule, build_classifier_windows, collect_signals,
                                fingerprint_range, match_exact_rules)
from models import (ContentItem, SourceItem, SegmentOverride, SourceClassificationRule,
                    ClassificationRevision, ClassificationBlock, CLASSIFICATION_REASONS, utc_now)
from s3_utils import (DEFAULT_MAX_BYTES, StorageError, StorageTransientError,
                      validate_artifact_key)
from transcripts import canonical_json, parse_canonical_transcript, TranscriptValidationError


RETRYABLE_CLASSIFICATION_ERRORS = frozenset({"claude_timeout", "claude_unavailable",
    "claude_invalid_output", "storage_transient", "dispatch_pending", "worker_timeout"})
PERMANENT_CLASSIFICATION_ERRORS = frozenset({"claude_auth", "claude_config", "model_mismatch",
    "invalid_canonical", "invalid_policy", "revision_budget_exhausted"})
_LOCK_NAMESPACE = 0x434C5346
_BLOCK_SCHEMA = "commercial-block-decisions/v1"
_ITEM_FIELDS = ("id", "admin_id", "platform", "external_id", "transcript_s3_key",
                "segments_s3_key", "transcript_sha256", "segments_sha256")
_OVERRIDE_FIELDS = ("id", "item_id", "source_id", "admin_id", "transcript_sha256",
    "first_segment_id", "last_segment_id", "label", "range_fingerprint_sha256", "supersedes_id")
_RULE_FIELDS = ("id", "source_id", "originating_override_id", "fingerprint_sha256", "label",
                "fingerprint_version", "supersedes_id")
_TOTAL_FIELDS = ("included_segments", "excluded_segments", "uncertain_segments",
                "included_milliseconds", "excluded_milliseconds", "uncertain_milliseconds", "review_required")
_REVISION_IDENTITY_FIELDS = ("id", "item_id", "source_id", "previous_revision_id", "revision", "request_reason",
    "transcript_sha256", "segments_sha256", "input_identity_sha256", "idempotency_key", "policy_version",
    "prompt_version", "prompt_sha256", "requested_model", "classifier_cli_version", "source_rule_set_sha256", "override_set_sha256")
_PREPARED_REQUEST_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedRevisionRequest:
    """Opaque, process-local proof of validated artifacts; contains metadata only."""

    _item_id: int
    _source_id: int
    _identity: str
    _previous_revision_id: int | None
    _allow_precompletion: bool
    _seal: str

    def __repr__(self):
        return "<PreparedRevisionRequest>"


def _request_seal(request):
    payload = (request._item_id, request._source_id, request._identity,
               request._previous_revision_id, request._allow_precompletion)
    return hmac.new(_PREPARED_REQUEST_KEY, canonical_json(payload).encode(), sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class DispatchInstruction:
    revision_id: int
    phase: str
    block_ordinal: int | None
    task_id: str


@dataclass(frozen=True, slots=True)
class PreparationResult:
    revision_id: int
    state: str
    block_ordinals: tuple[int, ...]
    next_dispatch: DispatchInstruction | None


@dataclass(frozen=True, slots=True)
class BlockResult:
    revision_id: int
    block_ordinal: int
    state: str
    total_cost_microusd: int
    next_dispatch: DispatchInstruction | None


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    revision_id: int
    state: str
    included_segments: int
    excluded_segments: int
    uncertain_segments: int
    included_milliseconds: int
    excluded_milliseconds: int
    uncertain_milliseconds: int
    review_required: bool
    next_dispatch: None = None


class ClassificationServiceError(RuntimeError):
    """A closed category, never an exception, transcript, or storage payload."""

    def __init__(self, category, *, record=True):
        self.category = category if category in RETRYABLE_CLASSIFICATION_ERRORS | PERMANENT_CLASSIFICATION_ERRORS else "invalid_policy"
        self.retryable = self.category in RETRYABLE_CLASSIFICATION_ERRORS
        self.record = record
        super().__init__(self.category)


def _fail(category="invalid_policy", *, record=True):
    raise ClassificationServiceError(category, record=record) from None


def _hash(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def _values(row, fields):
    return {field: getattr(row, field) for field in fields}


def _key(item, kind, digest):
    return validate_artifact_key(f"items/{item['platform']}/{item['external_id']}/{kind}/{digest}.json")


def _boundary(transcript, row):
    positions = {segment.id: index for index, segment in enumerate(transcript.segments)}
    first, last = positions.get(row["first_segment_id"]), positions.get(row["last_segment_id"])
    if first is None or last is None or first > last:
        _fail()
    segments = transcript.segments[first:last + 1]
    if fingerprint_range(segments) != row["range_fingerprint_sha256"]:
        _fail()
    return segments


@dataclass(frozen=True, slots=True)
class _Inputs:
    item: dict
    metadata: dict
    transcript: object
    examples: tuple
    exact: tuple
    windows: tuple
    rule_hash: str
    override_hash: str
    identity: str


class ClassificationService:
    def __init__(self, *, session, storage, classifier, settings):
        self.session, self.storage, self.classifier, self.settings = session, storage, classifier, settings

    @contextmanager
    def _transaction(self):
        # The injected session is the service unit of work. End even read-only
        # autobegun transactions before crossing an external boundary.
        try:
            yield
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def _item_lock(self, item_id):
        if type(item_id) is not int or not 0 < item_id < 2**31:
            _fail("invalid_canonical")
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(sa.text("SELECT pg_advisory_xact_lock(:namespace, :item_id)"),
                {"namespace": _LOCK_NAMESPACE, "item_id": item_id})
        item = self.session.scalar(sa.select(ContentItem).where(ContentItem.id == item_id)
            .with_for_update().execution_options(populate_existing=True))
        if item is None:
            _fail("invalid_canonical")
        return item

    def _revision_lock(self, revision_id):
        item_id = self.session.scalar(sa.select(ClassificationRevision.item_id).where(ClassificationRevision.id == revision_id))
        item = self._item_lock(item_id)
        revision = self.session.scalar(sa.select(ClassificationRevision).where(ClassificationRevision.id == revision_id)
            .with_for_update().execution_options(populate_existing=True))
        return item, revision

    def _blocks(self, revision_id):
        return list(self.session.scalars(sa.select(ClassificationBlock).where(ClassificationBlock.revision_id == revision_id)
            .order_by(ClassificationBlock.ordinal).with_for_update().execution_options(populate_existing=True)))

    def _metadata(self, item, source_id):
        if type(source_id) is not int or source_id <= 0 or not self.session.scalar(sa.select(SourceItem.id)
                .where(SourceItem.item_id == item.id, SourceItem.source_id == source_id)):
            _fail("invalid_canonical")
        overrides = list(self.session.scalars(sa.select(SegmentOverride).where(SegmentOverride.source_id == source_id)
            .order_by(SegmentOverride.created_at.desc(), SegmentOverride.id.desc()).execution_options(populate_existing=True)))
        effective, seen = [], set()
        for row in overrides:
            boundary = (row.item_id, row.transcript_sha256, row.first_segment_id, row.last_segment_id)
            if boundary not in seen:
                seen.add(boundary)
                effective.append(row)
        rules = list(self.session.scalars(sa.select(SourceClassificationRule).where(SourceClassificationRule.source_id == source_id)
            .order_by(SourceClassificationRule.created_at.desc(), SourceClassificationRule.id.desc()).execution_options(populate_existing=True)))
        superseded = set()
        by_id = {row.id: row for row in rules}
        for row in rules:
            previous = by_id.get(row.supersedes_id)
            if previous is not None and (row.created_at, row.id) > (previous.created_at, previous.id):
                superseded.add(previous.id)
        active = [row for row in rules if row.id not in superseded and row.originating_override_id is not None]
        origins = {}
        for row in active:
            origin = self.session.get(SegmentOverride, row.originating_override_id, populate_existing=True)
            if origin is None or origin.source_id != source_id:
                _fail()
            origins[origin.id] = _values(origin, _OVERRIDE_FIELDS)
        referenced = {row.item_id for row in effective} | {row["item_id"] for row in origins.values()}
        items = {}
        for item_id in sorted(referenced):
            reference = self.session.get(ContentItem, item_id, populate_existing=True)
            if reference is None or reference.admin_id != item.admin_id or not self.session.scalar(sa.select(SourceItem.id)
                    .where(SourceItem.item_id == item_id, SourceItem.source_id == source_id)):
                _fail()
            items[item_id] = _values(reference, ("id", "admin_id", "platform", "external_id"))
        return {"source_id": source_id, "overrides": [_values(row, _OVERRIDE_FIELDS) for row in effective],
                "rules": [_values(row, _RULE_FIELDS) for row in active], "origins": origins, "items": items}

    def _provenance(self):
        return ClassifierProvenance(self.settings.CLAUDE_EXPECTED_VERSION,
            self.settings.CLAUDE_CLASSIFIER_MODEL, self.settings.CLAUDE_CLASSIFIER_MODEL,
            PROMPT_VERSION, sha256(SYSTEM_PROMPT.encode()).hexdigest())

    @staticmethod
    def _eligible(item, allow_precompletion=False):
        return item.status == "completed" or allow_precompletion and item.status == "transcribing"

    def _read_inputs(self, item_id, *, build_windows=True, allow_precompletion=False):
        with self._transaction():
            item = self._item_lock(item_id)
            if not self._eligible(item, allow_precompletion):
                _fail("invalid_canonical")
            snapshot = _values(item, _ITEM_FIELDS)
        try:
            if (not all(snapshot[field] for field in _ITEM_FIELDS)
                    or snapshot["segments_s3_key"] != _key(snapshot, "transcripts", snapshot["transcript_sha256"])):
                _fail("invalid_canonical")
            raw = self.storage.get_bytes(snapshot["segments_s3_key"], max_bytes=DEFAULT_MAX_BYTES)
            transcript = parse_canonical_transcript(raw, expected_item_id=item_id,
                expected_transcript_sha256=snapshot["transcript_sha256"], expected_segments_sha256=snapshot["segments_sha256"])
            if (transcript.platform, transcript.external_id) != (snapshot["platform"], snapshot["external_id"]):
                _fail("invalid_canonical")
        except (TranscriptValidationError, StorageError) as error:
            _fail("storage_transient" if isinstance(error, StorageTransientError) else "invalid_canonical")
        with self._transaction():
            item = self._item_lock(item_id)
            if not self._eligible(item, allow_precompletion) or _values(item, _ITEM_FIELDS) != snapshot:
                _fail("invalid_canonical")
            metadata = self._metadata(item, transcript.source_id)
        cache = {(item_id, transcript.transcript_sha256): transcript}

        def referenced_range(row):
            if row["source_id"] != transcript.source_id or row["admin_id"] != snapshot["admin_id"]:
                _fail()
            identity = (row["item_id"], row["transcript_sha256"])
            if identity not in cache:
                reference = metadata["items"][row["item_id"]]
                try:
                    data = self.storage.get_bytes(_key(reference, "transcripts", row["transcript_sha256"]), max_bytes=DEFAULT_MAX_BYTES)
                    value = parse_canonical_transcript(data, expected_item_id=row["item_id"],
                        expected_transcript_sha256=row["transcript_sha256"], expected_segments_sha256=None)
                    if (value.source_id, value.platform, value.external_id) != (transcript.source_id, reference["platform"], reference["external_id"]):
                        _fail()
                    # Retain only the current canonical and the latest referenced
                    # artifact. Selected examples retain their small slices only.
                    cache.clear()
                    cache[(item_id, transcript.transcript_sha256)] = transcript
                    cache[identity] = value
                except StorageTransientError:
                    _fail("storage_transient")
                except (TranscriptValidationError, StorageError):
                    _fail()
            return _boundary(cache[identity], row)

        rules = []
        for row in metadata["rules"]:
            origin = metadata["origins"][row["originating_override_id"]]
            segments = referenced_range(origin)
            if row["fingerprint_version"] != "commercial-range-fingerprint/v1" or row["fingerprint_sha256"] != fingerprint_range(segments):
                _fail()
            rules.append(ExactRule(row["id"], row["label"], row["fingerprint_sha256"], len(segments)))
        examples, example_ids = [], set()
        for row in metadata["overrides"]:
            if len(examples) == 20:
                break
            segments = referenced_range(row)
            cursor = 0
            while cursor < len(segments) and len(examples) < 20:
                if len(segments[cursor].text) > MAX_BLOCK_TEXT_CHARS:
                    cursor += 1
                    continue
                chunk, size = [], 0
                while cursor < len(segments) and len(chunk) < TARGET_SEGMENTS_PER_BLOCK:
                    segment = segments[cursor]
                    if len(segment.text) > MAX_BLOCK_TEXT_CHARS:
                        break
                    if size + len(segment.text) > MAX_BLOCK_TEXT_CHARS:
                        break
                    chunk.append(segment)
                    size += len(segment.text)
                    cursor += 1
                identity = (row["label"], tuple(segment.id for segment in chunk))
                if identity not in example_ids:
                    examples.append(ClassifierExample(row["label"], tuple(chunk)))
                    example_ids.add(identity)
        signals = collect_signals(transcript.segments)
        signal_map = self._signals_by_segment(transcript, signals)
        matches = match_exact_rules(transcript.segments, rules)
        positions = {segment.id: index for index, segment in enumerate(transcript.segments)}
        resolutions, matching = {}, {}
        for match in matches:
            for segment in transcript.segments[positions[match.first_segment_id]:positions[match.last_segment_id] + 1]:
                matching.setdefault(segment.id, []).append(match)
        for segment in transcript.segments:
            found = matching.get(segment.id, ())
            if found:
                labels = {match.label for match in found}
                label = next(iter(labels)) if len(labels) == 1 else "uncertain"
                resolutions[segment.id] = ClassificationRange(segment.id, segment.id, segment.start, segment.end,
                    label, label == "editorial", 1.0, ("source_example",) if len(labels) == 1 else ("ambiguous_boundary",),
                    signal_map[segment.id], "source_rule", tuple(sorted({match.rule_id for match in found})), None)
        current_overrides = [row for row in metadata["overrides"] if row["item_id"] == item_id and row["transcript_sha256"] == transcript.transcript_sha256]
        for row in reversed(current_overrides):
            for segment in referenced_range(row):
                resolutions[segment.id] = ClassificationRange(segment.id, segment.id, segment.start, segment.end,
                    row["label"], row["label"] == "editorial", 1.0, ("source_example",), signal_map[segment.id], "exact_override", (), row["id"])
        exact = tuple(resolutions[segment.id] for segment in transcript.segments if segment.id in resolutions)
        try:
            windows = build_classifier_windows(transcript.segments, exact, signals) if build_windows else ()
        except CommercialSignalError:
            _fail()
        rule_hash, override_hash, identity = self._input_identity(snapshot, metadata)
        return _Inputs(snapshot, metadata, transcript, tuple(examples), exact, windows, rule_hash, override_hash, identity)

    def _input_identity(self, snapshot, metadata):
        current_overrides = [row for row in metadata["overrides"] if row["item_id"] == snapshot["id"]
                             and row["transcript_sha256"] == snapshot["transcript_sha256"]]
        rule_hash, override_hash = _hash(metadata["rules"]), _hash(current_overrides)
        identity = _hash({"item": snapshot, "source_id": metadata["source_id"], "policy_version": POLICY_VERSION,
            "classifier": asdict(self._provenance()), "source_rule_set_sha256": rule_hash,
            "override_set_sha256": override_hash, "examples": metadata["overrides"],
            "rule_origins": metadata["origins"], "referenced_items": metadata["items"]})
        return rule_hash, override_hash, identity

    @staticmethod
    def _signals_by_segment(transcript, signals):
        values = {segment.id: set() for segment in transcript.segments}
        positions = {segment.id: i for i, segment in enumerate(transcript.segments)}
        for signal in signals:
            for segment in transcript.segments[positions[signal.first_segment_id]:positions[signal.last_segment_id] + 1]:
                values[segment.id].add(signal.code)
        return {key: tuple(code for code in SIGNAL_CODES if code in codes) for key, codes in values.items()}

    def prepare_revision_request(self, item_id: int, *, allow_precompletion: bool = False) -> PreparedRevisionRequest:
        if type(allow_precompletion) is not bool:
            _fail("invalid_canonical")
        inputs = self._read_inputs(item_id, build_windows=False, allow_precompletion=allow_precompletion)
        with self._transaction():
            item = self._item_lock(item_id)
            if (not self._same_inputs(item, inputs, allow_precompletion=allow_precompletion)
                    or self._input_identity(inputs.item, inputs.metadata)[2] != inputs.identity):
                _fail("invalid_canonical")
            request = PreparedRevisionRequest(item_id, inputs.transcript.source_id, inputs.identity,
                item.active_classification_revision_id, allow_precompletion, "")
            request = replace(request, _seal=_request_seal(request))
        return request

    def ensure_revision(self, item_id, reason, *, idempotency_key=None,
                        prepared_request: PreparedRevisionRequest | None = None, commit: bool = True) -> int:
        if reason not in CLASSIFICATION_REASONS or idempotency_key is not None and (
                not isinstance(idempotency_key, str) or re.fullmatch(r"[A-Za-z0-9:._-]{1,255}", idempotency_key) is None):
            _fail()
        if type(commit) is not bool or not commit and prepared_request is None:
            _fail()
        if prepared_request is None:
            prepared_request = self.prepare_revision_request(item_id)
        try:
            valid = (type(prepared_request) is PreparedRevisionRequest
                and type(item_id) is int and prepared_request._item_id == item_id
                and hmac.compare_digest(prepared_request._seal, _request_seal(prepared_request)))
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            _fail("invalid_canonical")
        if not commit:
            return self._insert_revision(item_id, reason, idempotency_key, prepared_request)
        with self._transaction():
            return self._insert_revision(item_id, reason, idempotency_key, prepared_request)

    def _insert_revision(self, item_id, reason, idempotency_key, request):
        # No transaction manager or external I/O here: the caller may own the
        # atomic transcript-completion transaction, including failure rollback.
        item = self._item_lock(item_id)
        if (not self._eligible(item, request._allow_precompletion)
                or item.active_classification_revision_id != request._previous_revision_id):
            _fail("invalid_canonical")
        snapshot = _values(item, _ITEM_FIELDS)
        metadata = self._metadata(item, request._source_id)
        rule_hash, override_hash, identity = self._input_identity(snapshot, metadata)
        if identity != request._identity:
            _fail("invalid_canonical")
        key = idempotency_key or f"auto:{reason}:{identity}"
        previous = self.session.scalar(sa.select(ClassificationRevision).where(
            ClassificationRevision.item_id == item_id, ClassificationRevision.idempotency_key == key).with_for_update())
        if previous is not None:
            return previous.id
        ordinal = (self.session.scalar(sa.select(sa.func.max(ClassificationRevision.revision)).where(ClassificationRevision.item_id == item_id)) or 0) + 1
        provenance = self._provenance()
        revision = ClassificationRevision(item_id=item_id, source_id=request._source_id,
            previous_revision_id=item.active_classification_revision_id, revision=ordinal,
            request_reason=reason, transcript_sha256=item.transcript_sha256,
            segments_sha256=item.segments_sha256, input_identity_sha256=identity,
            idempotency_key=key, policy_version=POLICY_VERSION, prompt_version=provenance.prompt_version,
            prompt_sha256=provenance.prompt_sha256, requested_model=provenance.requested_model,
            classifier_cli_version=provenance.cli_version, source_rule_set_sha256=rule_hash,
            override_set_sha256=override_hash, error_category="dispatch_pending",
            celery_task_id=f"classify-{item_id}-{item.transcript_sha256[:16]}-r{ordinal}-prepare")
        self.session.add(revision)
        self.session.flush()
        return revision.id

    def _same_inputs(self, item, inputs, *, allow_precompletion=False):
        return (self._eligible(item, allow_precompletion) and _values(item, _ITEM_FIELDS) == inputs.item
            and self._metadata(item, inputs.transcript.source_id) == inputs.metadata)

    @contextmanager
    def _operation(self, revision_id, block_ordinal=None, claim=None):
        try:
            yield
        except SQLAlchemyError:
            # A failed commit may leave immutable objects behind. Preserve the
            # checkpoint for reconciliation; never replace DB errors with text.
            self.session.rollback()
            _fail("invalid_policy", record=False)
        except Exception as error:
            if isinstance(error, ClassificationServiceError):
                failure = error
            elif isinstance(error, ClassifierError):
                failure = ClassificationServiceError(error.category)
            elif isinstance(error, StorageTransientError):
                failure = ClassificationServiceError("storage_transient")
            else:
                failure = ClassificationServiceError("invalid_policy")
            if failure.record:
                self._record_failure(revision_id, failure, block_ordinal, claim)
            raise failure from None

    def _record_failure(self, revision_id, failure, ordinal, claim):
        with self._transaction():
            _, revision = self._revision_lock(revision_id)
            if revision.state in {"completed", "superseded"}:
                return
            blocks = self._blocks(revision_id)
            block = next((block for block in blocks if block.ordinal == ordinal), None)
            if claim and claim.get("token") is not None:
                if (block is None or block.state != "running"
                        or (block.attempts, block.started_at) != claim["token"]):
                    return
            elif any(block.state == "running" for block in blocks):
                return
            revision.state = "retrying" if failure.retryable else "failed"
            revision.error_category = failure.category
            if block is not None and block.state not in {"completed", "superseded"}:
                block.state, block.error_category = "failed", failure.category

    def _current(self, item, revision, inputs, identity):
        provenance = self._provenance()
        expected = dict(item_id=inputs.item["id"], source_id=inputs.transcript.source_id, transcript_sha256=inputs.transcript.transcript_sha256,
            segments_sha256=inputs.item["segments_sha256"], input_identity_sha256=inputs.identity,
            source_rule_set_sha256=inputs.rule_hash, override_set_sha256=inputs.override_hash,
            policy_version=POLICY_VERSION, prompt_version=provenance.prompt_version,
            prompt_sha256=provenance.prompt_sha256, requested_model=provenance.requested_model,
            classifier_cli_version=provenance.cli_version)
        valid = (_values(revision, _REVISION_IDENTITY_FIELDS) == identity
            and _values(revision, expected) == expected and self._same_inputs(item, inputs)
            and item.active_classification_revision_id == revision.previous_revision_id
            and not self.session.scalar(sa.select(ClassificationRevision.id).where(
                ClassificationRevision.item_id == item.id, ClassificationRevision.revision > revision.revision).limit(1)))
        if not valid and revision.state != "completed":
            revision.state, revision.error_category = "superseded", None
        return valid

    @staticmethod
    def _window_hash(window, examples):
        return _hash({"window": asdict(window), "examples": [asdict(example) for example in examples]})

    def _validate_blocks(self, blocks, inputs):
        if len(blocks) != len(inputs.windows):
            _fail()
        for block, window in zip(blocks, inputs.windows):
            if (block.ordinal != window.ordinal or block.target_first_segment_id != window.target_segments[0].id
                    or block.target_last_segment_id != window.target_segments[-1].id
                    or block.input_sha256 != self._window_hash(window, inputs.examples)):
                _fail()

    @staticmethod
    def _dispatch(revision, blocks):
        if revision.state in {"completed", "failed", "superseded"}:
            return None
        incomplete = next((block for block in blocks if block.state != "completed"), None)
        if incomplete is not None and incomplete.state == "running":
            return None
        phase, ordinal = ("block", incomplete.ordinal) if incomplete else ("finalize", None)
        suffix = f"b{ordinal}" if incomplete else "finalize"
        task_id = f"classify-{revision.item_id}-{revision.transcript_sha256[:16]}-r{revision.revision}-{suffix}"
        return DispatchInstruction(revision.id, phase, ordinal, task_id)

    def _outbox(self, revision, blocks):
        dispatch = self._dispatch(revision, blocks)
        if dispatch is not None:
            revision.celery_task_id, revision.error_category = dispatch.task_id, "dispatch_pending"
        return dispatch

    @staticmethod
    def _final_result(revision):
        return FinalizationResult(revision.id, revision.state, **_values(revision, _TOTAL_FIELDS))

    def prepare_revision(self, revision_id):
        with self._operation(revision_id):
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                item_id = item.id
                identity = _values(revision, _REVISION_IDENTITY_FIELDS)
                if revision.state in {"completed", "superseded", "failed"}:
                    return PreparationResult(revision_id, revision.state, tuple(b.ordinal for b in self._blocks(revision_id)), None)
            inputs = self._read_inputs(item_id)
            if len(inputs.windows) > MAX_BLOCKS_PER_REVISION:
                _fail("revision_budget_exhausted")
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                blocks = self._blocks(revision_id)
                if revision.state in {"completed", "superseded", "failed"}:
                    return PreparationResult(revision_id, revision.state, tuple(b.ordinal for b in blocks), None)
                if not self._current(item, revision, inputs, identity):
                    return PreparationResult(revision_id, revision.state, tuple(b.ordinal for b in blocks), None)
                if revision.started_at is None:
                    if blocks:
                        _fail()
                    revision.started_at = utc_now()
                    for window in inputs.windows:
                        block = ClassificationBlock(revision_id=revision_id, ordinal=window.ordinal,
                            target_first_segment_id=window.target_segments[0].id,
                            target_last_segment_id=window.target_segments[-1].id,
                            input_sha256=self._window_hash(window, inputs.examples))
                        self.session.add(block)
                        blocks.append(block)
                    self.session.flush()
                self._validate_blocks(blocks, inputs)
                revision.state, revision.heartbeat_at = "running", utc_now()
                dispatch = self._outbox(revision, blocks)
                return PreparationResult(revision_id, revision.state, tuple(b.ordinal for b in blocks), dispatch)

    def run_block(self, revision_id, block_ordinal):
        if type(block_ordinal) is not int or block_ordinal <= 0:
            _fail(record=False)
        ownership = {}
        with self._operation(revision_id, block_ordinal, ownership):
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                item_id = item.id
                identity = _values(revision, _REVISION_IDENTITY_FIELDS)
                if revision.state in {"completed", "superseded", "failed"}:
                    return BlockResult(revision_id, block_ordinal, revision.state, revision.total_cost_microusd, None)
                if any(block.ordinal == block_ordinal and block.state == "running" for block in self._blocks(revision_id)):
                    _fail("worker_timeout", record=False)
            inputs = self._read_inputs(item_id)
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                blocks = self._blocks(revision_id)
                if revision.state in {"completed", "superseded", "failed"} or not self._current(item, revision, inputs, identity):
                    return BlockResult(revision_id, block_ordinal, revision.state, revision.total_cost_microusd, None)
                if revision.started_at is None:
                    _fail(record=False)
                self._validate_blocks(blocks, inputs)
                block = next((b for b in blocks if b.ordinal == block_ordinal), None)
                if block is None:
                    _fail(record=False)
                total = sum(b.cost_microusd for b in blocks if b.state == "completed")
                if block.state == "completed":
                    return BlockResult(revision_id, block_ordinal, "completed", total, self._dispatch(revision, blocks))
                if block.state == "running" or any(b.ordinal < block_ordinal and b.state != "completed" for b in blocks):
                    _fail("worker_timeout", record=False)
                elapsed = max(0, (utc_now() - revision.started_at).total_seconds())
                if (len(blocks) > MAX_BLOCKS_PER_REVISION
                        or total + self.settings.CLAUDE_CLASSIFIER_MAX_BUDGET_USD * 1_000_000 > self.settings.CLAUDE_CLASSIFIER_MAX_REVISION_BUDGET_USD * 1_000_000
                        or elapsed + self.settings.CLAUDE_CLASSIFIER_TIMEOUT_SECONDS > self.settings.CLAUDE_CLASSIFIER_MAX_REVISION_SECONDS):
                    _fail("revision_budget_exhausted")
                block.state, block.started_at, block.heartbeat_at = "running", utc_now(), utc_now()
                block.attempts += 1
                block.error_category = None
                revision.state, revision.heartbeat_at, revision.error_category = "running", utc_now(), None
                claim = (block.attempts, block.started_at)
                ownership["token"] = claim
                window = inputs.windows[block_ordinal - 1]
                input_hash = block.input_sha256
            try:
                result = self.classifier.classify(window, examples=inputs.examples)
            except (ClassifierError, StorageError):
                raise
            except Exception:
                _fail("claude_config")
            payload = {"schema": _BLOCK_SCHEMA, "revision_id": revision_id, "block_ordinal": block_ordinal,
                "input_sha256": input_hash, "decisions": [asdict(d) for d in result.decisions],
                "classifier": asdict(result.provenance), "cost_microusd": result.total_cost_microusd}
            raw = canonical_json(payload).encode()
            result = self._parse_decisions(raw, revision_id, block_ordinal, input_hash, window)
            digest = sha256(raw).hexdigest()
            key = _key(inputs.item, "classification", digest)
            self.storage.put_content_addressed(raw, key)
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                blocks = self._blocks(revision_id)
                block = next(b for b in blocks if b.ordinal == block_ordinal)
                if revision.state in {"completed", "superseded", "failed"} or not self._current(item, revision, inputs, identity):
                    return BlockResult(revision_id, block_ordinal, revision.state, revision.total_cost_microusd, None)
                self._validate_blocks(blocks, inputs)
                if block.state == "completed":
                    return BlockResult(revision_id, block_ordinal, "completed", revision.total_cost_microusd, self._dispatch(revision, blocks))
                if block.state != "running" or (block.attempts, block.started_at) != claim:
                    _fail("worker_timeout", record=False)
                block.decision_s3_key, block.decision_sha256 = key, digest
                block.cost_microusd, block.state = result.total_cost_microusd, "completed"
                block.completed_at, block.heartbeat_at = utc_now(), utc_now()
                revision.total_cost_microusd = sum(b.cost_microusd for b in blocks if b.state == "completed")
                revision.heartbeat_at = utc_now()
                return BlockResult(revision_id, block_ordinal, "completed", revision.total_cost_microusd, self._outbox(revision, blocks))

    def _parse_decisions(self, raw, revision_id, ordinal, input_hash, window):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    _fail()
                result[key] = value
            return result
        try:
            if not isinstance(raw, bytes) or not 0 < len(raw) <= self.settings.CLAUDE_CLASSIFIER_MAX_OUTPUT_BYTES:
                _fail()
            data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: _fail())
            if (not isinstance(data, dict) or set(data) != {"schema", "revision_id", "block_ordinal", "input_sha256", "decisions", "classifier", "cost_microusd"}
                    or data["schema"] != _BLOCK_SCHEMA or type(data["revision_id"]) is not int or data["revision_id"] != revision_id
                    or type(data["block_ordinal"]) is not int or data["block_ordinal"] != ordinal or data["input_sha256"] != input_hash
                    or canonical_json(data).encode() != raw):
                _fail()
            provenance = ClassifierProvenance(**data["classifier"])
            if provenance != self._provenance():
                _fail()
            cost = data["cost_microusd"]
            if type(cost) is not int or not 0 <= cost <= self.settings.CLAUDE_CLASSIFIER_MAX_BUDGET_USD * 1_000_000:
                _fail()
            rows = data["decisions"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= TARGET_SEGMENTS_PER_BLOCK:
                _fail()
            positions = {segment.id: index for index, segment in enumerate(window.target_segments)}
            cursor, decisions = 0, []
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(BlockDecision.__dataclass_fields__):
                    _fail()
                first, last = positions.get(row["first_segment_id"]), positions.get(row["last_segment_id"])
                if first != cursor or last is None or last < cursor or row["label"] not in LABELS:
                    _fail()
                confidence, reasons = row["confidence"], row["reason_codes"]
                if (type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1
                        or not isinstance(reasons, list) or not 1 <= len(reasons) <= 4
                        or any(not isinstance(reason, str) or reason not in REASON_CODES for reason in reasons)
                        or len(set(reasons)) != len(reasons)):
                    _fail()
                decisions.append(BlockDecision(row["first_segment_id"], row["last_segment_id"], row["label"], confidence, tuple(reasons)))
                cursor = last + 1
            if cursor != len(positions):
                _fail()
            return ClassifierResult(tuple(decisions), cost, provenance)
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
            _fail()

    def finalize_revision(self, revision_id):
        with self._operation(revision_id):
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                item_id = item.id
                identity = _values(revision, _REVISION_IDENTITY_FIELDS)
                if revision.state in {"completed", "superseded", "failed"}:
                    return self._final_result(revision)
            inputs = self._read_inputs(item_id)
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                blocks = self._blocks(revision_id)
                if revision.state in {"completed", "superseded", "failed"} or not self._current(item, revision, inputs, identity):
                    return self._final_result(revision)
                if revision.started_at is None or any(block.state != "completed" for block in blocks):
                    _fail(record=False)
                self._validate_blocks(blocks, inputs)
                checkpoints = [_values(block, ("ordinal", "input_sha256", "decision_s3_key", "decision_sha256", "cost_microusd", "completed_at")) for block in blocks]
                processing_ms = max(0, round((max(b.completed_at for b in blocks) - revision.started_at).total_seconds() * 1000)) if blocks else 0
                previous = self.session.get(ClassificationRevision, revision.previous_revision_id) if revision.previous_revision_id else None
                if previous is not None and (previous.item_id != item_id or previous.state != "completed"):
                    _fail()
                previous_hash = previous.policy_sha256 if previous else None
            classifications = list(inputs.exact)
            signal_map = self._signals_by_segment(inputs.transcript, collect_signals(inputs.transcript.segments))
            for checkpoint, window in zip(checkpoints, inputs.windows):
                if checkpoint["decision_s3_key"] != _key(inputs.item, "classification", checkpoint["decision_sha256"]):
                    _fail()
                raw = self.storage.get_bytes(checkpoint["decision_s3_key"], max_bytes=self.settings.CLAUDE_CLASSIFIER_MAX_OUTPUT_BYTES)
                if sha256(raw).hexdigest() != checkpoint["decision_sha256"]:
                    _fail()
                result = self._parse_decisions(raw, revision_id, checkpoint["ordinal"], checkpoint["input_sha256"], window)
                if result.total_cost_microusd != checkpoint["cost_microusd"]:
                    _fail()
                positions = {segment.id: index for index, segment in enumerate(window.target_segments)}
                for decision in result.decisions:
                    for segment in window.target_segments[positions[decision.first_segment_id]:positions[decision.last_segment_id] + 1]:
                        classifications.append(ClassificationRange(segment.id, segment.id, segment.start, segment.end,
                            decision.label, decision.label == "editorial", decision.confidence, decision.reason_codes,
                            signal_map[segment.id], "claude", (), None))
            positions = {segment.id: i for i, segment in enumerate(inputs.transcript.segments)}
            classifications.sort(key=lambda entry: positions[entry.first_segment_id])
            options = dict(transcript=inputs.transcript, classifier=self._provenance(), source_rule_set_sha256=inputs.rule_hash,
                override_set_sha256=inputs.override_hash, previous_policy_sha256=previous_hash, processing_milliseconds=processing_ms)
            policy = build_segment_policy(classifications=classifications, **options)
            coalesced = []
            metadata_fields = ("label", "eligible", "confidence", "reason_codes", "signal_codes", "decision_source", "matched_rule_ids", "override_id")
            for entry in policy.ranges:
                if coalesced and _values(coalesced[-1], metadata_fields) == _values(entry, metadata_fields):
                    coalesced[-1] = replace(coalesced[-1], last_segment_id=entry.last_segment_id, end=entry.end)
                else:
                    coalesced.append(entry)
            policy = build_segment_policy(classifications=coalesced, **options)
            policy = SegmentPolicy.from_json(policy.to_json(), transcript=inputs.transcript)
            projection = build_learning_projection(inputs.transcript, policy)
            projection = LearningProjection.from_json(projection.to_json(), transcript=inputs.transcript, policy=policy)
            policy_key = _key(inputs.item, "policy", policy.policy_sha256)
            projection_key = _key(inputs.item, "learning", projection.projection_sha256)
            self.storage.put_content_addressed(policy.to_json().encode(), policy_key, address_sha256=policy.policy_sha256)
            self.storage.put_content_addressed(projection.to_json().encode(), projection_key, address_sha256=projection.projection_sha256)
            with self._transaction():
                item, revision = self._revision_lock(revision_id)
                if revision.state in {"completed", "superseded", "failed"} or not self._current(item, revision, inputs, identity):
                    return self._final_result(revision)
                blocks = self._blocks(revision_id)
                self._validate_blocks(blocks, inputs)
                if any(block.state != "completed" for block in blocks) or checkpoints != [
                        _values(block, checkpoints[0]) for block in blocks]:
                    _fail()
                # SQL CAS also protects SQLite integrations without row locks.
                changed = self.session.execute(sa.update(ContentItem).where(ContentItem.id == item_id,
                    ContentItem.status == "completed", ContentItem.transcript_sha256 == inputs.item["transcript_sha256"],
                    ContentItem.segments_sha256 == inputs.item["segments_sha256"],
                    ContentItem.active_classification_revision_id == revision.previous_revision_id,
                    ~sa.exists(sa.select(ClassificationRevision.id).where(ClassificationRevision.item_id == item_id,
                        ClassificationRevision.revision > revision.revision))).values(active_classification_revision_id=revision_id))
                if changed.rowcount != 1:
                    revision.state, revision.error_category = "superseded", None
                    return self._final_result(revision)
                revision.policy_s3_key, revision.policy_sha256 = policy_key, policy.policy_sha256
                revision.projection_s3_key, revision.projection_sha256 = projection_key, projection.projection_sha256
                for field in _TOTAL_FIELDS:
                    setattr(revision, field, getattr(policy, field))
                revision.total_cost_microusd = sum(block.cost_microusd for block in blocks)
                revision.resolved_model = policy.classifier.resolved_model
                revision.state, revision.completed_at, revision.error_category = "completed", utc_now(), None
                return self._final_result(revision)
