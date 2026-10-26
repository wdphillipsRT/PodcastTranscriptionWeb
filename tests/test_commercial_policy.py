"""Public policy contracts fail closed and retain canonical segment provenance."""

from dataclasses import FrozenInstanceError, asdict, replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from commercial_policy import (
    ClassificationRange, ClassifierProvenance, SegmentPolicy, LearningProjection,
    PromotionalProjection, apply_confidence_threshold, build_segment_policy,
    build_learning_projection, build_promotional_projection,
)
from transcripts import canonical_json, normalize_amazon_transcribe, normalize_vtt, parse_canonical_transcript


@pytest.fixture
def sample():
    fixture = json.loads(Path("tests/fixtures/commercial-segments.json").read_text())
    original = normalize_vtt(fixture["vtt"], **fixture["metadata"])
    raw = canonical_json(fixture["canonical_transcript"])
    assert original.to_json() == raw
    transcript = parse_canonical_transcript(raw, expected_item_id=7,
        expected_transcript_sha256=original.transcript_sha256,
        expected_segments_sha256=sha256(raw.encode()).hexdigest())
    ranges = [ClassificationRange(s.id, s.id, s.start, s.end, label,
        label == "editorial", 1.0, (reason,), (), "claude", (), None)
        for s, label, reason in zip(transcript.segments, fixture["labels"], fixture["reason_codes"])]
    classifier = ClassifierProvenance("2.1.0", "sonnet", "claude-sonnet-4-6", "commercial-segment-classifier/v1", "a" * 64)
    return transcript, ranges, classifier


def policy_for(sample, **updates):
    transcript, ranges, classifier = sample
    return build_segment_policy(**{**dict(transcript=transcript, classifications=ranges,
        classifier=classifier, source_rule_set_sha256="b" * 64, override_set_sha256="c" * 64,
        previous_policy_sha256=None, processing_milliseconds=125), **updates})


def rehash(data, field):
    data = dict(data)
    data.pop(field, None)
    return {**data, field: sha256(canonical_json(data).encode()).hexdigest()}


def test_policy_is_immutable_deterministic_and_covers_all_segments(sample):
    transcript, ranges, classifier = sample
    policy = policy_for(sample)
    assert policy.schema == "commercial-segment-policy-artifact/v1"
    assert policy.policy_version == "commercial-segment-policy/v1"
    assert policy.classifier == classifier
    assert policy.ranges == tuple(ranges)
    assert policy.item_id == 7 and policy.source_id == 3
    assert policy.transcript_sha256 == transcript.transcript_sha256
    assert policy.segments_sha256 == sha256(transcript.to_json().encode()).hexdigest()
    assert (policy.included_segments, policy.excluded_segments, policy.uncertain_segments) == (3, 5, 1)
    assert (policy.included_milliseconds, policy.excluded_milliseconds, policy.uncertain_milliseconds) == (22000, 30000, 3000)
    assert policy.review_required is True
    assert policy.processing_milliseconds == 125
    assert policy.to_json() == policy_for(sample).to_json()
    assert policy.to_dict() == rehash(policy.to_dict(), "policy_sha256")
    assert SegmentPolicy.from_json(policy.to_json(), transcript=transcript) == policy
    with pytest.raises(FrozenInstanceError):
        policy.review_required = False
    with pytest.raises(FrozenInstanceError):
        policy.ranges[0].label = "uncertain"
    ranges.clear()
    assert len(policy.ranges) == 9


@pytest.mark.parametrize("label,confidence,expected", [
    ("editorial", .8, "editorial"), ("editorial", .799, "uncertain"),
    ("paid_commercial", .95, "paid_commercial"), ("self_promotion", .949, "uncertain"),
    ("cross_promotion", .95, "cross_promotion"), ("housekeeping", .94, "uncertain"),
    ("uncertain", 1, "uncertain"),
])
def test_fixed_confidence_thresholds(label, confidence, expected):
    assert apply_confidence_threshold(label, confidence) == expected


@pytest.mark.parametrize("confidence", [True, "0.9", None, -.1, 1.1, float("nan"), float("inf")])
def test_confidence_requires_finite_non_boolean_number(sample, confidence):
    with pytest.raises(ValueError):
        replace(sample[1][0], confidence=confidence)
    with pytest.raises(ValueError):
        apply_confidence_threshold("editorial", confidence)


def test_thresholding_and_exact_decisions_preserve_provenance(sample):
    ranges = sample[1]
    ranges[0] = replace(ranges[0], confidence=.79)
    ranges[1] = replace(ranges[1], confidence=.94)
    policy = policy_for(sample)
    assert all(r.label == "uncertain" and not r.eligible and r.decision_source == "uncertain_threshold" for r in policy.ranges[:2])
    for source, updates in [("exact_override", {"override_id": 1}), ("source_rule", {"matched_rule_ids": (2,)})]:
        ranges[0] = replace(ranges[0], confidence=0, decision_source=source, **updates)
        assert policy_for(sample).ranges[0].label == "editorial"
        with pytest.raises(ValueError):
            policy_for(sample, classifications=[replace(ranges[0], start=1), *ranges[1:]])


@pytest.mark.parametrize("updates", [
    {"eligible": False}, {"eligible": 1}, {"label": "advertisement"},
    {"start": True}, {"end": float("inf")}, {"start": -1},
    {"reason_codes": ("invented",)}, {"reason_codes": ()},
    {"reason_codes": ("substantive_content",) * 13},
    {"reason_codes": ("substantive_content", "sponsor_transition", "return_to_program", "creator_offer", "source_example")},
    {"signal_codes": ("invented",)},
    {"decision_source": "human"}, {"matched_rule_ids": (True,)},
    {"matched_rule_ids": (-1,)}, {"matched_rule_ids": (1, 1)},
    {"matched_rule_ids": tuple(range(1, 102))}, {"override_id": True},
    {"override_id": 0}, {"first_segment_id": "s3://private/key"},
    {"decision_source": "exact_override"}, {"decision_source": "source_rule"},
])
def test_range_constructor_rejects_inconsistent_or_unbounded_values(sample, updates):
    with pytest.raises(ValueError):
        replace(sample[1][0], **updates)


@pytest.mark.parametrize("damage", ["gap", "overlap", "order", "unknown", "start", "end", "empty"])
def test_policy_rejects_invalid_segment_coverage(sample, damage):
    transcript, ranges, _ = sample
    if damage == "gap":
        ranges.pop(1)
    elif damage == "overlap":
        ranges.insert(1, ranges[0])
    elif damage == "order":
        ranges[0], ranges[1] = ranges[1], ranges[0]
    elif damage == "unknown":
        ranges[0] = replace(ranges[0], first_segment_id="seg-999999-" + "a" * 16)
    elif damage in {"start", "end"}:
        ranges[0] = replace(ranges[0], **{damage: .1 if damage == "start" else 9.9})
    else:
        ranges.clear()
    with pytest.raises(ValueError):
        policy_for(sample)


def test_policy_accepts_contiguous_multisegment_ranges(sample):
    transcript, ranges, _ = sample
    ranges[1] = replace(ranges[1], last_segment_id=ranges[2].last_segment_id, end=ranges[2].end)
    del ranges[2]
    policy = policy_for(sample)
    assert policy.excluded_segments == 5
    assert policy.excluded_milliseconds == 30000


@pytest.mark.parametrize("field,value", [
    ("cli_version", "x" * 129), ("requested_model", "s3://private/model"),
    ("resolved_model", ""), ("prompt_sha256", "bad"), ("prompt_version", "v2"),
])
def test_provenance_requires_bounded_versioned_public_values(sample, field, value):
    with pytest.raises(ValueError):
        replace(sample[2], **{field: value})


@pytest.mark.parametrize("field", ["cli_version", "requested_model", "resolved_model"])
def test_provenance_constructor_rejects_all_unicode_control_characters(sample, field):
    # Unicode Cc comprises the C0 controls, DEL, and every C1 control.
    for codepoint in (*range(0x20), *range(0x7F, 0xA0)):
        with pytest.raises(ValueError):
            replace(sample[2], **{field: f"model{chr(codepoint)}identity"})


@pytest.mark.parametrize("field", ["cli_version", "requested_model", "resolved_model"])
def test_policy_parser_rejects_all_unicode_control_characters_in_provenance(sample, field):
    data = policy_for(sample).to_dict()
    for codepoint in (*range(0x20), *range(0x7F, 0xA0)):
        data["classifier"][field] = f"model{chr(codepoint)}identity"
        raw = canonical_json(rehash(data, "policy_sha256"))
        with pytest.raises(ValueError):
            SegmentPolicy.from_json(raw, transcript=sample[0])


def test_every_identity_input_changes_policy_hash(sample):
    baseline = policy_for(sample).policy_sha256
    for field, value in [("cli_version", "2.2.0"), ("requested_model", "opus"),
                         ("resolved_model", "claude-opus-4-6"), ("prompt_sha256", "d" * 64)]:
        assert policy_for(sample, classifier=replace(sample[2], **{field: value})).policy_sha256 != baseline
    for field, value in [("source_rule_set_sha256", "d" * 64), ("override_set_sha256", "d" * 64),
                         ("previous_policy_sha256", "d" * 64), ("processing_milliseconds", 126)]:
        assert policy_for(sample, **{field: value}).policy_sha256 != baseline


@pytest.mark.parametrize("field,value", [("processing_milliseconds", True), ("processing_milliseconds", -1),
    ("source_rule_set_sha256", "invalid"), ("override_set_sha256", None), ("previous_policy_sha256", "bad")])
def test_invalid_policy_metadata_is_rejected(sample, field, value):
    with pytest.raises(ValueError):
        policy_for(sample, **{field: value})


@pytest.mark.parametrize("damage", ["top", "range", "hash", "eligible", "metrics", "schema", "bool_identity", "nan", "duplicate_key"])
def test_policy_parser_rejects_tampering_even_with_recomputed_hash(sample, damage):
    transcript = sample[0]
    data = policy_for(sample).to_dict()
    if damage == "top": data["extra"] = "ignored"
    elif damage == "range": data["ranges"][0]["extra"] = "ignored"
    elif damage == "eligible": data["ranges"][0]["eligible"] = False
    elif damage == "metrics": data["included_segments"] = 9
    elif damage == "schema": data["schema"] = "other/v1"
    elif damage == "bool_identity": data["item_id"] = True
    elif damage == "nan": data["ranges"][0]["confidence"] = float("nan")
    if damage == "hash": data["policy_sha256"] = "f" * 64
    elif damage != "nan": data = rehash(data, "policy_sha256")
    raw = json.dumps(data)
    if damage == "duplicate_key": raw = raw[:-1] + ',"item_id":7}'
    with pytest.raises(ValueError):
        SegmentPolicy.from_json(raw, transcript=transcript)


def test_learning_projection_contains_only_full_editorial_records(sample):
    transcript = sample[0]
    policy = policy_for(sample)
    projection = build_learning_projection(transcript, policy)
    assert projection.schema == "learning-transcript-projection/v1"
    assert projection.segments == tuple(transcript.segments[i] for i in (0, 3, 4))
    assert projection.policy_sha256 == policy.policy_sha256
    assert projection.to_dict() == rehash(projection.to_dict(), "projection_sha256")
    assert LearningProjection.from_json(projection.to_json(), transcript=transcript, policy=policy) == projection
    for i in (1, 2, 5, 6, 7, 8):
        assert transcript.segments[i].id not in projection.to_json()
        assert transcript.segments[i].text not in projection.to_json()
    with pytest.raises(FrozenInstanceError):
        projection.item_id = 8


def test_promotional_projection_is_explicit_and_separately_typed(sample):
    transcript = sample[0]
    policy = policy_for(sample)
    promotional = build_promotional_projection(transcript, policy,
        labels=["cross_promotion", "self_promotion"], purpose="offer-analysis")
    assert promotional.schema == "promotional-analysis-projection/v1"
    assert promotional.purpose == "offer-analysis"
    assert promotional.segments == transcript.segments[5:7]
    assert promotional.labels == ("self_promotion", "cross_promotion")
    assert PromotionalProjection.from_json(promotional.to_json(), transcript=transcript, policy=policy) == promotional
    with pytest.raises(ValueError):
        LearningProjection.from_json(promotional.to_json(), transcript=transcript, policy=policy)
    for labels, purpose in [([], "offers"), (["paid_commercial"], "offers"), (["editorial"], "offers"),
                            (["self_promotion"] * 2, "offers"), (["self_promotion"], ""),
                            (["self_promotion"], "x" * 129), (["self_promotion"], "s3://private/purpose"),
                            (["self_promotion"], "offer analysis"), (["self_promotion"], "offers..private")]:
        with pytest.raises(ValueError):
            build_promotional_projection(transcript, policy, labels=labels, purpose=purpose)


@pytest.mark.parametrize("damage", ["unknown", "hash", "excluded", "duplicate", "text", "bool"])
def test_projection_parser_rejects_invalid_records_even_with_recomputed_hash(sample, damage):
    transcript = sample[0]
    policy = policy_for(sample)
    data = build_learning_projection(transcript, policy).to_dict()
    if damage == "unknown": data["extra"] = True
    elif damage == "excluded": data["segments"].append(asdict(transcript.segments[1]))
    elif damage == "duplicate": data["segments"].append(data["segments"][0])
    elif damage == "text": data["segments"][0]["text"] = "changed"
    elif damage == "bool": data["segments"][0]["start"] = False
    data = rehash(data, "projection_sha256")
    if damage == "hash": data["projection_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        LearningProjection.from_json(json.dumps(data), transcript=transcript, policy=policy)


def test_transcript_or_policy_hash_mismatch_prevents_artifacts(sample):
    transcript = sample[0]
    policy = policy_for(sample)
    other = replace(transcript, source_sha256="e" * 64)
    with pytest.raises(ValueError):
        build_learning_projection(other, policy)
    with pytest.raises(ValueError):
        SegmentPolicy.from_json(policy.to_json(), transcript=other)
    object.__setattr__(transcript.segments[0], "text", "mutated")
    with pytest.raises(ValueError):
        policy_for(sample)
    with pytest.raises(ValueError):
        build_learning_projection(transcript, policy)


def test_canonical_rolling_caption_overlap_is_covered_once(sample):
    transcript = normalize_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nOne.\n\n00:00:02.000 --> 00:00:04.000\nTwo.",
        platform="youtube", external_id="abc123xyz_1", item_id=7, source_id=3,
        language="en", provider_version="1", origin="creator_caption")
    ranges = [ClassificationRange(s.id, s.id, s.start, s.end, "editorial", True,
        1, ("substantive_content",), (), "claude", (), None) for s in transcript.segments]
    policy = policy_for(sample, transcript=transcript, classifications=ranges)
    assert policy.included_segments == 2
    assert policy.included_milliseconds == 4000
    assert build_learning_projection(transcript, policy).segments == transcript.segments


def test_general_projection_can_be_empty_when_no_segments_are_editorial(sample):
    ranges = [replace(r, label="housekeeping", eligible=False) for r in sample[1]]
    policy = policy_for(sample, classifications=ranges)
    projection = build_learning_projection(sample[0], policy)
    assert projection.segments == ()
    assert not policy.review_required
    assert LearningProjection.from_json(projection.to_json(), transcript=sample[0], policy=policy) == projection


def test_policy_rejects_duration_that_overflows_bounded_milliseconds(sample):
    transcript = normalize_amazon_transcribe({"results": {"items": [
        {"type": "pronunciation", "start_time": "0", "end_time": "1e308", "alternatives": [{"content": "word"}]}]}},
        platform="youtube", external_id="abc123xyz_1", item_id=7, source_id=3,
        language="en", provider_version="1")
    segment = transcript.segments[0]
    classification = replace(sample[1][0], first_segment_id=segment.id,
        last_segment_id=segment.id, start=segment.start, end=segment.end)
    with pytest.raises(ValueError):
        policy_for(sample, transcript=transcript, classifications=[classification])


def test_model_is_a_decision_source_and_claude_era_artifacts_stay_valid():
    from commercial_policy import DECISION_SOURCES, PolicyValidationError
    identity = "seg-000001-" + "a" * 16
    assert DECISION_SOURCES[:2] == ("claude", "model")
    for source in ("claude", "model"):
        ClassificationRange(identity, identity, 0.0, 1.0, "editorial", True, 0.9, ("substantive_content",), (),
                            source, (), None)
    with pytest.raises(PolicyValidationError):
        ClassificationRange(identity, identity, 0.0, 1.0, "editorial", True, 0.9, ("substantive_content",), (),
                            "router", (), None)
