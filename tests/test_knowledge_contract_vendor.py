"""KNOWLEDGE-INTAKE-0001 section 6: the vendored contract conforms.

1. The vendored files match PIN, and PIN lists every vendored file.
2. Every golden batch is accepted with a real JSON Schema validator.
3. The negative cases of bezalel-rfc tests/test_knowledge_contract.py pass,
   by running the vendored copy of that suite against the vendored oracle.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from integrations.knowledge.contract import (
    PLATFORMS, VENDOR_ROOT, ContractError, canonical_json, validate_podcast_batch,
)

GOLDEN = VENDOR_ROOT / "tests/fixtures/knowledge/golden"
REQUIRED = {
    "reference/knowledge_contract.py",
    "schemas/knowledge-batch-manifest-v1.schema.json",
    "schemas/knowledge-record-v1.schema.json",
    "tests/test_knowledge_contract.py",
    "tests/fixtures/knowledge/build_golden.py",
    "tests/fixtures/knowledge/golden/podcast-20261022-001/manifest.json",
    "tests/fixtures/knowledge/golden/podcast-20261023-001/manifest.json",
    "tests/fixtures/knowledge/golden/issachar-20261022-001/manifest.json",
}


def read_pin():
    commit, files = "", {}
    for line in (VENDOR_ROOT / "PIN").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("source_repository "):
            continue
        if line.startswith("source_commit "):
            commit = line.removeprefix("source_commit ")
            continue
        digest, path = line.split("  ", 1)
        files[path] = digest
    return commit, files


def vendored_files():
    return {path.relative_to(VENDOR_ROOT).as_posix() for path in VENDOR_ROOT.rglob("*")
            if path.is_file() and path.name != "PIN" and "__pycache__" not in path.parts}


def test_pin_names_the_source_commit():
    commit, files = read_pin()
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert REQUIRED <= set(files)


def test_vendored_files_match_pin_exactly():
    _, files = read_pin()
    assert vendored_files() == set(files)
    for path, digest in files.items():
        assert hashlib.sha256((VENDOR_ROOT / path).read_bytes()).hexdigest() == digest, path


def test_vendored_oracle_has_the_podcast_platform_rule():
    assert PLATFORMS == {"podcast", "youtube"}


@pytest.mark.parametrize("batch_id", ["podcast-20261022-001", "podcast-20261023-001"])
def test_podcast_golden_batches_are_accepted_with_a_real_schema_validator(batch_id):
    summary = validate_podcast_batch(GOLDEN / batch_id)
    assert summary.batch_id == batch_id
    assert summary.producer == "podcast"


def test_schema_validator_is_real(tmp_path):
    batch = tmp_path / "podcast-20261022-001"
    shutil.copytree(GOLDEN / batch.name, batch)
    records = [json.loads(line) for line in (batch / "records.ndjson").read_bytes().splitlines()]
    records[0]["sensitivity"] = "confidential"  # Only the JSON Schema rejects this value.
    lines = b"".join(canonical_json(record) + b"\n" for record in records)
    (batch / "records.ndjson").write_bytes(lines)
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    manifest["records_sha256"] = hashlib.sha256(lines).hexdigest()
    (batch / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    with pytest.raises(ContractError, match="record schema violation"):
        validate_podcast_batch(batch)


def test_vendored_negative_cases_pass_against_the_vendored_oracle(tmp_path):
    suite = VENDOR_ROOT / "tests/test_knowledge_contract.py"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(suite)],
        capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    summary = result.stdout.strip().splitlines()[-1]
    assert result.returncode == 0, result.stdout[-2000:]
    assert re.search(r"\b\d+ passed\b", summary) and "failed" not in summary, summary


def test_default_collection_skips_the_vendored_suite():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "integrations"],
        capture_output=True, text=True, cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert "test_knowledge_contract.py" not in result.stdout
