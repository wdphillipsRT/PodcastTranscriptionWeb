"""Private, region-pinned artifact storage. Public URLs are not capabilities."""

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import stat

from botocore.exceptions import (ClientError, ConnectionError, HTTPClientError,
                                 IncompleteReadError, ResponseStreamingError)

from aws_clients import create_aws_client
from config import load_settings


BUCKET_NAME = "podcast-transcriptions-web-2025"
DEFAULT_MAX_BYTES = 52_428_800
MEDIA_RANGE_MAX_BYTES = 8 * 1024 * 1024
AUDIO_CONTENT_TYPES = frozenset({"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav",
    "audio/x-wav", "audio/flac", "audio/ogg", "audio/amr", "audio/webm"})


class StorageError(ValueError):
    """Safe storage failure; provider payloads never cross this boundary."""


class StorageTransientError(StorageError):
    """The caller may retry this operation."""


class StoragePermanentError(StorageError):
    """Invalid configuration, input, or denied operation."""


class StorageIntegrityError(StoragePermanentError):
    """Missing or corrupt artifact; a pipeline may repair its own references."""


class StorageRangeError(StoragePermanentError):
    """A byte range cannot be served; contains no storage diagnostics."""


@dataclass(frozen=True)
class VerifiedDownload:
    """Verified content metadata, without storage or local path details."""

    byte_count: int
    sha256: str


def parse_media_range(header):
    """One explicit start, optional end; reject suffixes and multipart ranges."""
    match = re.fullmatch(r"bytes=([0-9]{1,19})-([0-9]{1,19})?", header) if isinstance(header, str) else None
    if match is None:
        raise StorageRangeError("invalid media range")
    start, end = int(match[1]), int(match[2]) if match[2] else None
    if start > 2**63 - 1 or end is not None and (end < start or end > 2**63 - 1 or end - start + 1 > MEDIA_RANGE_MAX_BYTES):
        raise StorageRangeError("invalid media range")
    return start, end


def setting(settings, name):
    return settings[name] if isinstance(settings, Mapping) else getattr(settings, name)


def validate_segment(value):
    # ASCII rejects encoded traversal, controls and URL syntax as well.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}", value) or ".." in value:
        raise StoragePermanentError("invalid artifact segment")
    return value


def validate_artifact_key(key):
    if not isinstance(key, str):
        raise StoragePermanentError("invalid artifact key")
    parts = key.split("/")
    if len(parts) != 5 or parts[0] != "items" or parts[1] not in {"podcast", "youtube"}:
        raise StoragePermanentError("invalid artifact key")
    for part in parts[1:]:
        validate_segment(part)
    if len(key.encode()) > 1024:
        raise StoragePermanentError("invalid artifact key")
    return key


def _failure(error):
    code = error.response.get("Error", {}).get("Code", "") if isinstance(error, ClientError) else ""
    if code in {"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "InternalError", "ServiceUnavailable", "500", "503"} or isinstance(error, (ConnectionError, HTTPClientError, IncompleteReadError, ResponseStreamingError)):
        raise StorageTransientError("storage temporarily unavailable") from None
    raise StoragePermanentError("storage operation failed") from None


class S3Manager:
    """One configured bucket and region, probed before the first operation.

    Credentials use boto3's normal chain. Inject a client for tests. Operations
    accept no bucket override, URL, endpoint or presigning option.
    """

    def __init__(self, settings=None, *, client=None):
        settings = settings if settings is not None else load_settings()
        self._bucket_name = setting(settings, "AWS_S3_BUCKET_NAME")
        self._region = setting(settings, "AWS_REGION")
        if self._bucket_name != BUCKET_NAME:
            raise StoragePermanentError("invalid configured bucket")
        if not isinstance(self._region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", self._region):
            raise StoragePermanentError("invalid configured region")
        try:
            self.s3_client = client if client is not None else create_aws_client("s3", self.region)
            client_region = self.s3_client.meta.region_name
            if isinstance(client_region, str) and client_region != self.region:
                raise StoragePermanentError("storage client region mismatch")
            # S3's region redirector is independent of ordinary retries.
            self.s3_client.meta.events.register_first("needs-retry.s3", self._reject_redirect)
            self.probe()
        except StorageError:
            raise
        except Exception as error:
            _failure(error)

    @property
    def bucket_name(self):
        return self._bucket_name

    @property
    def region(self):
        return self._region

    def _reject_redirect(self, response=None, **kwargs):
        if response is None:
            return
        http_response, parsed = response
        headers = {str(k).lower(): v for k, v in http_response.headers.items()}
        region = headers.get("x-amz-bucket-region")
        if http_response.status_code in {301, 302, 303, 307, 308} or "location" in headers or region and region != self.region:
            # This hook runs before get_object returns, so get_bytes cannot yet
            # own/close the stream. Release every available response layer here.
            body = parsed.get("Body") if isinstance(parsed, Mapping) else None
            candidates = (body, getattr(http_response, "raw", None), http_response)
            closed = set()
            for candidate in candidates:
                if candidate is None or id(candidate) in closed:
                    continue
                closed.add(id(candidate))
                try:
                    close = getattr(candidate, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
            raise StoragePermanentError("storage redirect or region mismatch") from None

    def _check_response(self, response):
        metadata = response.get("ResponseMetadata", {})
        headers = {str(k).lower(): v for k, v in metadata.get("HTTPHeaders", {}).items()}
        status = metadata.get("HTTPStatusCode", 200)
        region = headers.get("x-amz-bucket-region")
        if 300 <= status < 400 or "location" in headers or region and region != self.region:
            raise StoragePermanentError("storage redirect or region mismatch")
        if not 200 <= status < 300:
            raise StoragePermanentError("storage operation failed")

    def probe(self):
        try:
            response = self.s3_client.get_bucket_location(Bucket=self.bucket_name)
            self._check_response(response)
            if "LocationConstraint" not in response:
                raise StoragePermanentError("bucket region unavailable")
            actual = response["LocationConstraint"]
            actual = "us-east-1" if actual is None else actual
            if actual != self.region:
                raise StoragePermanentError("bucket region mismatch")
        except StorageError:
            raise
        except Exception as error:
            _failure(error)

    @staticmethod
    def generate_s3_key(platform, external_id, kind, filename):
        for part in (platform, external_id, kind, filename):
            validate_segment(part)
        return validate_artifact_key(f"items/{platform}/{external_id}/{kind}/{filename}")

    @staticmethod
    def _content_type(content_type):
        if not isinstance(content_type, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+(?:; charset=utf-8)?", content_type):
            raise StoragePermanentError("explicit content type required")
        return content_type

    def upload_binary_content(self, content, s3_key, content_type):
        validate_artifact_key(s3_key)
        self._content_type(content_type)
        if not isinstance(content, bytes):
            raise StoragePermanentError("artifact body must be bytes")
        try:
            response = self.s3_client.put_object(Bucket=self.bucket_name, Key=s3_key, Body=content, ContentType=content_type, ServerSideEncryption="AES256")
            self._check_response(response)
            return s3_key
        except StorageError:
            raise
        except Exception as error:
            _failure(error)

    def upload_text_content(self, content, s3_key, content_type="text/plain"):
        if not isinstance(content, str):
            raise StoragePermanentError("artifact body must be text")
        return self.upload_binary_content(content.encode("utf-8"), s3_key, content_type)

    def put_content_addressed(self, content: bytes, s3_key: str,
                              content_type: str = "application/json", *,
                              address_sha256: str | None = None) -> str:
        """Create once; conflicting writers must have exactly the same bytes.

        An explicit address supports artifacts whose semantic digest excludes
        their embedded digest field. The caller validates that artifact schema.
        """
        validate_artifact_key(s3_key)
        self._content_type(content_type)
        if not isinstance(content, bytes) or not content:
            raise StoragePermanentError("artifact body must be nonempty bytes")
        address = sha256(content).hexdigest() if address_sha256 is None else address_sha256
        if (not isinstance(address, str) or re.fullmatch(r"[a-f0-9]{64}", address) is None
                or s3_key.rsplit("/", 1)[1] != address + ".json"):
            raise StoragePermanentError("invalid content address")
        try:
            response = self.s3_client.put_object(Bucket=self.bucket_name, Key=s3_key,
                Body=content, ContentType=content_type, ServerSideEncryption="AES256", IfNoneMatch="*")
            self._check_response(response)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                    "PreconditionFailed", "412", "ConditionalRequestConflict"}:
                _failure(error)
            if self.get_bytes(s3_key, max_bytes=len(content)) != content:
                raise StorageIntegrityError("immutable artifact conflict") from None
        except StorageError:
            raise
        except Exception as error:
            _failure(error)
        return s3_key

    def upload_file(self, local_file_path, s3_key, content_type):
        validate_artifact_key(s3_key)
        self._content_type(content_type)
        try:
            self.s3_client.upload_file(str(local_file_path), self.bucket_name, s3_key, ExtraArgs={"ContentType": content_type, "ServerSideEncryption": "AES256"})
            return s3_key
        except StorageError:
            raise
        except Exception as error:
            _failure(error)

    def _object_chunks(self, s3_key, *, max_bytes):
        validate_artifact_key(s3_key)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise StoragePermanentError("invalid artifact byte limit")
        body = None
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key)
            body = response.get("Body")
            self._check_response(response)
            length = response.get("ContentLength")
            if type(length) is not int or not 0 <= length <= max_bytes:
                raise StorageIntegrityError("invalid artifact content length")
            total = 0
            while True:
                chunk = body.read(min(65536, max_bytes - total + 1))
                if not isinstance(chunk, bytes):
                    raise StorageIntegrityError("invalid artifact body")
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes or total > length:
                    raise StorageIntegrityError("artifact exceeds declared byte limit")
                yield chunk
            if total != length:
                raise StorageIntegrityError("artifact content length mismatch")
        except StorageError:
            raise
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                raise StorageIntegrityError("artifact unavailable") from None
            _failure(error)
        except Exception as error:
            _failure(error)
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass

    def get_bytes(self, s3_key, *, max_bytes=DEFAULT_MAX_BYTES):
        with closing(self._object_chunks(s3_key, max_bytes=max_bytes)) as chunks:
            return b"".join(chunks)

    def media_info(self, s3_key):
        """Project only validated audio metadata; never return provider headers."""
        validate_artifact_key(s3_key)
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
            self._check_response(response)
            length, mime = response.get("ContentLength"), response.get("ContentType")
            if type(length) is not int or not 0 < length <= 2**63 - 1 or mime not in AUDIO_CONTENT_TYPES:
                raise StorageIntegrityError("media unavailable")
            return {"length": length, "content_type": mime}
        except StorageError:
            raise
        except Exception as error:
            _failure(error)

    def download_range(self, s3_key, range_header):
        """Read exactly one validated private audio range, at most eight MiB."""
        validate_artifact_key(s3_key)
        start, end = parse_media_range(range_header)
        open_ended = end is None
        expected_total = None
        if open_ended:
            info = self.media_info(s3_key)
            expected_total = info["length"]
            if start >= expected_total:
                raise StorageRangeError("invalid media range")
            end = min(start + MEDIA_RANGE_MAX_BYTES - 1, expected_total - 1)
        body = None
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key, Range=f"bytes={start}-{end}")
            body = response.get("Body")
            self._check_response(response)
            content_range = response.get("ContentRange")
            match = re.fullmatch(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})", content_range) if isinstance(content_range, str) else None
            if match is None or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 206:
                raise StorageIntegrityError("invalid media response")
            actual_start, actual_end, total = map(int, match.groups())
            length, mime = response.get("ContentLength"), response.get("ContentType")
            if (not 0 <= actual_start <= actual_end < total <= 2**63 - 1 or actual_start != start
                    or actual_end != min(end, total - 1) or expected_total is not None and total != expected_total
                    or type(length) is not int or length != actual_end - start + 1
                    or not 0 < length <= MEDIA_RANGE_MAX_BYTES or mime not in AUDIO_CONTENT_TYPES):
                raise StorageIntegrityError("invalid media response")
            if not open_ended and end >= total:
                raise StorageRangeError("invalid media range")
            chunks, received = [], 0
            while True:
                chunk = body.read(min(65536, length - received + 1))
                if not isinstance(chunk, bytes) or received + len(chunk) > length:
                    raise StorageIntegrityError("invalid media body")
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            if received != length:
                raise StorageIntegrityError("invalid media body")
            return dict(body=b"".join(chunks), length=length, content_type=mime, content_range=content_range)
        except StorageError:
            raise
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"InvalidRange", "RequestedRangeNotSatisfiable", "416"}:
                raise StorageRangeError("invalid media range") from None
            _failure(error)
        except Exception as error:
            _failure(error)
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass

    def verify_sha256(self, s3_key, *, expected_sha256=None, max_bytes=DEFAULT_MAX_BYTES):
        """Hash bounded object chunks without retaining an audio-sized buffer."""
        from hashlib import sha256
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None):
            raise StoragePermanentError("invalid artifact hash")
        digest = sha256()
        with closing(self._object_chunks(s3_key, max_bytes=max_bytes)) as chunks:
            for chunk in chunks:
                digest.update(chunk)
        result = digest.hexdigest()
        if expected_sha256 is not None and result != expected_sha256:
            raise StorageIntegrityError("artifact hash mismatch")
        return result

    def download_text_content(self, s3_key, *, max_bytes=DEFAULT_MAX_BYTES):
        try:
            return self.get_bytes(s3_key, max_bytes=max_bytes).decode("utf-8")
        except UnicodeError:
            raise StoragePermanentError("invalid artifact text encoding") from None

    def download_file(self, s3_key, local_file_path, *, max_bytes=DEFAULT_MAX_BYTES):
        content = self.get_bytes(s3_key, max_bytes=max_bytes)
        try:
            Path(local_file_path).write_bytes(content)
        except OSError:
            raise StoragePermanentError("artifact local write failed") from None
        return True

    def download_file_verified(self, s3_key, local_path, *, expected_sha256,
                               max_bytes) -> VerifiedDownload:
        """Stream verified audio to a new private file in an existing directory.

        The caller owns the temporary directory. Existing destinations are never
        replaced; failures remove only the regular file created by this call.
        """
        validate_artifact_key(s3_key)
        if (not isinstance(expected_sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None):
            raise StoragePermanentError("invalid artifact hash")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise StoragePermanentError("invalid artifact byte limit")
        destination, identity, descriptor = None, None, None
        complete = False
        try:
            destination = Path(local_path)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = os.fstat(descriptor)
            identity = (created.st_dev, created.st_ino)
            digest, total = sha256(), 0
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None  # The file context now owns the descriptor.
                with closing(self._object_chunks(s3_key, max_bytes=max_bytes)) as chunks:
                    for chunk in chunks:
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                actual_sha256 = digest.hexdigest()
                if not total or actual_sha256 != expected_sha256:
                    raise StorageIntegrityError("artifact hash or empty body mismatch")
                output.flush()
                os.fsync(output.fileno())
            complete = True
            return VerifiedDownload(byte_count=total, sha256=actual_sha256)
        except StorageError:
            raise
        except Exception as error:
            _failure(error)
        finally:
            if descriptor is not None:
                if identity is None:
                    try:
                        # fstat may have failed during acquisition. Recover
                        # identity from the still-owned descriptor, never from
                        # the path, which could already name a replacement.
                        created = os.stat(descriptor)
                        identity = (created.st_dev, created.st_ino)
                    except OSError:
                        pass
                try:
                    os.close(descriptor)
                except OSError:
                    # A failed close must not skip unlink or expose diagnostics.
                    # Do not retry: the descriptor may already be released.
                    pass
            if not complete and identity is not None:
                try:
                    current = destination.lstat()
                    if (stat.S_ISREG(current.st_mode)
                            and (current.st_dev, current.st_ino) == identity):
                        destination.unlink()
                except OSError:
                    # Cleanup must not replace a safe error with local details.
                    pass

    def get_file_info(self, s3_key):
        validate_artifact_key(s3_key)
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
            self._check_response(response)
            return {"size": response["ContentLength"], "content_type": response.get("ContentType")}
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            _failure(error)
        except StorageError:
            raise
        except Exception as error:
            _failure(error)

    def file_exists(self, s3_key):
        return self.get_file_info(s3_key) is not None

    def delete_file(self, s3_key):
        validate_artifact_key(s3_key)
        try:
            response = self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
            self._check_response(response)
            return True
        except StorageError:
            raise
        except Exception as error:
            _failure(error)


def get_s3_manager(settings=None):
    """Build a probed manager for the current worker configuration."""
    return S3Manager(settings)
