"""Canonical bytes, lineage, timestamps and citations are deterministic."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import importlib
import json
from pathlib import Path
import subprocess
import sys

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


@pytest.mark.parametrize("spacer", [" \t", ""])
def test_vtt_accepts_whitespace_split_cue_payload(spacer):
    raw = f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{spacer}\nSynthetic caption.\n"
    result = module().normalize_vtt(raw, origin="automatic_caption", **METADATA)
    assert [segment.text for segment in result.segments] == ["Synthetic caption."]


def test_vtt_accepts_youtube_whitespace_split_payloads_and_skips_empty_cues():
    raw = (b"WEBVTT\r\nKind: captions\r\n\r\n"
           b"00:00:00.000 --> 00:00:01.000\r\n \t\r\n\r\n"
           b"first\r\n00:00:01.000 --> 00:00:03.000 align:start\r\n \t\r\n"
           b"<00:00:01.500><c>One &amp; two.</c>\r\n\r\n"
           b"00:00:02.000 --> 00:00:04.000\r\n \t\r\n\r\n"
           b"00:00:03.000 --> 00:00:05.000\r\nThree.\r\n\r\n"
           b"00:00:05.000 --> 00:00:06.000\r\n \t\r\n")
    first = module().normalize_vtt(raw, origin="automatic_caption", **METADATA)
    second = module().normalize_vtt(raw, origin="automatic_caption", **METADATA)
    assert [(segment.start, segment.end, segment.text) for segment in first.segments] == [
        (1.0, 3.0, "One & two."), (3.0, 5.0, "Three.")]
    assert first.source_sha256 == sha256(raw).hexdigest()
    assert first.to_json() == second.to_json()
    assert first.segments[0].citation == "https://www.youtube.com/watch?v=abc123xyz_1&t=1s"


@pytest.mark.parametrize("following", [
    "next-cue\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "NOTE hidden note\nhidden\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "STYLE\n::cue { color: lime; }\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "STYLE \n::cue { color: lime; }\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "STYLE\t\n::cue { color: lime; }\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "REGION\nid:region\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "REGION \nid:region\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
    "REGION\t\nid:region\n\n00:00:02.000 --> 00:00:03.000\nSecond.",
])
def test_vtt_empty_cues_do_not_consume_next_cue_ids_or_metadata(following):
    raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nFirst.\n\n"
           "00:00:01.000 --> 00:00:02.000\n \t\n\n" + following)
    result = module().normalize_vtt(raw, origin="automatic_caption", **METADATA)
    assert [segment.text for segment in result.segments] == ["First.", "Second."]


@pytest.mark.parametrize("payload", ["STYLEguide", "REGIONAL", "STYLE caption", "REGION caption"])
def test_vtt_metadata_prefixes_remain_detached_caption_text(payload):
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n \t\n" + payload
    result = module().normalize_vtt(raw, origin="automatic_caption", **METADATA)
    assert [segment.text for segment in result.segments] == [payload]


@pytest.mark.parametrize("payload", ["\u00a0", "&nbsp;", "\u2003"])
def test_vtt_whitespace_only_cues_are_skipped(payload):
    raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nFirst.\n\n"
           "00:00:01.000 --> 00:00:02.000\n" + payload +
           "\n\n00:00:02.000 --> 00:00:03.000\nSecond.")
    result = module().normalize_vtt(raw, origin="automatic_caption", **METADATA)
    assert [segment.text for segment in result.segments] == ["First.", "Second."]


@pytest.mark.parametrize("payload", ["", " \t\n", "<c></c>", "\u00a0", "&nbsp;"])
def test_vtt_without_usable_caption_text_is_rejected(payload):
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n" + payload
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        module().normalize_vtt(raw, origin="automatic_caption", **METADATA)


@pytest.mark.parametrize("cue", [
    "00:00:03.000 --> 00:00:02.000\n \t",
    "00:00:00.000 --> 00:00:04.000\n \t",
    "00:00:02.000 --> 00:00:02.500\n \t",
    "00:00:61.000 --> 00:01:03.000\n \t",
    "00:00:02.000 --> malformed\n \t",
    "00:00:02.000 --> 00:00:04.000\n\nSECRET\x00",
    "00:00:02.000 --> 00:00:04.000\n\n\x0b",
    "00:00:02.000 --> 00:00:04.000\n\n\x0c",
    "00:00:02.000 --> 00:00:04.000\n\ns3://private/SECRET",
    "00:00:02.000 --> 00:00:04.000\n\nhttps://bucket.s3.amazonaws.com/SECRET",
    "00:00:02.000 --> 00:00:04.000\n\nhttps://example.com/?token=SECRET",
    "00:00:02.000 --> 00:00:04.000\n\nNOTE ignored\n\nSECRET orphan",
])
def test_vtt_empty_and_split_cues_retain_safe_timing_and_text_validation(cue):
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nFirst.\n\n" + cue
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        module().normalize_vtt(raw, origin="automatic_caption", **METADATA)


def test_vtt_still_rejects_orphan_text_after_a_complete_cue():
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nFirst.\n\nSECRET orphan"
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        module().normalize_vtt(raw, origin="automatic_caption", **METADATA)


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


def parse_stored(raw, transcript, **updates):
    expectations = dict(expected_item_id=transcript.item_id,
                        expected_transcript_sha256=transcript.transcript_sha256,
                        expected_segments_sha256=sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest())
    return module().parse_canonical_transcript(raw, **{**expectations, **updates})


def test_public_parser_round_trips_exact_stored_bytes_and_optional_hashes():
    transcript = module().normalize_vtt(VTT, origin="creator_caption", **METADATA)
    for raw in (transcript.to_json(), transcript.to_json().encode()):
        parsed = parse_stored(raw, transcript)
        assert parsed == transcript
        assert parsed.to_json() == transcript.to_json()
    assert parse_stored(transcript.to_json(), transcript, expected_segments_sha256=None,
                        expected_transcript_sha256=None) == transcript


@pytest.mark.parametrize("updates", [
    {"expected_item_id": 8}, {"expected_item_id": True},
    {"expected_transcript_sha256": "a" * 64}, {"expected_segments_sha256": "b" * 64},
    {"expected_transcript_sha256": "bad"}, {"expected_segments_sha256": False},
])
def test_public_parser_rejects_mismatched_expected_lineage(updates):
    transcript = module().normalize_vtt(VTT, origin="creator_caption", **METADATA)
    with pytest.raises(module().TranscriptValidationError):
        parse_stored(transcript.to_json(), transcript, **updates)


@pytest.mark.parametrize("damage", ["unknown", "segment_unknown", "bool_time", "string_time", "nan", "duplicate_id", "text", "schema", "hash", "private", "oversized"])
def test_public_parser_rejects_invalid_stored_contract(damage):
    transcript = module().normalize_vtt(VTT, origin="creator_caption", **METADATA)
    data = transcript.to_dict()
    if damage == "unknown":
        data["extra"] = "ignored"
    elif damage == "segment_unknown":
        data["segments"][0]["extra"] = "ignored"
    elif damage in {"bool_time", "string_time", "nan"}:
        data["segments"][0]["start"] = {"bool_time": True, "string_time": "1.2", "nan": float("nan")}[damage]
    elif damage == "duplicate_id":
        data["segments"][1] = data["segments"][0]
    elif damage == "text":
        data["segments"][0]["text"] = "tampered"
    elif damage == "private":
        data["segments"][0]["citation"] = "s3://private/secret"
    elif damage == "oversized":
        data["provider_version"] = "x" * 129
    else:
        data["schema" if damage == "schema" else "transcript_sha256"] = "invalid"
    raw = json.dumps(data)
    with pytest.raises(module().TranscriptValidationError):
        parse_stored(raw, transcript)


@pytest.mark.parametrize("raw", [b"SECRET", b"\xff", "null", "[]", "1", '{"schema":1,"schema":2}'])
def test_public_parser_always_raises_safe_validation_error(raw):
    with pytest.raises(module().TranscriptValidationError) as caught:
        module().parse_canonical_transcript(raw, expected_item_id=7,
                                           expected_transcript_sha256=None, expected_segments_sha256=None)
    assert "SECRET" not in str(caught.value)


def test_public_parser_rejects_valid_json_with_noncanonical_storage_bytes():
    transcript = module().normalize_vtt(VTT, origin="creator_caption", **METADATA)
    for raw in (json.dumps(transcript.to_dict()), transcript.to_json() + "\n"):
        with pytest.raises(module().TranscriptValidationError):
            parse_stored(raw, transcript)


def whisper_data():
    return json.loads(Path("tests/fixtures/whisper-provider-result.json").read_bytes())


def normalize_whisper(content=None, **updates):
    api = module()
    raw = api.canonical_json(whisper_data() if content is None else content).encode()
    options = dict(platform="youtube", external_id=METADATA["external_id"], item_id=7,
                   source_id=3, audio_sha256="a" * 64, raw_sha256=sha256(raw).hexdigest())
    return api.normalize_local_whisper(raw, **{**options, **updates})


def test_local_whisper_builds_stable_canonical_segments_and_round_trips():
    import whisper_transcribe as provider
    first = normalize_whisper()
    repeated = normalize_whisper(json.loads(json.dumps(whisper_data(), sort_keys=True)))
    assert first.to_json() == repeated.to_json()
    assert first.transcript_sha256 == "17a9a415ac9039028861b3059f77e0029e52f37cb83f1d45f33512bcddfa4b50"
    assert sha256(first.to_json().encode()).hexdigest() == "3e2b41db79af34b70615d9f3eb5e1a24dac7cf1ccb607a5dd34dacee1e606909"
    assert first.origin == "local_whisper" and first.provider == "faster-whisper"
    assert first.language == "en" and first.duration == 2.0
    assert first.provider_version == provider.CANONICAL_PROVIDER_VERSION
    assert first.tool_version == module().NORMALIZER_VERSION == provider.NORMALIZER_VERSION
    artifact = module().canonical_json(whisper_data()).encode("utf-8")
    assert first.source_sha256 == sha256(artifact).hexdigest()
    assert [(s.start, s.end, s.text) for s in first.segments] == [(0.0, 2.0, "Hello world.")]
    assert first.segments[0].id == f"seg-000001-{first.segments[0].sha256[:16]}"
    assert first.segments[0].sha256 == "8790915715c93f562582d4d630819b293d19f37c1d7f873e2e126b5ce1c3320c"
    assert first.segments[0].citation == "https://www.youtube.com/watch?v=abc123xyz_1&t=0s"
    assert "words" not in first.to_json() and "probability" not in first.to_json()
    assert first.to_text() == "[0.000–2.000] Hello world."
    assert parse_stored(first.to_json(), first) == first


def test_local_whisper_preserves_multiple_segments_and_podcast_citations():
    data = whisper_data()
    data["duration"] = 4.0
    data["segments"].append(dict(start=2.5, end=4.0, text="Next.", words=[]))
    result = normalize_whisper(data, platform="podcast", external_id="episode-123",
                               canonical_url="https://podcasts.example/episode")
    assert [(s.start, s.end, s.text) for s in result.segments] == [(0.0, 2.0, "Hello world."), (2.5, 4.0, "Next.")]
    assert result.segments[1].citation == "https://podcasts.example/episode#t=2.5,4"
    with pytest.raises(module().TranscriptValidationError):
        normalize_whisper(data, max_segments=1)


@pytest.mark.parametrize("serialization", ["indented", "newline", "normalized_text", "integer_time"])
def test_local_whisper_rejects_noncanonical_provider_serialization(serialization):
    api = module()
    data = whisper_data()
    if serialization == "normalized_text":
        data["segments"][0]["text"] = "  Hello world.  "
    elif serialization == "integer_time":
        data["segments"][0]["start"] = 0
    raw = json.dumps(data, indent=2) if serialization == "indented" else api.canonical_json(data)
    if serialization == "newline":
        raw = Path("tests/fixtures/whisper-provider-result.json").read_bytes()
    with pytest.raises(api.TranscriptValidationError, match="^invalid transcript input$"):
        api.normalize_local_whisper(raw, platform="youtube", external_id=METADATA["external_id"],
                                     item_id=7, source_id=3, audio_sha256="a" * 64,
                                     raw_sha256=sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest())


def test_local_whisper_accepts_canonical_utf8_text_and_preserves_exact_artifact_hash():
    import whisper_transcribe as provider
    raw = provider.parse_whisper_result(Path("tests/fixtures/whisper-provider-result.json").read_bytes()).to_json()
    artifact_hash = sha256(raw.encode("utf-8")).hexdigest()
    result = module().normalize_local_whisper(raw, platform="youtube", external_id=METADATA["external_id"],
                                             item_id=7, source_id=3, audio_sha256="a" * 64,
                                             raw_sha256=artifact_hash)
    assert result.source_sha256 == artifact_hash


@pytest.mark.parametrize("updates", [{"provider": "yt-dlp"}, {"provider_version": "1.2.1"}, {"language": "fr"}])
def test_local_whisper_lineage_rejects_inconsistent_canonical_identity(updates):
    with pytest.raises(module().TranscriptValidationError):
        replace(normalize_whisper(), **updates)


@pytest.mark.parametrize("field", ["schema", "provider", "provider_version", "model", "model_repository", "model_revision", "compute_type", "language", "audio_sha256", "job_name"])
def test_local_whisper_rejects_identity_mismatches(field):
    data = whisper_data()
    data[field] = {"audio_sha256": "b" * 64, "job_name": "whisper-" + "b" * 64}.get(field, "unexpected")
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        normalize_whisper(data)


@pytest.mark.parametrize("damage", ["unknown", "segment_unknown", "word_unknown", "inference_unknown", "empty", "null", "text", "regression", "reverse", "nan", "infinity", "bool_time", "string_time", "duration", "private_text", "private_word"])
def test_local_whisper_rejects_malformed_provider_output(damage):
    data = whisper_data()
    if damage == "unknown":
        data["extra"] = "SECRET"
    elif damage == "segment_unknown":
        data["segments"][0]["extra"] = "SECRET"
    elif damage == "word_unknown":
        data["segments"][0]["words"][0]["extra"] = "SECRET"
    elif damage == "inference_unknown":
        data["inference"]["extra"] = "SECRET"
    elif damage in {"empty", "null"}:
        data["segments"] = [] if damage == "empty" else None
    elif damage == "text":
        data["segments"][0]["text"] = "  "
    elif damage == "regression":
        data["segments"].append(dict(start=0.0, end=1.0, text="SECRET", words=[]))
    elif damage in {"reverse", "bool_time", "string_time"}:
        data["segments"][0]["start"] = {"reverse": 3.0, "bool_time": True, "string_time": "0"}[damage]
    elif damage == "duration":
        data["duration"] = 1.0
    elif damage == "private_text":
        data["segments"][0]["text"] = "s3://private/SECRET"
    elif damage == "private_word":
        data["segments"][0]["words"][0]["word"] = "https://example.com/?token=SECRET"
    else:
        data["segments"][0]["end"] = float("nan" if damage == "nan" else "inf")
    raw = json.dumps(data).encode()
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        module().normalize_local_whisper(raw, platform="youtube", external_id=METADATA["external_id"],
                                         item_id=7, source_id=3, audio_sha256="a" * 64,
                                         raw_sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize("updates", [
    {"audio_sha256": "b" * 64}, {"audio_sha256": "bad"},
    {"raw_sha256": "b" * 64}, {"raw_sha256": None},
    {"max_bytes": 5}, {"max_bytes": True}, {"max_text_chars": 2}, {"max_segments": 0},
    {"canonical_url": "https://bucket.s3.amazonaws.com/SECRET"},
])
def test_local_whisper_enforces_hashes_and_shared_limits(updates):
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        normalize_whisper(**updates)


@pytest.mark.parametrize("raw", [b"SECRET not-json", b"\xff", b"null", b"{}", b"", b'{"schema":1,"schema":2}'])
def test_local_whisper_rejects_unparseable_bytes_safely(raw):
    with pytest.raises(module().TranscriptValidationError, match="^invalid transcript input$"):
        module().normalize_local_whisper(raw, platform="youtube", external_id=METADATA["external_id"],
                                         item_id=7, source_id=3, audio_sha256="a" * 64,
                                         raw_sha256=sha256(raw).hexdigest())


def test_local_whisper_normalization_never_imports_inference_runtime():
    code = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"faster_whisper", "ctranslate2"}:
        raise AssertionError("inference runtime imported")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from tests.test_transcripts import normalize_whisper
normalize_whisper()
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_historical_caption_and_amazon_canonical_bytes_remain_unchanged():
    caption = module().normalize_vtt(VTT, origin="creator_caption", **METADATA)
    amazon = module().normalize_amazon_transcribe(provider_data(), canonical_url="https://podcasts.example/episode",
               **{**METADATA, "platform": "podcast", "external_id": "episode-123", "provider_version": "2017-10-26"})
    assert (caption.transcript_sha256, sha256(caption.to_json().encode()).hexdigest()) == (
        "9a398858e6f0b231b8a647cfb1870bcd8a763eebf6b95d3fd11ab203cb2244d3",
        "c839771da8f769cdab9e9e0591372dbce069dec700ecd87d634f73e3e837c12f")
    assert (amazon.transcript_sha256, sha256(amazon.to_json().encode()).hexdigest()) == (
        "387eb7d1709d1ebc32d95c13d3dc4c535f65f485afdf253c68c9fd4630cac3ca",
        "4c8de6f27cad889db890578f68a1eb1bcbd79a07173eca9b1983ec12d0b58e27")
