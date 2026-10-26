"""Private operator controls. Output is deliberately limited to safe aggregates."""
from functools import wraps
from types import SimpleNamespace

import click
from flask import current_app
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
            raise click.ClickException("Classification operation failed") from None
    return call


def _integer(value, *, maximum=2**31 - 1):
    try:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError
        result = int(value)
        if not 1 <= result <= maximum:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise click.ClickException("Invalid numeric option") from None


@click.group("classification")
@with_appcontext
def classification_cli():
    """Inspect and operate transcript classification without revealing content."""


@classification_cli.command("status")
@_safe
def status():
    import sqlalchemy as sa
    from extensions import db
    from datetime import timezone
    from models import ClassificationBlock, ClassificationRevision, CLASSIFICATION_STATES
    from tasks import _missing_classification_query
    counts = {state: count for state, count in db.session.execute(sa.select(ClassificationRevision.state, sa.func.count())
        .group_by(ClassificationRevision.state))}
    review = db.session.scalar(sa.select(sa.func.count()).select_from(ClassificationRevision)
        .where(ClassificationRevision.review_required.is_(True)))
    missing = db.session.scalar(sa.select(sa.func.count()).select_from(_missing_classification_query().subquery()))
    # Busy retries never end by design; these show how many wait and since when.
    busy = db.session.scalar(sa.select(sa.func.count()).select_from(ClassificationRevision).where(
        ClassificationRevision.state == "retrying", ClassificationRevision.error_category == "router_busy"))
    sent, updated = db.session.execute(sa.select(sa.func.min(ClassificationBlock.request_generation_started_at),
        sa.func.min(ClassificationBlock.updated_at)).where(ClassificationBlock.state == "pending",
        ClassificationBlock.error_category == "router_busy")).one()
    db.session.commit()

    def stamp(value):
        return "none" if value is None else (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    click.echo(" ".join(f"{state}={counts.get(state, 0)}" for state in CLASSIFICATION_STATES)
        + f" review_required={review} missing_current_policy={missing}"
        + f" router_busy={busy} oldest_busy_key_sent={stamp(sent)} oldest_busy_updated={stamp(updated)}")


@classification_cli.command("enqueue-backlog")
@click.option("--limit", default="100", metavar="1-100")
@_safe
def enqueue_backlog(limit):
    from tasks import reconcile_classification_batch
    counts = reconcile_classification_batch(limit=_integer(limit, maximum=100), create_only=True)
    click.echo(" ".join(f"{name}={counts[name]}" for name in ("scanned", "created", "published", "failed")))


@classification_cli.command("retry")
@click.option("--item-id", required=True)
@_safe
def retry(item_id):
    from extensions import db
    from models import ContentItem, ClassificationRevision
    from tasks import _item_lock, get_classification_service, _revision_dispatch, _publish_classification
    identity = _integer(item_id)
    with _item_lock(identity) as acquired:
        if not acquired:
            raise click.ClickException("Classification work is busy")
        item = db.session.get(ContentItem, identity)
        if item is None or item.status != "completed":
            raise click.ClickException("Completed item required")
        failed = db.session.query(ClassificationRevision).filter_by(item_id=identity,
            transcript_sha256=item.transcript_sha256, state="failed").order_by(ClassificationRevision.id.desc()).first()
        if failed is None:
            raise click.ClickException("Failed classification required")
        key = f"manual_retry:{failed.id}"
        db.session.commit()
        revision_id = get_classification_service().ensure_revision(identity, "manual_retry", idempotency_key=key)
        revision = db.session.get(ClassificationRevision, revision_id)
        dispatch = _revision_dispatch(revision) if revision.error_category == "dispatch_pending" else None
        db.session.commit()
    published = _publish_classification(dispatch)
    click.echo(f"requested=1 published={int(published)}")


@classification_cli.command("auth-smoke")
@click.option("--acknowledge-claude-cost", is_flag=True)
@_safe
def auth_smoke(acknowledge_claude_cost):
    if not acknowledge_claude_cost:
        raise click.ClickException("Claude cost acknowledgement required")
    from claude_classifier import ClaudeClassifier
    from commercial_signals import ClassifierWindow
    from transcripts import TranscriptSegment
    settings = SimpleNamespace(**dict(current_app.config))
    # This fixture is compiled in and has no connection to database/storage data.
    segment = TranscriptSegment("seg-000001-aaaaaaaaaaaaaaaa", "a" * 64, 0.0, 1.0,
        "This synthetic example explains how rain forms from clouds.", "https://example.com/synthetic#t=0,1")
    window = ClassifierWindow(1, (segment,), (), (), ())
    result = ClaudeClassifier(settings).classify(window)
    click.echo(f"cli={result.provenance.cli_version} model={result.provenance.resolved_model} "
        f"cost_microusd={result.total_cost_microusd}")


@classification_cli.command("smoke")
@click.option("--item-id")
@click.option("--oldest-completed", is_flag=True)
@click.option("--acknowledge-claude-cost", is_flag=True)
@_safe
def smoke(item_id, oldest_completed, acknowledge_claude_cost):
    if not acknowledge_claude_cost:
        raise click.ClickException("Claude cost acknowledgement required")
    if (item_id is not None) == oldest_completed:
        raise click.ClickException("Select exactly one completed item")
    from extensions import db
    from models import ContentItem, ClassificationRevision
    from commercial_policy import MAX_BLOCKS_PER_REVISION
    from tasks import _item_lock, get_classification_service, _revision_dispatch, _classification_step
    identity = _integer(item_id) if item_id is not None else db.session.query(ContentItem.id).filter_by(
        status="completed").order_by(ContentItem.created_at, ContentItem.id).limit(1).scalar()
    db.session.commit()
    if identity is None:
        raise click.ClickException("Completed item required")
    with _item_lock(identity) as acquired:
        if not acquired:
            raise click.ClickException("Classification work is busy")
        item = db.session.get(ContentItem, identity)
        if item is None or item.status != "completed":
            raise click.ClickException("Completed item required")
        db.session.commit()
        service = get_classification_service()
        revision_id = service.ensure_revision(identity, "live_smoke")
        revision = db.session.get(ClassificationRevision, revision_id)
        dispatch = _revision_dispatch(revision)
        db.session.commit()
        for _ in range(MAX_BLOCKS_PER_REVISION + 2):
            if dispatch is None:
                break
            dispatch, _, _ = _classification_step(service, revision_id, dispatch.phase,
                dispatch.block_ordinal, handoff_superseded=False)
        revision = db.session.get(ClassificationRevision, revision_id, populate_existing=True)
        if revision.state not in {"completed", "failed", "superseded"}:
            db.session.commit()
            raise click.ClickException("Classification smoke did not complete")
        output = (f"state={revision.state} included={revision.included_segments} excluded={revision.excluded_segments} "
            f"uncertain={revision.uncertain_segments} cost_microusd={revision.total_cost_microusd}")
        db.session.commit()
    click.echo(output)
