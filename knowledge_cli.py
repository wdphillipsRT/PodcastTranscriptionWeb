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
            .order_by(ContentSource.id)):
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
