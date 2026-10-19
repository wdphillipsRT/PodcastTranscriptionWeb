"""Pure advisory commercial evidence, exact fingerprints, and bounded windows.

Patterns never resolve a classification. Exact rules must already be scoped to
the transcript's source by the caller, which also resolves conflicting matches.
"""

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata

from commercial_policy import (
    CONTEXT_SEGMENTS_PER_SIDE, LABELS, MAX_BLOCK_TEXT_CHARS, MAX_INTEGER,
    SIGNAL_CODES, TARGET_SEGMENTS_PER_BLOCK,
)
from transcripts import TranscriptSegment


class CommercialSignalError(ValueError):
    """Invalid evidence or unclassifiable input; never includes transcript text."""


def _invalid():
    raise CommercialSignalError("invalid commercial signal input") from None


def _segment_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"seg-[0-9]{6}-[a-f0-9]{16}", value):
        _invalid()


def _rule_fields(rule_id, label, fingerprint_sha256):
    if type(rule_id) is not int or not 0 < rule_id <= MAX_INTEGER:
        _invalid()
    if not isinstance(label, str) or label not in LABELS:
        _invalid()
    if not isinstance(fingerprint_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint_sha256):
        _invalid()


@dataclass(frozen=True, slots=True)
class Signal:
    code: str
    first_segment_id: str
    last_segment_id: str

    def __post_init__(self):
        if not isinstance(self.code, str) or self.code not in SIGNAL_CODES:
            _invalid()
        _segment_id(self.first_segment_id)
        _segment_id(self.last_segment_id)


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule_id: int
    label: str
    first_segment_id: str
    last_segment_id: str
    fingerprint_sha256: str

    def __post_init__(self):
        _rule_fields(self.rule_id, self.label, self.fingerprint_sha256)
        _segment_id(self.first_segment_id)
        _segment_id(self.last_segment_id)


@dataclass(frozen=True, slots=True)
class ClassifierWindow:
    ordinal: int
    target_segments: tuple[TranscriptSegment, ...]
    context_before: tuple[TranscriptSegment, ...]
    context_after: tuple[TranscriptSegment, ...]
    signals: tuple[Signal, ...]


@dataclass(frozen=True, slots=True)
class ExactRule:
    rule_id: int
    label: str
    fingerprint_sha256: str
    segment_count: int

    def __post_init__(self):
        _rule_fields(self.rule_id, self.label, self.fingerprint_sha256)
        if type(self.segment_count) is not int or self.segment_count <= 0:
            _invalid()


def _normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


_PATTERNS = {
    "sponsor_transition": re.compile(
        r"\b(?:brought to you by|(?:episode|show|program|podcast) is sponsored by|"
        r"a word from our sponsor|thanks to (?:our|today['’]s) sponsor)\b"),
    "return_to_program": re.compile(
        r"\b(?:back to (?:the|our) (?:show|program|episode|conversation)|"
        r"return to (?:the|our) (?:show|program|conversation))\b"),
    "purchase_instruction": re.compile(
        r"\b(?:buy now|order (?:now|today)|shop now|(?:start|try|get|claim) "
        r"(?:your |a |our )?(?:free |\d+[- ]day )?trial|sign up for "
        r"(?:a |your )?(?:free )?trial)\b"),
    "discount_or_affiliate": re.compile(
        r"\b(?:(?:promo|discount|coupon) code|use (?:the |my |our )?code|"
        r"affiliate (?:link|code)|\d+\s*(?:%|percent) off)\b"),
    "creator_offer": re.compile(
        r"\b(?:(?:my|our) (?:new |online )?(?:course|book|workshop|membership|"
        r"coaching program|paid newsletter)|(?:join|support) (?:my|our|us on) patreon)\b"),
    "other_show_promo": re.compile(
        r"\b(?:(?:listen to|check out|subscribe to) (?:our |my |the )?"
        r"(?:other|new|sister) (?:podcast|show)|(?:our|my) other podcast)\b"),
    "routine_housekeeping": re.compile(
        r"\b(?:subscribe (?:and|to (?:the|our|this) (?:channel|show|podcast))|"
        r"leave (?:us )?a (?:review|rating)|rate (?:and review|the (?:show|podcast))|"
        r"like and subscribe|hit the (?:bell|subscribe button))\b"),
}
_URL = re.compile(r"\b(?:https?://|www\.)[^\s]+|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,63}\b")
_PROMOTIONAL_ACTION = re.compile(
    r"\b(?:visit|go to|use (?:the |my |our )?code|sign up|subscribe|shop|buy|"
    r"order|get|save|discount|trial)\b")
_SIGNAL_ORDER = {code: index for index, code in enumerate(SIGNAL_CODES)}


def collect_signals(segments: tuple[TranscriptSegment, ...]) -> tuple[Signal, ...]:
    """Find minimal phrase spans of at most three segments, as evidence only."""
    normalized = tuple(_normalize(segment.text) for segment in segments)
    matches = {code: set() for code in SIGNAL_CODES}
    for width in range(1, 4):
        for first in range(len(segments) - width + 1):
            last = first + width - 1
            text = " ".join(normalized[first:last + 1])
            codes = [code for code, pattern in _PATTERNS.items() if pattern.search(text)]
            if _URL.search(text) and _PROMOTIONAL_ACTION.search(text):
                codes.append("promotional_url")
            for code in codes:
                # Width increases, so all strictly smaller matching spans have
                # already been examined. There are at most six subspans here.
                if not any((start, end) in matches[code]
                           for start in range(first, last + 1)
                           for end in range(start, last + 1)):
                    matches[code].add((first, last))
    occurrences = defaultdict(list)
    for index, text in enumerate(normalized):
        if len(text.replace(" ", "")) >= 32:
            occurrences[text].append(index)
    for indexes in occurrences.values():
        if len(indexes) > 1:
            matches["repeated_block"].update((index, index) for index in indexes)
    ordered = sorted((first, last, _SIGNAL_ORDER[code], code)
                     for code, spans in matches.items() for first, last in spans)
    return tuple(Signal(code, segments[first].id, segments[last].id)
                 for first, last, _, code in ordered)


_FINGERPRINT_VERSION = b"commercial-range-fingerprint/v1\0"


def _encoded_segment(segment):
    text = _normalize(segment.text).encode("utf-8")
    return len(text).to_bytes(8, "big") + text


def _fingerprint(encoded_segments):
    digest = sha256(_FINGERPRINT_VERSION + len(encoded_segments).to_bytes(8, "big"))
    for text in encoded_segments:
        digest.update(text)
    return digest.hexdigest()


def fingerprint_range(segments: tuple[TranscriptSegment, ...]) -> str:
    """Hash normalized text and segment boundaries, independent of lineage."""
    return _fingerprint(tuple(_encoded_segment(segment) for segment in segments))


def match_exact_rules(segments, rules) -> tuple[RuleMatch, ...]:
    """Return every exact match for caller-supplied, already source-scoped rules."""
    grouped = defaultdict(lambda: defaultdict(set))
    for rule in rules:
        if not isinstance(rule, ExactRule):
            _invalid()
        rule.__post_init__()
        if rule.segment_count <= len(segments):
            grouped[rule.segment_count][rule.fingerprint_sha256].add(rule)
    if not grouped:
        return ()
    encoded = tuple(_encoded_segment(segment) for segment in segments)
    found = []
    for count, fingerprints in grouped.items():
        for first in range(len(segments) - count + 1):
            last = first + count - 1
            fingerprint = _fingerprint(encoded[first:last + 1])
            for rule in fingerprints.get(fingerprint, ()):
                found.append((first, last, rule.rule_id, rule.label, RuleMatch(
                    rule.rule_id, rule.label, segments[first].id,
                    segments[last].id, fingerprint)))
    return tuple(value[-1] for value in sorted(found, key=lambda value: value[:4]))


def build_classifier_windows(segments, resolved_ranges, signals) -> tuple[ClassifierWindow, ...]:
    """Partition consecutive unresolved segments, retaining canonical context."""
    positions = {segment.id: index for index, segment in enumerate(segments)}
    resolved = set()
    for entry in resolved_ranges:
        first = positions.get(entry.first_segment_id)
        last = positions.get(entry.last_segment_id)
        if first is None or last is None or first > last:
            _invalid()
        resolved.update(range(first, last + 1))
    indexed_signals = []
    for signal in set(signals):
        if not isinstance(signal, Signal):
            _invalid()
        first = positions.get(signal.first_segment_id)
        last = positions.get(signal.last_segment_id)
        if first is None or last is None or first > last:
            _invalid()
        indexed_signals.append((first, last, _SIGNAL_ORDER[signal.code], signal))
    indexed_signals.sort(key=lambda value: value[:3])
    signal_starts = [value[0] for value in indexed_signals]
    windows = []
    cursor = 0
    while cursor < len(segments):
        if cursor in resolved:
            cursor += 1
            continue
        first = cursor
        cost = 0
        while cursor < len(segments) and cursor not in resolved:
            length = len(segments[cursor].text)
            if length > MAX_BLOCK_TEXT_CHARS:
                raise CommercialSignalError("unresolved segment exceeds classifier text limit") from None
            if cursor - first >= TARGET_SEGMENTS_PER_BLOCK or cost + length > MAX_BLOCK_TEXT_CHARS:
                break
            cost += length
            cursor += 1
        context_first = max(0, first - CONTEXT_SEGMENTS_PER_SIDE)
        context_end = min(len(segments), cursor + CONTEXT_SEGMENTS_PER_SIDE)
        signal_first = bisect_left(signal_starts, context_first)
        signal_end = bisect_left(signal_starts, context_end)
        evidence = tuple(value[3] for value in indexed_signals[signal_first:signal_end]
                         if value[1] < context_end)
        windows.append(ClassifierWindow(
            len(windows) + 1, tuple(segments[first:cursor]),
            tuple(segments[context_first:first]), tuple(segments[cursor:context_end]), evidence,
        ))
    return tuple(windows)
