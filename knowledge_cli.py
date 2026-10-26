"""Owner commands for the knowledge exporter. Output is limited to safe aggregates."""

from collections import Counter
from functools import wraps

import click
from flask.cli import with_appcontext


def _safe(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except click.ClickException:
            raise
        except Exception:
            from extensions import db
            db.session.rollback()
            raise click.ClickException("Knowledge operation failed") from None
    return call


@click.group("knowledge")
@with_appcontext
def knowledge_cli():
    """Prepare and export knowledge batches for the Platform intake."""


@knowledge_cli.command("seed-hosts")
@_safe
def seed_hosts():
    """Seed missing YouTube hosts from stored channel data.

    Podcast feeds refresh their declared hosts on every discovery run.
    """
    import sqlalchemy as sa
    from extensions import db
    from ingestion_types import person_names
    from models import ContentItem, ContentSource, SourceItem, utc_now
    seeded = 0
    for source in db.session.scalars(sa.select(ContentSource).where(
            ContentSource.feed_hosts.is_(None), ContentSource.source_type.like("youtube_%"))
            .order_by(ContentSource.id).with_for_update(of=ContentSource)):  # discovery may write feed_hosts
        if source.source_type == "youtube_channel":
            names = [source.name]
        else:
            channels = Counter(db.session.scalars(sa.select(ContentItem.channel_name).join(SourceItem).where(
                SourceItem.source_id == source.id, ContentItem.channel_name.is_not(None))))
            names = [name for name, _ in channels.most_common(1)]
        hosts = person_names(names, limit=1)
        if hosts:
            source.feed_hosts = list(hosts)
            if source.admin_hosts is None:
                source.hosts_updated_at = utc_now()
            seeded += 1
    db.session.commit()
    missing = db.session.scalar(sa.select(sa.func.count()).select_from(ContentSource).where(
        ContentSource.feed_hosts.is_(None), ContentSource.admin_hosts.is_(None)))
    db.session.commit()
    click.echo(f"seeded={seeded} missing={missing}")


def _date(value):
    from datetime import date
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise click.ClickException("Dates must be YYYY-MM-DD") from None


def _batches(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 999:
        raise click.ClickException("--max-batches must be 1 to 999")
    return int(value)


@knowledge_cli.command("export")
@click.option("--since", default=None, metavar="YYYY-MM-DD", help="First publication date (inclusive).")
@click.option("--until", default=None, metavar="YYYY-MM-DD", help="Last publication date (inclusive).")
@click.option("--max-batches", default="20", metavar="1-999", show_default=True)
@_safe
def export(since, until, max_batches):
    """Export new and changed items now, for example to backfill the library.

    Writes ordinary sequenced batches through the same state as the nightly
    task, so items already exported are never repeated. Without --since or
    --until every eligible item is a candidate, including undated ones.
    """
    from types import SimpleNamespace
    from flask import current_app
    from extensions import db
    from integrations.knowledge.exporter import export_knowledge
    from models import utc_now
    from tasks import get_processing_storage
    start, end, limit = _date(since), _date(until), _batches(max_batches)
    if start and end and start > end:
        raise click.ClickException("--since must not be after --until")
    settings = SimpleNamespace(**dict(current_app.config))
    report = export_knowledge(session=db.session, storage=get_processing_storage(), settings=settings,
        inbox=settings.KNOWLEDGE_EXPORT_INBOX, now=utc_now(), since=start, until=end, max_batches=limit)
    skipped = "".join(f" {reason}={count}" for reason, count in sorted(report.skipped.items()))
    click.echo(f"status={report.status} reason={report.reason or '-'} batches={len(report.batches)} "
               f"items={report.items} records={report.records}{skipped}")
    if report.status == "failed":
        raise SystemExit(1)


@knowledge_cli.command("status")
@_safe
def status():
    """Count export batches by state, exported items, and sources without a host."""
    import sqlalchemy as sa
    from extensions import db
    from models import KNOWLEDGE_BATCH_STATES, ContentSource, KnowledgeExportBatch, KnowledgeItemExport
    states = dict(db.session.execute(sa.select(KnowledgeExportBatch.state, sa.func.count())
                                     .group_by(KnowledgeExportBatch.state)).all())
    items = db.session.scalar(sa.select(sa.func.count(sa.distinct(KnowledgeItemExport.item_id)))
        .join(KnowledgeExportBatch).where(KnowledgeExportBatch.state == "written"))
    hostless = db.session.scalar(sa.select(sa.func.count()).select_from(ContentSource).where(
        ContentSource.feed_hosts.is_(None), ContentSource.admin_hosts.is_(None)))
    db.session.commit()
    click.echo(" ".join(f"batches_{state}={states.get(state, 0)}" for state in KNOWLEDGE_BATCH_STATES)
               + f" items_exported={items} sources_without_host={hostless}")
