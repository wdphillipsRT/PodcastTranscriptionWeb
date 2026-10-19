"""The artifact boundary never exposes URLs or follows bucket redirects."""

from io import BytesIO
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import s3_utils


BUCKET = "podcast-transcriptions-web-2025"
KEY = "items/youtube/abc123xyz_1/audio/media.mp3"


def manager(region="us-east-1", location=None):
    client = Mock()
    client.get_bucket_location.return_value = {"LocationConstraint": location}
    settings = SimpleNamespace(AWS_REGION=region, AWS_S3_BUCKET_NAME=BUCKET)
    return s3_utils.S3Manager(settings, client=client), client


def test_startup_probes_exact_configured_bucket_and_null_east_region():
    storage, client = manager()
    client.get_bucket_location.assert_called_once_with(Bucket=BUCKET)
    assert storage.bucket_name == BUCKET
    with pytest.raises(ValueError, match="region"):
        manager("us-west-2", None)
    with pytest.raises(ValueError, match="region"):
        manager("eu-west-1", "EU")


def test_startup_fails_closed_without_disclosing_provider_error():
    client = Mock()
    client.get_bucket_location.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "SECRET https://signed.invalid?token=x"}}, "GetBucketLocation")
    with pytest.raises(ValueError) as caught:
        s3_utils.S3Manager(SimpleNamespace(AWS_REGION="us-east-1", AWS_S3_BUCKET_NAME=BUCKET), client=client)
    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_configured_bucket_cannot_be_overridden():
    with pytest.raises(ValueError, match="bucket"):
        s3_utils.S3Manager(SimpleNamespace(AWS_REGION="us-east-1", AWS_S3_BUCKET_NAME="other"), client=Mock())


def test_private_encrypted_puts_return_only_keys(tmp_path):
    storage, client = manager()
    client.put_object.return_value = {}
    assert storage.generate_s3_key("youtube", "abc123xyz_1", "audio", "media.mp3") == KEY
    assert storage.upload_binary_content(b"audio", KEY, "audio/mpeg") == KEY
    client.put_object.assert_called_with(Bucket=BUCKET, Key=KEY, Body=b"audio", ContentType="audio/mpeg", ServerSideEncryption="AES256")
    text_key = KEY.replace("audio/media.mp3", "transcripts/transcript.txt")
    assert storage.upload_text_content("hello", text_key, "text/plain") == text_key
    assert client.put_object.call_args.kwargs["ServerSideEncryption"] == "AES256"
    path = tmp_path / "media.mp3"
    path.write_bytes(b"audio")
    client.upload_file.return_value = None
    assert storage.upload_file(path, KEY, "audio/mpeg") == KEY
    assert client.upload_file.call_args.kwargs["ExtraArgs"] == {"ContentType": "audio/mpeg", "ServerSideEncryption": "AES256"}


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a\\b", "a\n", "a\x7f", "%2e%2e", "a?token=b", "https://x"])
def test_artifact_segments_reject_ambiguous_or_unsafe_components(value):
    storage, _ = manager()
    with pytest.raises(ValueError):
        storage.generate_s3_key("podcast", value, "audio", "audio.mp3")


@pytest.mark.parametrize("key", ["https://bucket.s3.amazonaws.com/a?X-Amz-Signature=x", "s3://bucket/a", "items/podcast/id/audio/../x", "items//id/audio/a", "other/podcast/id/audio/a", "items/podcast/id/audio/a/b"])
def test_all_object_operations_reject_urls_and_non_artifact_keys(key):
    storage, client = manager()
    for call in (lambda: storage.get_bytes(key), lambda: storage.upload_text_content("x", key), lambda: storage.file_exists(key), lambda: storage.delete_file(key)):
        with pytest.raises(ValueError):
            call()
    client.get_object.assert_not_called()


def test_bounded_streaming_read_validates_length_and_closes():
    storage, client = manager()
    body = BytesIO(b"hello")
    client.get_object.return_value = {"Body": body, "ContentLength": 5}
    assert storage.get_bytes(KEY, max_bytes=5) == b"hello"
    assert body.closed
    client.get_object.assert_called_once_with(Bucket=BUCKET, Key=KEY)


@pytest.mark.parametrize("declared,actual,maximum", [(6, b"hello!", 5), (4, b"hello", 5), (5, b"hey", 5), (None, b"hello", 5), (-1, b"", 5), (True, b"x", 5)])
def test_reads_reject_oversize_or_inconsistent_lengths_and_always_close(declared, actual, maximum):
    storage, client = manager()
    body = BytesIO(actual)
    client.get_object.return_value = {"Body": body, "ContentLength": declared}
    with pytest.raises(ValueError):
        storage.get_bytes(KEY, max_bytes=maximum)
    assert body.closed


def test_read_rejects_redirect_and_closes_body():
    storage, client = manager()
    body = BytesIO(b"hello")
    client.get_object.return_value = {"Body": body, "ContentLength": 5, "ResponseMetadata": {"HTTPStatusCode": 301, "HTTPHeaders": {"location": "https://signed.invalid?token=secret"}}}
    with pytest.raises(ValueError, match="redirect"):
        storage.get_bytes(KEY)
    assert body.closed


def test_reads_and_writes_redact_provider_exceptions():
    storage, client = manager()
    error = ClientError({"Error": {"Code": "SlowDown", "Message": "SECRET https://signed.invalid"}}, "GetObject")
    client.get_object.side_effect = error
    client.put_object.side_effect = error
    for call in (lambda: storage.get_bytes(KEY), lambda: storage.upload_text_content("text", KEY)):
        with pytest.raises(s3_utils.StorageTransientError) as caught:
            call()
        assert "SECRET" not in str(caught.value)
        assert caught.value.__cause__ is None


def test_region_retry_hook_blocks_botocore_redirect_before_another_request():
    storage, client = manager()
    client.meta.events.register_first.assert_called_once()
    event, handler = client.meta.events.register_first.call_args.args
    assert event == "needs-retry.s3"
    for status, headers in ((301, {}), (200, {"x-amz-bucket-region": "us-west-2"}), (302, {"Location": "https://secret.invalid"})):
        response = SimpleNamespace(status_code=status, headers=headers)
        with pytest.raises(s3_utils.StoragePermanentError, match="redirect"):
            handler(response=(response, {}))


def test_bucket_and_region_properties_are_not_overridable():
    storage, _ = manager()
    with pytest.raises(AttributeError):
        storage.bucket_name = "other"
    with pytest.raises(AttributeError):
        storage.region = "us-west-2"


def test_missing_bucket_location_and_wrong_client_region_fail_closed():
    client = Mock()
    client.get_bucket_location.return_value = {}
    settings = SimpleNamespace(AWS_REGION="us-east-1", AWS_S3_BUCKET_NAME=BUCKET)
    with pytest.raises(ValueError, match="region"):
        s3_utils.S3Manager(settings, client=client)
    client.meta.region_name = "us-west-2"
    client.get_bucket_location.return_value = {"LocationConstraint": None}
    with pytest.raises(ValueError, match="region"):
        s3_utils.S3Manager(settings, client=client)


def test_credential_errors_are_permanent_and_stream_errors_close_body():
    from botocore.exceptions import NoCredentialsError

    storage, client = manager()
    client.put_object.side_effect = NoCredentialsError()
    with pytest.raises(s3_utils.StoragePermanentError):
        storage.upload_binary_content(b"audio", KEY, "audio/mpeg")
    body = Mock()
    body.read.side_effect = OSError("SECRET")
    client.get_object.return_value = {"Body": body, "ContentLength": 10}
    with pytest.raises(s3_utils.StorageError) as caught:
        storage.get_bytes(KEY)
    assert "SECRET" not in str(caught.value)
    body.close.assert_called_once()


@pytest.mark.parametrize("status,headers", [(301, {}), (200, {"x-amz-bucket-region": "us-west-2"}), (200, {"location": "https://SECRET.invalid?token=SECRET"})])
def test_rejected_retry_hook_closes_parsed_body_raw_and_http_response(status, headers):
    storage, _ = manager()
    body, raw, http_response = Mock(), Mock(), Mock()
    http_response.status_code, http_response.headers, http_response.raw = status, headers, raw
    with pytest.raises(s3_utils.StoragePermanentError) as caught:
        storage._reject_redirect(response=(http_response, {"Body": body}))
    assert "SECRET" not in str(caught.value) and caught.value.__cause__ is None
    body.close.assert_called_once()
    raw.close.assert_called_once()
    http_response.close.assert_called_once()


def test_retry_hook_close_errors_do_not_escape_redaction():
    storage, _ = manager()
    raw = Mock()
    raw.close.side_effect = OSError("SECRET")
    body = Mock()
    response = SimpleNamespace(status_code=301, headers={}, raw=raw)
    with pytest.raises(s3_utils.StoragePermanentError) as caught:
        storage._reject_redirect(response=(response, {"Body": body}))
    assert "SECRET" not in str(caught.value)
    body.close.assert_called_once()
    raw.close.assert_called_once()


def test_streaming_object_hash_uses_bounded_chunks_without_buffering_audio():
    storage, client = manager()
    function = getattr(storage, "verify_sha256", None)
    assert callable(function), "bounded object SHA-256 verifier missing"
    size = 180_000
    class Stream:
        remaining = size
        closed = False
        def read(self, count):
            assert type(count) is int and 0 < count <= 65536
            produced = min(count, self.remaining)
            self.remaining -= produced
            return b"a" * produced
        def close(self):
            self.closed = True
    body = Stream()
    client.get_object.return_value = {"Body": body, "ContentLength": size}
    digest = sha256(b"a" * size).hexdigest()
    assert function(KEY, expected_sha256=digest, max_bytes=size) == digest
    assert body.closed and body.remaining == 0
    client.get_object.assert_called_once_with(Bucket=BUCKET, Key=KEY)


@pytest.mark.parametrize("declared,actual,maximum", [(6, b"abcdef", 5), (4, b"abcde", 5),
    (5, b"abc", 5), (None, b"abc", 5), (-1, b"", 5), (True, b"x", 5)])
def test_object_hash_rejects_length_corruption_and_closes(declared, actual, maximum):
    storage, client = manager()
    function = getattr(storage, "verify_sha256", None)
    assert callable(function), "bounded object SHA-256 verifier missing"
    body = BytesIO(actual)
    client.get_object.return_value = {"Body": body, "ContentLength": declared}
    with pytest.raises(s3_utils.StoragePermanentError):
        function(KEY, max_bytes=maximum)
    assert body.closed


def test_object_hash_mismatch_is_repairable_and_redacted():
    storage, client = manager()
    function = getattr(storage, "verify_sha256", None)
    assert callable(function), "bounded object SHA-256 verifier missing"
    body = BytesIO(b"wrong audio")
    client.get_object.return_value = {"Body": body, "ContentLength": 11}
    with pytest.raises(s3_utils.StorageIntegrityError) as caught:
        function(KEY, expected_sha256="f" * 64, max_bytes=100)
    assert body.closed and KEY not in str(caught.value) and "wrong audio" not in str(caught.value)


def test_object_hash_reuses_region_checks_and_closes_rejected_body():
    storage, client = manager()
    function = getattr(storage, "verify_sha256", None)
    assert callable(function), "bounded object SHA-256 verifier missing"
    body = BytesIO(b"audio")
    client.get_object.return_value = {"Body": body, "ContentLength": 5,
        "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"x-amz-bucket-region": "us-west-2"}}}
    with pytest.raises(s3_utils.StoragePermanentError, match="redirect"):
        function(KEY, max_bytes=10)
    assert body.closed


@pytest.mark.parametrize("operation", ["get_bytes", "verify_sha256"])
def test_missing_objects_are_distinct_from_denied_objects(operation):
    storage, client = manager()
    function = getattr(storage, operation, None)
    assert callable(function), "bounded object SHA-256 verifier missing"
    for code in ("NoSuchKey", "404", "NotFound"):
        client.get_object.side_effect = ClientError({"Error": {"Code": code, "Message": "SECRET"}}, "GetObject")
        with pytest.raises(s3_utils.StorageIntegrityError) as caught:
            function(KEY, max_bytes=10)
        assert "SECRET" not in str(caught.value)
    client.get_object.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "SECRET"}}, "GetObject")
    with pytest.raises(s3_utils.StoragePermanentError) as caught:
        function(KEY, max_bytes=10)
    assert not isinstance(caught.value, s3_utils.StorageIntegrityError)


def test_object_hash_stream_failure_closes_and_preserves_transient_category():
    from botocore.exceptions import ResponseStreamingError
    storage, client = manager()
    function = getattr(storage, "verify_sha256", None)
    assert callable(function), "bounded object SHA-256 verifier missing"
    body = Mock()
    body.read.side_effect = ResponseStreamingError(error="SECRET")
    client.get_object.return_value = {"Body": body, "ContentLength": 10}
    with pytest.raises(s3_utils.StorageTransientError) as caught:
        function(KEY, max_bytes=10)
    assert "SECRET" not in str(caught.value)
    body.close.assert_called_once()


@pytest.mark.parametrize("kind", ["classification", "policy", "learning"])
def test_content_addressed_creation_is_private_and_conditional(kind):
    storage, client = manager()
    put = getattr(storage, "put_content_addressed", None)
    assert callable(put), "conditional immutable artifact writer missing"
    content = b'{"decisions":[]}'
    key = storage.generate_s3_key("podcast", "episode", kind, sha256(content).hexdigest() + ".json")
    client.put_object.return_value = {}
    assert put(content, key) == key
    client.put_object.assert_called_once_with(Bucket=BUCKET, Key=key, Body=content,
        ContentType="application/json", ServerSideEncryption="AES256", IfNoneMatch="*")


def test_content_addressed_semantic_digest_is_explicit_and_validated():
    storage, client = manager()
    put = getattr(storage, "put_content_addressed", None)
    assert callable(put), "conditional immutable artifact writer missing"
    key = "items/podcast/episode/policy/" + "a" * 64 + ".json"
    client.put_object.return_value = {}
    assert put(b"{}", key, address_sha256="a" * 64) == key
    for digest in ("A" * 64, "../bad", True, "b" * 64):
        with pytest.raises(s3_utils.StoragePermanentError):
            put(b"{}", key, address_sha256=digest)
    with pytest.raises(s3_utils.StoragePermanentError):
        put(b"{}", key)
    assert client.put_object.call_count == 1


@pytest.mark.parametrize("code", ["PreconditionFailed", "412", "ConditionalRequestConflict"])
def test_content_addressed_conflict_reuses_only_identical_bounded_bytes(code):
    storage, client = manager()
    put = getattr(storage, "put_content_addressed", None)
    assert callable(put), "conditional immutable artifact writer missing"
    content = b'{"a":1}'
    key = "items/podcast/episode/classification/" + sha256(content).hexdigest() + ".json"
    client.put_object.side_effect = ClientError({"Error": {"Code": code}}, "PutObject")
    for existing in (content, b"short", content + b"!", b'{"a":2}'):
        body = BytesIO(existing)
        client.get_object.return_value = {"Body": body, "ContentLength": len(existing)}
        if existing == content:
            assert put(content, key) == key
        else:
            with pytest.raises(s3_utils.StorageIntegrityError):
                put(content, key)
        assert body.closed


def test_content_addressed_missing_conflict_and_transport_failures_are_safe():
    storage, client = manager()
    put = getattr(storage, "put_content_addressed", None)
    assert callable(put), "conditional immutable artifact writer missing"
    content = b"{}"
    key = "items/podcast/episode/classification/" + sha256(content).hexdigest() + ".json"
    client.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "SECRET"}}, "GetObject")
    with pytest.raises(s3_utils.StorageIntegrityError):
        put(content, key)
    for code, error in (("SlowDown", s3_utils.StorageTransientError), ("AccessDenied", s3_utils.StoragePermanentError)):
        client.put_object.side_effect = ClientError({"Error": {"Code": code, "Message": "SECRET"}}, "PutObject")
        with pytest.raises(error) as caught:
            put(content, key)
        assert "SECRET" not in str(caught.value)


def test_concurrent_immutable_writers_create_once_and_reuse_identical_bytes():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    storage, client = manager()
    content = b'{"decisions":[]}'
    key = "items/podcast/episode/classification/" + sha256(content).hexdigest() + ".json"
    objects, writes, guard, started = {}, [], Lock(), Barrier(2)
    def put(**kwargs):
        assert kwargs["IfNoneMatch"] == "*"
        started.wait(timeout=5)
        with guard:
            if kwargs["Key"] in objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            objects[kwargs["Key"]] = kwargs["Body"]
            writes.append(kwargs["Body"])
        return {}
    def get(**kwargs):
        value = objects[kwargs["Key"]]
        return {"Body": BytesIO(value), "ContentLength": len(value)}
    client.put_object.side_effect, client.get_object.side_effect = put, get
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: storage.put_content_addressed(content, key), range(2)))
    assert results == [key, key] and writes == [content] and objects == {key: content}
