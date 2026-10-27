"""Router classification: subset schema, Python-side checks, fitting and provenance; offline."""

from dataclasses import replace
from hashlib import sha256
import json

import pytest

from commercial_policy import (EDITORIAL_AUTO_THRESHOLD, PROMPT_VERSION, ClassifierProvenance,
                               PolicyValidationError)
from commercial_signals import ClassifierWindow, Signal
from config import load_settings
import router_classifier
from router_classifier import (IMPLIED_SIGNALLED_CONFIDENCE, OUTPUT_SCHEMA, SYSTEM_PROMPT, ClassifierError, ClassifierExample,
                               RouterClassifier, expected_provenance, fitted_messages)
from router_client import CLASSIFICATION_CONTEXT_TOKENS, RouterClient, estimated_context_tokens
from tests.router_fake import MODEL, FakeRouter, fake_router  # noqa: F401
from tests.router_schema_validator import (RouterError as PinnedRouterError, estimate_tokens, provider_messages,
                                           validate_instance, validate_schema)
from tests.test_config import valid_environment  # noqa: F401
from transcripts import TranscriptSegment


def segment(index, text="Quoted transcript text"):
    return TranscriptSegment(f"seg-{index:06d}-{'a' * 16}", "a" * 64, float(index), float(index + 1), text,
                             "https://public.example/omitted-citation")


def window(targets=(2, 3), before=(1,), after=(4,), texts=None):
    texts = texts or {}
    # The signal ends at the second target: signals longer than 3 segments are rejected.
    target = tuple(segment(i, texts.get(i, "Quoted transcript text")) for i in targets)
    return ClassifierWindow(1, target, tuple(segment(i, texts.get(i, "Context text")) for i in before),
                            tuple(segment(i, texts.get(i, "Context text")) for i in after),
                            (Signal("sponsor_transition", target[0].id, target[:2][-1].id),))


def entry(first, last, label="editorial", confidence=0.99, reasons=("substantive_content",)):
    return {"first_segment_id": first, "last_segment_id": last, "label": label, "confidence": confidence,
            "reason_codes": list(reasons)}


@pytest.fixture
def settings(valid_environment):  # noqa: F811
    return load_settings(valid_environment)


@pytest.fixture
def classifier(fake_router, settings):  # noqa: F811
    return RouterClassifier(settings, client=RouterClient(fake_router.url, token_file=fake_router.token_file,
                                                          expected_model=MODEL))


def test_output_schema_uses_only_the_router_subset():
    validate_schema(OUTPUT_SCHEMA)
    win = window()
    validate_instance({"ranges": [entry(win.target_segments[0].id, win.target_segments[-1].id)]}, OUTPUT_SCHEMA)
    validate_instance({"ranges": []}, OUTPUT_SCHEMA)
    rendered = json.dumps(OUTPUT_SCHEMA)
    assert "pattern" not in rendered and "uniqueItems" not in rendered
    with pytest.raises(PinnedRouterError):
        validate_schema({"type": "string", "pattern": "^seg-[0-9]{6}-[a-f0-9]{16}$"})


def test_the_prompt_asks_for_non_editorial_ranges_only_under_v2():
    assert PROMPT_VERSION == "commercial-segment-classifier/v2"
    assert "not plain editorial" in SYSTEM_PROMPT and "is editorial" in SYSTEM_PROMPT
    assert OUTPUT_SCHEMA["properties"]["ranges"]["minItems"] == 0


def test_claude_era_v1_provenance_stays_valid():
    ClassifierProvenance("2.1.0", "claude-x", "claude-x", "commercial-segment-classifier/v1", "a" * 64)
    with pytest.raises(PolicyValidationError):
        ClassifierProvenance("2.1.0", "claude-x", "claude-x", "commercial-segment-classifier/v0", "a" * 64)


def test_classify_sends_one_structured_task_and_returns_router_provenance(fake_router, classifier, settings):  # noqa: F811
    win = window()
    fake_router.output = {"ranges": [entry(win.target_segments[0].id, win.target_segments[0].id),
                                     entry(win.target_segments[1].id, win.target_segments[1].id, "paid_commercial",
                                           0.97, ("sponsor_transition", "purchase_instruction"))]}
    result = classifier.classify(win, request_key="podcast.classification:rev-1:block-1:g0")
    assert [(d.label, d.confidence, d.reason_codes) for d in result.decisions] == [
        ("editorial", 0.99, ("substantive_content",)),
        ("paid_commercial", 0.97, ("sponsor_transition", "purchase_instruction"))]
    assert result.total_cost_microusd == 0 and result.provenance == expected_provenance(settings)
    _, _, headers, body = fake_router.requests[-1]
    assert headers["Idempotency-Key"] == "podcast.classification:rev-1:block-1:g0"
    assert (body["route"], body["prompt_version"], body["generation"]["max_output_tokens"],
            body["requirements"]["max_latency_ms"]) == ("podcast.classification", PROMPT_VERSION, 8192, 540_000)
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    payload = json.loads(body["messages"][1]["content"])
    assert payload["untrusted_quoted_data"] is True and payload["target_segment_ids"] == [
        s.id for s in win.target_segments]


def _bad_outputs():
    t2, t3 = segment(2).id, segment(3).id
    return [
        {"ranges": [entry("seg-00000x-aaaaaaaaaaaaaaaa", t3)]},
        {"ranges": [entry(segment(9).id, t3)]},
        {"ranges": [entry(t2, t3, reasons=("substantive_content", "substantive_content"))]},
        {"ranges": [entry(t3, t2)]},
        {"ranges": [entry(t2, segment(4).id)]},
        {"ranges": [entry(t2, t3), entry(t3, t3)]},
        {"ranges": [entry(t3, t3), entry(t2, t2)]},
    ]


@pytest.mark.parametrize("output", _bad_outputs())
def test_checks_the_subset_cannot_express_fail_as_invalid_output(fake_router, classifier, output):  # noqa: F811
    fake_router.output = output
    with pytest.raises(ClassifierError) as caught:
        classifier.classify(window())
    assert (caught.value.category, caught.value.reason, caught.value.advance_key) == (
        "model_invalid_output", "output_invalid", True)


def _implied(first, last, confidence=EDITORIAL_AUTO_THRESHOLD):
    return (first, last, "editorial", confidence, ("substantive_content",))


def _rows(result):
    return [(d.first_segment_id, d.last_segment_id, d.label, d.confidence, d.reason_codes) for d in result.decisions]


def test_unlisted_target_segments_are_implied_editorial(fake_router, classifier):  # noqa: F811
    win = window(targets=(2, 3, 4, 5, 6), before=(1,), after=(7,))
    ids = [s.id for s in win.target_segments]
    fake_router.output = {"ranges": [entry(ids[1], ids[2], "paid_commercial", 0.97, ("sponsor_transition",))]}
    assert _rows(classifier.classify(win)) == [
        _implied(ids[0], ids[0], IMPLIED_SIGNALLED_CONFIDENCE),  # inside the helper's signal (targets 1-2)
        (ids[1], ids[2], "paid_commercial", 0.97, ("sponsor_transition",)),
        _implied(ids[3], ids[4])]


def test_omitted_targets_inside_a_signal_are_implied_at_the_below_threshold_confidence(fake_router, classifier):  # noqa: F811
    win = window(targets=(2, 3, 4, 5, 6), before=(1,), after=(7,))
    ids = [s.id for s in win.target_segments]
    # The helper's signal covers targets 2-3; a second one runs from the context into target 6.
    win = replace(win, signals=(*win.signals, Signal("sponsor_transition", segment(6).id, segment(7).id)))
    fake_router.output = {"ranges": [entry(ids[1], ids[1], "paid_commercial", 0.97, ("sponsor_transition",))]}
    rows = _rows(classifier.classify(win))
    assert rows == [
        (ids[0], ids[0], "editorial", IMPLIED_SIGNALLED_CONFIDENCE, ("substantive_content",)),
        (ids[1], ids[1], "paid_commercial", 0.97, ("sponsor_transition",)),
        (ids[2], ids[3], "editorial", EDITORIAL_AUTO_THRESHOLD, ("substantive_content",)),
        (ids[4], ids[4], "editorial", IMPLIED_SIGNALLED_CONFIDENCE, ("substantive_content",))]
    assert IMPLIED_SIGNALLED_CONFIDENCE < EDITORIAL_AUTO_THRESHOLD


def test_an_empty_list_means_the_whole_block_is_editorial(fake_router, classifier):  # noqa: F811
    win = window(targets=(2, 3, 4), after=(5,))
    fake_router.output = {"ranges": []}
    ids = [s.id for s in win.target_segments]
    assert _rows(classifier.classify(win)) == [_implied(ids[0], ids[1], IMPLIED_SIGNALLED_CONFIDENCE), _implied(ids[2], ids[2])]


def test_explicit_editorial_ranges_and_full_coverage_still_work(fake_router, classifier):  # noqa: F811
    win = window()
    t2, t3 = (s.id for s in win.target_segments)
    fake_router.output = {"ranges": [entry(t2, t2), entry(t3, t3, "housekeeping", 0.96, ("routine_housekeeping",))]}
    assert _rows(classifier.classify(win)) == [
        (t2, t2, "editorial", 0.99, ("substantive_content",)),
        (t3, t3, "housekeeping", 0.96, ("routine_housekeeping",))]


def test_examples_are_trimmed_until_the_request_fits():
    win = window()
    examples = tuple(ClassifierExample("paid_commercial", tuple(segment(100 * n + i, "x" * 1000) for i in range(1, 41)))
                     for n in range(1, 21))
    messages = fitted_messages(win, examples)
    kept = len(json.loads(messages[1]["content"])["source_examples"])
    assert 0 < kept < 20
    assert estimated_context_tokens(messages, OUTPUT_SCHEMA, 8192) <= CLASSIFICATION_CONTEXT_TOKENS
    one_more = router_classifier._messages(router_classifier._input(win, examples[:kept + 1]))
    assert not router_classifier._fits(one_more)


def test_fitting_keeps_a_safety_margin_under_the_routers_own_estimate():
    win = window()
    examples = tuple(ClassifierExample("paid_commercial", tuple(segment(100 * n + i, "x" * 1000) for i in range(1, 41)))
                     for n in range(1, 21))
    messages = fitted_messages(win, examples)
    pinned = estimate_tokens(provider_messages(messages, OUTPUT_SCHEMA)) + 8192
    assert pinned == estimated_context_tokens(messages, OUTPUT_SCHEMA, 8192)
    assert pinned <= CLASSIFICATION_CONTEXT_TOKENS - router_classifier.FIT_MARGIN_TOKENS
    assert router_classifier.FIT_MARGIN_TOKENS > 0


def test_a_window_with_examples_near_the_limit_is_sent_and_accepted(fake_router, classifier):  # noqa: F811
    win = window(texts={2: "t" * 20_000, 3: "u" * 19_000})
    examples = tuple(ClassifierExample("housekeeping", tuple(segment(100 * n + i, "y" * 2_500) for i in range(1, 5)))
                     for n in range(1, 21))
    assert not router_classifier._fits(router_classifier._messages(router_classifier._input(win, examples)))
    fake_router.output = {"ranges": [entry(win.target_segments[0].id, win.target_segments[-1].id)]}
    result = classifier.classify(win, examples=examples, request_key="podcast.classification:rev-1:block-1:g0")
    assert len(result.decisions) == 1
    _, _, _, body = fake_router.requests[-1]
    sent = estimate_tokens(provider_messages(body["messages"], body["response_schema"]))
    assert sent + body["generation"]["max_output_tokens"] <= CLASSIFICATION_CONTEXT_TOKENS
    assert 0 < len(json.loads(body["messages"][1]["content"])["source_examples"]) < 20


def test_a_reused_key_resends_a_byte_identical_body(fake_router, classifier):  # noqa: F811
    win = window()
    examples = (ClassifierExample("paid_commercial", (segment(10), segment(11))),
                ClassifierExample("housekeeping", (segment(20),)))
    fake_router.output = {"ranges": [entry(win.target_segments[0].id, win.target_segments[-1].id)]}
    key = "podcast.classification:rev-7:block-2:g3"
    first = classifier.classify(win, examples=examples, request_key=key)
    second = classifier.classify(win, examples=examples, request_key=key)
    assert first == second and fake_router.generations == 1
    (_, _, first_headers, first_body), (_, _, second_headers, second_body) = fake_router.requests[-2:]
    assert first_body == second_body and first_headers["Idempotency-Key"] == second_headers["Idempotency-Key"] == key
    assert second_headers.get("Content-Length") == first_headers.get("Content-Length")


def test_context_is_trimmed_when_dropping_examples_is_not_enough():
    big = {i: "c" * 30_000 for i in (1, 2, 3, 6, 7, 8)}
    targets = (4, 5)
    target = tuple(segment(i) for i in targets)
    win = ClassifierWindow(1, target, tuple(segment(i, big[i]) for i in (1, 2, 3)),
                           tuple(segment(i, big[i]) for i in (6, 7, 8)),
                           (Signal("sponsor_transition", segment(1).id, segment(2).id),
                            Signal("return_to_program", target[0].id, target[1].id)))
    messages = fitted_messages(win, ())
    payload = json.loads(messages[1]["content"])
    visible = {s["id"] for s in payload["context_before"] + payload["target_segments"] + payload["context_after"]}
    assert payload["target_segment_ids"] == [s.id for s in target]
    assert len(payload["context_before"]) + len(payload["context_after"]) < 6
    assert all(s["first_segment_id"] in visible and s["last_segment_id"] in visible for s in payload["signals"])
    assert estimated_context_tokens(messages, OUTPUT_SCHEMA, 8192) <= CLASSIFICATION_CONTEXT_TOKENS


def test_a_window_that_cannot_fit_is_a_configuration_error(fake_router, classifier, monkeypatch):  # noqa: F811
    monkeypatch.setattr(router_classifier, "CLASSIFICATION_CONTEXT_TOKENS", 9_000)
    with pytest.raises(ClassifierError) as caught:
        classifier.classify(window(targets=(2,), before=(), after=(), texts={2: "t" * 3_000}))
    assert (caught.value.category, caught.value.reason) == ("model_config", "too_large") and not fake_router.requests


@pytest.mark.parametrize("change", ["not_window", "too_many_targets", "gap", "unknown_example_label",
                                    "duplicate_example"])
def test_invalid_inputs_are_configuration_errors_without_a_request(fake_router, classifier, change):  # noqa: F811
    win, examples = window(), ()
    if change == "not_window":
        win = "window"
    elif change == "too_many_targets":
        win = ClassifierWindow(1, tuple(segment(i) for i in range(1, 82)), (), (), ())
    elif change == "gap":
        win = ClassifierWindow(1, (segment(2), segment(4)), (), (), ())
    elif change == "unknown_example_label":
        examples = (ClassifierExample("advert", (segment(10),)),)
    else:
        examples = (ClassifierExample("housekeeping", (segment(10),)),) * 2
    with pytest.raises(ClassifierError) as caught:
        classifier.classify(win, examples=examples)
    assert (caught.value.category, caught.value.reason) == ("model_config", "refused") and not fake_router.requests


@pytest.mark.parametrize("setup, category, advance", [
    (lambda router: router.reply(429, FakeRouter.error("MODEL_RATE_LIMITED")), "router_busy", False),
    (lambda router: setattr(router, "model", "other:1b"), "model_mismatch", True),
])
def test_router_failures_surface_unchanged(fake_router, classifier, setup, category, advance):  # noqa: F811
    win = window()
    fake_router.output = {"ranges": [entry(win.target_segments[0].id, win.target_segments[-1].id)]}
    setup(fake_router)
    with pytest.raises(ClassifierError) as caught:
        classifier.classify(win)
    assert (caught.value.category, caught.value.advance_key) == (category, advance)


def test_requests_without_a_key_get_unique_adhoc_keys(fake_router, classifier):  # noqa: F811
    win = window()
    fake_router.output = {"ranges": [entry(win.target_segments[0].id, win.target_segments[-1].id)]}
    classifier.classify(win)
    classifier.classify(win)
    keys = [headers["Idempotency-Key"] for _, _, headers, _ in fake_router.requests]
    assert len(set(keys)) == 2 and all(key.startswith("podcast.classification:adhoc:") for key in keys)


def test_expected_provenance_names_route_model_and_prompt(settings):
    provenance = expected_provenance(settings)
    assert (provenance.cli_version, provenance.requested_model, provenance.resolved_model,
            provenance.prompt_version) == ("router:podcast.classification", "bonsai2:27b-pq2_0",
                                           "bonsai2:27b-pq2_0", PROMPT_VERSION)
    assert provenance.prompt_sha256 == sha256(SYSTEM_PROMPT.encode()).hexdigest()
