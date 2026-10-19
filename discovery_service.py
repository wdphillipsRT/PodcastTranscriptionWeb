"""Durable discovery; provider values never enter task messages.

ProcessingJob is the processing outbox: its stable task ID and dispatch_pending
marker commit with the item before publishing. Ambiguous broker acknowledgements
may redeliver that ID; process_item must claim the durable job idempotently.
"""
from datetime import datetime, timedelta
import json

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from ingestion_types import SourceDescriptor, normalize_public_url
from models import (BackfillRequest, ContentItem, ContentSource, DiscoveryCandidate,
                    DiscoveryRun, ProcessingJob, SourceItem, normalized_descriptor, utc_now)
from source_classifier import youtube_locator


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("expected a positive integer ID")
    return value


def source_descriptor(source):
    return SourceDescriptor(source_type=source.source_type, external_id=source.external_id,
                            canonical_url=source.canonical_url, title=source.name,
                            description=source.description or "", image_url=source.image_url,
                            provider_metadata=source.provider_metadata or {})


def item_projection(item):
    names = ("platform", "item_type", "external_id", "title", "description", "creator_name",
             "creator_id", "channel_id", "channel_name", "channel_url", "thumbnail_url",
             "duration_seconds", "requires_reacquisition")
    value = {name: getattr(item, name) for name in names}
    value.update(canonical_url=item.canonical_url, remote_media_url=item.remote_media_url,
                 published_at=item.published_at.isoformat() if item.published_at else None)
    return normalized_descriptor(value)


class PodcastDiscoveryAdapter:
    """Read available RSS history only; providers need not expose older items."""
    def __init__(self, settings):
        self.settings = settings

    def discover_page(self, source, cursor=None):
        import requests
        from episode_fetcher import discover_podcast_items
        with requests.Session() as client:
            client.trust_env = False
            return discover_podcast_items(source, self.settings, client, limit=5000, cursor=cursor)


def adapter_for_source(source, settings):
    if source.source_type == "podcast_rss":
        return PodcastDiscoveryAdapter(settings)
    from youtube_discovery import YouTubeDiscoveryClient
    return YouTubeDiscoveryClient(settings.YOUTUBE_API_KEY)


class DiscoveryService:
    def __init__(self, session, settings, adapter_factory, dispatch):
        self.session, self.settings = session, settings
        self.adapter_factory, self.dispatch = adapter_factory, dispatch

    def _locked(self, model, identity):
        return self.session.query(model).filter_by(id=positive_id(identity)).populate_existing().with_for_update().one_or_none()

    def _unique(self, model, identity, values=None):
        row = self.session.query(model).filter_by(**identity).first()
        if row is not None:
            return row, False
        # sqlite3's legacy transaction mode does not BEGIN for SELECT/SAVEPOINT.
        # Without an explicit outer transaction RELEASE would commit a candidate
        # even if the page transaction is subsequently rolled back.
        connection = self.session.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        try:
            with self.session.begin_nested():
                row = model(**identity, **(values or {}))
                self.session.add(row)
                self.session.flush()
            return row, True
        except IntegrityError:
            return self.session.query(model).filter_by(**identity).one(), False

    def _send(self, name, identity, **options):
        positive_id(identity)
        if name == "process_item" and not self._enabled_memberships(identity).first():
            return False
        try:
            self.dispatch(name, identity, **options)
            return True
        except Exception:
            # Never retain exception strings, URLs or provider credentials.
            return False

    def _enabled_memberships(self, item_id=None):
        query = self.session.query(SourceItem.item_id).join(ContentSource).filter(ContentSource.enabled.is_(True))
        return query.filter(SourceItem.item_id == item_id) if item_id is not None else query

    def _pending_run(self, source, run_type):
        run = self.session.query(DiscoveryRun).filter(
            DiscoveryRun.source_id == source.id, DiscoveryRun.backfill_id.is_(None),
            DiscoveryRun.state.in_(("pending", "scanning", "finalizing", "failed"))
        ).order_by(DiscoveryRun.id).first()
        if run is None:
            run = DiscoveryRun(source_id=source.id, run_type=run_type)
            self.session.add(run)
            self.session.flush()
        return run

    def discover_source(self, source_id, run_type=None):
        source = self._locked(ContentSource, source_id)
        if source is None or not source.enabled:
            self.session.commit()
            return None
        if run_type is not None and run_type not in {"initial", "scheduled"}:
            raise ValueError("invalid discovery run type")
        run = self._pending_run(source, run_type or ("scheduled" if source.last_success_at else "initial"))
        run_id = run.id
        self.session.commit()
        self.continue_discovery(run_id)
        return run_id

    def _stage(self, run, items, unavailable_ids=()):
        for identity in unavailable_ids:
            _, created = self._unique(DiscoveryCandidate,
                dict(run_id=run.id, platform="youtube", external_id=identity),
                dict(was_available=False))
            if created:
                run.items_scanned += 1
        for item in items:
            candidate, created = self._unique(DiscoveryCandidate,
                dict(run_id=run.id, platform=item.platform, external_id=item.external_id),
                dict(descriptor=item_projection(item)))
            if created:
                run.items_scanned += 1
            elif not candidate.was_available:
                candidate.descriptor, candidate.was_available = item_projection(item), True

    @staticmethod
    def _cursor(cursor):
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
                                   or any(ord(char) < 32 for char in cursor)):
            raise ValueError("invalid discovery cursor")
        return cursor

    def _cancelled(self, request):
        if request is None:
            return False
        terminal = ("completed", "cancelled", "failed")
        with self.session.no_autoflush:
            request = self._locked(BackfillRequest, request.id)
            if request is None:
                self.session.commit()
                return True
            # Even a replayable failure must retain the source lock until its
            # enabled-only replay transition commits.
            run_ids = self.session.query(DiscoveryRun.id).filter_by(backfill_id=request.id).order_by(DiscoveryRun.id).all()
            for (run_id,) in run_ids:
                self._locked(DiscoveryRun, run_id)
            source = self._locked(ContentSource, request.source_id)
            enabled = source is not None and source.enabled
            if request.state in terminal:
                # Failed requests without cancellation remain explicitly
                # replayable while enabled; source disable/cancellation cannot
                # rewrite their failure into a different terminal state.
                stop = request.state in {"completed", "cancelled"} or request.cancel_requested or not enabled
                if stop:
                    self.session.commit()
                return stop
            if not request.cancel_requested and enabled:
                return False
            if source is None:
                self.session.commit()
                return True
            disabled = not source.enabled
            if not request.cancel_requested and not disabled:
                return False
            values = {"state": "cancelled", "completed_at": utc_now()}
            if disabled:
                values["error_category"] = "source_disabled"
            # Guard every write as well as locking: SQLite ignores FOR UPDATE,
            # and a terminal commit after our reads must remain authoritative.
            changed = self.session.query(BackfillRequest).filter(
                BackfillRequest.id == request.id,
                BackfillRequest.state.in_(("pending", "running")),
                ~sa.exists().where(DiscoveryRun.backfill_id == request.id, DiscoveryRun.state.in_(terminal)),
                sa.or_(BackfillRequest.cancel_requested.is_(True), sa.exists().where(
                    ContentSource.id == request.source_id, ContentSource.enabled.is_(False)))
            ).update(values, synchronize_session=False)
            if not changed:
                self.session.rollback()
                return True
            self.session.query(DiscoveryRun).filter(
                DiscoveryRun.backfill_id == request.id, DiscoveryRun.state.notin_(terminal)
            ).update({"state": "cancelled", "completed_at": values["completed_at"]}, synchronize_session=False)
            self.session.commit()
            return True

    def _cancel_disabled_run(self, run_id):
        """Cancel standalone discovery only while the run is nonterminal."""
        with self.session.no_autoflush:
            # Resolve the association without taking a run lock before a
            # backfill request lock, matching the failure/cancellation order.
            request_id = self.session.query(DiscoveryRun.backfill_id).filter_by(id=run_id).scalar()
            if request_id:
                return self._cancelled(self._locked(BackfillRequest, request_id))
            run = self._locked(DiscoveryRun, run_id)
            if run is None:
                self.session.commit()
                return True
            source = self._locked(ContentSource, run.source_id)
            if source is not None and source.enabled:
                return False
            self.session.query(DiscoveryRun).filter(
                DiscoveryRun.id == run_id, DiscoveryRun.backfill_id.is_(None),
                DiscoveryRun.state.in_(("pending", "scanning", "finalizing")),
                sa.exists().where(ContentSource.id == run.source_id, ContentSource.enabled.is_(False))
            ).update({"state": "cancelled", "completed_at": utc_now(), "error_category": "source_disabled"},
                     synchronize_session=False)
        self.session.commit()
        return True

    def _scan(self, run, request=None):
        source = run.source
        adapter = self.adapter_factory(source)
        descriptor = source_descriptor(source)
        for _ in range(min(self.settings.YOUTUBE_MAX_PAGES_PER_TASK, 5)):
            if request is not None:
                request = self._locked(BackfillRequest, request.id)
            if self._cancelled(request):
                return False
            if request is None and self._cancel_disabled_run(run.id):
                return False
            run = self._locked(DiscoveryRun, run.id)
            if run.state in {"completed", "cancelled"}:
                self.session.commit()
                return False
            if run.state == "finalizing":
                return True
            self.session.refresh(source)
            if not source.enabled:
                if request is not None:
                    self._cancelled(request)
                else:
                    self._cancel_disabled_run(run.id)
                return False
            page = adapter.discover_page(descriptor, run.cursor)
            cursor = self._cursor(page.next_cursor)
            if not page.exhausted and (cursor is None or cursor == run.cursor):
                raise ValueError("nonadvancing discovery cursor")
            self._stage(run, page.items, page.unavailable_ids)
            run.cursor = cursor
            run.state = "finalizing" if page.exhausted else "scanning"
            if request:
                request.items_scanned = run.items_scanned
                request.cursor = json.dumps({"page": cursor}, separators=(",", ":"))
            self.session.commit()
            if page.exhausted:
                return True
        self._send("run_backfill" if request else "continue_discovery", request.id if request else run.id)
        return False

    def _fail(self, run_id, request_id=None):
        self.session.rollback()
        terminal = ("completed", "cancelled")
        try:
            with self.session.no_autoflush:
                if request_id is None:
                    # Read only the association, without locking/mutating the
                    # run before its request. All authoritative reads follow.
                    request_id = self.session.query(DiscoveryRun.backfill_id).filter_by(id=positive_id(run_id)).scalar()
                request = self._locked(BackfillRequest, request_id) if request_id else None
                run = self._locked(DiscoveryRun, run_id)
                if (run is None or run.state in terminal or (request_id and request is None)
                        or (request and (request.state in terminal or run.backfill_id != request.id))):
                    self.session.commit()
                    return False
                source = self._locked(ContentSource, run.source_id)
                if source is None:
                    self.session.commit()
                    return False
                # PostgreSQL locks serialize request -> run -> source. Guarded
                # writes also protect terminal rows when SQLite ignores those
                # locks or a future caller changes a row between the reads.
                run_update = self.session.query(DiscoveryRun).filter(
                    DiscoveryRun.id == run_id, DiscoveryRun.state.notin_(terminal))
                if request:
                    run_update = run_update.filter(sa.exists().where(
                        BackfillRequest.id == request_id, BackfillRequest.state.notin_(terminal)))
                if not run_update.update({"state": "failed", "error_category": "discovery_failed"}, synchronize_session=False):
                    self.session.commit()
                    return False
                if request:
                    changed = self.session.query(BackfillRequest).filter(
                        BackfillRequest.id == request_id, BackfillRequest.state.notin_(terminal)
                    ).update({"state": "failed", "error_category": "discovery_failed"}, synchronize_session=False)
                    if not changed:
                        self.session.rollback()
                        return False
                self.session.query(ContentSource).filter_by(id=source.id).update(
                    {"error_category": "discovery_failed"}, synchronize_session=False)
            self.session.commit()
            return True
        except Exception:
            # Failure recording must not leave an unusable transaction or expose
            # the original provider/transport exception. Durable work can replay.
            self.session.rollback()
            return False

    def continue_discovery(self, run_id):
        run = self._locked(DiscoveryRun, run_id)
        if run is None:
            self.session.commit()
            return None
        if run.backfill_id:
            self.session.commit()
            return self.run_backfill(run.backfill_id)
        if run.state in {"completed", "cancelled"}:
            self.session.commit()
            self.replay_pending_dispatches(run.source_id)
            return run.id
        if self._cancel_disabled_run(run.id):
            return run.id
        observed_state = run.state
        activated = self.session.query(DiscoveryRun).filter(
            DiscoveryRun.id == run.id, DiscoveryRun.state == observed_state,
            DiscoveryRun.state.in_(("pending", "scanning", "finalizing", "failed")),
            sa.exists().where(ContentSource.id == run.source_id, ContentSource.enabled.is_(True))
        ).update({"started_at": run.started_at or utc_now(), "error_category": None,
                  "state": "finalizing" if observed_state == "finalizing" else "scanning"}, synchronize_session=False)
        if not activated:
            self.session.rollback()
            self._cancel_disabled_run(run.id)
            return run.id
        self.session.commit()
        try:
            if run.state == "finalizing" or self._scan(run):
                self.finalize_discovery(run.id)
        except Exception:
            self._fail(run.id)
        return run.id

    def _ordered_candidates(self, run, limit=None):
        # Sort normalized UTC timestamps in SQL so a backfill invocation loads
        # only its bounded batch, even when a playlist has extensive history.
        query = self.session.query(DiscoveryCandidate).filter(
            DiscoveryCandidate.run_id == run.id, DiscoveryCandidate.descriptor.is_not(None)
        )
        if run.run_type == "scheduled":
            previous, prior_run = aliased(DiscoveryCandidate), aliased(DiscoveryRun)
            # Completed scans retain every observed identity, including the
            # initial history deliberately omitted by newest-25. No timestamp
            # cutoff can substitute for this per-source baseline.
            query = query.filter(~sa.exists().where(
                previous.run_id == prior_run.id, prior_run.source_id == run.source_id,
                prior_run.id != run.id, prior_run.backfill_id.is_(None),
                prior_run.state == "completed", previous.was_available.is_(True),
                previous.platform == DiscoveryCandidate.platform,
                previous.external_id == DiscoveryCandidate.external_id))
        query = query.order_by(DiscoveryCandidate.descriptor["published_at"].as_string().desc().nullslast(),
                   DiscoveryCandidate.external_id.desc())
        return (query.limit(limit) if limit is not None else query).all()

    def _accept(self, source, candidate):
        values = dict(candidate.descriptor)
        values.pop("requires_reacquisition", None)
        if values.get("published_at"):
            values["published_at"] = datetime.fromisoformat(values["published_at"])
        values["canonical_url"] = values.get("canonical_url") or source.canonical_url
        identity = {name: values.pop(name) for name in ("platform", "external_id")}
        item, created = self._unique(ContentItem, identity, dict(admin_id=source.admin_id, **values))
        _, membership = self._unique(SourceItem, dict(source_id=source.id, item_id=item.id))
        job_created = False
        if created:
            stage = "podcast_audio" if item.platform == "podcast" else "youtube_captions"
            _, job_created = self._unique(ProcessingJob, dict(item_id=item.id, stage=stage),
                dict(state="pending", celery_task_id=f"process-item-{item.id}-{stage}", error_category="dispatch_pending"))
        candidate.descriptor = None
        return item.id, membership, job_created

    def replay_pending_dispatches(self, source_id=None):
        query = self.session.query(ProcessingJob).filter(
            ProcessingJob.error_category == "dispatch_pending",
            ProcessingJob.item_id.in_(self._enabled_memberships()))
        if source_id is not None:
            query = query.join(ContentItem).join(SourceItem).filter(SourceItem.source_id == source_id)
        identities = [row.id for row in query.all()]
        self.session.commit()
        for identity in identities:
            job = self._locked(ProcessingJob, identity)
            if job is None or job.error_category != "dispatch_pending":
                self.session.commit()
                continue
            item_id, task_id, attempts = job.item_id, job.celery_task_id, job.attempts
            observed_state = job.state
            self.session.commit()
            if self._send("process_item", item_id, task_id=task_id) and observed_state == "pending":
                self.session.query(ProcessingJob).filter_by(id=identity, state="pending", attempts=attempts,
                    celery_task_id=task_id, error_category="dispatch_pending").update(
                    {"error_category": None}, synchronize_session="fetch")
                self.session.commit()

    def finalize_discovery(self, run_id):
        run = self._locked(DiscoveryRun, run_id)
        if run is None or run.state != "finalizing":
            self.session.commit()
            return None
        source = self._locked(ContentSource, run.source_id)
        if source is None or not source.enabled:
            self.session.commit()
            return None
        candidates = self._ordered_candidates(run, 25 if run.run_type == "initial" else None)
        for candidate in candidates:
            _, membership, queued = self._accept(run.source, candidate)
            run.items_accepted += int(membership)
            run.items_queued += int(queued)
        self.session.query(DiscoveryCandidate).filter_by(run_id=run.id).update({"descriptor": None}, synchronize_session="fetch")
        run.state, run.completed_at, run.error_category = "completed", utc_now(), None
        run.source.last_success_at, run.source.error_category = utc_now(), None
        run.source.next_check_at = utc_now() + timedelta(seconds=self.settings.DISCOVERY_INTERVAL_SECONDS)
        source_id = run.source_id
        self.session.commit()
        self.replay_pending_dispatches(source_id)
        return run.id

    def _validate_request(self, request, source=None):
        request.validate_selector()
        if request.selector_type == "count" and request.requested_count > min(self.settings.BACKFILL_REQUEST_MAX_ITEMS, 5000):
            raise ValueError("backfill request exceeds 5000 items")
        if request.explicit_url:
            url = normalize_public_url(request.explicit_url)
            if (source or request.source).source_type.startswith("youtube_"):
                kind, _, identity = youtube_locator(url)
                if kind != "youtube_video":
                    raise ValueError("explicit backfill URL must select one item")
                url = "https://www.youtube.com/watch?v=" + identity
            request.explicit_url = url

    def create_backfill(self, source_id, **selector):
        source = self._locked(ContentSource, source_id)
        if source is None:
            raise ValueError("source does not exist")
        request = BackfillRequest(source_id=source.id, **selector)
        self._validate_request(request, source)
        self.session.add(request)
        self.session.commit()
        self._send("run_backfill", request.id)
        return request.id

    def cancel_backfill(self, request_id):
        with self.session.no_autoflush:
            request = self._locked(BackfillRequest, request_id)
            if request is None:
                self.session.commit()
                return None
            changed = False
            if request.state in {"pending", "running"}:
                changed = bool(self.session.query(BackfillRequest).filter(
                    BackfillRequest.id == request.id, BackfillRequest.state.in_(("pending", "running"))
                ).update({"cancel_requested": True}, synchronize_session=False))
            # The durable flag lets a worker stop even if cancellation
            # finalization is interrupted. Finalization reacquires all locks.
            self.session.commit()
            if changed:
                self._cancelled(request)
            request = self._locked(BackfillRequest, request_id)
            if request is None:
                self.session.commit()
                return None
            outcome = dict(id=request.id, state=request.state, cancel_requested=request.cancel_requested,
                           changed=changed and request.state == "cancelled")
        self.session.commit()
        return outcome

    def _backfill_run(self, request):
        run = self.session.query(DiscoveryRun).filter_by(backfill_id=request.id).first()
        if run is None:
            run = DiscoveryRun(source_id=request.source_id, backfill_id=request.id, run_type="backfill")
            self.session.add(run)
            self.session.flush()
        return run

    def _resolve_owned_video(self, source, url):
        identity = youtube_locator(url)[2]
        if source.source_type == "youtube_video" and source.external_id != identity:
            raise ValueError("explicit video does not belong to the selected source")
        adapter = self.adapter_factory(source)
        item = adapter.resolve_video(url)
        owned = item.external_id == identity
        if source.source_type == "youtube_channel":
            owned = owned and item.channel_id == source.external_id
        elif source.source_type == "youtube_playlist":
            owned = owned and adapter.source_contains_video(source_descriptor(source), identity) is True
        if not owned:
            raise ValueError("explicit video does not belong to the selected source")
        return item

    def run_backfill(self, request_id):
        request = self._locked(BackfillRequest, request_id)
        if request is None:
            self.session.commit()
            return None
        self._validate_request(request)
        # Validation may canonicalize a persisted URL. Preserve that update
        # before refreshing, retaining the request lock through run creation.
        self.session.flush()
        if request.state == "completed":
            self.session.commit()
            self.replay_pending_dispatches(request.source_id)
            return request.id
        if self._cancelled(request):
            return request.id
        request = self._locked(BackfillRequest, request_id)
        if request is None:
            self.session.commit()
            return None
        observed_state = request.state
        run = self._backfill_run(request)
        run = self._locked(DiscoveryRun, run.id)
        source = self._locked(ContentSource, request.source_id)
        if source is None or not source.enabled:
            self._cancelled(request)
            return request.id
        # SQLite ignores row locks: a disable committed after the enabled read
        # must also prevent replay from clearing a terminal failure diagnosis.
        activated = self.session.query(BackfillRequest).filter(
            BackfillRequest.id == request.id, BackfillRequest.state == observed_state,
            BackfillRequest.state.in_(("pending", "running", "failed")),
            sa.exists().where(ContentSource.id == request.source_id, ContentSource.enabled.is_(True))
        ).update({"state": "running", "started_at": request.started_at or utc_now(), "error_category": None},
                 synchronize_session=False)
        if not activated:
            self.session.rollback()
            self._cancelled(request)
            return request.id
        run.started_at, run.error_category = run.started_at or utc_now(), None
        if run.state == "failed":
            run.state = "finalizing" if request.cursor and "after" in json.loads(request.cursor) else "scanning"
        self.session.commit()
        try:
            if request.selector_type == "explicit_url" and request.source.source_type.startswith("youtube_") and run.state != "finalizing":
                item = self._resolve_owned_video(request.source, request.explicit_url)
                self._stage(run, (item,))
                run.state, request.items_scanned = "finalizing", run.items_scanned
                self.session.commit()
            elif run.state != "finalizing" and not self._scan(run, request):
                return request.id
            self._finalize_backfill(run, request)
        except Exception:
            self._fail(run.id, request.id)
        return request.id

    def _finalize_backfill(self, run, request):
        examined_now = 0
        batch_limit = min(self.settings.YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK, 100)
        request_id, run_id = request.id, run.id
        while True:
            # A new transaction reacquires this lock after each committed item.
            # No candidate snapshot or progress read survives the lock boundary.
            request = self._locked(BackfillRequest, request_id)
            if request.state == "completed":
                self.session.commit()
                self.replay_pending_dispatches(request.source_id)
                return
            if self._cancelled(request):
                return
            run = self._locked(DiscoveryRun, run_id)
            if run.state != "finalizing":
                self.session.commit()
                self._send("run_backfill", request.id)
                return
            maximum = min(self.settings.BACKFILL_REQUEST_MAX_ITEMS, 5000)
            limit = request.requested_count if request.selector_type == "count" else maximum
            if request.selector_type == "explicit_url":
                limit = 1
                matching_urls = self.session.query(DiscoveryCandidate.id).filter(
                    DiscoveryCandidate.run_id == run.id,
                    DiscoveryCandidate.descriptor["canonical_url"].as_string() == request.explicit_url
                ).limit(2)
                if matching_urls.count() > 1:
                    raise ValueError("explicit item URL is ambiguous in available provider history")
            # Both terminal decisions and remaining-work reads happen under the
            # request lock, before any commit can release it to another worker.
            remaining_query = self.session.query(DiscoveryCandidate).filter(
                DiscoveryCandidate.run_id == run.id, DiscoveryCandidate.descriptor.is_not(None))
            if request.items_accepted >= limit or remaining_query.with_entities(DiscoveryCandidate.id).first() is None:
                if request.items_accepted >= limit:
                    remaining_query.update({"descriptor": None}, synchronize_session=False)
                request.state, request.completed_at = "completed", utc_now()
                run.state, run.completed_at = "completed", utc_now()
                run.items_accepted, run.items_queued = request.items_accepted, request.items_queued
                if request.source.source_type == "podcast_rss":
                    request.error_category = "rss_history_limited"
                self.session.commit()
                self.replay_pending_dispatches(request.source_id)
                return
            if examined_now >= batch_limit:
                self.session.commit()
                self._send("run_backfill", request.id)
                return
            candidates = self._ordered_candidates(run, 1)
            if not candidates:
                # Defensive against an externally changed/inconsistent snapshot:
                # known remaining work is never cleared on an empty batch.
                self.session.commit()
                self._send("run_backfill", request.id)
                return
            candidate = self._locked(DiscoveryCandidate, candidates[0].id)
            if candidate.descriptor is None:
                self.session.commit()
                continue
            examined_now += 1
            value = candidate.descriptor
            published = datetime.fromisoformat(value["published_at"]).date() if value.get("published_at") else None
            matches = True
            if request.selector_type == "date_range":
                matches = published is not None and request.start_date <= published <= request.end_date
            elif request.selector_type == "explicit_url":
                matches = value.get("canonical_url") == request.explicit_url
            if matches:
                _, membership, queued = self._accept(request.source, candidate)
                request.items_accepted += int(membership)
                request.items_queued += int(queued)
                run.items_accepted, run.items_queued = request.items_accepted, request.items_queued
            else:
                candidate.descriptor = None
            self.session.flush()
            request.cursor = json.dumps({"after": candidate.id, "remaining": remaining_query.with_entities(DiscoveryCandidate.id).first() is not None},
                                        separators=(",", ":"))
            self.session.commit()
            self.replay_pending_dispatches(request.source_id)

    def check_all_sources(self):
        self.replay_pending_dispatches()
        identities = [row.id for row in self.session.query(ContentSource).filter_by(enabled=True).order_by(ContentSource.id).all()]
        self.session.commit()
        scheduled = 0
        for identity in identities:
            try:
                source = self._locked(ContentSource, identity)
                if source is None or not source.enabled:
                    self.session.commit()
                    continue
                self._pending_run(source, "scheduled" if source.last_success_at else "initial")
                self.session.commit()
                if not self._send("discover_source", identity):
                    source.error_category = "dispatch_failed"
                    self.session.commit()
                scheduled += 1
            except Exception:
                self.session.rollback()
                self.session.query(ContentSource).filter_by(id=identity).update(
                    {"error_category": "discovery_failed"}, synchronize_session="fetch")
                self.session.commit()
        requests = self.session.query(BackfillRequest).filter(BackfillRequest.state.in_(("pending", "running"))).all()
        identities = [request.id for request in requests]
        self.session.commit()
        for identity in identities:
            self._send("run_backfill", identity)
        return scheduled + len(identities)
