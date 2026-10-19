"""Evidence stays advisory; only validated exact ranges remove classifier targets."""

from dataclasses import FrozenInstanceError, replace
import re

import pytest

from commercial_policy import ClassificationRange, SIGNAL_CODES
from commercial_signals import (
    ClassifierWindow, CommercialSignalError, ExactRule, RuleMatch, Signal,
    build_classifier_windows, collect_signals, fingerprint_range, match_exact_rules,
)
from transcripts import TranscriptSegment


def segments(*texts, offset=0):
    return tuple(TranscriptSegment(
        f"seg-{index + offset + 1:06d}-{index + offset:016x}",
        f"{index + offset:064x}", index + offset, index + offset + 1,
        " ".join(text.split()), "",
    ) for index, text in enumerate(texts))


def resolved(first, last=None):
    last = last or first
    return ClassificationRange(
        first.id, last.id, first.start, last.end, "editorial", True, 1,
        ("substantive_content",), (), "exact_override", (), 1,
    )


@pytest.mark.parametrize("code,text", [
    ("sponsor_transition", "This episode is BROUGHT TO YOU BY Acme."),
    ("sponsor_transition", "A word from our sponsor."),
    ("return_to_program", "And now, BACK TO THE SHOW."),
    ("discount_or_affiliate", "Use promo code LEARN for a discount."),
    ("discount_or_affiliate", "This is an affiliate link."),
    ("promotional_url", "Visit https://example.com/offer to get started."),
    ("purchase_instruction", "Buy now and start your free trial."),
    ("creator_offer", "Enroll in my course today."),
    ("other_show_promo", "Listen to our other podcast, Learning Weekly."),
    ("routine_housekeeping", "Subscribe and leave a review."),
    ("sponsor_transition", "ＴＨＩＳ ＥＰＩＳＯＤＥ ＩＳ ＳＰＯＮＳＯＲＥＤ ＢＹ Acme."),
])
def test_explicit_patterns_emit_only_bounded_advisory_evidence(code, text):
    source = segments(text)
    evidence = collect_signals(source)
    assert Signal(code, source[0].id, source[0].id) in evidence
    assert all(signal.code in SIGNAL_CODES for signal in evidence)
    assert all(set(signal.__dataclass_fields__) == {
        "code", "first_segment_id", "last_segment_id",
    } for signal in evidence)
    windows = build_classifier_windows(source, (), evidence)
    assert tuple(s for window in windows for s in window.target_segments) == source


def test_signals_are_unique_and_sorted_by_segment_then_vocabulary():
    source = segments(
        "Buy now; buy now; use promo code LEARN. This episode is sponsored by Acme.",
        "Back to the show. Subscribe and leave a review.",
    )
    evidence = collect_signals(source)
    assert evidence == collect_signals(source)
    assert len(evidence) == len(set(evidence))
    position = {segment.id: index for index, segment in enumerate(source)}
    assert evidence == tuple(sorted(evidence, key=lambda value: (
        position[value.first_segment_id], position[value.last_segment_id],
        SIGNAL_CODES.index(value.code),
    )))


@pytest.mark.parametrize("text", [
    "In this lesson, buy now is an example of an imperative sentence.",
    "Use promo code as a case study in persuasive language.",
    "Subscribe and leave a review are examples of routine podcast requests.",
    "Visit https://example.org to inspect the source data for this lesson.",
])
def test_teaching_calls_to_action_never_resolve_or_exclude_segments(text):
    source = segments(text)
    evidence = collect_signals(source)
    windows = build_classifier_windows(source, (), evidence)
    assert windows[0].target_segments == source
    assert match_exact_rules(source, ()) == ()


def test_ordinary_editorial_text_does_not_create_pattern_evidence():
    assert collect_signals(segments(
        "The sponsor of the bill introduced it yesterday.",
        "An affiliate is a legally separate organization.",
        "The program returns a value; the course of the river changed.",
    )) == ()


def test_phrase_matching_uses_smallest_one_to_three_segment_spans():
    source = segments("A lesson.", "This episode is brought", "to", "you by Acme.",
                      "Use promo", "code LEARN.", "Another lesson.")
    evidence = collect_signals(source)
    sponsor = tuple(s for s in evidence if s.code == "sponsor_transition")
    discount = tuple(s for s in evidence if s.code == "discount_or_affiliate")
    assert sponsor == (Signal("sponsor_transition", source[1].id, source[3].id),)
    assert discount == (Signal("discount_or_affiliate", source[4].id, source[5].id),)
    too_wide = segments("Brought", "to", "you", "by Acme.")
    assert not any(s.code == "sponsor_transition" for s in collect_signals(too_wide))


def test_repeated_block_requires_32_non_whitespace_characters_and_marks_each_occurrence():
    long = "Our thoughtful community supports these lessons every single week."
    source = segments("Hi.", long.upper(), "Hi.", long, "a" * 31, "a" * 31,
                      "b" * 32, "B" * 32, "c " * 31, "C " * 31)
    evidence = tuple(s for s in collect_signals(source) if s.code == "repeated_block")
    assert evidence == tuple(Signal("repeated_block", source[i].id, source[i].id) for i in (1, 3, 6, 7))


def test_promotional_urls_need_action_language_and_support_split_captions():
    source = segments("The study is at example.org.", "A lesson.", "Another lesson.",
                      "A final lesson.", "Go to", "example.com/offer", "for a free trial.")
    evidence = tuple(s for s in collect_signals(source) if s.code == "promotional_url")
    assert evidence == (
        Signal("promotional_url", source[4].id, source[5].id),
        Signal("promotional_url", source[5].id, source[6].id),
    )


def test_fingerprint_normalizes_unicode_case_and_preserves_words_and_punctuation():
    original = segments("Ｆｒｅｅ ＴＲＩＡＬ Straße!", "Use code LEARN.")
    normalized = segments("free trial STRASSE!", "use\u2003code\u00a0learn.", offset=30)
    digest = fingerprint_range(original)
    assert re.fullmatch(r"[a-f0-9]{64}", digest)
    assert digest == fingerprint_range(original) == fingerprint_range(normalized)
    assert digest != fingerprint_range(segments("free trial strasse", "use code learn."))
    assert digest != fingerprint_range(segments("free trial strasse!", "use learn."))


def test_fingerprint_preserves_segment_partitions_and_ignores_source_identity():
    original = segments("ab", "c")
    assert fingerprint_range(original) != fingerprint_range(segments("a", "bc"))
    assert fingerprint_range(original) != fingerprint_range(segments("ab c"))
    changed_identity = tuple(replace(s, id=f"seg-{i + 40:06d}-{'f' * 16}",
        sha256="f" * 64, start=s.start + 100, end=s.end + 100,
        citation="https://example.org/different-source") for i, s in enumerate(original))
    assert fingerprint_range(original) == fingerprint_range(changed_identity)


def test_exact_rule_matcher_returns_every_match_in_canonical_order():
    source = segments("Use code LEARN.", "Buy now.", "Use code learn.", "Buy now.")
    pair = fingerprint_range(source[:2])
    rules = (
        ExactRule(9, "self_promotion", pair, 2),
        ExactRule(2, "paid_commercial", pair, 2),
        ExactRule(7, "editorial", fingerprint_range(source[1:2]), 1),
        ExactRule(10, "housekeeping", "f" * 64, 100),
    )
    actual = match_exact_rules(source, rules)
    assert actual == (
        RuleMatch(2, "paid_commercial", source[0].id, source[1].id, pair),
        RuleMatch(9, "self_promotion", source[0].id, source[1].id, pair),
        RuleMatch(7, "editorial", source[1].id, source[1].id, fingerprint_range(source[1:2])),
        RuleMatch(2, "paid_commercial", source[2].id, source[3].id, pair),
        RuleMatch(9, "self_promotion", source[2].id, source[3].id, pair),
        RuleMatch(7, "editorial", source[3].id, source[3].id, fingerprint_range(source[3:])),
    )
    assert actual == match_exact_rules(source, tuple(reversed(rules)))
    assert match_exact_rules(source, ()) == ()


@pytest.mark.parametrize("changes", [
    {"rule_id": 0}, {"rule_id": True}, {"rule_id": "1"},
    {"label": "untrusted-transcript-marker"}, {"label": []},
    {"fingerprint_sha256": "A" * 64}, {"fingerprint_sha256": "untrusted-transcript-marker"},
    {"segment_count": 0}, {"segment_count": -1}, {"segment_count": True},
    {"segment_count": 1.5},
])
def test_exact_rules_reject_invalid_structure_with_safe_errors(changes):
    with pytest.raises(CommercialSignalError) as error:
        ExactRule(**{**dict(rule_id=1, label="editorial", fingerprint_sha256="a" * 64,
                          segment_count=1), **changes})
    assert "untrusted-transcript-marker" not in str(error.value)


@pytest.mark.parametrize("count,expected_sizes", [(1, [1]), (80, [80]), (81, [80, 1]), (803, [80] * 10 + [3])])
def test_windows_cover_every_unresolved_segment_once_with_bounded_context(count, expected_sizes):
    source = segments(*(f"Lesson {i}." for i in range(count)))
    windows = build_classifier_windows(source, (), ())
    assert [window.ordinal for window in windows] == list(range(1, len(windows) + 1))
    assert [len(window.target_segments) for window in windows] == expected_sizes
    assert tuple(s for window in windows for s in window.target_segments) == source
    assert len({s.id for window in windows for s in window.target_segments}) == count
    for window in windows:
        first = source.index(window.target_segments[0])
        end = source.index(window.target_segments[-1]) + 1
        assert window.context_before == source[max(0, first - 3):first]
        assert window.context_after == source[end:end + 3]
        assert len(window.context_before) <= 3 and len(window.context_after) <= 3
        assert len(window.target_segments) <= 80
        assert sum(len(s.text) for s in window.target_segments) <= 40_000


def test_windows_enforce_full_text_character_bound_and_do_not_charge_context():
    source = segments("a" * 20_000, "b" * 20_000, "c", "d" * 39_999)
    windows = build_classifier_windows(source, (), ())
    assert [window.target_segments for window in windows] == [source[:2], source[2:]]
    assert windows[0].context_after == source[2:]
    assert windows[1].context_before == source[:2]


def test_exact_ranges_split_targets_but_remain_read_only_context():
    source = segments(*(f"Lesson {i}." for i in range(12)))
    rule = RuleMatch(2, "paid_commercial", source[5].id, source[7].id, "a" * 64)
    windows = build_classifier_windows(source, (resolved(source[1], source[3]), rule), ())
    assert [window.target_segments for window in windows] == [source[:1], source[4:5], source[8:]]
    assert source[1] in windows[0].context_after
    assert source[7] in windows[-1].context_before
    assert build_classifier_windows(source, (resolved(source[0], source[-1]),), ()) == ()


def test_window_signals_are_wholly_contained_and_deduplicated():
    source = segments(*(f"Lesson {i}." for i in range(90)))
    first = Signal("purchase_instruction", source[0].id, source[0].id)
    context = Signal("return_to_program", source[79].id, source[82].id)
    outside = Signal("sponsor_transition", source[0].id, source[-1].id)
    windows = build_classifier_windows(source, (), (outside, context, first, first))
    assert windows[0].signals == (first, context)
    assert windows[1].signals == (context,)
    for window in windows:
        ids = {s.id for s in window.context_before + window.target_segments + window.context_after}
        assert all(signal.first_segment_id in ids and signal.last_segment_id in ids for signal in window.signals)


def test_oversized_unresolved_segment_fails_safely_but_resolved_segment_is_context():
    private_text = "private-transcript-marker" + "x" * 40_001
    source = segments(private_text, "A lesson.")
    with pytest.raises(CommercialSignalError) as error:
        build_classifier_windows(source, (), ())
    assert "private-transcript-marker" not in str(error.value)
    assert len(str(error.value)) < 150
    windows = build_classifier_windows(source, (resolved(source[0]),), ())
    assert windows[0].target_segments == source[1:]
    assert windows[0].context_before == source[:1]


def test_empty_inputs_and_immutable_dtos():
    assert collect_signals(()) == ()
    assert match_exact_rules((), ()) == ()
    assert build_classifier_windows((), (), ()) == ()
    source = segments("Buy now.")
    signal = Signal("purchase_instruction", source[0].id, source[0].id)
    rule = ExactRule(1, "editorial", fingerprint_range(source), 1)
    match = RuleMatch(1, "editorial", source[0].id, source[0].id, rule.fingerprint_sha256)
    window = ClassifierWindow(1, source, (), (), (signal,))
    for value, field in ((signal, "code"), (rule, "rule_id"), (match, "label"), (window, "ordinal")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)
