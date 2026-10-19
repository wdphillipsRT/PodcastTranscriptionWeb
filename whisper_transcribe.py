"""Pure, bounded local Whisper provider with no application dependencies.

Only the default model factory imports the optional inference runtime. The caller
owns verification of the local audio bytes and all persistence and retry policy.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from threading import Lock


PROVIDER = "faster-whisper"
PROVIDER_VERSION = "1.2.1"
MODEL = "medium.en"
MODEL_REPOSITORY = "Systran/faster-whisper-medium.en"
MODEL_REVISION = "a29b04bd15381511a9af671baec01072039215e3"
COMPUTE_TYPE = "int8"
LANGUAGE = "en"
RESULT_SCHEMA = "local-whisper-result/v1"
MODEL_LOCK_SCHEMA = "whisper-model-lock/v1"
MODEL_MANIFEST_SCHEMA = "whisper-model-manifest/v1"
CANONICAL_PROVIDER_VERSION = "1.2.1+medium.en.a29b04bd15381511a9af671baec01072039215e3.int8"
NORMALIZER_VERSION = "media-transcript-normalizer/v1"
MODEL_DIRECTORY = f"/models/{MODEL}-{MODEL_REVISION}"
MAX_SEGMENTS = 100_000
MAX_WORDS = 2_000_000
MAX_TEXT_CHARS = 5_000_000
MAX_RESULT_BYTES = 52_428_800
MAX_AUDIO_DURATION_SECONDS = 21_600
WHISPER_RETRYABLE = (
    "storage_transient", "worker_timeout", "whisper_runtime_transient", "resource_exhausted",
)
WHISPER_PERMANENT = (
    "invalid_audio", "unsupported_media", "duration_too_large",
    "whisper_configuration", "invalid_whisper_output", "artifact_mismatch",
)


class WhisperError(ValueError):
    """Closed safe error boundary; exception messages never include input data."""

    def __init__(self, category, retryable):
        if (category not in WHISPER_RETRYABLE + WHISPER_PERMANENT
                or type(retryable) is not bool
                or retryable != (category in WHISPER_RETRYABLE)):
            raise ValueError("invalid Whisper error category")
        self.category = category
        self.retryable = retryable
        super().__init__(category)


def _fail(category="invalid_whisper_output"):
    raise WhisperError(category, category in WHISPER_RETRYABLE) from None


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _fail()
    return 0.0 if value == 0 else float(value)


def _text(value, *, strip=False):
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS
            or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value)):
        _fail()
    # Validate Unicode before it can escape the safe error boundary on encoding.
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail()
    return " ".join(value.split()) if strip else value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        _fail("artifact_mismatch")
    return value


def whisper_job_name(item_id, audio_sha256):
    """Bind work to input content, immutable inference identity and normalizer."""
    if type(item_id) is not int or item_id <= 0:
        _fail("artifact_mismatch")
    identity = {
        "item_id": item_id, "audio_sha256": _sha(audio_sha256),
        "provider": PROVIDER, "provider_version": PROVIDER_VERSION,
        "model": MODEL, "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION, "compute_type": COMPUTE_TYPE,
        "language": LANGUAGE, "normalizer_version": NORMALIZER_VERSION,
    }
    return "whisper-" + sha256(canonical_json(identity).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WhisperWord:
    start: float
    end: float
    word: str
    probability: float

    def __post_init__(self):
        start, end, probability = _number(self.start), _number(self.end), _number(self.probability)
        if end < start or probability > 1:
            _fail()
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "probability", probability)
        _text(self.word)


@dataclass(frozen=True, slots=True)
class WhisperSegment:
    start: float
    end: float
    text: str
    words: tuple[WhisperWord, ...]

    def __post_init__(self):
        start, end = _number(self.start), _number(self.end)
        if end < start or not isinstance(self.words, (tuple, list)) or len(self.words) > MAX_WORDS:
            _fail()
        previous_start, previous_end = start, start
        for word in self.words:
            if (not isinstance(word, WhisperWord) or word.start < previous_start
                    or word.end < previous_end or word.end > end):
                _fail()
            previous_start, previous_end = word.start, word.end
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "text", _text(self.text, strip=True))
        object.__setattr__(self, "words", tuple(self.words))


@dataclass(frozen=True, slots=True)
class WhisperResult:
    job_name: str
    audio_sha256: str
    duration: float
    segments: tuple[WhisperSegment, ...]
    schema: str = RESULT_SCHEMA
    provider: str = PROVIDER
    provider_version: str = PROVIDER_VERSION
    model: str = MODEL
    model_repository: str = MODEL_REPOSITORY
    model_revision: str = MODEL_REVISION
    compute_type: str = COMPUTE_TYPE
    language: str = LANGUAGE

    def __post_init__(self):
        expected = (RESULT_SCHEMA, PROVIDER, PROVIDER_VERSION, MODEL,
                    MODEL_REPOSITORY, MODEL_REVISION, COMPUTE_TYPE, LANGUAGE)
        if (self.schema, self.provider, self.provider_version, self.model,
                self.model_repository, self.model_revision, self.compute_type, self.language) != expected:
            _fail()
        _sha(self.audio_sha256)
        if not isinstance(self.job_name, str) or not re.fullmatch(r"whisper-[a-f0-9]{64}", self.job_name):
            _fail()
        duration = _number(self.duration)
        if duration > MAX_AUDIO_DURATION_SECONDS:
            _fail("duration_too_large")
        if not isinstance(self.segments, (tuple, list)) or not 0 < len(self.segments) <= MAX_SEGMENTS:
            _fail()
        previous_start = previous_end = total_text = total_words = 0
        for segment in self.segments:
            if (not isinstance(segment, WhisperSegment) or segment.start < previous_start
                    or segment.end < previous_end or segment.end > duration):
                _fail()
            total_text += len(segment.text) + sum(len(word.word) for word in segment.words)
            total_words += len(segment.words)
            if total_text > MAX_TEXT_CHARS or total_words > MAX_WORDS:
                _fail()
            previous_start, previous_end = segment.start, segment.end
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "segments", tuple(self.segments))

    def to_dict(self):
        return {**asdict(self), "inference": {"beam_size": 5, "vad_filter": True, "word_timestamps": True}}

    def to_json(self):
        encoded = canonical_json(self.to_dict())
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            _fail()
        return encoded

    def to_bytes(self):
        return self.to_json().encode("utf-8")

    @property
    def result_sha256(self):
        return sha256(self.to_bytes()).hexdigest()


def parse_whisper_result(raw, *, expected_job_name=None, expected_audio_sha256=None):
    """Parse only the closed provider schema, without importing inference code."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        if not isinstance(raw, (str, bytes)):
            _fail()
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(raw) > MAX_RESULT_BYTES:
            _fail()
        payload = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(payload, dict):
            _fail()
        inference = payload.pop("inference")
        if (not isinstance(inference, dict) or set(inference) != {"beam_size", "vad_filter", "word_timestamps"}
                or type(inference["beam_size"]) is not int or inference["beam_size"] != 5
                or inference["vad_filter"] is not True or inference["word_timestamps"] is not True):
            _fail()
        if set(payload) != set(WhisperResult.__dataclass_fields__):
            _fail()
        segments = payload.pop("segments")
        if not isinstance(segments, list) or not 0 < len(segments) <= MAX_SEGMENTS:
            _fail()
        result_segments = []
        for segment in segments:
            if not isinstance(segment, dict) or set(segment) != {"start", "end", "text", "words"}:
                _fail()
            words = segment.pop("words")
            if not isinstance(words, list) or len(words) > MAX_WORDS:
                _fail()
            result_segments.append(WhisperSegment(**segment, words=tuple(WhisperWord(**word) for word in words)))
        result = WhisperResult(**payload, segments=tuple(result_segments))
        if ((expected_job_name is not None and result.job_name != expected_job_name)
                or (expected_audio_sha256 is not None and result.audio_sha256 != expected_audio_sha256)):
            _fail("artifact_mismatch")
        result.to_bytes()
        return result
    except WhisperError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        _fail()


_model_cache = {}
_model_lock = Lock()


def _default_model_factory(*args, **kwargs):
    import faster_whisper

    if faster_whisper.__version__ != PROVIDER_VERSION:
        _fail("whisper_configuration")
    return faster_whisper.WhisperModel(*args, **kwargs)


def _model(model_dir, cpu_threads, factory):
    if type(cpu_threads) is not int or not 1 <= cpu_threads <= 10:
        _fail("whisper_configuration")
    try:
        directory = Path(model_dir).resolve(strict=True)
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
            path = directory / name
            if not path.is_file() or not path.resolve().is_relative_to(directory):
                _fail("whisper_configuration")
    except (OSError, TypeError, ValueError):
        _fail("whisper_configuration")
    key = (os.getpid(), str(directory), PROVIDER, PROVIDER_VERSION, MODEL, MODEL_REPOSITORY,
           MODEL_REVISION, COMPUTE_TYPE, cpu_threads, factory)
    with _model_lock:
        if key not in _model_cache:
            try:
                _model_cache[key] = factory(str(directory), device="cpu", compute_type=COMPUTE_TYPE,
                                            cpu_threads=cpu_threads, num_workers=1, local_files_only=True)
            except WhisperError:
                raise
            except MemoryError:
                _fail("resource_exhausted")
            except OSError as error:
                _fail("resource_exhausted" if error.errno in (12, 24, 28) else "whisper_configuration")
            except Exception:
                _fail("whisper_configuration")
        return _model_cache[key]


def transcribe_audio(local_audio_path, *, item_id, audio_sha256, cpu_threads=10,
                     model_dir=MODEL_DIRECTORY, model_factory=None):
    """Consume inference fully and return only validated immutable provider data."""
    job_name = whisper_job_name(item_id, audio_sha256)
    model = _model(model_dir, cpu_threads, model_factory or _default_model_factory)
    try:
        generated, info = model.transcribe(
            str(local_audio_path), language=LANGUAGE, beam_size=5, word_timestamps=True,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 2000},
            condition_on_previous_text=False,
        )
        duration = _number(info.duration)
        if duration > MAX_AUDIO_DURATION_SECONDS:
            _fail("duration_too_large")
        segments = []
        total_words = total_text = 0
        for segment in generated:
            if len(segments) >= MAX_SEGMENTS:
                _fail()
            words = []
            for word in segment.words:
                total_words += 1
                if total_words > MAX_WORDS:
                    _fail()
                words.append(WhisperWord(word.start, word.end, word.word, word.probability))
                total_text += len(words[-1].word)
                if total_text > MAX_TEXT_CHARS:
                    _fail()
            converted = WhisperSegment(segment.start, segment.end, segment.text, tuple(words))
            total_text += len(converted.text)
            if total_text > MAX_TEXT_CHARS:
                _fail()
            segments.append(converted)
        result = WhisperResult(job_name, audio_sha256, duration, tuple(segments))
        result.to_bytes()
        return result
    except WhisperError:
        raise
    except Exception as error:
        if isinstance(error, MemoryError) or isinstance(error, OSError) and error.errno in (12, 24, 28):
            _fail("resource_exhausted")
        if type(error).__module__.split(".")[0] == "av":
            unsupported = {"DecoderNotFoundError", "DemuxerNotFoundError", "ProtocolNotFoundError"}
            _fail("unsupported_media" if type(error).__name__ in unsupported else "invalid_audio")
        if isinstance(error, (OSError, ValueError, EOFError)):
            _fail("invalid_audio")
        if isinstance(error, (AttributeError, TypeError, OverflowError)):
            _fail()
        _fail("whisper_runtime_transient")
