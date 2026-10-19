"""Pure adapter tests: inference is always a deterministic injected fake."""

import ast
from dataclasses import FrozenInstanceError
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import whisper_transcribe as whisper


ROOT = Path(__file__).resolve().parents[1]
AUDIO_SHA = "a" * 64


def word(**changes):
    return SimpleNamespace(**({"start": 0.0, "end": 1.0, "word": " Hello", "probability": 0.9} | changes))


def segment(**changes):
    return SimpleNamespace(**({"start": 0.0, "end": 1.0, "text": " Hello world.",
                              "words": [word(), word(start=1.0, end=2.0, word=" world.", probability=0.8)]} | {"end": 2.0} | changes))


class FakeModel:
    def __init__(self, segments=None, duration=2.0, failure=None, late_failure=None):
        self.segments = [segment()] if segments is None else segments
        self.duration = duration
        self.failure = failure
        self.late_failure = late_failure
        self.calls = []
        self.materialized = False

    def transcribe(self, path, **options):
        self.calls.append((path, options))
        if self.failure:
            raise self.failure

        def generate():
            yield from self.segments
            if self.late_failure:
                raise self.late_failure
            self.materialized = True

        return generate(), SimpleNamespace(duration=self.duration, language="en")


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "_model_cache", {})
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
        (model_dir / name).write_bytes(b"fake model")
    audio = tmp_path / "audio.flac"
    audio.write_bytes(b"verified audio")
    created = []
    model = FakeModel()

    def factory(*args, **kwargs):
        created.append((args, kwargs))
        return model

    def run(**kwargs):
        return whisper.transcribe_audio(audio, item_id=7, audio_sha256=AUDIO_SHA,
                                        model_dir=model_dir, model_factory=factory, **kwargs)

    return SimpleNamespace(run=run, model=model, created=created, model_dir=model_dir, audio=audio)


def test_lazy_singleton_exact_local_cpu_int8_loading_and_process_identity(adapter, monkeypatch):
    assert adapter.created == []
    adapter.run()
    adapter.run()
    assert adapter.created == [((str(adapter.model_dir),), {
        "device": "cpu", "compute_type": "int8", "cpu_threads": 10,
        "num_workers": 1, "local_files_only": True,
    })]
    adapter.run(cpu_threads=3)
    assert len(adapter.created) == 2
    monkeypatch.setattr(whisper.os, "getpid", lambda: 123456789)
    adapter.run()
    assert len(adapter.created) == 3


def test_complete_generation_fixed_english_vad_and_bounded_beam(adapter):
    result = adapter.run()
    assert adapter.model.materialized
    assert adapter.model.calls == [(str(adapter.audio), {
        "language": "en", "beam_size": 5, "word_timestamps": True,
        "vad_filter": True, "vad_parameters": {"min_silence_duration_ms": 2000},
        "condition_on_previous_text": False,
    })]
    assert result.duration == 2.0
    assert result.segments[0].text == "Hello world."
    assert result.segments[0].words[0].word == " Hello"


def test_result_is_frozen_slotted_and_has_deterministic_canonical_bytes(adapter):
    result = adapter.run()
    fixture = (ROOT / "tests/fixtures/whisper-provider-result.json").read_bytes().rstrip(b"\n")
    assert result.to_bytes() == fixture
    assert result.to_json().encode() == fixture
    assert result.result_sha256 == sha256(fixture).hexdigest()
    assert adapter.run().to_bytes() == fixture
    for value in (result, result.segments[0], result.segments[0].words[0]):
        assert not hasattr(value, "__dict__")
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            value.start = 4


def test_job_identity_is_deterministic_and_binds_input_and_normalizer(monkeypatch):
    first = whisper.whisper_job_name(7, AUDIO_SHA)
    assert first == whisper.whisper_job_name(7, AUDIO_SHA)
    assert first != whisper.whisper_job_name(8, AUDIO_SHA)
    assert first != whisper.whisper_job_name(7, "b" * 64)
    monkeypatch.setattr(whisper, "NORMALIZER_VERSION", "other/v2")
    assert first != whisper.whisper_job_name(7, AUDIO_SHA)


@pytest.mark.parametrize("segments,duration", [
    ([], 2.0), ([segment(text="  ")], 2.0),
    ([segment(start="bad")], 2.0), ([segment(start=True)], 2.0),
    ([segment(end=-1)], 2.0), ([segment(end=3)], 2.0),
    ([segment(start=float("nan"))], 2.0), ([segment(end=float("inf"))], 2.0),
    ([segment(words=[word(probability=float("nan"))])], 2.0),
    ([segment(words=[word(probability=float("inf"))])], 2.0),
    ([segment(words=[word(probability=1.1)])], 2.0),
    ([segment(words=[word(probability=-0.1)])], 2.0),
    ([segment(words=[word(start=1, end=0)])], 2.0),
    ([segment(words=[word(end=3)])], 2.0),
    ([segment(words=[word(start=1, end=2), word(start=0, end=1)])], 2.0),
    ([segment(start=1, end=2, words=[]), segment(start=0, end=1, words=[])], 2.0),
    ([segment(text="private\x00text")], 2.0),
    ([segment()], float("nan")), ([segment()], float("inf")),
])
def test_malformed_output_is_permanent_and_safe(adapter, segments, duration):
    adapter.model.segments = segments
    adapter.model.duration = duration
    with pytest.raises(whisper.WhisperError) as error:
        adapter.run()
    assert (error.value.category, error.value.retryable) == ("invalid_whisper_output", False)
    assert str(error.value) == "invalid_whisper_output"


@pytest.mark.parametrize("limit,value", [("MAX_TEXT_CHARS", 2), ("MAX_WORDS", 1),
                                         ("MAX_SEGMENTS", 0), ("MAX_RESULT_BYTES", 10)])
def test_output_limits_are_enforced(adapter, monkeypatch, limit, value):
    monkeypatch.setattr(whisper, limit, value)
    with pytest.raises(whisper.WhisperError, match="invalid_whisper_output"):
        adapter.run()


def test_six_hour_duration_is_a_permanent_media_failure(adapter):
    adapter.model.duration = 21600.1
    with pytest.raises(whisper.WhisperError) as error:
        adapter.run()
    assert (error.value.category, error.value.retryable) == ("duration_too_large", False)


@pytest.mark.parametrize("failure,category,retryable", [
    (ValueError("secret decode payload"), "invalid_audio", False),
    (MemoryError("secret resource payload"), "resource_exhausted", True),
    (OSError(12, "secret resource payload"), "resource_exhausted", True),
    (RuntimeError("secret runtime payload"), "whisper_runtime_transient", True),
])
@pytest.mark.parametrize("late", [False, True])
def test_closed_errors_also_cover_generator_failures(adapter, failure, category, retryable, late):
    if late:
        adapter.model.late_failure = failure
    else:
        adapter.model.failure = failure
    with pytest.raises(whisper.WhisperError) as error:
        adapter.run()
    assert (error.value.category, error.value.retryable) == (category, retryable)
    assert str(error.value) == category
    assert error.value.__suppress_context__


def test_invalid_configuration_and_missing_local_model_fail_before_loading(adapter):
    for threads in (0, 11, True, "10"):
        with pytest.raises(whisper.WhisperError, match="whisper_configuration"):
            adapter.run(cpu_threads=threads)
    (adapter.model_dir / "model.bin").unlink()
    with pytest.raises(whisper.WhisperError, match="whisper_configuration"):
        adapter.run()
    assert adapter.created == []


def test_closed_error_constructor_rejects_unknown_categories():
    with pytest.raises(ValueError):
        whisper.WhisperError("private payload", True)
    with pytest.raises(ValueError):
        whisper.WhisperError("invalid_audio", True)


@pytest.mark.parametrize("name,category", [("InvalidDataError", "invalid_audio"),
                                          ("DecoderNotFoundError", "unsupported_media"),
                                          ("DemuxerNotFoundError", "unsupported_media")])
def test_pyav_decode_failures_never_become_paid_fallback_candidates(adapter, name, category):
    failure_type = type(name, (Exception,), {"__module__": "av.error"})
    adapter.model.failure = failure_type("private decode payload")
    with pytest.raises(whisper.WhisperError) as error:
        adapter.run()
    assert (error.value.category, error.value.retryable) == (category, False)


def test_model_startup_resource_exhaustion_is_retryable(adapter):
    def factory(*args, **kwargs):
        raise OSError(12, "private startup payload")

    with pytest.raises(whisper.WhisperError) as error:
        whisper.transcribe_audio(adapter.audio, item_id=7, audio_sha256=AUDIO_SHA,
                                 model_dir=adapter.model_dir, model_factory=factory)
    assert (error.value.category, error.value.retryable) == ("resource_exhausted", True)


@pytest.mark.parametrize("key,value", [("unexpected", "private"), ("provider", "other"),
                                      ("language", "fr"), ("model_revision", "main"),
                                      ("compute_type", "float32")])
def test_parser_rejects_unknown_fields_and_identity_mismatches(adapter, key, value):
    payload = adapter.run().to_dict()
    payload[key] = value
    with pytest.raises(whisper.WhisperError, match="invalid_whisper_output"):
        whisper.parse_whisper_result(json.dumps(payload))


def test_parser_round_trip_and_expected_lineage(adapter):
    result = adapter.run()
    parsed = whisper.parse_whisper_result(result.to_bytes(), expected_job_name=result.job_name,
                                          expected_audio_sha256=AUDIO_SHA)
    assert parsed == result
    with pytest.raises(whisper.WhisperError, match="artifact_mismatch"):
        whisper.parse_whisper_result(result.to_bytes(), expected_audio_sha256="b" * 64)


@pytest.mark.parametrize("input_type", [str, bytes])
def test_parser_rejects_oversized_utf8_before_whitespace_normalization(adapter, monkeypatch, input_type):
    result = adapter.run()
    payload = result.to_dict()
    payload["segments"][0]["text"] = "Hello" + "\u2003" * 32 + "world."
    raw = whisper.canonical_json(payload)
    maximum = len(raw)
    assert len(result.to_bytes()) < maximum < len(raw.encode("utf-8"))
    monkeypatch.setattr(whisper, "MAX_RESULT_BYTES", maximum)
    raw_input = raw if input_type is str else raw.encode("utf-8")
    with pytest.raises(whisper.WhisperError, match="invalid_whisper_output"):
        whisper.parse_whisper_result(raw_input)


def test_adapter_has_no_application_or_shell_imports_and_runtime_import_is_lazy():
    tree = ast.parse((ROOT / "whisper_transcribe.py").read_text())
    banned = {"flask", "sqlalchemy", "celery", "boto3", "s3_utils", "models", "app", "subprocess", "config"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
            assert not any(name.split(".")[0] in banned for name in names)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "faster_whisper" not in ast.unparse(node)
    assert "faster_whisper" not in sys.modules


def prep_module():
    spec = importlib.util.spec_from_file_location("cache_whisper_model", ROOT / "scripts/cache-whisper-model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_preparation_uses_immutable_snapshot_and_deterministic_manifest(tmp_path):
    prep = prep_module()
    snapshot = tmp_path / "snapshot"
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        snapshot.mkdir()
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
            (snapshot / name).write_bytes(name.encode())
        (snapshot / "nested").mkdir()
        (snapshot / "nested/extra").write_bytes(b"extra")
        return str(snapshot)

    path = prep.cache_model(ROOT / "whisper-model.lock.json", snapshot, snapshot_download=download)
    assert calls == [{"repo_id": whisper.MODEL_REPOSITORY, "revision": whisper.MODEL_REVISION,
                      "local_dir": str(snapshot)}]
    manifest = json.loads(path.read_bytes())
    assert manifest["schema"] == "whisper-model-manifest/v1"
    assert manifest["model_revision"] == whisper.MODEL_REVISION
    assert manifest["lock_sha256"] == sha256((ROOT / "whisper-model.lock.json").read_bytes()).hexdigest()
    assert set(manifest) == {"schema", "model_revision", "lock_sha256", "files"}
    assert set(manifest["files"]) == {"config.json", "model.bin", "tokenizer.json", "vocabulary.json", "nested/extra"}
    for name, identity in manifest["files"].items():
        content = (snapshot / name).read_bytes()
        assert identity == {"bytes": len(content), "sha256": sha256(content).hexdigest()}
    first = path.read_bytes()
    prep.cache_model(ROOT / "whisper-model.lock.json", snapshot, snapshot_download=lambda **_: str(snapshot))
    assert path.read_bytes() == first
    assert str(tmp_path).encode() not in first


@pytest.mark.parametrize("bad", ["mutable", "missing", "escape", "special"])
def test_model_preparation_refuses_untrusted_snapshot(tmp_path, bad):
    prep = prep_module()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    lock = json.loads((ROOT / "whisper-model.lock.json").read_bytes())
    if bad == "mutable":
        lock["model_revision"] = "main"
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock))
    for name in lock["required_files"]:
        (snapshot / name).write_bytes(b"model")
    if bad == "missing":
        (snapshot / "model.bin").unlink()
    if bad == "escape":
        (tmp_path / "private").write_text("private")
        (snapshot / "escape").symlink_to(tmp_path / "private")
    if bad == "special":
        import os
        os.mkfifo(snapshot / "fifo")
    with pytest.raises(ValueError):
        prep.cache_model(lock_path, snapshot, snapshot_download=lambda **_: str(snapshot))
    assert not (snapshot / "whisper-model-manifest.json").exists()


def test_smoke_asset_has_exact_upstream_identity():
    content = (ROOT / "assets/whisper-smoke.flac").read_bytes()
    assert len(content) == 1_152_693
    assert sha256(content).hexdigest() == "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715"
    assert "public domain" in (ROOT / "assets/WHISPER-SMOKE-ATTRIBUTION.md").read_text().lower()
