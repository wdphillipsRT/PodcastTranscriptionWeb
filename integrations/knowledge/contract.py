"""This application's binding to the vendored KNOWLEDGE-INTAKE-0001 contract.

Runtime code reaches the reference oracle only through this module. The files
under `vendor/` are byte-for-byte copies of bezalel-rfc, pinned by `vendor/PIN`
and refreshed only with `scripts/vendor_knowledge_contract.py`.
"""

from collections.abc import Mapping
from functools import cache
import json
from pathlib import Path

# vendor/ and vendor/reference/ import as namespace packages: an __init__.py there
# would be an unpinned file and fail the PIN exactness test.
from integrations.knowledge.vendor.reference.knowledge_contract import (
    MAX_BATCH_BYTES, MAX_RECORDS, PLATFORMS, BatchSummary, ContractError, canonical_json, check_record,
    person_entity_id, record_hash, validate_batch, write_batch,
)

__all__ = [
    "MAX_BATCH_BYTES", "MAX_RECORDS", "PLATFORMS", "PRODUCER", "VENDOR_ROOT", "BatchSummary",
    "ContractError", "canonical_json", "check_podcast_record", "person_entity_id", "record_hash", "schema_validator",
    "validate_podcast_batch", "write_podcast_batch",
]

PRODUCER = "podcast"
VENDOR_ROOT = Path(__file__).resolve().parent / "vendor"
_SCHEMAS = {
    "manifest": "schemas/knowledge-batch-manifest-v1.schema.json",
    "record": "schemas/knowledge-record-v1.schema.json",
}


@cache
def schema_validator():
    """A real JSON Schema 2020-12 validator for both contract documents."""
    from jsonschema import Draft202012Validator, FormatChecker

    validators = {
        name: Draft202012Validator(json.loads((VENDOR_ROOT / path).read_text(encoding="utf-8")),
                                   format_checker=FormatChecker())
        for name, path in _SCHEMAS.items()
    }

    def validate(name: str, document: Mapping) -> None:
        validators[name].validate(document)

    return validate


def check_podcast_record(record: Mapping) -> None:
    """Validate one podcast record alone: the real schema, then the oracle's semantic checks.

    Raises only ContractError, naming where a violation is but never its value.
    """
    from jsonschema import ValidationError

    try:
        schema_validator()("record", record)
    except ValidationError as error:
        where = "/".join(str(part) for part in error.absolute_path)
        raise ContractError(f"record schema violation{f' at {where}' if where else ''}") from None
    try:
        check_record(record, PRODUCER)
    except (KeyError, TypeError, AttributeError):
        raise ContractError("malformed record") from None


def validate_podcast_batch(path: Path) -> BatchSummary:
    """Validate one podcast batch directory with the oracle and the real schemas."""
    return validate_batch(path, expected_producer=PRODUCER, validate_schema=schema_validator())


def write_podcast_batch(inbox: Path, *, batch_id, producer_version, created_at, records, blobs) -> Path:
    """Atomically publish one podcast batch, schema-validated, into the spool inbox."""
    return write_batch(inbox, producer=PRODUCER, batch_id=batch_id, producer_version=producer_version,
                       created_at=created_at, records=records, blobs=blobs,
                       validate_schema=schema_validator())
