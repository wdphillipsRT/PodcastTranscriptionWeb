"""Private, owner-scoped workspace. Never serialize operational ORM records."""
from datetime import date
import json
import math
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from extensions import db
from ingestion_types import SourceDescriptor, normalize_public_url
from models import (BackfillRequest, ContentItem, ContentSource, DiscoveryRun,
                    ProcessingJob, SourceItem, SOURCE_TYPES, utc_now)
from s3_utils import get_s3_manager
from source_classifier import CHANNEL_ID, PLAYLIST_ID, classify_source_url, youtube_locator


workspace = Blueprint("workspace", __name__)
SOURCE_LABELS = {"podcast_rss": "Podcast RSS", "youtube_channel": "YouTube channel",
                 "youtube_playlist": "YouTube playlist", "youtube_video": "YouTube video"}
ORIGIN_LABELS = {"creator_caption": "Creator captions", "automatic_caption": "Automatic captions",
                 "amazon_transcribe": "Amazon Transcribe"}
PROVIDER_LABELS = {"yt-dlp": "YouTube captions", "amazon_transcribe": "Amazon Transcribe"}
TRANSCRIPT_MAX_BYTES = 10 * 1024 * 1024
ITEM_PAGE_SIZE = 200


def settings():
    return SimpleNamespace(**dict(current_app.config))


def resolve_source(url, configuration):
    """Classify before provider I/O; the adapters return canonical descriptors."""
    kind = classify_source_url(url)
    if kind.startswith("youtube_"):
        from youtube_discovery import YouTubeDiscoveryClient
        return YouTubeDiscoveryClient(configuration.YOUTUBE_API_KEY).resolve_source(url)
    import requests
    from feed_resolver import resolve_podcast_source
    with requests.Session() as session:
        session.trust_env = False
        return resolve_podcast_source(url, configuration, session)


def source_owned(identity):
    return db.session.scalar(db.select(ContentSource).filter_by(id=identity, admin_id=current_user.id)) or abort(404)


def item_owned(identity):
    return db.session.scalar(db.select(ContentItem).filter_by(id=identity, admin_id=current_user.id)) or abort(404)


def backfill_owned(identity):
    return db.session.scalar(db.select(BackfillRequest).join(ContentSource).where(
        BackfillRequest.id == identity, ContentSource.admin_id == current_user.id)) or abort(404)


def source_view(source):
    count = db.session.scalar(db.select(db.func.count()).select_from(SourceItem).join(ContentItem).where(
        SourceItem.source_id == source.id, ContentItem.admin_id == current_user.id))
    run = db.session.scalar(db.select(DiscoveryRun).where(DiscoveryRun.source_id == source.id)
                            .order_by(DiscoveryRun.id.desc()).limit(1))
    return dict(id=source.id, name=source.name, source_type=SOURCE_LABELS.get(source.source_type, "Source"),
                canonical_url=source.canonical_url, enabled=source.enabled, item_count=count,
                health="Needs attention" if source.error_category else "Healthy" if source.last_success_at else "Waiting for discovery",
                discovery_state=run.state if run else "pending", last_success_at=source.last_success_at,
                next_check_at=source.next_check_at)


def item_view(item):
    language = item.language if isinstance(item.language, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", item.language) else "Unknown"
    return dict(id=item.id, title=item.title, status=item.status,
                platform="YouTube" if item.platform == "youtube" else "Podcast",
                transcript_available=bool(item.transcript_s3_key), media_available=bool(item.media_s3_key),
                provider_transcript_available=bool(item.provider_transcript_s3_key),
                segments_available=bool(item.segments_s3_key),
                transcript_origin=ORIGIN_LABELS.get(item.transcript_origin, "Not available"),
                transcription_provider=PROVIDER_LABELS.get(item.transcription_provider, "Not available"),
                language=language)


def backfill_view(row):
    return dict(id=row.id, source_id=row.source_id, selector_type=row.selector_type,
                state=row.state, cancel_requested=row.cancel_requested, items_scanned=row.items_scanned,
                items_accepted=row.items_accepted, items_queued=row.items_queued,
                history_limited=row.error_category == "rss_history_limited")


def payload():
    value = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    if not isinstance(value, dict):
        abort(400)
    return value


def result(data, *, location, status=200, message=None):
    if request.path.startswith("/api/"):
        return jsonify(data), status
    if message:
        flash(message)
    return redirect(location, code=303)


def failure(message, status=400):
    if request.path.startswith("/api/"):
        return jsonify(error=message), status
    return render_template("index.html", error=message), status


@workspace.get("/")
@workspace.get("/dashboard")
@login_required
def dashboard():
    sources = db.session.scalars(db.select(ContentSource).filter_by(admin_id=current_user.id)
                                 .order_by(ContentSource.created_at.desc())).all()
    return render_template("dashboard.html", sources=[source_view(row) for row in sources])


@workspace.post("/api/sources")
@workspace.post("/sources")
@login_required
def add_source():
    data = payload()
    try:
        url = data.get("url")
        if not isinstance(url, str) or not 0 < len(url) <= 4096:
            raise ValueError
        classified = classify_source_url(url)
        descriptor = resolve_source(url, settings())
        if not isinstance(descriptor, SourceDescriptor) or descriptor.source_type != classified or descriptor.source_type not in SOURCE_TYPES:
            raise ValueError
        # Keep only canonical normalized fields and the typed discovery metadata.
        canonical = SourceDescriptor(descriptor.source_type, descriptor.external_id, descriptor.canonical_url,
                                     descriptor.title, descriptor.description, descriptor.image_url)
        if not isinstance(canonical.external_id, str) or not 0 < len(canonical.external_id) <= 1024:
            raise ValueError
        if not isinstance(canonical.title, str) or not 0 < len(canonical.title) <= 500:
            raise ValueError
        metadata = {}
        for key, pattern in (("uploads_playlist_id", PLAYLIST_ID), ("channel_id", CHANNEL_ID)):
            value = descriptor.provider_metadata.get(key)
            if value is not None:
                if not isinstance(value, str) or not pattern.fullmatch(value):
                    raise ValueError
                metadata[key] = value
    except Exception:
        return failure("Unable to add source. Check the URL and try again.")
    source = ContentSource(admin_id=current_user.id, source_type=canonical.source_type,
        external_id=canonical.external_id, canonical_url=canonical.canonical_url, name=canonical.title,
        description=canonical.description, image_url=canonical.image_url, provider_metadata=metadata,
        initial_item_limit=25, next_check_at=utc_now())
    try:
        db.session.add(source)
        db.session.flush()
        db.session.add(DiscoveryRun(source_id=source.id, run_type="initial", state="pending"))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return failure("Source already exists", 409)
    from tasks import dispatch_task
    try:
        dispatch_task("discover_source", source.id)
    except Exception:
        pass  # The source's due check and pending discovery run are recoverable.
    return result({"id": source.id}, status=201, location=url_for("workspace.source", source_id=source.id),
                  message="Source added. The newest 25 items will be imported.")


def source_items(source_id):
    """Date/ID keyset pages, including undated items after dated history."""
    query = db.select(ContentItem).join(SourceItem).where(
        SourceItem.source_id == source_id, ContentItem.admin_id == current_user.id)
    cursors = [(key, request.args.getlist(key)) for key in ("before", "after") if key in request.args]
    if len(cursors) > 1:
        abort(400)
    newer = bool(cursors and cursors[0][0] == "after")

    def boundary(pivot, *, older):
        if pivot.published_at is None:
            return (and_(ContentItem.published_at.is_(None), ContentItem.id < pivot.id) if older else
                or_(ContentItem.published_at.is_not(None), and_(ContentItem.published_at.is_(None), ContentItem.id > pivot.id)))
        if older:
            return or_(ContentItem.published_at < pivot.published_at, ContentItem.published_at.is_(None),
                and_(ContentItem.published_at == pivot.published_at, ContentItem.id < pivot.id))
        return or_(ContentItem.published_at > pivot.published_at,
            and_(ContentItem.published_at == pivot.published_at, ContentItem.id > pivot.id))

    page_query = query
    if cursors:
        values = cursors[0][1]
        if len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,18}", values[0]) or int(values[0]) > 2 ** 63 - 1:
            abort(400)
        pivot = db.session.scalar(query.where(ContentItem.id == int(values[0]))) or abort(404)
        page_query = query.where(boundary(pivot, older=not newer))
    order = ((ContentItem.published_at.asc().nulls_first(), ContentItem.id.asc()) if newer else
             (ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc()))
    rows = list(db.session.scalars(page_query.order_by(*order).limit(ITEM_PAGE_SIZE)))
    if newer:
        rows.reverse()
    older_url = newer_url = None
    if rows:
        if db.session.scalar(query.where(boundary(rows[-1], older=True)).with_only_columns(ContentItem.id).limit(1)):
            older_url = url_for("workspace.items", source_id=source_id, before=rows[-1].id)
        if db.session.scalar(query.where(boundary(rows[0], older=False)).with_only_columns(ContentItem.id).limit(1)):
            newer_url = url_for("workspace.items", source_id=source_id, after=rows[0].id)
    return dict(items=[item_view(row) for row in rows], older_url=older_url, newer_url=newer_url)


@workspace.get("/sources/<int:source_id>")
@login_required
def source(source_id):
    row = source_owned(source_id)
    backfills = db.session.scalars(db.select(BackfillRequest).filter_by(source_id=row.id)
                                   .order_by(BackfillRequest.id.desc()).limit(50)).all()
    return render_template("source.html", source=source_view(row), **source_items(row.id),
                           backfills=[backfill_view(backfill) for backfill in backfills])


@workspace.get("/sources/<int:source_id>/items")
@login_required
def items(source_id):
    row = source_owned(source_id)
    return render_template("source_items.html", source=source_view(row), **source_items(row.id))


def citation_views(row):
    """Project only public targets and numeric labels; persisted URLs/text are untrusted."""
    if not row.segments_s3_key:
        return []
    try:
        if not isinstance(row.canonical_url, str) or len(row.canonical_url) > 4096:
            return []
        canonical = normalize_public_url(row.canonical_url)
        host = urlsplit(canonical).hostname
        if any(host == domain or host.endswith("." + domain) for domain in ("amazonaws.com", "amazonaws.com.cn")):
            return []
        if row.platform == "youtube":
            kind, _, identity = youtube_locator(canonical)
            if kind != "youtube_video" or identity != row.external_id:
                return []
            canonical = f"https://www.youtube.com/watch?v={identity}"
        elif row.platform != "podcast":
            return []
        stored = get_s3_manager(settings()).download_text_content(row.segments_s3_key, max_bytes=TRANSCRIPT_MAX_BYTES)
        payload = json.loads(stored)
        if not isinstance(payload, dict) or payload.get("schema") != "media-transcript-segments/v1" or any(
            payload.get(key) != value for key, value in (
                ("item_id", row.id), ("platform", row.platform), ("external_id", row.external_id))):
            return []
        segments = payload.get("segments")
        if not isinstance(segments, list) or not 0 < len(segments) <= 100_000:
            return []
        citations = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            start, end = segment.get("start"), segment.get("end")
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (start, end)) or end < start:
                continue
            if row.platform == "youtube":
                target = f"{canonical}&t={math.floor(start)}s"
            else:
                times = [f"{value:.3f}".rstrip("0").rstrip(".") for value in (start, end)]
                target = f"{canonical}#t={','.join(times)}"
            citations.append(dict(url=target, label=f"{start:.3f}–{end:.3f} seconds"))
        return citations
    except Exception:
        # A missing/corrupt private artifact must not expose storage diagnostics.
        return []


@workspace.get("/api/items/<int:item_id>")
@workspace.get("/items/<int:item_id>")
@login_required
def item(item_id):
    row = item_owned(item_id)
    view = item_view(row)
    if request.path.startswith("/api/"):
        return jsonify({key: value for key, value in view.items() if key != "title"})
    memberships = db.session.scalars(db.select(ContentSource).join(SourceItem).where(
        SourceItem.item_id == row.id, ContentSource.admin_id == current_user.id)).all()
    return render_template("item.html", item=view, memberships=[source_view(source) for source in memberships],
                           citations=citation_views(row))


@workspace.get("/items/<int:item_id>/transcript")
@login_required
def transcript(item_id):
    row = item_owned(item_id)
    if not row.transcript_s3_key:
        abort(404)
    try:
        text = get_s3_manager(settings()).download_text_content(row.transcript_s3_key, max_bytes=TRANSCRIPT_MAX_BYTES)
    except Exception:
        return render_template("index.html", error="Transcript is temporarily unavailable."), 503
    return render_template("item.html", item=item_view(row), memberships=[], transcript=text, citations=citation_views(row))


def selector(data):
    keys = {"selector_type", "start_date", "end_date", "requested_count", "explicit_url", "csrf_token"}
    if set(data) - keys:
        raise ValueError
    kind = data.get("selector_type")
    value = {"selector_type": kind}
    for key in ("start_date", "end_date", "requested_count", "explicit_url"):
        entry = data.get(key)
        if entry is None or entry == "":
            continue
        if key.endswith("_date"):
            entry = date.fromisoformat(entry)
        elif key == "requested_count":
            if type(entry) is not int:
                if not isinstance(entry, str) or not re.fullmatch(r"[0-9]{1,4}", entry):
                    raise ValueError
                entry = int(entry)
            if not 0 < entry <= 5000:
                raise ValueError
        value[key] = entry
    BackfillRequest(**value).validate_selector()
    return value


@workspace.post("/api/sources/<int:source_id>/backfills")
@workspace.post("/sources/<int:source_id>/backfills")
@login_required
def create_backfill(source_id):
    row = source_owned(source_id)
    if not row.enabled:
        return failure("Source is disabled", 409)
    try:
        values = selector(payload())
        from tasks import get_discovery_service
        identity = get_discovery_service().create_backfill(row.id, **values)
    except (ValueError, TypeError, OverflowError):
        db.session.rollback()
        return failure("Choose exactly one valid backfill selector (up to 5000 items).")
    return result(backfill_view(backfill_owned(identity)), status=201,
                  location=url_for("workspace.source", source_id=row.id), message="Backfill requested.")


@workspace.get("/api/backfills/<int:backfill_id>")
@login_required
def backfill(backfill_id):
    return jsonify(backfill_view(backfill_owned(backfill_id)))


@workspace.post("/api/backfills/<int:backfill_id>/cancel")
@workspace.post("/backfills/<int:backfill_id>/cancel")
@login_required
def cancel_backfill(backfill_id):
    row = backfill_owned(backfill_id)
    from tasks import get_discovery_service
    outcome = get_discovery_service().cancel_backfill(row.id)
    if outcome is None:
        abort(404)
    view = backfill_view(backfill_owned(backfill_id))
    view.update(state=outcome["state"], cancel_requested=outcome["cancel_requested"])
    message = ("Cancellation requested. Imported items and artifacts are preserved." if outcome["changed"]
               else "Backfill is already finished. No cancellation was applied.")
    return result(view, location=url_for("workspace.source", source_id=row.source_id), message=message)


@workspace.post("/api/items/<int:item_id>/retry")
@workspace.post("/items/<int:item_id>/retry")
@login_required
def retry_item(item_id):
    item_owned(item_id)
    from tasks import _item_lock, dispatch_task
    with _item_lock(item_id) as acquired:
        if not acquired:
            return failure("Item is already processing", 409)
        row = db.session.scalar(db.select(ContentItem).filter_by(id=item_id, admin_id=current_user.id)
                                .execution_options(populate_existing=True).with_for_update())
        if row is None:
            abort(404)
        if row.status not in {"failed", "deferred", "unavailable"}:
            return failure("Item is not available for retry", 409)
        enabled = db.session.scalar(db.select(ContentSource.id).join(SourceItem).where(
            SourceItem.item_id == row.id, ContentSource.admin_id == current_user.id, ContentSource.enabled.is_(True)).limit(1))
        if enabled is None:
            return failure("No enabled source is available", 409)
        # A repair may need an earlier stage whose old terminal failure would
        # otherwise block the worker. Preserve successful stages and artifacts.
        for failed in db.session.scalars(db.select(ProcessingJob).filter_by(item_id=row.id, state="failed")):
            failed.state, failed.error_category, failed.completed_at = "pending", None, None
        stage = row.processing_stage or ("youtube_captions" if row.platform == "youtube" else "podcast_audio")
        job = db.session.scalar(db.select(ProcessingJob).filter_by(item_id=row.id, stage=stage))
        if job is None:
            job = ProcessingJob(item_id=row.id, stage=stage)
            db.session.add(job)
        job.state, job.error_category, job.completed_at = "pending", "dispatch_pending", None
        job.celery_task_id = f"process-item-{row.id}-{stage}"
        row.status, row.error_category = "discovered", None
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return failure("Retry already requested", 409)
        task_id, job_id, attempts = job.celery_task_id, job.id, job.attempts
    try:
        dispatch_task("process_item", row.id, task_id=task_id)
    except Exception:
        pass  # Preserve the processing outbox for scheduled recovery.
    else:
        db.session.query(ProcessingJob).filter_by(id=job_id, state="pending", attempts=attempts,
            celery_task_id=task_id, error_category="dispatch_pending").update({"error_category": None})
        db.session.commit()
    return result({"id": row.id, "status": "discovered"}, status=202,
                  location=url_for("workspace.item", item_id=row.id), message="Retry requested.")
