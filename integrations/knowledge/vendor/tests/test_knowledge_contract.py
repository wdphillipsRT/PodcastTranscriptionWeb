from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from collections import deque
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "tests/fixtures/knowledge"))

import build_golden  # noqa: E402
import knowledge_contract  # noqa: E402
from knowledge_contract import (  # noqa: E402
    ContractError, canonical_json, example_id, person_entity_id, validate_batch, write_batch,
)

GOLDEN = ROOT / "tests/fixtures/knowledge/golden"
VALIDATORS = {
    name: Draft202012Validator(json.loads((ROOT / path).read_text()), format_checker=FormatChecker())
    for name, path in {
        "manifest": "schemas/knowledge-batch-manifest-v1.schema.json",
        "record": "schemas/knowledge-record-v1.schema.json",
    }.items()
}


def validate_schema(name: str, document: dict) -> None:
    VALIDATORS[name].validate(document)


def noop_schema(name: str, document: dict) -> None:
    return None


def check(path: Path, producer: str = "podcast", schema=validate_schema):
    return validate_batch(path, expected_producer=producer, validate_schema=schema)


def tree(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.fixture
def batch(tmp_path: Path) -> Path:
    target = tmp_path / "podcast-20261022-001"
    shutil.copytree(GOLDEN / "podcast-20261022-001", target)
    return target


def rewrite_records(path: Path, mutate) -> None:
    """Apply `mutate` to the parsed records and refresh the manifest digest."""
    records = [json.loads(line) for line in (path / "records.ndjson").read_bytes().splitlines()]
    mutate(records)
    lines = b"".join(canonical_json(record) + b"\n" for record in records)
    (path / "records.ndjson").write_bytes(lines)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["records_sha256"] = hashlib.sha256(lines).hexdigest()
    manifest["record_count"] = len(records)
    (path / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")


@pytest.mark.parametrize("batch_id", ["podcast-20261022-001", "issachar-20261022-001"])
def test_golden_batches_are_valid(batch_id: str) -> None:
    summary = check(GOLDEN / batch_id, producer=batch_id.split("-")[0])

    assert summary.batch_id == batch_id
    assert summary.record_count == 2


def test_golden_batches_rebuild_byte_for_byte(tmp_path: Path) -> None:
    build_golden.build(tmp_path)

    assert tree(tmp_path) == tree(GOLDEN)


def test_example_id_is_stable() -> None:
    assert example_id("issachar:issachar.forecast:fx-0123456789abcdef") == "7bda1901-e32e-5fbe-b6f0-e824a176125e"
    assert example_id("a") != example_id("b")


def test_person_entity_id_folds_accents_and_punctuation() -> None:
    assert person_entity_id("Zoë O'Brien-Smith") == "person:zoe-o-brien-smith"
    with pytest.raises(ContractError):
        person_entity_id("!!!")


def test_rejects_tampered_records(batch: Path) -> None:
    (batch / "records.ndjson").write_bytes((batch / "records.ndjson").read_bytes().replace(b"habit", b"HABIT"))

    with pytest.raises(ContractError, match="records_sha256"):
        check(batch)


def test_rejects_tampered_blob(batch: Path) -> None:
    blob = next((batch / "blobs").iterdir())
    blob.write_bytes(b"tampered")

    with pytest.raises(ContractError, match="does not match its content"):
        check(batch)


def test_rejects_wrong_record_count(batch: Path) -> None:
    manifest = json.loads((batch / "manifest.json").read_text())
    manifest["record_count"] = 3
    (batch / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")

    with pytest.raises(ContractError, match="record_count"):
        check(batch)


def test_rejects_batch_in_another_producers_spool(batch: Path) -> None:
    with pytest.raises(ContractError, match="does not own this spool"):
        check(batch, producer="issachar")


def test_rejects_podcast_without_host(batch: Path) -> None:
    def drop_host(records):
        for record in records:
            record["participants"] = [p for p in record["participants"] if p["role"] != "host"]

    rewrite_records(batch, drop_host)

    with pytest.raises(ContractError, match="require a host"):
        check(batch)


def test_rejects_declared_participant_with_partial_confidence(batch: Path) -> None:
    def weaken(records):
        records[0]["participants"][0]["confidence"] = 0.5

    rewrite_records(batch, weaken)

    with pytest.raises(ContractError, match="confidence 1"):
        check(batch)


def test_rejects_duplicate_record_ids(batch: Path) -> None:
    rewrite_records(batch, lambda records: records.append(dict(records[0])))

    with pytest.raises(ContractError, match="duplicate record_id"):
        check(batch)


def test_rejects_kind_from_another_producer(batch: Path) -> None:
    def relabel(records):
        records[0]["kind"] = "issachar.forecast"

    rewrite_records(batch, relabel)

    with pytest.raises(ContractError, match="not produced by podcast"):
        check(batch)


def test_rejects_validity_window_that_ends_before_it_starts(batch: Path) -> None:
    def invert(records):
        records[0]["valid_from"] = "2026-10-22T12:00:00+02:00"
        records[0]["valid_until"] = "2026-10-22T11:00:00+00:00"

    rewrite_records(batch, invert)

    check(batch)  # 11:00Z is after 10:00Z; offsets must be compared as instants.

    def really_invert(records):
        records[0]["valid_until"] = "2026-10-22T09:00:00+00:00"

    rewrite_records(batch, really_invert)

    with pytest.raises(ContractError, match="precedes"):
        check(batch)


def test_schema_rejects_unknown_fields(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].update({"surprise": True}))

    with pytest.raises(ContractError, match="record schema violation"):
        check(batch)


def test_write_batch_refuses_to_overwrite(tmp_path: Path) -> None:
    golden = build_golden.podcast_batch()
    kwargs = dict(producer="podcast", batch_id=golden["batch_id"], producer_version="t",
                  created_at=build_golden.CREATED_AT, records=golden["records"], blobs=golden["blobs"])
    write_batch(tmp_path, **kwargs)

    with pytest.raises(ContractError, match="already exists"):
        write_batch(tmp_path, **kwargs)
    assert not (tmp_path / f".tmp-{golden['batch_id']}").exists()


def test_write_batch_leaves_no_visible_batch_when_invalid(tmp_path: Path) -> None:
    golden = build_golden.podcast_batch()
    golden["records"][0]["participants"] = []
    kwargs = dict(producer="podcast", batch_id=golden["batch_id"], producer_version="t",
                  created_at=build_golden.CREATED_AT, blobs=golden["blobs"])

    with pytest.raises(ContractError):
        write_batch(tmp_path, records=golden["records"], **kwargs)
    assert not (tmp_path / golden["batch_id"]).exists()
    assert not (tmp_path / f".tmp-{golden['batch_id']}").exists()

    write_batch(tmp_path, records=build_golden.podcast_batch()["records"], **kwargs)
    assert (tmp_path / golden["batch_id"]).is_dir()


@pytest.mark.parametrize("field, value", [
    ("observed_at", "2026-10-21"),
    ("observed_at", "2026-10-21T03:15:00"),
    ("observed_at", "yesterday"),
    ("observed_at", "2026-13-40T03:15:00Z"),
])
def test_rejects_timestamps_that_are_not_rfc3339_instants(batch: Path, field: str, value: str) -> None:
    rewrite_records(batch, lambda records: records[0].update({field: value}))

    with pytest.raises(ContractError, match="timestamp"):
        check(batch, schema=noop_schema)  # the oracle is authoritative, whatever format plugins are installed


def test_rejects_invalid_source_timestamp(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0]["source"].update({"retrieved_at": "2026-10-21 02:00"}))

    with pytest.raises(ContractError, match="source.retrieved_at"):
        check(batch, schema=noop_schema)  # the oracle is authoritative, whatever format plugins are installed


@pytest.mark.parametrize("name, document", [
    ("manifest", {"batch_id": "podcast-20261022-001\n"}),
    ("record", {"record_id": "podcast:podcast.episode:item-42\n"}),
])
def test_schema_patterns_reject_trailing_newline(name: str, document: dict) -> None:
    golden = GOLDEN / "podcast-20261022-001"
    base = (json.loads((golden / "manifest.json").read_text()) if name == "manifest"
            else json.loads((golden / "records.ndjson").read_bytes().splitlines()[0]))
    base.update(document)

    assert list(VALIDATORS[name].iter_errors(base))


def test_rejects_manifest_created_at_that_is_not_an_instant(batch: Path) -> None:
    manifest = json.loads((batch / "manifest.json").read_text())
    manifest["created_at"] = "2026-10-22"
    (batch / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")

    with pytest.raises(ContractError, match="manifest created_at"):
        check(batch, schema=noop_schema)  # the oracle is authoritative, whatever format plugins are installed


# --- Hardening for untrusted spools -----------------------------------------


def set_manifest(path: Path, **changes) -> None:
    manifest = json.loads((path / "manifest.json").read_text())
    manifest.update(changes)
    (path / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")


def set_raw_records(path: Path, data: bytes, **changes) -> None:
    (path / "records.ndjson").write_bytes(data)
    set_manifest(path, records_sha256=hashlib.sha256(data).hexdigest(), **changes)


def issachar_copy(tmp_path: Path) -> Path:
    target = tmp_path / "issachar-20261022-001"
    shutil.copytree(GOLDEN / "issachar-20261022-001", target)
    return target


def test_rejects_missing_records_file(batch: Path) -> None:
    (batch / "records.ndjson").unlink()

    with pytest.raises(ContractError):
        check(batch)


def test_rejects_records_file_that_is_a_directory(batch: Path) -> None:
    (batch / "records.ndjson").unlink()
    (batch / "records.ndjson").mkdir()

    with pytest.raises(ContractError):
        check(batch)


def test_rejects_subdirectory_in_blobs(batch: Path) -> None:
    (batch / "blobs" / "nested").mkdir()

    with pytest.raises(ContractError, match="regular file"):
        check(batch)


def test_rejects_missing_manifest(batch: Path) -> None:
    (batch / "manifest.json").unlink()

    with pytest.raises(ContractError):
        check(batch)


@pytest.mark.parametrize("payload", [b"[]\n", b"42\n", b"null\n"])
def test_rejects_manifest_that_is_not_an_object(batch: Path, payload: bytes) -> None:
    (batch / "manifest.json").write_bytes(payload)

    with pytest.raises(ContractError, match="manifest"):
        check(batch, schema=noop_schema)


def test_rejects_manifest_with_missing_keys(batch: Path) -> None:
    (batch / "manifest.json").write_bytes(b"{}\n")

    with pytest.raises(ContractError):
        check(batch, schema=noop_schema)


def test_rejects_malformed_record_fields_without_schema(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].pop("source"))

    with pytest.raises(ContractError, match="malformed record"):
        check(batch, schema=noop_schema)


def test_rejects_unhashable_kind_without_schema(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].update({"kind": ["x"]}))

    with pytest.raises(ContractError):
        check(batch, schema=noop_schema)


def test_rejects_non_object_record_line(batch: Path) -> None:
    set_raw_records(batch, b"[1]\n[2]\n")

    with pytest.raises(ContractError):
        check(batch, schema=noop_schema)


def test_rejects_non_json_record_line(batch: Path) -> None:
    set_raw_records(batch, b"not json\nalso not\n")

    with pytest.raises(ContractError, match="invalid JSON"):
        check(batch)


def test_rejects_non_canonical_record_line(batch: Path) -> None:
    lines = (batch / "records.ndjson").read_bytes().splitlines()
    spaced = json.dumps(json.loads(lines[0])).encode() + b"\n" + lines[1] + b"\n"
    set_raw_records(batch, spaced)

    with pytest.raises(ContractError, match="canonical"):
        check(batch)


def test_rejects_records_without_final_newline(batch: Path) -> None:
    set_raw_records(batch, (batch / "records.ndjson").read_bytes().rstrip(b"\n"))

    with pytest.raises(ContractError, match="LF"):
        check(batch)


def test_rejects_crlf_records(batch: Path) -> None:
    set_raw_records(batch, (batch / "records.ndjson").read_bytes().replace(b"\n", b"\r\n"))

    with pytest.raises(ContractError):
        check(batch)


def test_rejects_empty_batch_with_distinct_message(batch: Path) -> None:
    set_raw_records(batch, b"", record_count=0)

    with pytest.raises(ContractError, match="outside 1..10000"):
        check(batch, schema=noop_schema)


def test_rejects_record_count_mismatch_with_distinct_message(batch: Path) -> None:
    set_manifest(batch, record_count=1)

    with pytest.raises(ContractError, match="record_count does not match records.ndjson"):
        check(batch)


def test_rejects_symlinked_blob(batch: Path, tmp_path: Path) -> None:
    blob = next((batch / "blobs").iterdir())
    outside = tmp_path / "outside"
    outside.write_bytes(blob.read_bytes())
    blob.unlink()
    blob.symlink_to(outside)

    with pytest.raises(ContractError, match="regular file"):
        check(batch)


def test_rejects_symlinked_records_file(batch: Path, tmp_path: Path) -> None:
    outside = tmp_path / "records-elsewhere"
    outside.write_bytes((batch / "records.ndjson").read_bytes())
    (batch / "records.ndjson").unlink()
    (batch / "records.ndjson").symlink_to(outside)

    with pytest.raises(ContractError, match="regular file"):
        check(batch)


def test_rejects_symlinked_batch_root(batch: Path, tmp_path: Path) -> None:
    link = tmp_path / "podcast-20261022-009"
    link.symlink_to(batch, target_is_directory=True)

    with pytest.raises(ContractError, match="directory"):
        check(link)


def test_rejects_oversized_manifest(batch: Path) -> None:
    (batch / "manifest.json").write_bytes(b" " * 70_000 + (batch / "manifest.json").read_bytes())

    with pytest.raises(ContractError, match="too large"):
        check(batch)


def test_rejects_oversized_batch(batch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(knowledge_contract, "MAX_BATCH_BYTES", 10)

    with pytest.raises(ContractError, match="exceeds"):
        check(batch)


def test_rejects_extra_root_file(batch: Path) -> None:
    (batch / "notes.txt").write_text("x")

    with pytest.raises(ContractError, match="unexpected"):
        check(batch)


def test_rejects_unreferenced_blob(batch: Path) -> None:
    data = b"orphan"
    (batch / "blobs" / f"sha256-{hashlib.sha256(data).hexdigest()}").write_bytes(data)
    set_manifest(batch, blob_count=2)

    with pytest.raises(ContractError, match="not referenced"):
        check(batch)


def test_rejects_missing_blobs_directory_when_blobs_declared(batch: Path) -> None:
    shutil.rmtree(batch / "blobs")

    with pytest.raises(ContractError):
        check(batch)


def test_blobs_directory_may_be_absent_without_blobs(batch: Path) -> None:
    def strip(records):
        records[1]["content"] = None

    rewrite_records(batch, strip)
    shutil.rmtree(batch / "blobs")
    set_manifest(batch, blob_count=0)

    assert check(batch).record_count == 2


def test_validate_batch_requires_producer_and_schema(batch: Path) -> None:
    with pytest.raises(TypeError):
        validate_batch(batch)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        validate_batch(batch, expected_producer="podcast")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        validate_batch(batch, validate_schema=noop_schema)  # type: ignore[call-arg]


def test_summary_exposes_validated_records_read_only() -> None:
    summary = check(GOLDEN / "podcast-20261022-001")

    assert [r["record_id"] for r in summary.records] == list(summary.record_hashes)
    assert isinstance(summary.records, tuple)
    with pytest.raises(TypeError):
        summary.records[0]["title"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        summary.record_hashes["x"] = "y"  # type: ignore[index]


def test_schema_errors_do_not_leak_producer_values() -> None:
    class Violation(Exception):
        absolute_path = deque(["participants", 0])

    def fail(name: str, document: dict) -> None:
        raise Violation("SECRET producer value")

    with pytest.raises(ContractError) as info:
        check(GOLDEN / "podcast-20261022-001", schema=fail)

    assert "SECRET" not in str(info.value)
    assert "manifest schema violation" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_schema_error_includes_path_when_available(batch: Path) -> None:
    class Violation(Exception):
        def __init__(self) -> None:
            self.absolute_path = deque(["participants", 0])

    def fail(name: str, document: dict) -> None:
        if name == "record":
            raise Violation()

    with pytest.raises(ContractError, match="participants/0"):
        check(batch, schema=fail)


def test_error_messages_truncate_producer_values(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].update({"kind": "x" * 1000}))

    with pytest.raises(ContractError) as info:
        check(batch, schema=noop_schema)

    assert len(str(info.value)) < 600


def test_rejects_wrong_manifest_schema_version(batch: Path) -> None:
    set_manifest(batch, schema_version="knowledge-batch/v2")

    with pytest.raises(ContractError, match="schema_version"):
        check(batch, schema=noop_schema)


def test_rejects_directory_name_that_differs_from_manifest(batch: Path, tmp_path: Path) -> None:
    renamed = tmp_path / "podcast-20261022-002"
    batch.rename(renamed)

    with pytest.raises(ContractError, match="does not match directory"):
        check(renamed)


def test_rejects_wrong_record_schema_version(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].update({"schema_version": "knowledge-record/v2"}))

    with pytest.raises(ContractError, match="schema_version"):
        check(batch, schema=noop_schema)


def test_rejects_record_id_prefix_mismatch(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].update({"record_id": "issachar:podcast.episode:x"}))

    with pytest.raises(ContractError, match="must start with"):
        check(batch, schema=noop_schema)


@pytest.mark.parametrize("target", ["self", "other_kind"])
def test_rejects_bad_supersedes(batch: Path, target: str) -> None:
    def mutate(records):
        records[0]["supersedes"] = (records[0]["record_id"] if target == "self"
                                    else "podcast:podcast.segment:item-42/rev-2/seg-1")

    rewrite_records(batch, mutate)

    with pytest.raises(ContractError, match="supersedes"):
        check(batch)


def test_rejects_relation_to_self(batch: Path) -> None:
    def mutate(records):
        records[1]["relations"] = [{"type": "part_of", "target_record_id": records[1]["record_id"]}]

    rewrite_records(batch, mutate)

    with pytest.raises(ContractError, match="relate to itself"):
        check(batch)


def test_rejects_participants_on_issachar_records(tmp_path: Path) -> None:
    path = issachar_copy(tmp_path)
    person = build_golden.podcast_batch()["records"][0]["participants"]
    rewrite_records(path, lambda records: records[0].update({"participants": person}))

    with pytest.raises(ContractError, match="only podcast records"):
        check(path, producer="issachar", schema=noop_schema)


def test_rejects_entity_id_that_does_not_match_name(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0]["participants"][0].update({"entity_id": "person:someone-else"}))

    with pytest.raises(ContractError, match="entity_id"):
        check(batch)


@pytest.mark.parametrize("index, value", [(0, True), (1, True), (1, "0.9"), (0, None)])
def test_rejects_non_numeric_confidence(batch: Path, index: int, value) -> None:
    rewrite_records(batch, lambda records: records[0]["participants"][index].update({"confidence": value}))

    with pytest.raises(ContractError, match="confidence must be a number"):
        check(batch, schema=noop_schema)


def test_write_batch_rejects_blob_hash_mismatch(tmp_path: Path) -> None:
    golden = build_golden.podcast_batch()
    blobs = {sha: b"different" for sha in golden["blobs"]}

    with pytest.raises(ContractError, match="does not match its content"):
        write_batch(tmp_path, producer="podcast", batch_id=golden["batch_id"], producer_version="t",
                    created_at=build_golden.CREATED_AT, records=golden["records"], blobs=blobs)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("producer, batch_id", [
    ("podcast", "../escape"), ("podcast", "podcast-20261022-001/x"), ("podcast", "issachar-20261022-001"),
    ("podcast", "podcast-20261022-001\n"),
])
def test_write_batch_rejects_bad_batch_id_before_touching_disk(tmp_path: Path, producer: str, batch_id: str) -> None:
    golden = build_golden.podcast_batch()

    with pytest.raises(ContractError):
        write_batch(tmp_path / "inbox", producer=producer, batch_id=batch_id, producer_version="t",
                    created_at=build_golden.CREATED_AT, records=golden["records"], blobs=golden["blobs"])
    assert not (tmp_path / "inbox").exists()
    assert not (tmp_path / "escape").exists()


def test_write_batch_cleans_staging_on_unexpected_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    golden = build_golden.podcast_batch()

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(knowledge_contract, "validate_batch", boom)
    with pytest.raises(KeyboardInterrupt):
        write_batch(tmp_path, producer="podcast", batch_id=golden["batch_id"], producer_version="t",
                    created_at=build_golden.CREATED_AT, records=golden["records"], blobs=golden["blobs"])
    assert list(tmp_path.iterdir()) == []


def test_write_batch_syncs_staging_directories_before_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    golden = build_golden.podcast_batch()
    events: list[str] = []
    real_sync, real_rename = knowledge_contract._fsync_directory, os.rename
    monkeypatch.setattr(knowledge_contract, "_fsync_directory",
                        lambda p: (events.append(f"sync:{Path(p).relative_to(tmp_path)}"), real_sync(p))[1])
    monkeypatch.setattr(knowledge_contract.os, "rename",
                        lambda a, b: (events.append("rename"), real_rename(a, b))[1])

    write_batch(tmp_path, producer="podcast", batch_id=golden["batch_id"], producer_version="t",
                created_at=build_golden.CREATED_AT, records=golden["records"], blobs=golden["blobs"])

    staging = f".tmp-{golden['batch_id']}"
    assert events.index(f"sync:{staging}/blobs") < events.index(f"sync:{staging}") < events.index("rename")


# --- Second review round -----------------------------------------------------


def test_summary_records_serialise_to_their_recorded_hashes() -> None:
    summary = check(GOLDEN / "podcast-20261022-001")

    for record in summary.records:
        assert knowledge_contract.record_hash(record) == summary.record_hashes[record["record_id"]]
        assert json.loads(canonical_json(record))["record_id"] == record["record_id"]


def test_summary_records_are_deeply_read_only() -> None:
    summary = check(GOLDEN / "podcast-20261022-001")

    with pytest.raises(TypeError):
        summary.records[0]["source"]["uri"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        summary.records[0]["participants"][0]["name"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        summary.records[0]["participants"][0] = {}  # type: ignore[index]


@pytest.mark.parametrize("field", ["records_sha256", "record_count", "blob_count"])
def test_rejects_manifest_missing_integrity_fields(batch: Path, field: str) -> None:
    manifest = json.loads((batch / "manifest.json").read_text())
    del manifest[field]
    (batch / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")

    with pytest.raises(ContractError, match=field):
        check(batch, schema=noop_schema)


@pytest.mark.parametrize("field, value", [
    ("records_sha256", "abc"), ("records_sha256", 5), ("record_count", "2"), ("record_count", True),
    ("blob_count", None), ("blob_count", 1.5),
])
def test_rejects_manifest_integrity_fields_of_wrong_type(batch: Path, field: str, value) -> None:
    set_manifest(batch, **{field: value})

    with pytest.raises(ContractError, match=field):
        check(batch, schema=noop_schema)


def test_records_read_is_bounded(tmp_path: Path) -> None:
    big = tmp_path / "records.ndjson"
    big.write_bytes(b"x" * 100)

    with pytest.raises(ContractError, match="too large"):
        knowledge_contract._read_regular(big, "w", 5)


def test_records_lstat_failure_is_a_contract_error(batch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.lstat

    def flaky(self, *args, **kwargs):
        if self.name == "records.ndjson":
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", flaky)

    with pytest.raises(ContractError):
        check(batch)


def test_write_batch_leaves_a_preexisting_staging_directory_untouched(tmp_path: Path) -> None:
    golden = build_golden.podcast_batch()
    staging = tmp_path / f".tmp-{golden['batch_id']}"
    staging.mkdir()
    (staging / "keep.txt").write_text("mine")

    with pytest.raises(ContractError, match="staging"):
        write_batch(tmp_path, producer="podcast", batch_id=golden["batch_id"], producer_version="t",
                    created_at=build_golden.CREATED_AT, records=golden["records"], blobs=golden["blobs"])
    assert (staging / "keep.txt").read_text() == "mine"


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions are not enforced for root")
def test_unreadable_blob_error_names_the_batch(batch: Path) -> None:
    blob = next((batch / "blobs").iterdir())
    blob.chmod(0)

    with pytest.raises(ContractError, match="podcast-20261022-001"):
        check(batch)


def test_schema_rejects_confidential_sensitivity(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0].update({"sensitivity": "confidential"}))

    with pytest.raises(ContractError, match="record schema violation"):
        check(batch)


def test_kind_tables_are_pinned() -> None:
    schema = json.loads((ROOT / "schemas/knowledge-record-v1.schema.json").read_text())
    kinds = set(schema["properties"]["kind"]["enum"])

    assert len(kinds) == 9
    assert set(knowledge_contract.KIND_PRODUCERS) == kinds
    assert knowledge_contract.EBENEZER_KINDS == {
        "podcast.episode",
        "issachar.theme_version",
        "issachar.forecast_contract",
        "issachar.forecast",
        "issachar.resolution",
        "issachar.calibration",
    }


# --- Plan 2: podcast platform ------------------------------------------------


@pytest.mark.parametrize("schema, message", [
    (validate_schema, "record schema violation"),
    (noop_schema, "attributes.platform"),
])
def test_rejects_podcast_record_without_platform(batch: Path, schema, message: str) -> None:
    rewrite_records(batch, lambda records: records[1]["attributes"].pop("platform", None))

    with pytest.raises(ContractError, match=message):
        check(batch, schema=schema)


@pytest.mark.parametrize("schema, message", [
    (validate_schema, "record schema violation"),
    (noop_schema, "attributes.platform"),
])
def test_rejects_unknown_podcast_platform(batch: Path, schema, message: str) -> None:
    rewrite_records(batch, lambda records: records[0]["attributes"].update({"platform": "vimeo"}))

    with pytest.raises(ContractError, match=message):
        check(batch, schema=schema)


def test_accepts_youtube_platform(batch: Path) -> None:
    def to_youtube(records):
        for record in records:
            record["attributes"]["platform"] = "youtube"

    rewrite_records(batch, to_youtube)

    assert check(batch).record_count == 2


def test_rejects_unhashable_platform_without_schema(batch: Path) -> None:
    rewrite_records(batch, lambda records: records[0]["attributes"].update({"platform": ["podcast"]}))

    with pytest.raises(ContractError, match="malformed record"):
        check(batch, schema=noop_schema)


def test_platforms_are_pinned() -> None:
    schema = json.loads((ROOT / "schemas/knowledge-record-v1.schema.json").read_text())
    platform = schema["then"]["properties"]["attributes"]["properties"]["platform"]

    assert knowledge_contract.PLATFORMS == {"podcast", "youtube"}
    assert set(platform["enum"]) == knowledge_contract.PLATFORMS
    assert set(schema["if"]["properties"]["kind"]["enum"]) == knowledge_contract.PARTICIPANT_KINDS


# --- Plan 6: podcast exporter coverage ---------------------------------------


def test_podcast_followup_golden_covers_youtube_shared_blob_and_supersession() -> None:
    summary = check(GOLDEN / "podcast-20261023-001")
    records = {record["record_id"]: record for record in summary.records}
    youtube = [record for record in summary.records if record["attributes"]["platform"] == "youtube"]
    shared = {record["content"]["sha256"] for record in youtube if record["content"] is not None}

    assert summary.record_count == 5
    assert {record["attributes"]["platform"] for record in summary.records} == {"podcast", "youtube"}
    assert len(youtube) == 3 and len(shared) == 1
    assert len(list((GOLDEN / "podcast-20261023-001/blobs").iterdir())) == 2
    replaced = records["podcast:podcast.episode:item-42/rev-4/e1"]["supersedes"]
    assert replaced == "podcast:podcast.episode:item-42/rev-3"
    assert replaced in check(GOLDEN / "podcast-20261022-001").record_hashes
