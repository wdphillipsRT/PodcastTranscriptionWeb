"""Nightly knowledge exporter: new and changed items become batches in the spool inbox.

An item is exported when it is completed, its active classification revision is
completed, its classifying source names a host, and its episode facts differ
from its last live export (or it has none). Transcript text is read only
through `load_active_learning_projection`, so only editorial segments leave
the application. A changed item gets a new export generation whose episode
record supersedes the previous one; record identifiers are never reused.

Export state lives in `knowledge_export_batches` and `knowledge_item_exports`.
A batch is reserved (state `pending`, with its item rows) and committed before
`write_podcast_batch` publishes it, then marked `written`. A `pending` batch
left by a crash is reconciled on the next run: present in the inbox means
`written`; otherwise `abandoned` (its own staging directory is removed), and
its items are exported again under a new generation.
"""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
import fcntl
import logging
import os
from pathlib import Path
import shutil
import stat

import sqlalchemy as sa

from integrations.knowledge.contract import ContractError, canonical_json, person_entity_id, write_podcast_batch
from integrations.knowledge.people import guest_input_sha256, has_host, item_guests, participants, source_hosts
from integrations.knowledge.records import (
    build_records, episode_facts, episode_native_id, payload_sha256, publisher_name, timestamp,
)
from learning_projection import LearningProjectionUnavailable, load_active_learning_projection
from models import (ClassificationRevision, ContentItem, ContentSource, GuestExtraction, KnowledgeExportBatch,
                    KnowledgeItemExport, utc_now)


PRODUCER_VERSION = "podcast-transcription-web/knowledge-exporter-1"
LOCK_NAME = ".podcast-knowledge-export.lock"
MAX_BATCH_PAYLOAD_BYTES = 900 * 1024 * 1024  # Headroom under the contract's 1 GiB.
LIVE_STATES = ("pending", "written")
log = logging.getLogger(__name__)


class ExportFailed(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass
class ExportReport:
    status: str = "completed"
    reason: str | None = None
    batches: list = field(default_factory=list)
    items: int = 0
    records: int = 0
    skipped: dict = field(default_factory=dict)
    reconciled: dict = field(default_factory=lambda: {"written": 0, "abandoned": 0})

    def as_dict(self):
        return {"status": self.status, "reason": self.reason, "batches": list(self.batches), "items": self.items,
                "records": self.records, "skipped": dict(self.skipped), "reconciled": dict(self.reconciled)}


@dataclass(frozen=True)
class _Plan:
    item_id: int
    revision_id: int
    source_id: int
    policy_sha256: str
    facts: dict
    digest: str
    generation: int
    supersedes: str | None


def _preflight(inbox):
    """The inbox must be a writable, setgid directory owned by one of our groups."""
    try:
        info = os.stat(inbox)
    except OSError:
        return "inbox_missing"
    if not stat.S_ISDIR(info.st_mode):
        return "inbox_missing"
    if not os.access(inbox, os.W_OK | os.X_OK):
        return "inbox_unwritable"
    if not info.st_mode & stat.S_ISGID or info.st_gid not in {*os.getgroups(), os.getegid()}:
        return "inbox_group"
    return None


@contextmanager
def _locked(inbox):
    descriptor = os.open(inbox / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o660)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def _reconcile(session, inbox):
    counts = {"written": 0, "abandoned": 0}
    for batch in session.scalars(sa.select(KnowledgeExportBatch).where(KnowledgeExportBatch.state == "pending")
                                 .order_by(KnowledgeExportBatch.id)):
        if (inbox / batch.batch_id).is_dir():
            batch.state, batch.written_at = "written", utc_now()
            counts["written"] += 1
        else:
            shutil.rmtree(inbox / f".tmp-{batch.batch_id}", ignore_errors=True)
            batch.state, batch.error_category = "abandoned", "unconfirmed_write"
            counts["abandoned"] += 1
    session.commit()
    return counts


def _candidates(session, since, until):
    query = sa.select(ContentItem.id).join(
        ClassificationRevision, ClassificationRevision.id == ContentItem.active_classification_revision_id).where(
        ContentItem.status == "completed", ClassificationRevision.state == "completed")
    if since is not None:
        query = query.where(ContentItem.published_at >= datetime.combine(since, time(), timezone.utc))
    if until is not None:
        query = query.where(ContentItem.published_at < datetime.combine(until + timedelta(days=1), time(), timezone.utc))
    identities = list(session.scalars(query.order_by(ContentItem.id)))
    session.commit()
    return identities


def _plan(session, item_id, settings):
    """Decide from the database alone whether an item needs a new export."""
    item = session.get(ContentItem, item_id)
    revision = session.get(ClassificationRevision, item.active_classification_revision_id)
    source = session.get(ContentSource, revision.source_id)
    hosts, basis = source_hosts(source)
    if not hosts:
        return "no_host"
    try:
        person_entity_id(publisher_name(item, source))
    except ContractError:
        return "publisher_unnamed"
    extraction = session.scalar(sa.select(GuestExtraction).where(
        GuestExtraction.item_id == item.id, GuestExtraction.input_sha256 == guest_input_sha256(item)))
    if (settings.GUEST_EXTRACTION_ENABLED and item.admin_guests is None and not item.feed_guests
            and extraction is None):
        return "awaiting_guests"
    guests = item_guests(item, extraction)
    people = participants(hosts, basis, guests)
    if not has_host(people):
        return "no_host"
    inferred = extraction.created_at if extraction and any(g[1] == "inferred" for g in guests) else None
    observed = max(value for value in (revision.completed_at, source.hosts_updated_at, item.guests_updated_at,
                                       inferred) if value is not None)
    facts = episode_facts(item, source, revision, people, observed_at=observed)
    digest = payload_sha256(facts)
    last = session.scalar(sa.select(KnowledgeItemExport).join(KnowledgeExportBatch).where(
        KnowledgeItemExport.item_id == item.id, KnowledgeExportBatch.state.in_(LIVE_STATES))
        .order_by(KnowledgeItemExport.id.desc()).limit(1))
    if last is not None and last.classification_revision_id == revision.id and last.payload_sha256 == digest:
        return "unchanged"
    generation = 1 + (session.scalar(sa.select(sa.func.max(KnowledgeItemExport.generation)).where(
        KnowledgeItemExport.item_id == item.id,
        KnowledgeItemExport.classification_revision_id == revision.id)) or 0)
    return _Plan(item.id, revision.id, source.id, revision.policy_sha256, facts, digest, generation,
                 last.episode_record_id if last is not None else None)


def _batch_id(session, inbox, now):
    run_date = now.astimezone(timezone.utc).strftime("%Y%m%d")
    sequence = 1 + (session.scalar(sa.select(sa.func.max(KnowledgeExportBatch.sequence)).where(
        KnowledgeExportBatch.run_date == run_date)) or 0)
    # Never reuse a name that is already present in the inbox for any reason.
    while (inbox / f"podcast-{run_date}-{sequence:03d}").exists() or (
            inbox / f".tmp-podcast-{run_date}-{sequence:03d}").exists():
        sequence += 1
    if sequence > 999:
        raise ExportFailed("sequence_exhausted")
    return f"podcast-{run_date}-{sequence:03d}", run_date, sequence


def _flush(session, inbox, now, pending):
    records = [record for entry in pending for record in entry["records"]]
    blobs = {digest: data for entry in pending for digest, data in entry["blobs"].items()}
    batch_id, run_date, sequence = _batch_id(session, inbox, now)
    batch = KnowledgeExportBatch(batch_id=batch_id, run_date=run_date, sequence=sequence, state="pending",
                                 record_count=len(records), blob_count=len(blobs))
    session.add(batch)
    session.flush()
    for entry in pending:
        plan = entry["plan"]
        session.add(KnowledgeItemExport(export_batch_id=batch.id, item_id=plan.item_id,
            classification_revision_id=plan.revision_id, generation=plan.generation,
            episode_record_id=entry["records"][0]["record_id"], payload_sha256=plan.digest,
            passage_count=len(entry["records"]) - 1, supersedes_record_id=plan.supersedes))
    session.commit()  # The reservation precedes the write; see _reconcile.
    previous = os.umask(0o007)  # Group intake-podcast (setgid inbox) may read and move the batch.
    try:
        write_podcast_batch(inbox, batch_id=batch_id, producer_version=PRODUCER_VERSION,
                            created_at=timestamp(now), records=records, blobs=blobs)
    except (ContractError, OSError) as error:
        batch.state = "failed"
        batch.error_category = "contract_error" if isinstance(error, ContractError) else "write_failed"
        session.commit()
        raise ExportFailed(batch.error_category) from None
    finally:
        os.umask(previous)
    batch.state, batch.written_at = "written", utc_now()
    session.commit()
    return batch_id, len(records)


def export_knowledge(*, session, storage, settings, inbox, now, since=None, until=None, max_batches=None):
    """Export new and changed items into at most `max_batches` sequenced batches.

    Never raises for an unusable inbox, a disabled exporter, or a concurrent run:
    those return a `skipped` report and log a warning, so transcription and
    classification are never blocked by the knowledge intake.
    """
    report = ExportReport()
    inbox = Path(inbox)
    reason = "disabled" if not settings.KNOWLEDGE_EXPORT_ENABLED else _preflight(inbox)
    if reason is None:
        with _locked(inbox) as acquired:
            if acquired:
                _export(session, storage, settings, inbox, now, since, until,
                        max_batches or settings.KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN, report)
            else:
                reason = "busy"
    if reason is not None:
        report.status, report.reason = "skipped", reason
        log.warning("knowledge_export_skipped", extra={"knowledge_export": report.as_dict()})
    else:
        log.info("knowledge_export_" + report.status, extra={"knowledge_export": report.as_dict()})
    return report


def _export(session, storage, settings, inbox, now, since, until, max_batches, report):
    report.reconciled = _reconcile(session, inbox)
    skipped = Counter()
    limit = settings.KNOWLEDGE_EXPORT_MAX_RECORDS_PER_BATCH
    pending, pending_records, pending_bytes = [], 0, 0
    try:
        for item_id in _candidates(session, since, until):
            if len(report.batches) == max_batches:
                break
            plan = _plan(session, item_id, settings)
            session.commit()  # No database transaction is held across storage reads.
            if isinstance(plan, str):
                skipped[plan] += 1
                continue
            try:
                projection = load_active_learning_projection(session=session, storage=storage, item_id=item_id)
            except LearningProjectionUnavailable:
                skipped["projection_unavailable"] += 1
                continue
            if (projection.source_id, projection.policy_sha256) != (plan.source_id, plan.policy_sha256):
                skipped["changed_during_export"] += 1
                continue
            records, blobs = build_records(plan.facts, generation=plan.generation, supersedes=plan.supersedes,
                                           segments=projection.segments)
            size = sum(len(canonical_json(record)) + 1 for record in records) + sum(map(len, blobs.values()))
            if len(records) > limit or size > MAX_BATCH_PAYLOAD_BYTES:
                skipped["too_large"] += 1
                continue
            if pending and (pending_records + len(records) > limit or pending_bytes + size > MAX_BATCH_PAYLOAD_BYTES):
                _record(report, _flush(session, inbox, now, pending), len(pending))
                pending, pending_records, pending_bytes = [], 0, 0
                if len(report.batches) == max_batches:
                    break
            pending.append({"plan": plan, "records": records, "blobs": blobs})
            pending_records += len(records)
            pending_bytes += size
        if pending and len(report.batches) < max_batches:
            _record(report, _flush(session, inbox, now, pending), len(pending))
    except ExportFailed as error:
        report.status, report.reason = "failed", error.reason
    report.skipped = dict(skipped)


def _record(report, flushed, items):
    batch_id, records = flushed
    report.batches.append(batch_id)
    report.records += records
    report.items += items
