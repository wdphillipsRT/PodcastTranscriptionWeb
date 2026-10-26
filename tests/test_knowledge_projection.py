"""Editorial plus marked uncertain segments for the knowledge export; excluded labels never leave."""

from dataclasses import replace
import json

import pytest

from commercial_policy import (KnowledgeProjection, LearningProjection, PolicyValidationError,
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
