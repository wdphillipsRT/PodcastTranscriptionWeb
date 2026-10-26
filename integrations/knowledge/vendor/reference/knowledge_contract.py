"""Executable knowledge-batch/v1 and knowledge-record/v1 semantics.

This module is the conformance oracle for KNOWLEDGE-INTAKE-0001. It uses only
the standard library so producers and the Platform intake can vendor it
unchanged. JSON Schema validation is injected by the caller; every condition
checked here is one JSON Schema cannot express.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

BATCH_SCHEMA_VERSION = "knowledge-batch/v1"
RECORD_SCHEMA_VERSION = "knowledge-record/v1"
KNOWLEDGE_NAMESPACE = uuid.UUID("81deb573-2d13-520f-8c96-265db8eb174d")
MAX_RECORDS = 10_000
MAX_BATCH_BYTES = 1 << 30
MAX_MANIFEST_BYTES = 64 * 1024
_CHUNK = 1 << 20
_ROOT_ENTRIES = frozenset({"manifest.json", "records.ndjson", "blobs"})

KIND_PRODUCERS: Mapping[str, str] = {
    "podcast.episode": "podcast",
    "podcast.segment": "podcast",
    "issachar.article": "issachar",
    "issachar.evidence_span": "issachar",
    "issachar.theme_version": "issachar",
    "issachar.forecast_contract": "issachar",
    "issachar.forecast": "issachar",
    "issachar.resolution": "issachar",
    "issachar.calibration": "issachar",
}
EBENEZER_KINDS = frozenset({
    "podcast.episode",
    "issachar.theme_version",
    "issachar.forecast_contract",
    "issachar.forecast",
    "issachar.resolution",
    "issachar.calibration",
})
PARTICIPANT_KINDS = frozenset({"podcast.episode", "podcast.segment"})
HOST_ROLES = frozenset({"host", "co_host"})
PLATFORMS = frozenset({"podcast", "youtube"})
"""`attributes.platform` values for podcast kinds; Ebenezer maps each to a source system."""

_SHA256 = re.compile(r"[0-9a-f]{64}")
_BATCH_ID = re.compile(r"(podcast|issachar)-([0-9]{8})-([0-9]{3})")
# RFC 3339 date-time with a mandatory offset; JSON Schema `format` is not
# enforced by every validator, so this module is authoritative for timestamps.
# Leap seconds (":60") are deliberately rejected: datetime cannot represent them.
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})")

SchemaValidator = Callable[[str, Mapping[str, Any]], None]
"""Called as validate(schema_name, document); raises on schema violation.

`schema_name` is "manifest" or "record".
"""


class ContractError(ValueError):
    """A batch or record violates knowledge-batch/v1 or knowledge-record/v1."""


@dataclass(frozen=True)
class BatchSummary:
    batch_id: str
    producer: str
    record_count: int
    record_hashes: Mapping[str, str]
    records: tuple[Mapping[str, Any], ...] = ()
    """Parsed, validated records in file order; each is a read-only top-level view."""


def canonical_json(value: Any) -> bytes:
    """Python canonical JSON: sorted keys, no whitespace, UTF-8, no NaN.

    This is Python's json canonical form, not RFC 8785 (JCS); producers in other
    languages must reproduce it byte for byte.
    """
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
            default=_json_default,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("value is not canonical JSON") from exc


def _json_default(value: Any) -> Any:
    if isinstance(value, Mapping):  # Read-only views returned in BatchSummary.records.
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _freeze(value: Any) -> Any:
    """Recursively make parsed JSON read-only: dict -> mappingproxy, list -> tuple."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def record_hash(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(record)).hexdigest()


def example_id(record_id: str) -> str:
    """Deterministic Lamad example identifier for a record."""
    return str(uuid.uuid5(KNOWLEDGE_NAMESPACE, record_id))


def person_entity_id(name: str) -> str:
    """Stable `person:<slug>` identifier shared by every producer."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:128].rstrip("-")
    if not slug:
        raise ContractError("participant name has no identifier characters")
    return f"person:{slug}"


def blob_filename(sha256: str) -> str:
    return f"sha256-{sha256}"


def check_record(record: Mapping[str, Any], producer: str) -> None:
    """Semantic checks for one schema-valid record."""
    if record["schema_version"] != RECORD_SCHEMA_VERSION:
        raise ContractError(f"{_show(record['record_id'])}: unsupported record schema_version")
    kind = record["kind"]
    record_id = record["record_id"]
    shown = _show(record_id)
    if KIND_PRODUCERS.get(kind) != producer:
        raise ContractError(f"{shown}: kind {_show(kind)} is not produced by {producer}")
    prefix = f"{producer}:{kind}:"
    if not record_id.startswith(prefix) or len(record_id) == len(prefix):
        raise ContractError(f"{shown}: record_id must start with {prefix}")

    for field, value in _timestamps(record):
        if value is not None:
            _instant(value, f"{shown} {field}")

    participants = record.get("participants") or []
    if kind in PARTICIPANT_KINDS:
        if record["attributes"].get("platform") not in PLATFORMS:
            raise ContractError(f"{shown}: podcast records require attributes.platform podcast or youtube")
        if not any(p["role"] in HOST_ROLES for p in participants):
            raise ContractError(f"{shown}: podcast records require a host or co_host")
        for participant in participants:
            confidence = participant["confidence"]
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ContractError(f"{shown}: participant confidence must be a number")
            if participant["basis"] != "inferred" and confidence != 1:
                raise ContractError(f"{shown}: declared participants must have confidence 1")
            if participant["entity_id"] != person_entity_id(participant["name"]):
                raise ContractError(f"{shown}: participant entity_id must derive from name")
    elif participants:
        raise ContractError(f"{shown}: only podcast records carry participants")

    supersedes = record.get("supersedes")
    if supersedes is not None and (supersedes == record_id or not supersedes.startswith(prefix)):
        raise ContractError(f"{shown}: supersedes must name another record of the same kind")
    for relation in record.get("relations") or []:
        if relation["target_record_id"] == record_id:
            raise ContractError(f"{shown}: a record cannot relate to itself")

    valid_from, valid_until = record.get("valid_from"), record.get("valid_until")
    if valid_from and valid_until and _instant(valid_until, shown) < _instant(valid_from, shown):
        raise ContractError(f"{shown}: valid_until precedes valid_from")


def write_batch(
    inbox: Path,
    *,
    producer: str,
    batch_id: str,
    producer_version: str,
    created_at: str,
    records: Sequence[Mapping[str, Any]],
    blobs: Mapping[str, bytes],
    validate_schema: SchemaValidator | None = None,
) -> Path:
    """Atomically publish one batch into a producer spool inbox.

    The batch is assembled under `.tmp-<batch_id>`, validated, synced, and
    renamed, so the intake never observes a partial batch. Producers should
    pass `validate_schema` (JSON Schema validation of both documents); when it
    is None only the semantic checks in this module run. On any failure the
    staging directory is removed and the exception propagates.
    """
    match = _BATCH_ID.fullmatch(batch_id) if isinstance(batch_id, str) else None
    if not match or match.group(1) != producer:
        raise ContractError(f"{_show(batch_id)}: batch_id must be {producer}-YYYYMMDD-NNN")
    for sha256, data in blobs.items():
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ContractError(f"blob {_show(sha256)} does not match its content")

    final = inbox / batch_id
    if final.exists():
        raise ContractError(f"{batch_id}: batch already exists in {inbox}")
    staging = inbox / f".tmp-{batch_id}"
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        staging.mkdir()  # Fails if present, so cleanup below only removes what this call created.
    except FileExistsError as exc:
        raise ContractError(f"{batch_id}: stale staging directory {staging}") from exc
    try:
        (staging / "blobs").mkdir()
        for sha256, data in blobs.items():
            _write_synced(staging / "blobs" / blob_filename(sha256), data)

        lines = b"".join(canonical_json(record) + b"\n" for record in records)
        _write_synced(staging / "records.ndjson", lines)
        manifest = {
            "schema_version": BATCH_SCHEMA_VERSION,
            "batch_id": batch_id,
            "producer": producer,
            "producer_version": producer_version,
            "created_at": created_at,
            "record_count": len(records),
            "records_sha256": hashlib.sha256(lines).hexdigest(),
            "blob_count": len(blobs),
        }
        _write_synced(staging / "manifest.json", canonical_json(manifest) + b"\n")

        validate_batch(
            staging, batch_id=batch_id, expected_producer=producer,
            validate_schema=validate_schema or _no_schema,
        )
        _fsync_directory(staging / "blobs")
        _fsync_directory(staging)
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _fsync_directory(inbox)
    return final


def validate_batch(
    path: Path,
    *,
    expected_producer: str,
    validate_schema: SchemaValidator,
    batch_id: str | None = None,
) -> BatchSummary:
    """Verify a complete batch directory and return its records and hashes.

    Safe for untrusted spool directories: raises only ContractError, refuses
    symlinks and unexpected entries before reading anything, and bounds reads.
    """
    batch_id = batch_id or path.name
    blob_entries = _scan_batch(path, batch_id)

    manifest = _parse_json(_read_regular(path / "manifest.json", batch_id, MAX_MANIFEST_BYTES), "manifest")
    if not isinstance(manifest, dict):
        raise ContractError(f"{batch_id}: manifest must be a JSON object")
    try:
        producer, record_count, records_sha256, blob_count = _check_manifest(manifest, batch_id, expected_producer, validate_schema)
    except (KeyError, TypeError, AttributeError) as exc:
        missing = f" (missing or invalid {_show(exc.args[0])})" if isinstance(exc, KeyError) and exc.args else ""
        raise ContractError(f"{batch_id}: malformed manifest{missing}") from exc

    try:
        records_size = (path / "records.ndjson").lstat().st_size
    except OSError as exc:
        raise ContractError(f"{batch_id}: unreadable") from exc
    blob_total = sum(size for _, size in blob_entries.values())
    if blob_total + records_size > MAX_BATCH_BYTES:
        raise ContractError(f"{batch_id}: batch exceeds {MAX_BATCH_BYTES} bytes")

    # The file may grow after lstat; the read itself is bounded too.
    records_bytes = _read_regular(path / "records.ndjson", batch_id, MAX_BATCH_BYTES - blob_total)
    if hashlib.sha256(records_bytes).hexdigest() != records_sha256:
        raise ContractError(f"{batch_id}: records.ndjson does not match records_sha256")
    pieces = records_bytes.split(b"\n")
    if pieces[-1] != b"":
        raise ContractError(f"{batch_id}: records.ndjson must be LF-terminated")
    lines = pieces[:-1]
    if len(lines) != record_count:
        raise ContractError(f"{batch_id}: record_count does not match records.ndjson")
    if not 1 <= len(lines) <= MAX_RECORDS:
        raise ContractError(f"{batch_id}: record count outside 1..{MAX_RECORDS}")

    if len(blob_entries) != blob_count:
        raise ContractError(f"{batch_id}: blob_count does not match blobs/")
    for name, (entry, _) in blob_entries.items():
        if not name.startswith("sha256-") or _hash_file(entry, batch_id) != name[7:]:
            raise ContractError(f"{batch_id}: blob {_show(name)} does not match its content")

    hashes: dict[str, str] = {}
    parsed: list[Mapping[str, Any]] = []
    referenced: set[str] = set()
    for number, line in enumerate(lines, 1):
        where = f"{batch_id} line {number}"
        record = _parse_json(line, where)
        try:
            if not isinstance(record, dict):
                raise ContractError(f"{where}: record must be a JSON object")
            _schema(validate_schema, "record", record, where)
            if canonical_json(record) != line:
                raise ContractError(f"{where}: record is not canonical JSON")
            check_record(record, producer)
            record_id = record["record_id"]
            if record_id in hashes:
                raise ContractError(f"{batch_id}: duplicate record_id {_show(record_id)}")
            content = record.get("content")
            if content is not None:
                name = blob_filename(content["sha256"])
                blob = blob_entries.get(name)
                if blob is None or blob[1] != content["bytes"]:
                    raise ContractError(f"{_show(record_id)}: content blob missing or wrong size")
                referenced.add(name)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ContractError(f"{where}: malformed record") from exc
        hashes[record_id] = hashlib.sha256(line).hexdigest()
        parsed.append(_freeze(record))
    if referenced != set(blob_entries):
        raise ContractError(f"{batch_id}: blob not referenced by any record")

    return BatchSummary(
        batch_id=batch_id, producer=producer, record_count=len(hashes),
        record_hashes=MappingProxyType(hashes), records=tuple(parsed),
    )


def _no_schema(name: str, document: Mapping[str, Any]) -> None:
    return None


def _show(value: Any) -> str:
    """repr() of a producer-supplied value, truncated for error messages."""
    text = repr(value)
    return text if len(text) <= 200 else text[:200] + "..."


def _scandir(path: Path, where: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as entries:
            return list(entries)
    except OSError as exc:
        raise ContractError(f"{where}: unreadable") from exc


def _scan_batch(path: Path, batch_id: str) -> dict[str, tuple[Path, int]]:
    """Check the batch layout without reading any file; return {blob name: (path, size)}."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise ContractError(f"{batch_id}: batch is not a directory")
    except OSError as exc:
        raise ContractError(f"{batch_id}: unreadable") from exc
    root = {entry.name: entry for entry in _scandir(path, batch_id)}
    extra = set(root) - _ROOT_ENTRIES
    if extra:
        raise ContractError(f"{batch_id}: unexpected entries {_show(sorted(extra))}")
    for name in ("manifest.json", "records.ndjson"):
        if name not in root or not _is_regular(root[name]):
            raise ContractError(f"{batch_id}: {name} is not a regular file")
    blobs: dict[str, tuple[Path, int]] = {}
    if "blobs" in root:
        try:
            if not root["blobs"].is_dir(follow_symlinks=False):
                raise ContractError(f"{batch_id}: blobs is not a directory")
        except OSError as exc:
            raise ContractError(f"{batch_id}: unreadable") from exc
        for entry in _scandir(path / "blobs", batch_id):
            if not _is_regular(entry):
                raise ContractError(f"{batch_id}: blob {_show(entry.name)} is not a regular file")
            blobs[entry.name] = (Path(entry.path), entry.stat(follow_symlinks=False).st_size)
    return blobs


def _is_regular(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _check_manifest(
    manifest: Mapping[str, Any], batch_id: str, expected_producer: str, validate_schema: SchemaValidator,
) -> tuple[str, int, str, int]:
    _schema(validate_schema, "manifest", manifest, "manifest")
    if manifest.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise ContractError("unsupported batch schema_version")
    match = _BATCH_ID.fullmatch(batch_id)
    if manifest.get("batch_id") != batch_id or not match:
        raise ContractError(f"manifest batch_id does not match directory {_show(batch_id)}")
    _instant(manifest["created_at"], f"{batch_id} manifest created_at")
    producer = manifest["producer"]
    if match.group(1) != producer or producer != expected_producer:
        raise ContractError(f"{batch_id}: producer {_show(producer)} does not own this spool")
    records_sha256 = manifest["records_sha256"]
    if not isinstance(records_sha256, str) or not _SHA256.fullmatch(records_sha256):
        raise ContractError(f"{batch_id}: manifest records_sha256 must be 64 lowercase hex digits")
    counts = []
    for field in ("record_count", "blob_count"):
        value = manifest[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{batch_id}: manifest {field} must be an integer")
        counts.append(value)
    return producer, counts[0], records_sha256, counts[1]


def _open_regular(path: Path, where: str) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ContractError(f"{where}: unreadable") from exc


def _read_regular(path: Path, where: str, limit: int | None) -> bytes:
    descriptor = _open_regular(path, where)
    try:
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read() if limit is None else handle.read(limit + 1)
    except OSError as exc:
        raise ContractError(f"{where}: unreadable") from exc
    if limit is not None and len(data) > limit:
        raise ContractError(f"{where}: {path.name} is too large")
    return data


def _hash_file(path: Path, where: str) -> str:
    digest = hashlib.sha256()
    descriptor = _open_regular(path, f"{where} {path.name}")
    try:
        with os.fdopen(descriptor, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"{where} {path.name}: unreadable") from exc
    return digest.hexdigest()


def _timestamps(record: Mapping[str, Any]) -> list[tuple[str, Any]]:
    source = record["source"]
    return [
        ("observed_at", record["observed_at"]),
        ("valid_from", record.get("valid_from")),
        ("valid_until", record.get("valid_until")),
        ("source.published_at", source["published_at"]),
        ("source.retrieved_at", source["retrieved_at"]),
    ]


def _instant(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ContractError(f"{where}: timestamp must be RFC 3339 with an offset")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{where}: timestamp is not a valid instant") from exc


def _schema(validate_schema: SchemaValidator, name: str, document: Mapping[str, Any], where: str) -> None:
    try:
        validate_schema(name, document)
    except ContractError:
        raise
    except Exception as exc:  # The injected validator's own error type.
        # Report only where the violation is, never producer-supplied values.
        location = getattr(exc, "absolute_path", None)
        if location is not None:
            detail = "/".join(str(part) for part in location)
        else:
            detail = getattr(exc, "json_path", "")
        suffix = f" at {detail}" if detail and isinstance(detail, str) else ""
        raise ContractError(f"{where}: {name} schema violation{suffix}") from None


def _parse_json(data: bytes, where: str) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError(f"{where}: invalid JSON") from exc


def _write_synced(path: Path, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
