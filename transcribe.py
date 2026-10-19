"""Deterministic Amazon Transcribe jobs over private S3 artifacts only."""

import json
import math
import re
import time
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionError, HTTPClientError

from config import load_settings
from s3_utils import (BUCKET_NAME, DEFAULT_MAX_BYTES, StoragePermanentError,
                      StorageTransientError, get_s3_manager, setting,
                      validate_artifact_key, validate_segment)
from transcripts import TranscriptValidationError, normalize_amazon_transcribe


class TranscriptionError(ValueError):
    """Safe failure category without AWS messages or transient URLs."""


class TranscriptionTransientError(TranscriptionError):
    """Retryable transport or provider failure."""


class TranscriptionPermanentError(TranscriptionError):
    """Invalid input, mismatched job identity or permanent provider failure."""


class TranscriptionTimeoutError(TranscriptionTransientError):
    """The bounded polling window elapsed; the job remains resumable."""


def deterministic_job_name(item_id, audio_sha256):
    # Reject unsafe/oversized IDs instead of sanitizing or truncating identities.
    if type(item_id) is int:
        if item_id <= 0:
            raise TranscriptionPermanentError("invalid transcription item identity")
        item_id = str(item_id)
    if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,155}", item_id):
        raise TranscriptionPermanentError("invalid transcription item identity")
    if not isinstance(audio_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", audio_sha256):
        raise TranscriptionPermanentError("invalid audio hash")
    return f"media-{item_id}-{audio_sha256[:32]}-v1"


def _failure(error):
    code = error.response.get("Error", {}).get("Code", "") if isinstance(error, ClientError) else ""
    if code in {"ThrottlingException", "LimitExceededException", "InternalFailureException", "ServiceUnavailableException", "TooManyRequestsException", "RequestTimeout"} or isinstance(error, (ConnectionError, HTTPClientError)):
        raise TranscriptionTransientError("transcription temporarily unavailable") from None
    raise TranscriptionPermanentError("transcription provider operation failed") from None


def get_transcribe_client(settings=None):
    settings = settings if settings is not None else load_settings()
    try:
        return boto3.client("transcribe", region_name=setting(settings, "AWS_REGION"), config=Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=60))
    except Exception as error:
        _failure(error)


def _get_job(client, job_name, *, allow_missing=False):
    try:
        job = client.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
        if not isinstance(job, dict):
            raise TranscriptionPermanentError("invalid transcription job response")
        return job
    except TranscriptionError:
        raise
    except ClientError as error:
        details = error.response.get("Error", {})
        # AWS reports a missing job as BadRequestException, not a dedicated 404.
        message = details.get("Message", "").casefold()
        missing = details.get("Code") == "NotFoundException" or details.get("Code") == "BadRequestException" and ("not found" in message or "couldn't be found" in message)
        if allow_missing and missing:
            return None
        _failure(error)
    except Exception as error:
        _failure(error)


def _validate_result_uri(uri, bucket, key, region):
    """Validate an AWS output locator without retaining or using its capability.

    GetTranscriptionJob currently exposes the completed output only through
    TranscriptFileUri. Accept exact private S3 identity in canonical S3 or HTTPS
    endpoint forms. Reject signed/query-bearing, foreign, ambiguous and insecure
    locators; the caller still reads only the predicted key through S3Manager.
    """
    try:
        if not isinstance(uri, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri) or "?" in uri or "#" in uri:
            raise ValueError
        parts = urlsplit(uri)
        suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
        expected = {
            ("s3", bucket, f"/{key}"),
            ("https", f"{bucket}.s3.{region}.{suffix}", f"/{key}"),
            ("https", f"s3.{region}.{suffix}", f"/{bucket}/{key}"),
        }
        if region == "us-east-1":
            expected.update({
                ("https", f"{bucket}.s3.amazonaws.com", f"/{key}"),
                ("https", "s3.amazonaws.com", f"/{bucket}/{key}"),
            })
        if (parts.scheme, parts.netloc, parts.path) not in expected:
            raise ValueError
    except (TypeError, ValueError):
        raise TranscriptionPermanentError("transcription output identity mismatch") from None


def _validate_job(job, name, media_uri, audio_sha256, language, bucket, output_key, region):
    try:
        tags = {tag["Key"]: tag["Value"] for tag in job.get("Tags", [])}
        if job["TranscriptionJobName"] != name or job["Media"]["MediaFileUri"] != media_uri or tags.get("audio-sha256") != audio_sha256:
            raise TranscriptionPermanentError("transcription job identity mismatch")
        if job.get("LanguageCode", language) != language:
            raise TranscriptionPermanentError("transcription job language mismatch")
        for field, expected in (("OutputBucketName", bucket), ("OutputKey", output_key)):
            if field in job and job[field] != expected:
                raise TranscriptionPermanentError("transcription output identity mismatch")
        if job.get("TranscriptionJobStatus") == "COMPLETED":
            _validate_result_uri(job["Transcript"]["TranscriptFileUri"], bucket, output_key, region)
    except (KeyError, TypeError):
        raise TranscriptionPermanentError("transcription job identity unavailable") from None


def transcribe_audio_from_s3(audio_s3_key, *, item_id, source_id, platform,
                             external_id, audio_sha256, settings=None,
                             s3_manager=None, client=None, clock=time.monotonic,
                             sleeper=time.sleep, canonical_url=None,
                             max_result_bytes=DEFAULT_MAX_BYTES):
    """Start or resume a private job; return only persistent artifact metadata.

    A 128-bit job-name hash prefix is additionally verified against the full
    audio hash tag on every provider response. Existing jobs must match exact
    input identity and language. Completed jobs remain available for retries.
    Completed output locators are validated for exact identity and never fetched.
    """
    settings = settings if settings is not None else load_settings()
    job_name = deterministic_job_name(item_id, audio_sha256)
    validate_segment(external_id)
    validate_artifact_key(audio_s3_key)
    if audio_s3_key.split("/")[1:4] != [platform, external_id, "audio"]:
        raise TranscriptionPermanentError("audio artifact identity mismatch")
    if type(max_result_bytes) is not int or not 0 < max_result_bytes <= DEFAULT_MAX_BYTES:
        raise TranscriptionPermanentError("invalid provider result byte limit")
    poll, timeout = setting(settings, "TRANSCRIBE_POLL_SECONDS"), setting(settings, "TRANSCRIBE_TIMEOUT_SECONDS")
    if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0 for value in (poll, timeout)):
        raise TranscriptionPermanentError("invalid transcription polling settings")
    language = setting(settings, "TRANSCRIBE_LANGUAGE_CODE")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}-[A-Za-z]{2,4}(?:-[A-Za-z]{2})?", language):
        raise TranscriptionPermanentError("invalid transcription language")
    storage = s3_manager if s3_manager is not None else get_s3_manager(settings)
    if storage.bucket_name != BUCKET_NAME or storage.bucket_name != setting(settings, "AWS_S3_BUCKET_NAME") or storage.region != setting(settings, "AWS_REGION"):
        raise TranscriptionPermanentError("transcription storage configuration mismatch")
    client = client if client is not None else get_transcribe_client(settings)
    client_region = client.meta.region_name
    if isinstance(client_region, str) and client_region != storage.region:
        raise TranscriptionPermanentError("transcription client region mismatch")
    media_uri = f"s3://{storage.bucket_name}/{audio_s3_key}"
    provider_key = storage.generate_s3_key(platform, external_id, "provider", f"{job_name}.json")
    media_format = audio_s3_key.rsplit(".", 1)[-1]
    if media_format not in {"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"}:
        raise TranscriptionPermanentError("unsupported audio format")
    deadline = clock() + timeout
    job = _get_job(client, job_name, allow_missing=True)
    if job is None:
        try:
            client.start_transcription_job(TranscriptionJobName=job_name, LanguageCode=language,
                MediaFormat=media_format, Media={"MediaFileUri": media_uri},
                OutputBucketName=storage.bucket_name, OutputKey=provider_key,
                Tags=[{"Key": "audio-sha256", "Value": audio_sha256}])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConflictException":
                _failure(error)
        except Exception as error:
            _failure(error)
        job = _get_job(client, job_name)
    while True:
        _validate_job(job, job_name, media_uri, audio_sha256, language,
                      storage.bucket_name, provider_key, storage.region)
        status = job.get("TranscriptionJobStatus")
        if status == "COMPLETED":
            break
        if status == "FAILED":
            raise TranscriptionPermanentError("transcription job failed")
        if status not in {"QUEUED", "IN_PROGRESS"}:
            raise TranscriptionPermanentError("invalid transcription job status")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TranscriptionTimeoutError("transcription polling timed out")
        sleeper(min(poll, remaining))
        if clock() >= deadline:
            raise TranscriptionTimeoutError("transcription polling timed out")
        job = _get_job(client, job_name)
    try:
        data = storage.get_bytes(provider_key, max_bytes=max_result_bytes)
        try:
            data = json.loads(data)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise TranscriptionPermanentError("invalid transcription artifact") from None
        if not isinstance(data, dict) or data.get("jobName") != job_name:
            raise TranscriptionPermanentError("transcription result job identity mismatch")
        transcript = normalize_amazon_transcribe(data, platform=platform, external_id=external_id,
            item_id=item_id, source_id=source_id, language=language, canonical_url=canonical_url)
        digest = transcript.transcript_sha256
        transcript_key = storage.generate_s3_key(platform, external_id, "transcripts", f"{digest}.txt")
        segments_key = storage.generate_s3_key(platform, external_id, "transcripts", f"{digest}.json")
        storage.upload_text_content(transcript.to_text(), transcript_key, "text/plain")
        storage.upload_text_content(transcript.to_json(), segments_key, "application/json")
    except StorageTransientError:
        raise TranscriptionTransientError("transcription artifact temporarily unavailable") from None
    except (StoragePermanentError, TranscriptValidationError):
        raise TranscriptionPermanentError("invalid transcription artifact") from None
    return dict(transcript_key=transcript_key, segments_key=segments_key,
        provider_result_key=provider_key, audio_sha256=audio_sha256,
        source_sha256=transcript.source_sha256, transcript_sha256=digest,
        language=transcript.language, duration=transcript.duration,
        provider=transcript.provider, job_name=job_name)
