"""Information-only comparison of the router model with Claude-era classifications.

`compare_models` takes a fixed-seed sample of completed items whose active
revision Claude produced, spread round-robin across sources. It classifies at
most `max_blocks` windows of each with the current classifier and compares
every target segment with the stored Claude policy, on the model's raw labels
and again after the confidence threshold (so a near-miss advert that the
threshold turns into `uncertain` is visible).

Nothing is written to the database or storage (the router keeps its own
invocation records). Each item's inputs are read once and frozen for every
retry of its windows. Calls use their own idempotency-key namespace,
`podcast.classification:compare-<run>:item-<id>:block-<ordinal>:g<generation>`,
which never matches a revision's `...:rev-<id>:block-...` keys, and a fresh
random run id, so a comparison never replays or collides with a real outcome.
Like a block, a key is reused after a busy answer the router did not store and
advances only after one it may have stored.

A busy router is retried with the worker's back-off for a bounded number of
attempts; the episode is then reported as skipped. Invalid model output is
counted and the window skipped. Any other router error stops the run. The
report holds only integer identifiers, counts and timings, never text.
"""

from collections import defaultdict
import random
import time
import uuid

import sqlalchemy as sa

from commercial_policy import ELIGIBLE_LABELS, LABELS, SegmentPolicy, apply_confidence_threshold
from models import ClassificationRevision, ContentItem
from router_classifier import ClassifierError
from s3_utils import DEFAULT_MAX_BYTES

BUSY_ATTEMPTS = 6  # Waits 30, 60, 120, 240, 480 s between them: at most 15.5 minutes per window.
_COUNTS = ("blocks", "segments", "editorial_to_excluded", "excluded_to_editorial", "raw_editorial_to_excluded",
           "raw_excluded_to_editorial", "model_uncertain", "raw_model_uncertain", "threshold_to_uncertain",
           "invalid_output", "busy_skipped")
_AGREEMENTS = ("agreement", "raw_agreement")


class _Busy(Exception):
    pass


def _bucket(label):
    return "editorial" if label in ELIGIBLE_LABELS else "uncertain" if label == "uncertain" else "excluded"


def _empty():
    return {**dict.fromkeys(_COUNTS, 0), "call_seconds": [],
            **{name: {label: {"agree": 0, "total": 0} for label in LABELS} for name in _AGREEMENTS}}


def select_sample(session, *, sample, seed):
    rows = session.execute(sa.select(ContentItem.id, ClassificationRevision.source_id).join(
        ClassificationRevision, ClassificationRevision.id == ContentItem.active_classification_revision_id).where(
        ContentItem.status == "completed", ClassificationRevision.state == "completed",
        ClassificationRevision.requested_model.like("claude-%")).order_by(ContentItem.id)).all()
    session.commit()
    by_source = defaultdict(list)
    for item_id, source_id in rows:
        by_source[source_id].append(item_id)
    rng = random.Random(seed)
    sources = sorted(by_source)
    rng.shuffle(sources)
    for source_id in sources:
        rng.shuffle(by_source[source_id])
    chosen = []
    while len(chosen) < sample and any(by_source[source_id] for source_id in sources):
        for source_id in sources:
            if by_source[source_id] and len(chosen) < sample:
                chosen.append(by_source[source_id].pop())
    return chosen


def _classify(service, window, examples, key_prefix, report, *, sleep, monotonic):
    """One window, retried on a busy router with the worker's back-off; None for invalid output."""
    generation = 0
    for attempt in range(BUSY_ATTEMPTS):
        if attempt:
            sleep(min(600, 30 * 2 ** (attempt - 1)))
        started = monotonic()
        try:
            return service.classifier.classify(window, examples=examples, request_key=f"{key_prefix}:g{generation}"), \
                round(monotonic() - started, 3)
        except ClassifierError as error:
            if error.advance_key:
                generation += 1
            if error.category == "router_busy":
                continue
            if error.category == "model_invalid_output":
                report["invalid_output"] += 1
                return None, None
            raise
    raise _Busy


def _count(report, before, label, prefix=""):
    name = "raw_agreement" if prefix else "agreement"
    report[name][before]["total"] += 1
    report[name][before]["agree"] += int(label == before)
    report[f"{prefix}editorial_to_excluded"] += int(_bucket(before) == "editorial" and _bucket(label) == "excluded")
    report[f"{prefix}excluded_to_editorial"] += int(_bucket(before) == "excluded" and _bucket(label) == "editorial")
    report[f"{prefix}model_uncertain"] += int(label == "uncertain")


def compare_item(session, service, item_id, *, max_blocks, run, sleep=time.sleep, monotonic=time.monotonic):
    # Read once: every call and retry for this item sends these exact windows and examples.
    inputs = service._read_inputs(item_id)
    item = session.get(ContentItem, item_id)
    revision = session.get(ClassificationRevision, item.active_classification_revision_id)
    policy_key, policy_sha256, source_id = revision.policy_s3_key, revision.policy_sha256, revision.source_id
    transcript_sha256 = revision.transcript_sha256
    session.commit()
    if transcript_sha256 != inputs.transcript.transcript_sha256:
        raise ValueError("revision is not for the current transcript")
    policy = SegmentPolicy.from_json(service.storage.get_bytes(policy_key, max_bytes=DEFAULT_MAX_BYTES),
                                     transcript=inputs.transcript)
    if policy.policy_sha256 != policy_sha256:
        raise ValueError("policy does not match its revision")
    positions = {segment.id: index for index, segment in enumerate(inputs.transcript.segments)}
    claude = {}
    for entry in policy.ranges:
        for segment in inputs.transcript.segments[positions[entry.first_segment_id]:positions[entry.last_segment_id] + 1]:
            claude[segment.id] = entry.label
    report = {"item_id": item_id, "source_id": source_id, **_empty()}
    for window in inputs.windows[:max_blocks]:
        key_prefix = f"podcast.classification:compare-{run}:item-{item_id}:block-{window.ordinal}"
        result, seconds = _classify(service, window, inputs.examples, key_prefix, report, sleep=sleep,
                                    monotonic=monotonic)
        if result is None:
            continue
        report["call_seconds"].append(seconds)
        report["blocks"] += 1
        targets = {segment.id: index for index, segment in enumerate(window.target_segments)}
        for decision in result.decisions:
            label = apply_confidence_threshold(decision.label, decision.confidence)
            for segment in window.target_segments[targets[decision.first_segment_id]:targets[decision.last_segment_id] + 1]:
                before = claude[segment.id]
                report["segments"] += 1
                _count(report, before, decision.label, prefix="raw_")
                _count(report, before, label)
                report["threshold_to_uncertain"] += int(label == "uncertain" and decision.label != "uncertain")
    return report


def _summary(report):
    seconds = report.pop("call_seconds")
    report["seconds_per_call"] = {"calls": len(seconds),
                                  "mean": round(sum(seconds) / len(seconds), 3) if seconds else None,
                                  "max": max(seconds) if seconds else None}
    for prefix in ("", "raw_"):
        report[f"{prefix}model_uncertain_share"] = (round(report[f"{prefix}model_uncertain"] / report["segments"], 4)
                                                    if report["segments"] else None)
    return report


def compare_models(*, session, service, sample, seed, max_blocks, sleep=time.sleep, monotonic=time.monotonic):
    run = uuid.uuid4().hex[:12]
    items = select_sample(session, sample=sample, seed=seed)
    episodes, total = [], {"items": 0, **_empty()}
    for item_id in items:
        try:
            report = compare_item(session, service, item_id, max_blocks=max_blocks, run=run, sleep=sleep,
                                  monotonic=monotonic)
        except _Busy:
            session.rollback()
            total["busy_skipped"] += 1
            episodes.append({"item_id": item_id, "skipped": "router_busy"})
            continue
        except ClassifierError:
            raise
        except Exception:
            session.rollback()
            episodes.append({"item_id": item_id, "error": "unavailable"})
            continue
        total["items"] += 1
        for name in _COUNTS:
            total[name] += report[name]
        for name in _AGREEMENTS:
            for label in LABELS:
                for part in ("agree", "total"):
                    total[name][label][part] += report[name][label][part]
        total["call_seconds"] += report["call_seconds"]
        episodes.append(_summary(report))
    return {"schema": "podcast-router-comparison/v1", "seed": seed, "sample": sample, "max_blocks": max_blocks,
            "model": service.settings.ROUTER_EXPECTED_MODEL, "selected": len(items), "episodes": episodes,
            "total": _summary(total)}
