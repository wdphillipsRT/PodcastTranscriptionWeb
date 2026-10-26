"""Editorial plus marked uncertain segments for the knowledge export; excluded labels never leave."""

from dataclasses import replace
import json

import pytest

from commercial_policy import (KnowledgeProjection, LearningProjection, PolicyValidationError, build_segment_policy,
                               build_knowledge_projection)
from extensions import db
from learning_projection import (LearningProjectionUnavailable, assert_citable, load_active_knowledge_projection,
                                 load_active_learning_projection)
import models as m
from tests.test_classification_service import add_item, artifacts, env, finish  # noqa: F401
from tests.test_learning_projection import corrupt_row, ready, replace_artifact  # noqa: F401


def load(ready, **kwargs):  # noqa: F811
    return load_active_knowledge_projection(session=db.session, storage=ready.storage, item_id=ready.item_id, **kwargs)


def test_knowledge_projection_holds_editorial_and_marked_uncertain_segments_only(ready):  # noqa: F811
    result = load(ready)
    editorial, uncertain = ready.transcript.segments[0], ready.transcript.segments[5]
    assert isinstance(result, KnowledgeProjection) and result.schema == "knowledge-transcript-projection/v1"
    assert result.segments == (editorial, uncertain)
    assert result.uncertain_segment_ids == (uncertain.id,)
    assert (result.label(editorial.id), result.label(uncertain.id)) == ("editorial", "uncertain")
    for excluded in ready.transcript.segments[1:5]:
        assert excluded.text not in result.to_json() and excluded.id not in result.to_json()
    assert {key for key, _ in ready.storage.reads} == {ready.keys[k] for k in ("canonical", "policy", "projection")}


def test_the_editorial_projection_is_unchanged(ready):  # noqa: F811
    assert load_active_learning_projection(session=db.session, storage=ready.storage,
                                           item_id=ready.item_id).segments == ready.transcript.segments[:1]


def test_citations_reject_the_knowledge_projection(ready):  # noqa: F811
    result = load(ready)
    with pytest.raises(LearningProjectionUnavailable):
        assert_citable(result, result.segments[0].id)


@pytest.mark.parametrize("damage", ["excluded", "uncertain", "omission"])
def test_a_tampered_stored_editorial_projection_fails_closed(ready, damage):  # noqa: F811
    data = json.loads(ready.storage.objects[ready.keys["projection"]])
    raw = json.loads(ready.storage.objects[ready.keys["canonical"]])
    if damage == "omission":
        data["segments"] = []
    else:
        data["segments"].append(raw["segments"][1 if damage == "excluded" else 5])
    replace_artifact(ready, "projection", data)
    with pytest.raises(LearningProjectionUnavailable):
        load(ready)


def test_a_policy_relabelled_to_export_excluded_text_fails_closed(ready):  # noqa: F811
    data = json.loads(ready.storage.objects[ready.keys["policy"]])
    for entry in data["ranges"]:
        if entry["label"] == "paid_commercial":
            entry["label"] = "uncertain"
    replace_artifact(ready, "policy", data)
    with pytest.raises(LearningProjectionUnavailable):
        load(ready)


@pytest.mark.parametrize("state", ["pending", "retrying", "running", "failed", "superseded"])
def test_the_active_revision_must_be_completed(ready, state):  # noqa: F811
    corrupt_row(m.ClassificationRevision, ready.revision_id, state=state)
    with pytest.raises(LearningProjectionUnavailable):
        load(ready)
    assert not ready.storage.reads


@pytest.mark.parametrize("ids", ["absent", "duplicate", "unordered"])
def test_marked_ids_must_be_present_unique_and_in_order(ready, ids):  # noqa: F811
    built = load(ready)
    editorial, uncertain = (segment.id for segment in built.segments)
    values = {"absent": (ready.transcript.segments[1].id,), "duplicate": (uncertain, uncertain),
              "unordered": (uncertain, editorial)}[ids]
    with pytest.raises(PolicyValidationError):
        replace(built, uncertain_segment_ids=values, projection_sha256="")


def test_the_build_is_deterministic_and_hashes_its_content(ready):  # noqa: F811
    _, policy, learning = artifacts(ready, ready.revision_id)
    first, second = build_knowledge_projection(ready.transcript, policy), build_knowledge_projection(ready.transcript, policy)
    assert first == second and first.to_json() == second.to_json()
    assert first.policy_sha256 == policy.policy_sha256 and len(first.projection_sha256) == 64
    assert isinstance(learning, LearningProjection) and learning.segments == first.segments[:1]


def rebuilt(ready, policy, change):  # noqa: F811
    """The fixture policy rebuilt through build_segment_policy, so its totals stay consistent."""
    positions = {segment.id: index for index, segment in enumerate(ready.transcript.segments)}
    ranges = tuple(change(positions[entry.first_segment_id], entry) for entry in policy.ranges)
    return build_segment_policy(transcript=ready.transcript, classifications=ranges, classifier=policy.classifier,
        source_rule_set_sha256=policy.source_rule_set_sha256, override_set_sha256=policy.override_set_sha256,
        previous_policy_sha256=policy.previous_policy_sha256, processing_milliseconds=policy.processing_milliseconds)


@pytest.mark.parametrize("binding", ["revision_row", "stored_learning_projection"])
def test_a_consistently_rebuilt_relabelled_policy_fails_closed(ready, binding):  # noqa: F811
    _, policy, _ = artifacts(ready, ready.revision_id)
    relabelled = rebuilt(ready, policy, lambda index, entry: replace(entry, label="uncertain", eligible=False)
                         if entry.label == "paid_commercial" else entry)
    assert relabelled.uncertain_segments == policy.uncertain_segments + 1
    replace_artifact(ready, "policy", json.loads(relabelled.to_json()))
    assert ready.keys["policy"].endswith(relabelled.policy_sha256 + ".json")
    if binding == "stored_learning_projection":
        corrupt_row(m.ClassificationRevision, ready.revision_id,
                    **{name: getattr(relabelled, name) for name in (
                        "included_segments", "excluded_segments", "uncertain_segments", "included_milliseconds",
                        "excluded_milliseconds", "uncertain_milliseconds", "review_required")})
    ready.storage.reads.clear()
    with pytest.raises(LearningProjectionUnavailable):
        load(ready)
    reads = {key for key, _ in ready.storage.reads}
    assert (ready.keys["projection"] in reads) is (binding == "stored_learning_projection")


def test_the_claude_decision_source_projects_like_the_model_one(ready):  # noqa: F811
    _, policy, _ = artifacts(ready, ready.revision_id)
    assert any(entry.decision_source == "model" for entry in policy.ranges)
    claude = rebuilt(ready, policy, lambda index, entry: replace(entry, decision_source="claude")
                     if entry.decision_source == "model" else entry)
    assert {entry.decision_source for entry in claude.ranges} == {"claude"}
    baseline, result = build_knowledge_projection(ready.transcript, policy), build_knowledge_projection(ready.transcript, claude)
    assert (result.segments, result.uncertain_segment_ids) == (baseline.segments, baseline.uncertain_segment_ids)


def test_exact_overrides_decide_the_knowledge_projection(ready):  # noqa: F811
    _, policy, _ = artifacts(ready, ready.revision_id)
    overrides = {1: ("paid_commercial", False, 0.1), 5: ("editorial", True, 1.0)}

    def change(index, entry):
        if index not in overrides:
            return entry
        label, eligible, confidence = overrides[index]
        return replace(entry, label=label, eligible=eligible, confidence=confidence,
                       decision_source="exact_override", override_id=index)

    result = build_knowledge_projection(ready.transcript, rebuilt(ready, policy, change))
    segments = ready.transcript.segments
    assert result.segments == (segments[0], segments[5]) and result.uncertain_segment_ids == ()
    assert segments[1].id not in result.to_json() and segments[1].text not in result.to_json()


def test_thresholded_uncertain_segments_carry_the_model_label_in_step_with_their_ids(ready):  # noqa: F811
    _, policy, _ = artifacts(ready, ready.revision_id)
    lowered = {1: .9, 4: .9}  # paid_commercial and housekeeping, below the .95 exclusion threshold
    policy = rebuilt(ready, policy, lambda index, entry: replace(entry, confidence=lowered[index])
                     if index in lowered else entry)
    result = build_knowledge_projection(ready.transcript, policy)
    segments = ready.transcript.segments
    assert result.uncertain_segment_ids == (segments[1].id, segments[4].id, segments[5].id)
    assert result.uncertain_model_labels == ("paid_commercial", "housekeeping", None)
    assert result.model_label(segments[1].id) == "paid_commercial" and result.model_label(segments[5].id) is None
    assert result.model_label(segments[0].id) is None


def test_the_loaded_projection_has_no_model_labels_for_a_policy_that_recorded_none(ready):  # noqa: F811
    result = load(ready)
    assert result.uncertain_model_labels == (None,)


@pytest.mark.parametrize("labels", [(), ("paid_commercial", None), ("uncertain",), ("advertisement",), (1,)])
def test_model_labels_must_align_with_the_uncertain_ids(ready, labels):  # noqa: F811
    with pytest.raises(PolicyValidationError):
        replace(load(ready), uncertain_model_labels=labels, projection_sha256="")
