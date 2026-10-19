"""All AWS traffic is injected and provider URLs are never fetched."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest

import s3_utils
import transcribe


BUCKET = "podcast-transcriptions-web-2025"
AUDIO = "items/podcast/episode-123/audio/media.mp3"
DIGEST = sha256(b"audio").hexdigest()
JOB = f"media-7-{DIGEST[:32]}-v1"
OUTPUT = f"items/podcast/episode-123/provider/{JOB}.json"


def setup():
    settings = SimpleNamespace(AWS_S3_BUCKET_NAME=BUCKET, AWS_REGION="us-east-1", TRANSCRIBE_POLL_SECONDS=2, TRANSCRIBE_TIMEOUT_SECONDS=5, TRANSCRIBE_LANGUAGE_CODE="en-US")
    aws = Mock()
    aws.get_bucket_location.return_value = {"LocationConstraint": None}
    aws.put_object.return_value = {}
    payload = Path("tests/fixtures/transcribe-result.json").read_bytes()
    aws.get_object.side_effect = lambda **_: {"ContentLength": len(payload), "Body": BytesIO(payload)}
    storage = s3_utils.S3Manager(settings, client=aws)
    client = Mock()
    complete = {"TranscriptionJob": {"TranscriptionJobName": JOB, "TranscriptionJobStatus": "COMPLETED", "LanguageCode": "en-US", "Media": {"MediaFileUri": f"s3://{BUCKET}/{AUDIO}"}, "Tags": [{"Key": "audio-sha256", "Value": DIGEST}], "Transcript": {"TranscriptFileUri": f"https://s3.us-east-1.amazonaws.com/{BUCKET}/{OUTPUT}"}}}
    client.get_transcription_job.return_value = complete
    clock = Mock(side_effect=[0, 0, 2, 4, 5, 5])
    kwargs = dict(item_id=7, source_id=3, platform="podcast", external_id="episode-123", audio_sha256=DIGEST, settings=settings, s3_manager=storage, client=client, clock=clock, sleeper=Mock(), canonical_url="https://podcasts.example/episode")
    return kwargs, client, aws, complete


def test_job_name_is_deterministic_aws_safe_and_rejects_lossy_id_sanitization():
    function = getattr(transcribe, "deterministic_job_name", None)
    assert callable(function), "deterministic job naming is missing"
    assert function(7, DIGEST) == JOB
    assert function(7, DIGEST) != function(8, DIGEST)
    assert function(7, DIGEST) != function(7, "f" * 64)
    for item, digest in (("bad/id", DIGEST), ("bad id", DIGEST), ("a" * 200, DIGEST), (7, "abc"), (True, DIGEST)):
        with pytest.raises(ValueError):
            function(item, digest)


def test_private_transcribe_request_and_bounded_provider_read(monkeypatch):
    kwargs, client, aws, complete = setup()
    client.get_transcription_job.side_effect = [ClientError({"Error": {"Code": "BadRequestException", "Message": "not found"}}, "GetTranscriptionJob"), complete]
    monkeypatch.setattr("requests.get", Mock(side_effect=AssertionError("HTTP provider URL fetch forbidden")))
    result = transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    request = client.start_transcription_job.call_args.kwargs
    assert request["TranscriptionJobName"] == JOB
    assert request["Media"] == {"MediaFileUri": f"s3://{BUCKET}/{AUDIO}"}
    assert request["OutputBucketName"] == BUCKET and request["OutputKey"] == OUTPUT
    assert request["LanguageCode"] == "en-US"
    assert request["Tags"] == [{"Key": "audio-sha256", "Value": DIGEST}]
    aws.get_object.assert_called_once_with(Bucket=BUCKET, Key=OUTPUT)
    assert result["provider_result_key"] == OUTPUT
    assert result["job_name"] == JOB and result["duration"] == 33.5
    assert result["language"] == "en-US" and result["provider"] == "amazon_transcribe"
    assert result["audio_sha256"] == DIGEST
    assert set(result) == {"transcript_key", "segments_key", "provider_result_key", "audio_sha256", "source_sha256", "transcript_sha256", "language", "duration", "provider", "job_name"}
    assert "http" not in json.dumps(result) and "DO-NOT-FETCH" not in json.dumps(result)
    assert all(call.kwargs["ServerSideEncryption"] == "AES256" for call in aws.put_object.call_args_list)
    client.delete_transcription_job.assert_not_called()


def test_completed_job_is_reused_without_starting_or_deleting():
    kwargs, client, _, _ = setup()
    first = transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    second = transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    assert first == second
    client.start_transcription_job.assert_not_called()
    client.delete_transcription_job.assert_not_called()


def test_conflict_resumes_existing_job():
    kwargs, client, _, complete = setup()
    client.get_transcription_job.side_effect = [ClientError({"Error": {"Code": "NotFoundException"}}, "GetTranscriptionJob"), complete]
    client.start_transcription_job.side_effect = ClientError({"Error": {"Code": "ConflictException", "Message": "SECRET"}}, "StartTranscriptionJob")
    assert transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)["job_name"] == JOB


def test_conflicting_media_identity_fails_closed():
    kwargs, client, _, complete = setup()
    complete["TranscriptionJob"]["Media"]["MediaFileUri"] = "s3://other-bucket/other-key"
    with pytest.raises(transcribe.TranscriptionPermanentError):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)


def test_timeout_uses_injected_clock_sleeper_and_settings():
    kwargs, client, _, _ = setup()
    client.get_transcription_job.return_value = {"TranscriptionJob": {"TranscriptionJobName": JOB, "TranscriptionJobStatus": "IN_PROGRESS", "Media": {"MediaFileUri": f"s3://{BUCKET}/{AUDIO}"}, "Tags": [{"Key": "audio-sha256", "Value": DIGEST}]}}
    with pytest.raises(transcribe.TranscriptionTimeoutError):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    assert kwargs["sleeper"].call_args_list
    assert all(0 < call.args[0] <= 2 for call in kwargs["sleeper"].call_args_list)


def test_failed_job_and_provider_errors_are_typed_and_redacted():
    kwargs, client, _, complete = setup()
    complete["TranscriptionJob"].update(TranscriptionJobStatus="FAILED", FailureReason="SECRET https://signed.invalid?token=x")
    with pytest.raises(transcribe.TranscriptionPermanentError) as caught:
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    assert "SECRET" not in str(caught.value) and caught.value.__cause__ is None
    for code, expected in (("ThrottlingException", transcribe.TranscriptionTransientError), ("AccessDeniedException", transcribe.TranscriptionPermanentError)):
        client.get_transcription_job.side_effect = ClientError({"Error": {"Code": code, "Message": "SECRET"}}, "GetTranscriptionJob")
        with pytest.raises(expected) as caught:
            transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
        assert "SECRET" not in str(caught.value)


def test_audio_key_must_match_declared_item_identity():
    kwargs, client, _, _ = setup()
    with pytest.raises(ValueError):
        transcribe.transcribe_audio_from_s3("items/podcast/another/audio/media.mp3", **kwargs)
    client.get_transcription_job.assert_not_called()


def test_hash_prefix_collision_cannot_resume_different_full_audio_hash():
    kwargs, client, _, complete = setup()
    complete["TranscriptionJob"]["Tags"][0]["Value"] = DIGEST[:32] + "0" * 32
    with pytest.raises(transcribe.TranscriptionPermanentError, match="identity"):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)


def test_credentials_and_bounded_storage_failures_remain_typed():
    from botocore.exceptions import NoCredentialsError

    kwargs, client, aws, _ = setup()
    client.get_transcription_job.side_effect = NoCredentialsError()
    with pytest.raises(transcribe.TranscriptionPermanentError):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    client.get_transcription_job.side_effect = None
    aws.get_object.return_value = {"ContentLength": 50_000, "Body": BytesIO(b"x")}
    aws.get_object.side_effect = None
    with pytest.raises(transcribe.TranscriptionPermanentError):
        transcribe.transcribe_audio_from_s3(AUDIO, max_result_bytes=10, **kwargs)


def test_transcribe_rejects_client_region_mismatch_before_provider_calls():
    kwargs, client, _, _ = setup()
    client.meta.region_name = "us-west-2"
    with pytest.raises(transcribe.TranscriptionPermanentError, match="region"):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    client.get_transcription_job.assert_not_called()


def test_malformed_job_payload_is_typed_without_provider_data():
    kwargs, client, _, _ = setup()
    for malformed in ("SECRET", None, [], {"Tags": "SECRET"}):
        client.get_transcription_job.return_value = {"TranscriptionJob": malformed}
        with pytest.raises(transcribe.TranscriptionPermanentError) as caught:
            transcribe.transcribe_audio_from_s3(AUDIO, **{**kwargs, "clock": lambda: 0})
        assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("uri", [
    f"s3://{BUCKET}/{OUTPUT}",
    f"https://s3.us-east-1.amazonaws.com/{BUCKET}/{OUTPUT}",
    f"https://{BUCKET}.s3.us-east-1.amazonaws.com/{OUTPUT}",
    f"https://s3.amazonaws.com/{BUCKET}/{OUTPUT}",
    f"https://{BUCKET}.s3.amazonaws.com/{OUTPUT}",
])
def test_completed_job_exact_output_binding_reads_only_private_key(uri, monkeypatch):
    kwargs, _, aws, complete = setup()
    complete["TranscriptionJob"]["Transcript"]["TranscriptFileUri"] = uri
    monkeypatch.setattr("requests.get", Mock(side_effect=AssertionError("URI must never be fetched")))
    result = transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    assert result["provider_result_key"] == OUTPUT
    assert uri not in json.dumps(result)
    aws.get_object.assert_called_once_with(Bucket=BUCKET, Key=OUTPUT)


@pytest.mark.parametrize("uri", [
    f"s3://other/{OUTPUT}", f"s3://{BUCKET}/other.json",
    f"https://{BUCKET}.s3.us-east-1.amazonaws.com/other.json",
    f"https://other.s3.us-east-1.amazonaws.com/{OUTPUT}",
    f"https://{BUCKET}.s3.us-west-2.amazonaws.com/{OUTPUT}",
    f"https://s3.us-east-1.amazonaws.com/other/{OUTPUT}",
    f"https://s3.us-west-2.amazonaws.com/{BUCKET}/{OUTPUT}",
    f"https://foreign.invalid/{BUCKET}/{OUTPUT}",
    f"http://s3.us-east-1.amazonaws.com/{BUCKET}/{OUTPUT}",
    f"https://{BUCKET}.s3.us-east-1.amazonaws.com/{OUTPUT}?X-Amz-Signature=SECRET",
    f"s3://{BUCKET}/{OUTPUT}?token=SECRET", None, "SECRET malformed",
    f"https://user:SECRET@{BUCKET}.s3.us-east-1.amazonaws.com/{OUTPUT}",
    f"https://{BUCKET}.s3.us-east-1.amazonaws.com/{OUTPUT}#SECRET",
])
def test_completed_job_rejects_unbound_or_presigned_output_before_read(uri):
    kwargs, _, aws, complete = setup()
    complete["TranscriptionJob"]["Transcript"]["TranscriptFileUri"] = uri
    with pytest.raises(transcribe.TranscriptionPermanentError) as caught:
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    assert "SECRET" not in str(caught.value) and caught.value.__cause__ is None
    aws.get_object.assert_not_called()


@pytest.mark.parametrize("fields", [{"OutputBucketName": "other"}, {"OutputKey": "other.json"}, {"OutputBucketName": BUCKET, "OutputKey": "other.json"}])
def test_authoritative_output_fields_cannot_disagree_with_expected_key(fields):
    kwargs, _, aws, complete = setup()
    complete["TranscriptionJob"].update(fields)
    with pytest.raises(transcribe.TranscriptionPermanentError):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    aws.get_object.assert_not_called()


@pytest.mark.parametrize("job_name", ["another-job", "media-7-stale-v1", None])
def test_plausible_provider_json_must_name_the_expected_job(job_name):
    kwargs, _, aws, _ = setup()
    data = json.loads(Path("tests/fixtures/transcribe-result.json").read_text())
    if job_name is None:
        data.pop("jobName")
    else:
        data["jobName"] = job_name
    payload = json.dumps(data).encode()
    aws.get_object.side_effect = lambda **_: {"ContentLength": len(payload), "Body": BytesIO(payload)}
    with pytest.raises(transcribe.TranscriptionPermanentError, match="identity"):
        transcribe.transcribe_audio_from_s3(AUDIO, **kwargs)
    aws.put_object.assert_not_called()
