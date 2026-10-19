"""Deterministic release fixture; reports metric numerators/denominators only.

This evaluates policy thresholding/projection using supplied classifier outputs,
not the accuracy of a live model. Real reviewed data stays in .runtime/eval/.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

import commercial_policy as cp
from transcripts import normalize_vtt


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "commercial-eval.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def evaluate(data):
    """Build real validated policies and count outcomes against independent truth."""
    reports = {}
    for source in data["sources"]:
        def stamp(number):
            return f"{number // 3600:02}:{number // 60 % 60:02}:{number % 60:02}.000"
        cursor, cues = 0, []
        for case in data["cases"]:
            end = cursor + case["duration"]
            cues.append(f"{stamp(cursor)} --> {stamp(end)}\n{case['text']}")
            cursor = end
        transcript = normalize_vtt("WEBVTT\n\n" + "\n\n".join(cues),
            platform=source["platform"], external_id="synthetic01", item_id=source["source_id"],
            source_id=source["source_id"], language="en", tool_version="synthetic",
            origin="creator_caption", provider_version="synthetic", canonical_url="https://synthetic.invalid")
        ranges = [cp.ClassificationRange(s.id, s.id, s.start, s.end, case["output"],
            case["output"] == "editorial", case["confidence"], ("substantive_content",), (), "claude", (), None)
            for s, case in zip(transcript.segments, data["cases"])]
        policy = cp.build_segment_policy(transcript=transcript, classifications=ranges,
            classifier=cp.ClassifierProvenance("synthetic", "synthetic", "synthetic", cp.PROMPT_VERSION, "a" * 64),
            source_rule_set_sha256="b" * 64, override_set_sha256="c" * 64,
            previous_policy_sha256=None, processing_milliseconds=0)
        projection = cp.build_learning_projection(transcript, policy)
        truth = [case["truth"] for case in data["cases"]]
        predicted = [row.label for row in policy.ranges]
        assert {s.id for s in projection.segments} == {
            s.id for s, label in zip(transcript.segments, predicted) if label == "editorial"}
        truth_boundaries = {i for i in range(1, len(truth)) if truth[i] != truth[i - 1]}
        predicted_boundaries = {i for i in range(1, len(predicted)) if predicted[i] != predicted[i - 1]}
        by_label = {}
        for label in cp.LABELS:
            correct = sum(t == p == label for t, p in zip(truth, predicted))
            # Boundary counts per label count either edge of a labeled range.
            true_edges = {i for i in truth_boundaries if label in (truth[i - 1], truth[i])}
            pred_edges = {i for i in predicted_boundaries if label in (predicted[i - 1], predicted[i])}
            by_label[label] = {
                "precision": {"correct": correct, "predicted": predicted.count(label)},
                "recall": {"correct": correct, "truth": truth.count(label)},
                "boundary_accuracy": {"correct": len(true_edges & pred_edges),
                    "union": len(true_edges | pred_edges)},
            }
        reports[str(source["source_id"])] = {
            "labels": by_label,
            "false_exclusion_rate": {"excluded": sum(t == "editorial" and p != "editorial"
                for t, p in zip(truth, predicted)), "editorial": truth.count("editorial")},
            "uncertain_duration_rate": {"uncertain_milliseconds": policy.uncertain_milliseconds,
                "total_milliseconds": sum(round((s.end - s.start) * 1000) for s in transcript.segments)},
            "boundary_accuracy": {"correct": len(truth_boundaries & predicted_boundaries),
                "truth": len(truth_boundaries), "predicted": len(predicted_boundaries)},
        }
    return reports


def release_gate(data, reports):
    expected = data["expected_metrics"]
    assert expected["policy_version"] == cp.POLICY_VERSION, "policy version requires reviewed expected metrics update"
    for report in reports.values():
        assert report["false_exclusion_rate"]["excluded"] == 0, "editorial false exclusion blocks release"
        values = expected["per_source"]
        assert report["false_exclusion_rate"] == {"excluded": values["editorial_false_exclusions"], "editorial": values["editorial_segments"]}
        assert report["uncertain_duration_rate"] == {key: values[key] for key in ("uncertain_milliseconds", "total_milliseconds")}
        assert report["boundary_accuracy"] == {"correct": values["boundary_matches"],
            "truth": values["boundary_truth"], "predicted": values["boundary_predicted"]}
        for label, count in values["label_counts"].items():
            assert report["labels"][label]["precision"] == {"correct": count, "predicted": count}
            assert report["labels"][label]["recall"] == {"correct": count, "truth": count}
            boundary = report["labels"][label]["boundary_accuracy"]
            assert boundary["correct"] == boundary["union"]


def test_synthetic_release_metrics_cover_every_label_source_and_boundary(capsys):
    data = fixture()
    assert data["synthetic_only"] is True
    assert {s["platform"] for s in data["sources"]} == {"podcast", "youtube"}
    assert {c["truth"] for c in data["cases"]} == set(cp.LABELS)
    reports = evaluate(data)
    release_gate(data, reports)
    print(json.dumps(reports, sort_keys=True))
    printed = capsys.readouterr().out
    assert json.loads(printed) == reports
    assert all(case["text"] not in printed for case in data["cases"])
    assert not any(value in printed for value in ("https://", "sha256", "prompt", "confidence"))
    def counts_only(value):
        if isinstance(value, dict):
            return all(counts_only(child) for child in value.values())
        return type(value) is int and value >= 0
    assert counts_only(json.loads(printed))


def test_policy_version_drift_blocks_release_until_expected_metrics_are_updated(monkeypatch):
    data = fixture()
    monkeypatch.setattr(cp, "POLICY_VERSION", "commercial-segment-policy/next-synthetic")
    reports = evaluate(data)
    with pytest.raises(AssertionError, match="policy version"):
        release_gate(data, reports)
    data["expected_metrics"]["policy_version"] = cp.POLICY_VERSION
    release_gate(data, reports)


def test_editorial_false_exclusion_blocks_release_even_if_expected_counts_change():
    data = fixture()
    data["cases"][0].update(output="paid_commercial", confidence=1.0)
    data["expected_metrics"]["per_source"]["editorial_false_exclusions"] = 1
    with pytest.raises(AssertionError, match="editorial false exclusion"):
        release_gate(data, evaluate(data))


def test_metric_math_detects_nonperfect_precision_recall_duration_and_boundaries():
    data = deepcopy(fixture())
    data["cases"][1].update(output="editorial", confidence=1.0)
    report = evaluate(data)["101"]
    assert report["labels"]["editorial"]["precision"] == {"correct": 5, "predicted": 6}
    assert report["labels"]["paid_commercial"]["recall"] == {"correct": 2, "truth": 3}
    assert report["boundary_accuracy"] == {"correct": 8, "truth": 9, "predicted": 9}
    data["cases"][1].update(confidence=.79)
    report = evaluate(data)["101"]
    assert report["uncertain_duration_rate"] == {"uncertain_milliseconds": 23000, "total_milliseconds": 120000}
    with pytest.raises(AssertionError):
        release_gate(data, {"101": report})
