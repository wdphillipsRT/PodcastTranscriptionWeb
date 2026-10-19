"""Canonical bytes, lineage, timestamps and citations are deterministic."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import importlib
import json
from pathlib import Path

import pytest


VTT = "WEBVTT\n\n00:00:01.200 --> 00:00:02.000\nHello, <b>world!</b>\n\n00:00:32.000 --> 00:00:33.500\nNext.\n"
METADATA = dict(platform="youtube", external_id="abc123xyz_1", item_id=7, source_id=3, language="en-US", provider_version="2026.08.19", tool_version="normalizer-v1")


def module():
    assert importlib.util.find_spec("transcripts") is not None, "canonical transcript module is missing"
    return importlib.import_module("transcripts")


def provider_data():
    return json.loads(Path("tests/fixtures/transcribe-result.json").read_text())


def test_vtt_is_immutable_and_serializes_stable_hashed_canonical_json():
    api = module()
    first = api.normalize_vtt(VTT, origin="creator_caption", **METADATA)
    second = api.normalize_vtt(VTT.encode(), origin="creator_caption", **METADATA)
    assert isinstance(first, api.CanonicalTranscript)
    assert first.to_json() == second.to_json()
    with pytest.raises(FrozenInstanceError):
        first.language = "fr"
    with pytest.raises(FrozenInstanceError):
        first.segments[0].text = "changed"
    data = json.loads(first.to_json())
    assert first.to_json() == json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert data["schema"] == "media-transcript-segments/v1"
    assert data["source_id"] == 3 and data["item_id"] == 7
    assert data["provider_version"] == "2026.08.19" and data["tool_version"] == "normalizer-v1"
    assert data["origin"] == "creator_caption"
    assert data["duration"] == 33.5
    assert data["source_sha256"] == sha256(VTT.encode()).hexdigest()
    transcript_hash = data.pop("transcript_sha256")
    assert transcript_hash == sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert first.transcript_sha256 == transcript_hash
    assert first.segments[0].id == second.segments[0].id
    assert first.segments[0].text == "Hello, world!"
    assert first.segments[0].citation == "https://www.youtube.com/watch?v=abc123xyz_1&t=1s"
    assert "amazonaws.com" not in first.to_json()


def test_caption_origin_and_identity_change_hashes_and_preserve_language():
    api = module()
    creator = api.normalize_vtt(VTT, origin="creator_caption", **METADATA)
    automatic = api.normalize_vtt(VTT, origin="automatic_caption", **METADATA)
    different = api.normalize_vtt(VTT, origin="creator_caption", **{**METADATA, "source_id": 4})
    assert len({creator.transcript_sha256, automatic.transcript_sha256, different.transcript_sha256}) == 3
    assert creator.segments[0].sha256 != automatic.segments[0].sha256
    assert automatic.language == "en-US"
    assert automatic.origin == "automatic_caption"


def test_amazon_punctuation_duration_and_podcast_citations():
    result = module().normalize_amazon_transcribe(provider_data(), canonical_url="https://podcasts.example/episode", **{**METADATA, "platform": "podcast", "external_id": "episode-123", "provider_version": "2017-10-26"})
    assert result.origin == "amazon_transcribe" and result.provider == "amazon_transcribe"
    assert result.duration == 33.5
    assert [segment.text for segment in result.segments] == ["Hello, world!", "Next."]
    assert result.segments[0].citation == "https://podcasts.example/episode#t=1.2,2"
    shuffled = json.loads(json.dumps(provider_data(), sort_keys=True))
    repeated = module().normalize_amazon_transcribe(shuffled, canonical_url="https://podcasts.example/episode", **{**METADATA, "platform": "podcast", "external_id": "episode-123", "provider_version": "2017-10-26"})
    assert result.to_json() == repeated.to_json()


@pytest.mark.parametrize("timestamp", ["-1", "nan", "inf", "2.5"])
def test_amazon_rejects_invalid_or_reversed_word_times(timestamp):
    data = provider_data()
    data["results"]["items"][0]["start_time"] = timestamp
    with pytest.raises(ValueError):
        module().normalize_amazon_transcribe(data, **METADATA)


def test_amazon_rejects_nonmonotonic_word_times_even_inside_one_segment():
    data = provider_data()
    data["results"]["items"][2]["start_time"] = "0.1"
    with pytest.raises(ValueError):
        module().normalize_amazon_transcribe(data, **METADATA)


@pytest.mark.parametrize("vtt", ["not vtt", "WEBVTT\n\n00:00:03.000 --> 00:00:02.000\ntext", "WEBVTT\n\n00:00:61.000 --> 00:01:03.000\ntext", VTT + "\n00:00:00.000 --> 00:00:01.000\nbackwards"])
def test_invalid_vtt_is_rejected(vtt):
    with pytest.raises(ValueError):
        module().normalize_vtt(vtt, origin="creator_caption", **METADATA)


def test_normalization_enforces_input_text_and_lineage_bounds():
    api = module()
    for kwargs in ({"max_bytes": 5}, {"max_text_chars": 2}, {"max_segments": 1}, {"duration": 1}, {"origin": "amazon_transcribe"}):
        with pytest.raises(ValueError):
            api.normalize_vtt(VTT, **{**METADATA, "origin": "creator_caption", **kwargs})
    with pytest.raises(ValueError):
        api.normalize_amazon_transcribe(provider_data(), canonical_url="https://bucket.s3.amazonaws.com/key?X-Amz-Signature=secret", **METADATA)
    with pytest.raises(ValueError):
        api.normalize_vtt(VTT.replace("Next.", "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=secret"), origin="creator_caption", **METADATA)


def test_vtt_supports_cue_ids_settings_entities_and_rolling_caption_overlap():
    vtt = "WEBVTT\nKind: captions\n\nNOTE do not expose this\nignored\n\nfirst\n00:01.000 --> 00:03.000 align:start\n<v Speaker>One &amp; two.</v>\n\n00:02.000 --> 00:04.000\nThree."
    result = module().normalize_vtt(vtt, origin="automatic_caption", **METADATA)
    assert [segment.text for segment in result.segments] == ["One & two.", "Three."]


def test_canonical_constructor_freezes_children_and_validates_replacement():
    api = module()
    result = api.normalize_vtt(VTT, origin="creator_caption", **METADATA)
    mutable = list(result.segments)
    copy = replace(result, segments=mutable)
    mutable.clear()
    assert copy.to_json() == result.to_json()
    assert isinstance(copy.segments, tuple)
    for updates in ({"duration": float("nan")}, {"duration": 1}, {"segments": ()}, {"source_sha256": "wrong"}, {"origin": "invented"}):
        with pytest.raises(api.TranscriptValidationError):
            replace(result, **updates)
    with pytest.raises(api.TranscriptValidationError):
        replace(result.segments[0], start=-1)


def test_amazon_malformed_provider_data_is_always_a_safe_validation_error():
    api = module()
    for content in (b"SECRET not-json", {"SECRET": float("nan")}, {"results": {"items": [{"type": "pronunciation", "alternatives": "SECRET"}]}}, {"results": None}):
        with pytest.raises(api.TranscriptValidationError) as caught:
            api.normalize_amazon_transcribe(content, **METADATA)
        assert "SECRET" not in str(caught.value)


def test_provider_language_mismatch_cannot_be_silently_relabelled():
    data = provider_data()
    data["results"]["language_code"] = "fr-FR"
    with pytest.raises(module().TranscriptValidationError):
        module().normalize_amazon_transcribe(data, **METADATA)


@pytest.mark.parametrize("start,end,expected", [(12345.678, 12346.120, "12345.678,12346.12"), (1_000_000.001, 1_000_001.990, "1000000.001,1000001.99"), (1.200, 2.000, "1.2,2"), (0.000, 0.010, "0,0.01")])
def test_podcast_citations_preserve_milliseconds_without_exponent_or_rounding(start, end, expected):
    data = provider_data()
    data["results"]["items"] = [{"type": "pronunciation", "start_time": str(start), "end_time": str(end), "alternatives": [{"content": "Word."}]}]
    result = module().normalize_amazon_transcribe(data, canonical_url="https://podcasts.example/episode", **{**METADATA, "platform": "podcast", "external_id": "episode-123"})
    assert result.segments[0].citation == f"https://podcasts.example/episode#t={expected}"
