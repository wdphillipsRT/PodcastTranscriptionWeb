"""Immutable timed transcripts with deterministic hashes and origin lineage.

SHA-256 source hashes cover raw VTT bytes or sorted/compact provider JSON.
Segment hashes cover identity, lineage, ordinal, timing, text and citation,
excluding their own hash and ID. The transcript hash covers canonical JSON
excluding only transcript_sha256. No clock, random IDs or storage URLs enter it.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import html
import json
import math
import re

from ingestion_types import normalize_public_url
from s3_utils import DEFAULT_MAX_BYTES, validate_segment


SCHEMA = "media-transcript-segments/v1"
NORMALIZER_VERSION = "media-transcript-normalizer/v1"
MAX_TEXT_CHARS = 5_000_000
MAX_SEGMENTS = 100_000


class TranscriptValidationError(ValueError):
    """Malformed or oversized transcript; never contains a provider payload."""


def _invalid():
    raise TranscriptValidationError("invalid transcript input") from None


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _private_locator(value):
    return bool(re.search(r"(?:s3://|https?://[^\s<>]*amazonaws\.com|[?&]x-amz-|[?&](?:signature|token|credential)=)", value, re.I))


def _text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value) or _private_locator(value):
        _invalid()
    result = " ".join(value.split())
    if not result:
        _invalid()
    return result


def _time(value):
    try:
        if isinstance(value, bool):
            _invalid()
        number = float(value)
        if not math.isfinite(number) or number < 0:
            _invalid()
        return 0.0 if number == 0 else number
    except (ValueError, TypeError, OverflowError):
        _invalid()


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    id: str
    sha256: str
    start: float
    end: float
    text: str
    citation: str

    def __post_init__(self):
        start, end = _time(self.start), _time(self.end)
        if end < start or _text(self.text, MAX_TEXT_CHARS) != self.text:
            _invalid()
        if not isinstance(self.id, str) or not re.fullmatch(r"seg-[0-9]{6}-[a-f0-9]{16}", self.id):
            _invalid()
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            _invalid()
        if not isinstance(self.citation, str) or len(self.citation) > 8192 or _private_locator(self.citation) or any(ord(c) < 32 or ord(c) == 127 for c in self.citation):
            _invalid()
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class CanonicalTranscript:
    platform: str
    external_id: str
    item_id: int | str
    source_id: int | str
    origin: str
    provider: str
    provider_version: str
    tool_version: str
    language: str
    duration: float
    source_sha256: str
    segments: tuple[TranscriptSegment, ...]

    def __post_init__(self):
        lineage = _lineage(self.platform, self.external_id, self.item_id, self.source_id,
                           self.origin, self.provider, self.provider_version,
                           self.tool_version, self.language)
        if not isinstance(self.source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.source_sha256):
            _invalid()
        if not isinstance(self.segments, (list, tuple)) or not 0 < len(self.segments) <= MAX_SEGMENTS:
            _invalid()
        segments = tuple(self.segments)
        previous_start, previous_end, total = 0.0, 0.0, 0
        for ordinal, segment in enumerate(segments):
            if not isinstance(segment, TranscriptSegment):
                _invalid()
            if segment.start < previous_start or segment.end < previous_end:
                _invalid()
            total += len(segment.text)
            if total > MAX_TEXT_CHARS:
                _invalid()
            digest = _hash({**lineage, "ordinal": ordinal, "start": segment.start,
                            "end": segment.end, "text": segment.text, "citation": segment.citation})
            if digest != segment.sha256 or segment.id != f"seg-{ordinal + 1:06d}-{digest[:16]}":
                _invalid()
            previous_start, previous_end = segment.start, segment.end
        duration = _time(self.duration)
        if duration < previous_end:
            _invalid()
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "segments", segments)

    def _payload(self):
        return {"schema": SCHEMA, **asdict(self)}

    @property
    def transcript_sha256(self):
        return _hash(self._payload())

    def to_dict(self):
        return json.loads(self.to_json())

    def to_json(self):
        return canonical_json({**self._payload(), "transcript_sha256": self.transcript_sha256})

    def to_text(self):
        return "\n".join(f"[{s.start:.3f}–{s.end:.3f}] {s.text}" for s in self.segments)


def _identity(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str):
        return validate_segment(value)
    _invalid()


def _lineage(platform, external_id, item_id, source_id, origin, provider,
             provider_version, tool_version, language):
    try:
        validate_segment(external_id)
        item_id, source_id = _identity(item_id), _identity(source_id)
    except ValueError:
        _invalid()
    if platform not in {"youtube", "podcast"} or origin not in {"creator_caption", "automatic_caption", "amazon_transcribe"}:
        _invalid()
    if platform == "youtube" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", external_id):
        _invalid()
    if origin == "amazon_transcribe" and provider != "amazon_transcribe" or origin != "amazon_transcribe" and provider != "yt-dlp":
        _invalid()
    for value in (provider_version, tool_version, language, provider):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+/-]{1,128}", value):
            _invalid()
    return dict(platform=platform, external_id=external_id, item_id=item_id,
                source_id=source_id, origin=origin, provider=provider,
                provider_version=provider_version, tool_version=tool_version, language=language)


def _build(rows, *, source_sha256, platform, external_id, item_id, source_id,
           origin, provider, provider_version, tool_version, language, duration,
           canonical_url, max_text_chars, max_segments):
    lineage = _lineage(platform, external_id, item_id, source_id, origin, provider,
                       provider_version, tool_version, language)
    if canonical_url is not None:
        if _private_locator(canonical_url):
            _invalid()
        try:
            canonical_url = normalize_public_url(canonical_url)
        except ValueError:
            _invalid()
    if type(max_segments) is not int or not 0 < max_segments <= MAX_SEGMENTS or type(max_text_chars) is not int or not 0 < max_text_chars <= MAX_TEXT_CHARS:
        _invalid()
    segments, previous_start, previous_end, total = [], 0.0, 0.0, 0
    for start, end, raw_text in rows:
        start, end = _time(start), _time(end)
        if end < start or start < previous_start or end < previous_end:
            _invalid()
        text = _text(raw_text, max_text_chars)
        total += len(text)
        if total > max_text_chars or len(segments) >= max_segments:
            _invalid()
        if platform == "youtube":
            citation = f"https://www.youtube.com/watch?v={external_id}&t={math.floor(start)}s"
        else:
            start_target = f"{start:.3f}".rstrip("0").rstrip(".")
            end_target = f"{end:.3f}".rstrip("0").rstrip(".")
            citation = f"{canonical_url or 'podcast:' + external_id}#t={start_target},{end_target}"
        digest = _hash({**lineage, "ordinal": len(segments), "start": start, "end": end, "text": text, "citation": citation})
        segments.append(TranscriptSegment(f"seg-{len(segments) + 1:06d}-{digest[:16]}", digest, start, end, text, citation))
        previous_start, previous_end = start, end
    if not segments:
        _invalid()
    duration = previous_end if duration is None else _time(duration)
    if duration < previous_end:
        _invalid()
    return CanonicalTranscript(**lineage, duration=duration, source_sha256=source_sha256, segments=tuple(segments))


def _vtt_time(value):
    if not re.fullmatch(r"(?:[0-9]{2,}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3}", value):
        _invalid()
    parts = value.split(":")
    hours, minutes, seconds = (int(parts[0]), int(parts[1]), float(parts[2])) if len(parts) == 3 else (0, int(parts[0]), float(parts[1]))
    if minutes >= 60 or seconds >= 60:
        _invalid()
    return hours * 3600 + minutes * 60 + seconds


def normalize_vtt(content, *, platform, external_id, item_id, source_id, language,
                  origin, provider_version, tool_version=NORMALIZER_VERSION,
                  duration=None, canonical_url=None, max_bytes=DEFAULT_MAX_BYTES,
                  max_text_chars=MAX_TEXT_CHARS, max_segments=MAX_SEGMENTS):
    try:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not isinstance(raw, bytes) or type(max_bytes) is not int or not 0 < len(raw) <= max_bytes <= DEFAULT_MAX_BYTES:
            _invalid()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n(?:[ \t]*\n)+", text.strip(" \t\n"))
        if not re.fullmatch(r"WEBVTT(?:[ \t].*)?", blocks[0].split("\n")[0]) or origin not in {"creator_caption", "automatic_caption"}:
            _invalid()
        rows, previous_start, previous_end = [], 0.0, 0.0
        metadata = re.compile(r"^(?:NOTE(?:\s|$)|(?:STYLE|REGION)[ \t]*$)")
        block_index = 1
        while block_index < len(blocks):
            lines = blocks[block_index].split("\n")
            block_index += 1
            if metadata.match(lines[0]):
                continue
            index = 0 if " --> " in lines[0] else 1
            if index >= len(lines) or " --> " not in lines[index]:
                _invalid()
            start, end = lines[index].split(" --> ", 1)
            start, end = _vtt_time(start), _vtt_time(end.split()[0])
            if end < start or start < previous_start or end < previous_end:
                _invalid()
            previous_start, previous_end = start, end
            payload = " ".join(lines[index + 1:])
            if not payload.strip(" \t") and block_index < len(blocks):
                following = blocks[block_index]
                # Only an empty cue may own a detached provider payload.
                # Cue IDs/timing and metadata remain separate blocks.
                if not metadata.match(following.split("\n")[0]) and "-->" not in following:
                    payload = " ".join(following.split("\n"))
                    block_index += 1
            cue = html.unescape(re.sub(r"<[^>]*>", "", payload))
            if len(cue) > max_text_chars or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in cue):
                _invalid()
            if cue.split():
                rows.append((start, end, cue))
        return _build(rows, source_sha256=sha256(raw).hexdigest(), platform=platform, external_id=external_id,
                      item_id=item_id, source_id=source_id, origin=origin, provider="yt-dlp",
                      provider_version=provider_version, tool_version=tool_version, language=language,
                      duration=duration, canonical_url=canonical_url, max_text_chars=max_text_chars, max_segments=max_segments)
    except (UnicodeError, IndexError, KeyError, TypeError):
        _invalid()


def normalize_amazon_transcribe(content, *, platform, external_id, item_id, source_id,
                               language=None, provider_version="2017-10-26", tool_version=NORMALIZER_VERSION,
                               duration=None, canonical_url=None, max_bytes=DEFAULT_MAX_BYTES,
                               max_text_chars=MAX_TEXT_CHARS, max_segments=MAX_SEGMENTS):
    try:
        if isinstance(content, (str, bytes)):
            raw = content.encode("utf-8") if isinstance(content, str) else content
            if type(max_bytes) is not int or not 0 < len(raw) <= max_bytes <= DEFAULT_MAX_BYTES:
                _invalid()
            content = json.loads(raw)
        raw = canonical_json(content).encode("utf-8")
        if type(max_bytes) is not int or not 0 < len(raw) <= max_bytes <= DEFAULT_MAX_BYTES:
            _invalid()
        items = content["results"]["items"]
        if not isinstance(items, list) or len(items) > MAX_SEGMENTS * 20:
            _invalid()
        reported_language = content["results"].get("language_code")
        if language is not None and reported_language is not None and language != reported_language:
            _invalid()
        language = language or reported_language
        rows, previous_start, previous_end, total = [], 0.0, 0.0, 0
        for item in items:
            word = _text(item["alternatives"][0]["content"], max_text_chars)
            total += len(word)
            if total > max_text_chars:
                _invalid()
            if item["type"] == "punctuation":
                if not rows:
                    _invalid()
                rows[-1][2] += word
            elif item["type"] == "pronunciation":
                start, end = _time(item["start_time"]), _time(item["end_time"])
                if start > end or start < previous_start or end < previous_end:
                    _invalid()
                if not rows or start - rows[-1][0] >= 30:
                    rows.append([start, end, word])
                else:
                    rows[-1][1] = end
                    rows[-1][2] += " " + word
                previous_start, previous_end = start, end
            else:
                _invalid()
        return _build(rows, source_sha256=sha256(raw).hexdigest(), platform=platform, external_id=external_id,
                      item_id=item_id, source_id=source_id, origin="amazon_transcribe", provider="amazon_transcribe",
                      provider_version=provider_version, tool_version=tool_version, language=language,
                      duration=duration, canonical_url=canonical_url, max_text_chars=max_text_chars, max_segments=max_segments)
    except (ValueError, UnicodeError, IndexError, KeyError, TypeError, OverflowError, RecursionError):
        _invalid()
