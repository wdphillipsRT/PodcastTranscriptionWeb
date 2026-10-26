"""Immutable commercial decisions and purpose-specific transcript projections.

Every digest covers canonical JSON excluding only the artifact's own digest.
Canonical transcript reconstruction belongs exclusively to ``transcripts``.
"""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
import re
import unicodedata

from s3_utils import DEFAULT_MAX_BYTES
from transcripts import (CanonicalTranscript, TranscriptSegment, MAX_SEGMENTS,
                         canonical_json, parse_canonical_transcript)


POLICY_VERSION = "commercial-segment-policy/v1"
POLICY_SCHEMA = "commercial-segment-policy-artifact/v1"
PROJECTION_SCHEMA = "learning-transcript-projection/v1"
PROMOTIONAL_SCHEMA = "promotional-analysis-projection/v1"
KNOWLEDGE_SCHEMA = "knowledge-transcript-projection/v1"
PROMPT_VERSION = "commercial-segment-classifier/v1"
TARGET_SEGMENTS_PER_BLOCK = 80
CONTEXT_SEGMENTS_PER_SIDE = 3
MAX_BLOCK_TEXT_CHARS = 40_000
MAX_BLOCKS_PER_REVISION = 1_500
EDITORIAL_AUTO_THRESHOLD = 0.80
EXCLUSION_AUTO_THRESHOLD = 0.95
LABELS = ("editorial", "paid_commercial", "self_promotion", "cross_promotion", "housekeeping", "uncertain")
ELIGIBLE_LABELS = frozenset({"editorial"})
KNOWLEDGE_LABELS = frozenset({"editorial", "uncertain"})
REASON_CODES = ("substantive_content", "sponsor_transition", "return_to_program",
                "purchase_instruction", "discount_or_affiliate", "promotional_url",
                "creator_offer", "other_show_promo", "routine_housekeeping",
                "source_example", "ambiguous_boundary", "insufficient_context")
SIGNAL_CODES = ("sponsor_transition", "return_to_program", "purchase_instruction",
                "discount_or_affiliate", "promotional_url", "creator_offer",
                "other_show_promo", "routine_housekeeping", "repeated_block")
# "claude" stays valid for artifacts written before the Platform Model Router.
DECISION_SOURCES = ("claude", "model", "exact_override", "source_rule", "uncertain_threshold")
MAX_INTEGER = 2**63 - 1
MAX_RULE_IDS = 100


class PolicyValidationError(ValueError):
    """Invalid policy artifact; errors never include untrusted transcript text."""


def _invalid():
    raise PolicyValidationError("invalid commercial policy artifact") from None


def _digest(value, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        _invalid()


def _integer(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_INTEGER:
        _invalid()


def _number(value, *, maximum=None):
    if type(value) not in (int, float):
        _invalid()
    try:
        if not math.isfinite(value) or value < 0 or maximum is not None and value > maximum:
            _invalid()
    except (OverflowError, ValueError):
        _invalid()


def _public_text(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 128 or value != value.strip()
            or any(unicodedata.category(c) == "Cc" for c in value)
            or re.search(r"(?:s3://|https?://[^\s<>]*amazonaws\.com|[?&]x-amz-|[?&](?:signature|token|credential)=)", value, re.I)):
        _invalid()


def _sequence(value, *, maximum):
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        _invalid()
    return tuple(value)


def _codes(value, vocabulary, maximum, *, required=False):
    result = _sequence(value, maximum=maximum)
    if required and not result or any(not isinstance(v, str) or v not in vocabulary for v in result) or len(set(result)) != len(result):
        _invalid()
    return result


def apply_confidence_threshold(label: str, confidence: float) -> str:
    if not isinstance(label, str) or label not in LABELS:
        _invalid()
    _number(confidence, maximum=1)
    threshold = EDITORIAL_AUTO_THRESHOLD if label == "editorial" else EXCLUSION_AUTO_THRESHOLD
    return label if label == "uncertain" or confidence >= threshold else "uncertain"


@dataclass(frozen=True, slots=True)
class ClassifierProvenance:
    cli_version: str
    requested_model: str
    resolved_model: str
    prompt_version: str
    prompt_sha256: str

    def __post_init__(self):
        for value in (self.cli_version, self.requested_model, self.resolved_model, self.prompt_version):
            _public_text(value)
        if self.prompt_version != PROMPT_VERSION:
            _invalid()
        _digest(self.prompt_sha256)


@dataclass(frozen=True, slots=True)
class ClassificationRange:
    first_segment_id: str
    last_segment_id: str
    start: float
    end: float
    label: str
    eligible: bool
    confidence: float
    reason_codes: tuple[str, ...]
    signal_codes: tuple[str, ...]
    decision_source: str
    matched_rule_ids: tuple[int, ...]
    override_id: int | None

    def __post_init__(self):
        for value in (self.first_segment_id, self.last_segment_id):
            if not isinstance(value, str) or not re.fullmatch(r"seg-[0-9]{6}-[a-f0-9]{16}", value):
                _invalid()
        _number(self.start)
        _number(self.end)
        if self.end < self.start:
            _invalid()
        apply_confidence_threshold(self.label, self.confidence)
        if type(self.eligible) is not bool or self.eligible != (self.label in ELIGIBLE_LABELS):
            _invalid()
        if not isinstance(self.decision_source, str) or self.decision_source not in DECISION_SOURCES:
            _invalid()
        reasons = _codes(self.reason_codes, REASON_CODES, 4, required=True)
        signals = _codes(self.signal_codes, SIGNAL_CODES, len(SIGNAL_CODES))
        rules = _sequence(self.matched_rule_ids, maximum=MAX_RULE_IDS)
        for rule in rules:
            _integer(rule, positive=True)
        if len(set(rules)) != len(rules):
            _invalid()
        if self.override_id is not None:
            _integer(self.override_id, positive=True)
        if (self.decision_source == "exact_override" and self.override_id is None
                or self.decision_source == "source_rule" and not rules
                or self.decision_source == "uncertain_threshold" and self.label != "uncertain"):
            _invalid()
        for name, value in (("start", float(self.start)), ("end", float(self.end)),
                            ("confidence", float(self.confidence)), ("reason_codes", reasons),
                            ("signal_codes", signals), ("matched_rule_ids", rules)):
            object.__setattr__(self, name, value)


def _hash_payload(payload, field):
    return sha256(canonical_json({k: v for k, v in payload.items() if k != field}).encode()).hexdigest()


class _Artifact:
    __slots__ = ()
    _hash_field = "projection_sha256"

    def to_dict(self):
        return json.loads(self.to_json())

    def to_json(self):
        return canonical_json(asdict(self))

    def _finish_hash(self):
        expected = _hash_payload(asdict(self), self._hash_field)
        supplied = getattr(self, self._hash_field)
        if supplied != "" and supplied != expected:
            _invalid()
        object.__setattr__(self, self._hash_field, expected)


@dataclass(frozen=True, slots=True)
class SegmentPolicy(_Artifact):
    schema: str
    item_id: int
    source_id: int
    transcript_sha256: str
    segments_sha256: str
    policy_version: str
    classifier: ClassifierProvenance
    source_rule_set_sha256: str
    override_set_sha256: str
    previous_policy_sha256: str | None
    ranges: tuple[ClassificationRange, ...]
    included_segments: int
    excluded_segments: int
    uncertain_segments: int
    included_milliseconds: int
    excluded_milliseconds: int
    uncertain_milliseconds: int
    review_required: bool
    processing_milliseconds: int
    policy_sha256: str = ""

    _hash_field = "policy_sha256"

    def __post_init__(self):
        _identity(self, POLICY_SCHEMA)
        if self.policy_version != POLICY_VERSION or not isinstance(self.classifier, ClassifierProvenance):
            _invalid()
        # Revalidate nested values in case callers bypassed frozen protection.
        self.classifier.__post_init__()
        for value in (self.segments_sha256, self.source_rule_set_sha256, self.override_set_sha256):
            _digest(value)
        _digest(self.previous_policy_sha256, optional=True)
        ranges = _sequence(self.ranges, maximum=MAX_SEGMENTS)
        if not ranges:
            _invalid()
        previous_start, previous_end = 0, 0
        seen = set()
        for entry in ranges:
            if not isinstance(entry, ClassificationRange):
                _invalid()
            entry.__post_init__()
            if entry.start < previous_start or entry.end < previous_end or entry.first_segment_id in seen:
                _invalid()
            if (entry.decision_source not in {"exact_override", "source_rule"}
                    and apply_confidence_threshold(entry.label, entry.confidence) != entry.label):
                _invalid()
            seen.add(entry.first_segment_id)
            previous_start, previous_end = entry.start, entry.end
        for field in ("included_segments", "excluded_segments", "uncertain_segments", "included_milliseconds",
                      "excluded_milliseconds", "uncertain_milliseconds", "processing_milliseconds"):
            _integer(getattr(self, field))
        if (type(self.review_required) is not bool or self.review_required != bool(self.uncertain_segments)
                or not 0 < self.included_segments + self.excluded_segments + self.uncertain_segments <= MAX_SEGMENTS):
            _invalid()
        object.__setattr__(self, "ranges", ranges)
        self._finish_hash()

    @classmethod
    def from_json(cls, raw, *, transcript):
        data = _load(raw, cls)
        try:
            classifier = _construct(ClassifierProvenance, data["classifier"])
            ranges = tuple(_construct(ClassificationRange, row) for row in _sequence(data["ranges"], maximum=MAX_SEGMENTS))
            candidate = cls(**{**data, "classifier": classifier, "ranges": ranges})
            rebuilt = build_segment_policy(transcript=transcript, classifications=ranges,
                classifier=classifier, source_rule_set_sha256=candidate.source_rule_set_sha256,
                override_set_sha256=candidate.override_set_sha256,
                previous_policy_sha256=candidate.previous_policy_sha256,
                processing_milliseconds=candidate.processing_milliseconds)
            if rebuilt != candidate:
                _invalid()
            return candidate
        except (TypeError, KeyError, ValueError, OverflowError):
            _invalid()


def _identity(artifact, schema):
    if artifact.schema != schema:
        _invalid()
    _integer(artifact.item_id, positive=True)
    _integer(artifact.source_id, positive=True)
    _digest(artifact.transcript_sha256)


def _transcript(transcript):
    if not isinstance(transcript, CanonicalTranscript):
        _invalid()
    try:
        return parse_canonical_transcript(transcript.to_json(), expected_item_id=transcript.item_id,
            expected_transcript_sha256=transcript.transcript_sha256, expected_segments_sha256=None)
    except (TypeError, ValueError, OverflowError):
        _invalid()


def _coverage(transcript, ranges):
    positions = {segment.id: index for index, segment in enumerate(transcript.segments)}
    cursor = 0
    for entry in ranges:
        first, last = positions.get(entry.first_segment_id), positions.get(entry.last_segment_id)
        if (first != cursor or last is None or last < cursor
                or entry.start != transcript.segments[cursor].start
                or entry.end != transcript.segments[last].end):
            _invalid()
        yield entry, transcript.segments[cursor:last + 1]
        cursor = last + 1
    if cursor != len(transcript.segments):
        _invalid()


def build_segment_policy(*, transcript, classifications, classifier, source_rule_set_sha256,
                         override_set_sha256, previous_policy_sha256, processing_milliseconds) -> SegmentPolicy:
    transcript = _transcript(transcript)
    classifications = _sequence(classifications, maximum=MAX_SEGMENTS)
    ranges = []
    totals = {key: [0, 0] for key in ("included", "excluded", "uncertain")}
    for entry in classifications:
        if isinstance(entry, dict):
            entry = _construct(ClassificationRange, entry)
        if not isinstance(entry, ClassificationRange):
            _invalid()
        entry.__post_init__()
        label = entry.label if entry.decision_source in {"exact_override", "source_rule"} else apply_confidence_threshold(entry.label, entry.confidence)
        if label != entry.label:
            entry = replace(entry, label=label, eligible=False, decision_source="uncertain_threshold")
        ranges.append(entry)
    for entry, segments in _coverage(transcript, ranges):
        bucket = "included" if entry.eligible else "uncertain" if entry.label == "uncertain" else "excluded"
        totals[bucket][0] += len(segments)
        for segment in segments:
            milliseconds = (segment.end - segment.start) * 1000
            _number(milliseconds, maximum=MAX_INTEGER)
            totals[bucket][1] += round(milliseconds)
    return SegmentPolicy(POLICY_SCHEMA, transcript.item_id, transcript.source_id,
        transcript.transcript_sha256, sha256(transcript.to_json().encode()).hexdigest(), POLICY_VERSION,
        classifier, source_rule_set_sha256, override_set_sha256, previous_policy_sha256, tuple(ranges),
        totals["included"][0], totals["excluded"][0], totals["uncertain"][0],
        totals["included"][1], totals["excluded"][1], totals["uncertain"][1],
        bool(totals["uncertain"][0]), processing_milliseconds)


def _construct(cls, data):
    if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
        _invalid()
    try:
        return cls(**data)
    except (TypeError, ValueError, OverflowError):
        _invalid()


def _load(raw, cls):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _invalid()
            result[key] = value
        return result

    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= DEFAULT_MAX_BYTES:
            _invalid()
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: _invalid())
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            _invalid()
        _digest(data[cls._hash_field])
        if data[cls._hash_field] != _hash_payload(data, cls._hash_field):
            _invalid()
        return data
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _invalid()


def _projection_segments(artifact, schema):
    _identity(artifact, schema)
    _digest(artifact.policy_sha256)
    segments = _sequence(artifact.segments, maximum=MAX_SEGMENTS)
    seen = set()
    previous_start = 0
    for segment in segments:
        if not isinstance(segment, TranscriptSegment) or segment.id in seen or segment.start < previous_start:
            _invalid()
        segment.__post_init__()
        seen.add(segment.id)
        previous_start = segment.start
    object.__setattr__(artifact, "segments", segments)


@dataclass(frozen=True, slots=True)
class LearningProjection(_Artifact):
    schema: str
    item_id: int
    source_id: int
    transcript_sha256: str
    policy_sha256: str
    segments: tuple[TranscriptSegment, ...]
    projection_sha256: str = ""

    def __post_init__(self):
        _projection_segments(self, PROJECTION_SCHEMA)
        self._finish_hash()

    @classmethod
    def from_json(cls, raw, *, transcript, policy):
        data = _load(raw, cls)
        expected = build_learning_projection(transcript, policy)
        if canonical_json(data) != expected.to_json():
            _invalid()
        return expected


def _promotional_labels(labels):
    labels = _codes(labels, ("self_promotion", "cross_promotion"), 2, required=True)
    return tuple(label for label in ("self_promotion", "cross_promotion") if label in labels)


def _purpose(purpose):
    _public_text(purpose)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", purpose) or ".." in purpose:
        _invalid()


@dataclass(frozen=True, slots=True)
class PromotionalProjection(_Artifact):
    schema: str
    item_id: int
    source_id: int
    transcript_sha256: str
    policy_sha256: str
    purpose: str
    labels: tuple[str, ...]
    segments: tuple[TranscriptSegment, ...]
    projection_sha256: str = ""

    def __post_init__(self):
        _projection_segments(self, PROMOTIONAL_SCHEMA)
        _purpose(self.purpose)
        object.__setattr__(self, "labels", _promotional_labels(self.labels))
        self._finish_hash()

    @classmethod
    def from_json(cls, raw, *, transcript, policy):
        data = _load(raw, cls)
        expected = build_promotional_projection(transcript, policy, labels=data["labels"], purpose=data["purpose"])
        if canonical_json(data) != expected.to_json():
            _invalid()
        return expected


def _selected_segments(transcript, policy, labels):
    transcript = _transcript(transcript)
    if not isinstance(policy, SegmentPolicy):
        _invalid()
    policy = SegmentPolicy.from_json(policy.to_json(), transcript=transcript)
    return tuple(segment for entry, segments in _coverage(transcript, policy.ranges)
                 if entry.label in labels for segment in segments)


def build_learning_projection(transcript, policy) -> LearningProjection:
    segments = _selected_segments(transcript, policy, ELIGIBLE_LABELS)
    return LearningProjection(PROJECTION_SCHEMA, transcript.item_id, transcript.source_id,
        transcript.transcript_sha256, policy.policy_sha256, segments)


def build_promotional_projection(transcript, policy, *, labels, purpose) -> PromotionalProjection:
    labels = _promotional_labels(labels)
    _purpose(purpose)
    segments = _selected_segments(transcript, policy, labels)
    return PromotionalProjection(PROMOTIONAL_SCHEMA, transcript.item_id, transcript.source_id,
        transcript.transcript_sha256, policy.policy_sha256, purpose, labels, segments)


@dataclass(frozen=True, slots=True)
class KnowledgeProjection(_Artifact):
    """Editorial and uncertain segments in transcript order; uncertain ones are listed by ID.

    For the knowledge export only. Never stored, and rejected by `assert_citable`.
    Excluded labels (paid, self or cross promotion, housekeeping) never appear.
    """

    schema: str
    item_id: int
    source_id: int
    transcript_sha256: str
    policy_sha256: str
    segments: tuple[TranscriptSegment, ...]
    uncertain_segment_ids: tuple[str, ...]
    projection_sha256: str = ""

    def __post_init__(self):
        _projection_segments(self, KNOWLEDGE_SCHEMA)
        ids = _sequence(self.uncertain_segment_ids, maximum=MAX_SEGMENTS)
        order = {segment.id: index for index, segment in enumerate(self.segments)}
        if (any(not isinstance(value, str) or value not in order for value in ids) or len(set(ids)) != len(ids)
                or list(ids) != sorted(ids, key=order.__getitem__)):
            _invalid()
        object.__setattr__(self, "uncertain_segment_ids", ids)
        self._finish_hash()

    def label(self, segment_id):
        if not any(segment.id == segment_id for segment in self.segments):
            _invalid()
        return "uncertain" if segment_id in self.uncertain_segment_ids else "editorial"


def build_knowledge_projection(transcript, policy) -> KnowledgeProjection:
    transcript = _transcript(transcript)
    if not isinstance(policy, SegmentPolicy):
        _invalid()
    policy = SegmentPolicy.from_json(policy.to_json(), transcript=transcript)
    segments, uncertain = [], []
    for entry, covered in _coverage(transcript, policy.ranges):
        if entry.label in KNOWLEDGE_LABELS:
            segments.extend(covered)
            if entry.label == "uncertain":
                uncertain.extend(segment.id for segment in covered)
    return KnowledgeProjection(KNOWLEDGE_SCHEMA, transcript.item_id, transcript.source_id,
        transcript.transcript_sha256, policy.policy_sha256, tuple(segments), tuple(uncertain))
