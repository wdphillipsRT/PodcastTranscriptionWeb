"""Private, owner-scoped workspace. Never serialize operational ORM records."""
from datetime import date
import json
import math
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from extensions import db
from ingestion_types import SourceDescriptor, normalize_public_url
from models import (BackfillRequest, ClassificationRevision, ContentItem, ContentSource, DiscoveryRun,
                    GuestExtraction, ProcessingJob, SourceItem, SOURCE_TYPES, utc_now)
from s3_utils import (get_s3_manager, parse_media_range, StorageRangeError, AUDIO_CONTENT_TYPES)
from commercial_policy import LABELS, POLICY_VERSION
from source_classifier import CHANNEL_ID, PLAYLIST_ID, classify_source_url, youtube_locator


workspace = Blueprint("workspace", __name__)
SOURCE_LABELS = {"podcast_rss": "Podcast RSS", "youtube_channel": "YouTube channel",
                 "youtube_playlist": "YouTube playlist", "youtube_video": "YouTube video"}
ORIGIN_LABELS = {"creator_caption": "Creator captions", "automatic_caption": "Automatic captions",
                 "amazon_transcribe": "Amazon Transcribe"}
PROVIDER_LABELS = {"yt-dlp": "YouTube captions", "amazon_transcribe": "Amazon Transcribe"}
TRANSCRIPT_MAX_BYTES = 10 * 1024 * 1024
ITEM_PAGE_SIZE = 200
REVIEW_PAGE_SIZE = 50
CLASSIFICATION_STATES = {state: state.capitalize() for state in
    ("pending", "running", "retrying", "completed", "failed", "superseded")}
CLASSIFICATION_FAILURES = {"claude_timeout": "Classifier timeout", "claude_unavailable": "Classifier unavailable",
    "claude_invalid_output": "Invalid classifier result", "storage_transient": "Storage unavailable",
    "worker_timeout": "Worker timeout", "claude_auth": "Classifier authentication",
    "claude_config": "Classifier configuration", "model_mismatch": "Classifier configuration",
    "invalid_canonical": "Transcript unavailable", "invalid_policy": "Policy unavailable",
    "revision_budget_exhausted": "Classification budget reached", "dispatch_pending": "Awaiting dispatch"}
CLASSIFICATION_TOTALS = ("included_segments", "excluded_segments", "uncertain_segments",
    "included_milliseconds", "excluded_milliseconds", "uncertain_milliseconds")
PEOPLE_BASIS_LABELS = {"admin_set": "Set by the administrator", "feed_declared": "Declared by the feed or channel",
    "inferred": "Inferred from the title and description"}


def classification_view(latest=None, active=None):
    """Closed aggregate DTO: no artifact identities or free-form diagnostics."""
    return dict(state=CLASSIFICATION_STATES.get(latest.state, "Not classified") if latest else "Not classified",
        policy_version=POLICY_VERSION if latest and latest.policy_version == POLICY_VERSION else "Not available",
        available=bool(active),
        review_required=bool(active and active.review_required),
        failure=CLASSIFICATION_FAILURES.get(latest.error_category, "Classification unavailable") if latest and latest.error_category else None,
        **{name: getattr(active, name) if active else 0 for name in CLASSIFICATION_TOTALS})


def classification_views(items):
    """Two bounded revision joins for an entire item page, not one query per row."""
    if not items:
        return {}
    latest, active = aliased(ClassificationRevision), aliased(ClassificationRevision)
    newest = db.select(db.func.max(ClassificationRevision.id)).where(
        ClassificationRevision.item_id == ContentItem.id,
        ClassificationRevision.transcript_sha256 == ContentItem.transcript_sha256).correlate(ContentItem).scalar_subquery()
    rows = db.session.execute(db.select(ContentItem.id, latest, active).select_from(ContentItem)
        .outerjoin(latest, latest.id == newest)
        .outerjoin(active, and_(active.id == ContentItem.active_classification_revision_id,
            active.item_id == ContentItem.id, active.state == "completed",
            active.transcript_sha256 == ContentItem.transcript_sha256,
            active.segments_sha256 == ContentItem.segments_sha256))
        .where(ContentItem.id.in_([item.id for item in items]), ContentItem.admin_id == current_user.id))
    return {identity: classification_view(revision, policy) for identity, revision, policy in rows}


def review_query():
    newer = aliased(ClassificationRevision)
    return db.select(ClassificationRevision).join(ContentItem, ContentItem.id == ClassificationRevision.item_id).join(
        ContentSource, ContentSource.id == ClassificationRevision.source_id).where(
        ContentItem.admin_id == current_user.id, ContentSource.admin_id == current_user.id,
        ClassificationRevision.transcript_sha256 == ContentItem.transcript_sha256,
        ClassificationRevision.segments_sha256 == ContentItem.segments_sha256,
        or_(and_(ClassificationRevision.id == ContentItem.active_classification_revision_id,
                 ClassificationRevision.state == "completed", ClassificationRevision.review_required.is_(True)),
            and_(ClassificationRevision.state == "failed", ~db.select(newer.id).where(
                newer.item_id == ContentItem.id, newer.transcript_sha256 == ContentItem.transcript_sha256,
                newer.revision > ClassificationRevision.revision, newer.state != "superseded").exists())))


@workspace.app_context_processor
def review_navigation():
    if not getattr(current_user, "is_authenticated", False):
        return {}
    depth = db.session.scalar(db.select(db.func.count()).select_from(review_query().subquery()))
    return {"classification_review_depth": depth}


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
                next_check_at=source.next_check_at, **hosts_view(source))


def hosts_view(source):
    """Effective hosts; the form is prefilled only with administrator values."""
    from integrations.knowledge.people import source_hosts
    names, basis = source_hosts(source)
    return dict(hosts=list(names), hosts_basis=PEOPLE_BASIS_LABELS.get(basis),
                hosts_text="\n".join(source.admin_hosts or []))


def item_people(row):
    """Hosts of the classifying (else first) source and the effective guests."""
    from integrations.knowledge.people import guest_input_sha256, item_guests
    source_id = db.session.scalar(db.select(ClassificationRevision.source_id).where(
        ClassificationRevision.id == row.active_classification_revision_id))
    source = db.session.scalar(db.select(ContentSource).join(SourceItem).where(
        SourceItem.item_id == row.id, ContentSource.admin_id == current_user.id)
        .order_by((ContentSource.id == source_id).desc(), ContentSource.id).limit(1))
    extraction = db.session.scalar(db.select(GuestExtraction).where(
        GuestExtraction.item_id == row.id, GuestExtraction.input_sha256 == guest_input_sha256(row)))
    guests = item_guests(row, extraction)
    return guests, dict(hosts=hosts_view(source)["hosts"] if source else [],
        guests=[dict(name=name, basis=PEOPLE_BASIS_LABELS[basis], inferred=basis == "inferred",
                     confidence=confidence) for name, basis, confidence in guests],
        guests_overridden=row.admin_guests is not None,
        # Inferred guests are never prefilled: saving the form unchanged must not
        # turn a model's guess into an administrator-set guest.
        guests_text="\n".join(row.admin_guests if row.admin_guests is not None
                              else [name for name, basis, _ in guests if basis != "inferred"]))


def item_view(item, classification=None):
    language = item.language if isinstance(item.language, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", item.language) else "Unknown"
    return dict(id=item.id, title=item.title, status=item.status,
                platform="YouTube" if item.platform == "youtube" else "Podcast",
                transcript_available=bool(item.transcript_s3_key), media_available=bool(item.media_s3_key),
                provider_transcript_available=bool(item.provider_transcript_s3_key),
                segments_available=bool(item.segments_s3_key),
                transcript_origin=ORIGIN_LABELS.get(item.transcript_origin, "Not available"),
                transcription_provider=PROVIDER_LABELS.get(item.transcription_provider, "Not available"),
                language=language, classification=classification if classification is not None else classification_views([item]).get(item.id, classification_view()))


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
    recent = list(db.session.scalars(db.select(ContentItem).filter_by(admin_id=current_user.id)
        .order_by(ContentItem.created_at.desc(), ContentItem.id.desc()).limit(20)))
    classifications = classification_views(recent)
    return render_template("dashboard.html", sources=[source_view(row) for row in sources],
        items=[item_view(row, classifications[row.id]) for row in recent])


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
                                     descriptor.title, descriptor.description, descriptor.image_url,
                                     hosts=descriptor.hosts)
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
        initial_item_limit=25, next_check_at=utc_now(), feed_hosts=list(canonical.hosts) or None,
        hosts_updated_at=utc_now() if canonical.hosts else None)
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
    classifications = classification_views(rows)
    return dict(items=[item_view(row, classifications[row.id]) for row in rows], older_url=older_url, newer_url=newer_url)


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
                           citations=citation_views(row), people=item_people(row)[1])


@workspace.post("/api/sources/<int:source_id>/hosts")
@workspace.post("/sources/<int:source_id>/hosts")
@login_required
def update_source_hosts(source_id):
    """Administrator hosts win over the feed; an empty list restores the feed."""
    from integrations.knowledge.people import parse_admin_names, source_hosts
    row = source_owned(source_id)
    data = classification_payload({"hosts"})
    try:
        names = parse_admin_names(data.get("hosts"), limit=10)
    except ValueError:
        return failure("Enter up to 10 host names, one per line, each with letters or digits.")
    # observed_at feeds the export facts hash: only a real change moves the timestamp.
    if row.admin_hosts != (list(names) or None):
        row.admin_hosts, row.hosts_updated_at = list(names) or None, utc_now()
    db.session.commit()
    hosts, basis = source_hosts(row)
    return result(dict(id=row.id, hosts=list(hosts), basis=basis),
                  location=url_for("workspace.source", source_id=row.id), message="Hosts saved.")


@workspace.post("/api/items/<int:item_id>/guests")
@workspace.post("/items/<int:item_id>/guests")
@login_required
def update_item_guests(item_id):
    """Administrator guests win; an empty list records that there are none."""
    from integrations.knowledge.people import parse_admin_names
    row = item_owned(item_id)
    data = classification_payload({"guests"})
    try:
        names = parse_admin_names(data.get("guests"), limit=20)
    except ValueError:
        return failure("Enter up to 20 guest names, one per line, each with letters or digits.")
    if row.admin_guests != list(names):
        row.admin_guests, row.guests_updated_at = list(names), utc_now()
    db.session.commit()
    return result(dict(id=row.id, guests=list(names)),
                  location=url_for("workspace.item", item_id=row.id), message="Guests saved.")


@workspace.post("/api/items/<int:item_id>/guests/reset")
@workspace.post("/items/<int:item_id>/guests/reset")
@login_required
def reset_item_guests(item_id):
    """Drop the administrator guest list so feed, then inferred, guests apply."""
    row = item_owned(item_id)
    classification_payload(set())
    if row.admin_guests is not None:
        row.admin_guests, row.guests_updated_at = None, utc_now()
    db.session.commit()
    guests = item_people(row)[0]
    return result(dict(id=row.id, guests=[name for name, _, _ in guests]),
                  location=url_for("workspace.item", item_id=row.id), message="Guests reset.")


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


def public_review_target(row):
    """Rebuild a public citation; stored segment URLs are never rendered."""
    try:
        if not isinstance(row.canonical_url, str) or len(row.canonical_url) > 4096:
            return None
        canonical = normalize_public_url(row.canonical_url)
        host = urlsplit(canonical).hostname
        if any(host == domain or host.endswith("." + domain) for domain in ("amazonaws.com", "amazonaws.com.cn")):
            return None
        if row.platform == "youtube":
            kind, _, identity = youtube_locator(canonical)
            if kind != "youtube_video" or identity != row.external_id:
                return None
            return f"https://www.youtube.com/watch?v={identity}&t={{start}}s"
        if row.platform == "podcast":
            return canonical + "#t={start},{end}"
    except Exception:
        pass
    return None


def review_artifacts(item_id, storage, target):
    """Authenticated inspection shares the gateway's complete identity checks.

    Deliberately permits the still-active policy while a correction is pending.
    Both artifacts, their totals and membership are validated before any text
    reaches HTML. No learning projection or promotion gateway is bypassed.
    """
    from learning_projection import _inputs, _still_active
    snapshot, transcript, policy = _inputs(db.session, storage, item_id, False)
    positions = {segment.id: index for index, segment in enumerate(transcript.segments)}
    ranges = []
    for entry in policy.ranges:
        first, last = positions[entry.first_segment_id], positions[entry.last_segment_id]
        citation = None
        if target:
            start = str(math.floor(entry.start)) if "youtube.com/watch" in target else f"{entry.start:.3f}".rstrip("0").rstrip(".")
            end = f"{entry.end:.3f}".rstrip("0").rstrip(".")
            # Replacement is literal so untrusted URL braces are not interpreted.
            citation = target.replace("{start}", start).replace("{end}", end)
        ranges.append(dict(label=entry.label, eligible=entry.eligible, confidence=entry.confidence,
            reasons=entry.reason_codes, signals=entry.signal_codes, decision_source=entry.decision_source,
            start=entry.start, end=entry.end, citation=citation,
            text="\n".join(segment.text for segment in transcript.segments[first:last + 1])))
    _still_active(db.session, item_id, False, snapshot)
    return dict(active_revision_id=snapshot[1]["id"], completed_at=snapshot[1]["completed_at"],
        ranges=ranges, boundaries=[dict(id=segment.id, label=f"{index + 1}: {segment.start:.3f}–{segment.end:.3f} seconds")
            for index, segment in enumerate(transcript.segments)])


def owned_media_key(row):
    key = row.media_s3_key
    prefix = f"items/{row.platform}/{row.external_id}/audio/"
    return key if isinstance(key, str) and key.startswith(prefix) else None


@workspace.get("/classification/review")
@login_required
def classification_review():
    if set(request.args) - {"after"} or len(request.args.getlist("after")) > 1:
        abort(400)
    query = review_query()
    priority = db.case((ClassificationRevision.uncertain_segments > 0, 0), else_=1)
    if "after" in request.args:
        cursor = request.args["after"]
        if not re.fullmatch(r"[1-9][0-9]{0,18}", cursor) or int(cursor) > 2**63 - 1:
            abort(400)
        # Queue eligibility can change between requests. The immutable anchor
        # still supplies its ordering tuple after a review resolves/supersedes.
        pivot = db.session.scalar(db.select(ClassificationRevision).join(ContentItem,
            ContentItem.id == ClassificationRevision.item_id).join(ContentSource,
            ContentSource.id == ClassificationRevision.source_id).where(
            ClassificationRevision.id == int(cursor), ContentItem.admin_id == current_user.id,
            ContentSource.admin_id == current_user.id)) or abort(404)
        rank = 0 if pivot.uncertain_segments else 1
        query = query.where(or_(priority > rank, and_(priority == rank,
            or_(ClassificationRevision.created_at > pivot.created_at,
                and_(ClassificationRevision.created_at == pivot.created_at, ClassificationRevision.id > pivot.id)))))
    revisions = list(db.session.scalars(query.order_by(priority, ClassificationRevision.created_at,
        ClassificationRevision.id).limit(REVIEW_PAGE_SIZE + 1)))
    rows = revisions[:REVIEW_PAGE_SIZE]
    titles = dict(db.session.execute(db.select(ContentItem.id, ContentItem.title).where(
        ContentItem.id.in_([row.item_id for row in rows]), ContentItem.admin_id == current_user.id)).all()) if rows else {}
    return render_template("classification_review.html", reviews=[dict(id=row.id, item_id=row.item_id,
        title=titles.get(row.item_id, "Item"), created_at=row.created_at,
        classification=classification_view(row, row if row.state == "completed" else None)) for row in rows],
        next_url=url_for("workspace.classification_review", after=rows[-1].id) if len(revisions) > REVIEW_PAGE_SIZE else None)


@workspace.get("/items/<int:item_id>/classification")
@login_required
def classification_item(item_id):
    row = item_owned(item_id)
    view, target, key, active = item_view(row), public_review_target(row), owned_media_key(row), row.active_classification_revision_id
    db.session.commit()  # External artifact reads never retain a request transaction.
    details, media_available, storage = None, False, None
    try:
        if active:
            storage = get_s3_manager(settings())
            details = review_artifacts(item_id, storage, target)
    except Exception:
        return failure("Classification review is temporarily unavailable.", 503)
    if key:
        try:
            storage = storage or get_s3_manager(settings())
            info = storage.media_info(key)
            media_available = info["content_type"] in AUDIO_CONTENT_TYPES and type(info["length"]) is int and info["length"] > 0
        except Exception:
            pass
    return render_template("classification_item.html", item=view, review=details, labels=LABELS,
        media_available=media_available)


def classification_payload(allowed):
    def unique(pairs):
        value = {}
        for key, entry in pairs:
            if key in value:
                raise ValueError
            value[key] = entry
        return value

    def invalid_constant(_value):
        raise ValueError

    try:
        data = payload()
        if request.is_json:
            data = json.loads(request.get_data(), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        abort(400)
    if (set(data) - (allowed | {"csrf_token"})
            or not request.is_json and any(len(request.form.getlist(key)) != 1 for key in request.form)):
        abort(400)
    return data


@workspace.post("/api/items/<int:item_id>/classification/overrides")
@workspace.post("/items/<int:item_id>/classification/overrides")
@login_required
def classification_override(item_id):
    row = item_owned(item_id)
    data = classification_payload({"active_revision_id", "first_segment_id", "last_segment_id", "label"})
    identity = data.get("active_revision_id")
    if not request.is_json and isinstance(identity, str) and re.fullmatch(r"[1-9][0-9]{0,9}", identity):
        identity = int(identity)
    if (type(identity) is not int or not 0 < identity < 2**31
            or not isinstance(data.get("label"), str) or data["label"] not in LABELS
            or any(not isinstance(data.get(key), str) or not re.fullmatch(r"seg-[0-9]{6}-[a-f0-9]{16}", data[key])
                for key in ("first_segment_id", "last_segment_id"))):
        abort(400)
    revision = db.session.scalar(db.select(ClassificationRevision).where(
        ClassificationRevision.id == identity, ClassificationRevision.item_id == row.id))
    if revision is None:
        abort(400)
    if row.active_classification_revision_id != identity:
        return failure("Classification changed. Reload the review page.", 409)
    admin_id = current_user.id
    db.session.commit()
    from tasks import get_classification_service, publish_override
    try:
        outcome = get_classification_service().record_override(item_id=item_id, admin_id=admin_id,
            active_revision_id=identity, first_segment_id=data["first_segment_id"],
            last_segment_id=data["last_segment_id"], label=data["label"])
    except Exception:
        db.session.rollback()
        latest = item_owned(item_id).active_classification_revision_id
        if latest != identity:
            return failure("Classification changed. Reload the review page.", 409)
        abort(400)
    try:
        publish_override(outcome)  # Service committed both outboxes before this handoff.
    except Exception:
        db.session.rollback()  # Unknown publication keeps committed outboxes recoverable.
    return result(dict(id=item_id, revision_id=outcome.revision_id, state="pending"), status=202,
        location=url_for("workspace.classification_item", item_id=item_id), message="Correction saved. Classification requested.")


@workspace.post("/api/items/<int:item_id>/classification/retry")
@workspace.post("/items/<int:item_id>/classification/retry")
@login_required
def classification_retry(item_id):
    item_owned(item_id)
    classification_payload(set())
    from tasks import _item_lock, get_classification_service, _revision_dispatch, _publish_classification
    with _item_lock(item_id) as acquired:
        if not acquired:
            return failure("Classification is busy. Try again shortly.", 409)
        row = item_owned(item_id)
        if row.status != "completed":
            return failure("Classification retry is unavailable.", 409)
        failed = db.session.scalar(db.select(ClassificationRevision).where(
            ClassificationRevision.item_id == item_id, ClassificationRevision.transcript_sha256 == row.transcript_sha256,
            ClassificationRevision.state == "failed").order_by(ClassificationRevision.id.desc()).limit(1))
        if failed is None:
            return failure("Classification retry is unavailable.", 409)
        key = f"manual_retry:{failed.id}"
        db.session.commit()
        try:
            revision_id = get_classification_service().ensure_revision(item_id, "manual_retry", idempotency_key=key)
            revision = db.session.get(ClassificationRevision, revision_id)
            dispatch = _revision_dispatch(revision) if revision.error_category == "dispatch_pending" else None
            db.session.commit()
        except Exception:
            db.session.rollback()
            return failure("Classification retry is temporarily unavailable.", 503)
    try:
        _publish_classification(dispatch)
    except Exception:
        db.session.rollback()
    return result(dict(id=item_id, revision_id=revision_id, state="pending"), status=202,
        location=url_for("workspace.classification_item", item_id=item_id), message="Classification retry requested.")


@workspace.get("/items/<int:item_id>/media")
@login_required
def private_media(item_id):
    row = item_owned(item_id)
    key = owned_media_key(row)
    if not key:
        abort(404)
    header = request.headers.get("Range")
    try:
        parse_media_range(header)
    except StorageRangeError:
        abort(416)
    db.session.commit()
    try:
        media = get_s3_manager(settings()).download_range(key, header)
    except StorageRangeError:
        abort(416)
    except Exception:
        abort(404)
    return Response(media["body"], status=206, content_type=media["content_type"], headers={
        "Content-Length": str(media["length"]), "Content-Range": media["content_range"], "Accept-Ranges": "bytes"})


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
