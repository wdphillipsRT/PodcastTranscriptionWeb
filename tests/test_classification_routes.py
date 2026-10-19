"""Authenticated review, immutable corrections and private bounded playback."""
import json
from datetime import timedelta
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest
import sqlalchemy as sa

from extensions import db
import models as m
import tasks
from tests.test_classification_service import env, finish, add_item


@pytest.fixture
def review(env, app, monkeypatch):
    from web import routes
    env.item.title = "Episode <script>private</script>"
    env.classifier.labels[env.transcript.segments[1].id] = ("editorial", .4)
    db.session.commit()
    env.active, _ = finish(env)
    env.client = app.test_client()
    with env.client.session_transaction() as session:
        session["_user_id"], session["_fresh"] = str(env.owner.id), True
    monkeypatch.setattr(routes, "get_s3_manager", lambda *a: env.storage)
    monkeypatch.setattr(tasks, "get_classification_service", lambda: env.service)
    monkeypatch.setattr(tasks, "_item_lock", lambda *a: __import__("contextlib").nullcontext(True))
    return env


def correction(review, **changes):
    return dict(active_revision_id=review.active,
        first_segment_id=review.transcript.segments[0].id,
        last_segment_id=review.transcript.segments[1].id, label="paid_commercial", **changes)


def test_review_is_authenticated_escaped_and_private(review, app):
    paths = ("/classification/review", "/items/1/classification", "/items/1/media")
    for path in paths:
        assert app.test_client().get(path).status_code == 302
    # The service fixture holds an app context across requests; clear its
    # Flask-Login cache when switching from the anonymous client to the owner.
    from flask import g
    g.pop("_login_user", None)
    response = review.client.get("/items/1/classification")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert "First editorial insight" in response.text and "&lt;script&gt;" in response.text
    assert "<script>private" not in response.text
    for private in (review.item.segments_s3_key, review.item.transcript_sha256, "requested_model", "api_key"):
        assert private not in response.text
    page = BeautifulSoup(response.text, "html.parser")
    assert len(page.select('select[name="label"] option')) == 6
    assert page.select_one('select[name="first_segment_id"]')
    assert page.select_one('select[name="last_segment_id"]')
    assert "uncertain_threshold" in response.text and "substantive_content" in response.text
    assert "https://example.com/episode#t=" in response.text
    assert not page.select("audio")
    api = review.client.get("/api/items/1").text
    assert "First editorial insight" not in api and review.item.transcript_sha256 not in api


def test_review_scopes_gets_and_mutations(review, monkeypatch):
    from web import routes
    monkeypatch.setattr(routes, "current_user", SimpleNamespace(id=999))
    for path in ("/items/1/classification", "/items/1/media"):
        assert review.client.get(path).status_code == 404
    for suffix in ("overrides", "retry"):
        for prefix in ("", "/api"):
            assert review.client.post(f"{prefix}/items/1/classification/{suffix}", json=correction(review)).status_code == 404
    queue = review.client.get("/classification/review")
    assert queue.status_code == 200 and "Episode" not in queue.text


@pytest.mark.parametrize("values", [dict(label="unknown"), dict(first_segment_id="unknown"),
    dict(last_segment_id="unknown"), dict(first_segment_id=None), dict(active_revision_id=True),
    dict(active_revision_id=999999), dict(extra="private"), dict(label=[]),
    dict(first_segment_id="seg-000003-ffffffffffffffff")])
def test_override_invalid_fields_are_generic_and_write_nothing(review, values):
    data = correction(review)
    data.update(values)
    response = review.client.post("/api/items/1/classification/overrides", json=data)
    assert response.status_code == 400 and response.json == {"error": "Request could not be accepted"}
    assert m.SegmentOverride.query.count() == m.ClassificationSweep.query.count() == 0


def test_override_reversed_boundaries_and_duplicate_form_fields_are_rejected(review):
    from werkzeug.datastructures import MultiDict
    data = correction(review)
    data["first_segment_id"], data["last_segment_id"] = data["last_segment_id"], data["first_segment_id"]
    assert review.client.post("/api/items/1/classification/overrides", json=data).status_code == 400
    form = MultiDict(correction(review))
    form.add("label", "editorial")
    assert review.client.post("/items/1/classification/overrides", data=form).status_code == 400


def test_override_commits_both_outboxes_before_publication_and_keeps_media_complete(review, monkeypatch):
    calls = []
    def publish(value):
        assert not db.session.new and not db.session.dirty and not db.session().in_transaction()
        assert db.session.get(m.ClassificationRevision, value.revision_id).error_category == "dispatch_pending"
        assert db.session.get(m.ClassificationSweep, value.sweep_id).error_category == "dispatch_pending"
        calls.append(value)
        raise RuntimeError("secret broker credentials")
    monkeypatch.setattr(tasks, "publish_override", publish)
    response = review.client.post("/api/items/1/classification/overrides", json=correction(review))
    assert response.status_code == 202 and len(calls) == 1
    assert set(response.json) == {"id", "revision_id", "state"}
    assert review.item.status == "completed" and m.SegmentOverride.query.count() == 1
    assert "secret" not in response.text and "segment" not in response.text
    assert db.session.get(m.ClassificationSweep, calls[0].sweep_id).error_category == "dispatch_pending"


def test_stale_override_is_409_after_new_policy_activation(review, monkeypatch):
    monkeypatch.setattr(tasks, "publish_override", lambda *a: None)
    result = review.service.record_override(item_id=1, admin_id=review.owner.id, **correction(review))
    prepared = review.service.prepare_revision(result.revision_id)
    for ordinal in prepared.block_ordinals:
        review.service.run_block(result.revision_id, ordinal)
    review.service.finalize_revision(result.revision_id)
    response = review.client.post("/api/items/1/classification/overrides", json=correction(review))
    assert response.status_code == 409 and "secret" not in response.text


def test_retry_uses_durable_manual_retry_and_generic_validation(review, monkeypatch):
    failed = review.service.ensure_revision(1, "policy_upgrade", idempotency_key="failed")
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == failed)
        .values(state="failed", error_category="claude_timeout"))
    db.session.commit()
    published = []
    monkeypatch.setattr(tasks, "_publish_classification", lambda value: published.append(value))
    assert review.client.post("/api/items/1/classification/retry", json={"extra": 1}).status_code == 400
    response = review.client.post("/api/items/1/classification/retry", json={})
    assert response.status_code == 202 and published
    row = db.session.get(m.ClassificationRevision, response.json["revision_id"])
    assert row.request_reason == "manual_retry" and row.idempotency_key == f"manual_retry:{failed}"
    assert review.item.status == "completed"


def test_corrupt_policy_is_generic_and_never_displays_transcript(review):
    row = db.session.get(m.ClassificationRevision, review.active)
    review.storage.objects[row.policy_s3_key] = b"secret https://private.invalid?token=secret"
    response = review.client.get("/items/1/classification")
    assert response.status_code == 503
    assert "secret" not in response.text and "First editorial insight" not in response.text


def test_queue_keyset_uncertain_first_oldest_first_and_safe_dashboard_counts(review, monkeypatch):
    from web import routes
    monkeypatch.setattr(routes, "REVIEW_PAGE_SIZE", 2, raising=False)
    uncertain = review.active
    # Add multiple failed items using real durable revision creation.
    identities = []
    for number in range(4):
        item, _ = add_item(review, external_id=f"failed-{number}")
        revision_id = review.service.ensure_revision(item.id, "backfill")
        db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == revision_id)
            .values(state="failed", error_category="claude_timeout", created_at=m.utc_now() - timedelta(days=number + 1)))
        identities.append(revision_id)
        db.session.commit()
    seen, url = [], "/classification/review"
    while url:
        page = BeautifulSoup(review.client.get(url).text, "html.parser")
        rows = page.select("[data-review-revision]")
        assert 0 < len(rows) <= 2
        seen.extend(int(row["data-review-revision"]) for row in rows)
        next_page = page.find("a", string="Next reviews")
        url = next_page["href"] if next_page else None
        assert len(seen) <= 5
    assert seen == [uncertain, *reversed(identities)]
    dashboard = review.client.get("/dashboard").text
    assert "Classification reviews (5)" in dashboard and "Uncertain" in dashboard
    assert "commercial-segment-policy/v1" in review.client.get("/items/1").text
    assert "Classification" in review.client.get("/sources/1/items").text
    for query in ("after=0", "after=abc", "after=1&after=2", "unknown=1"):
        assert review.client.get("/classification/review?" + query).status_code == 400


def test_media_range_is_private_bounded_and_one_audio(review, monkeypatch):
    review.item.media_s3_key = "items/podcast/episode/audio/private.mp3"
    db.session.commit()
    review.storage.media_info = lambda key: {"content_type": "audio/mpeg", "length": 10}
    seen = []
    def download(key, header):
        seen.append((key, header))
        return dict(body=b"abcd", content_type="audio/mpeg", content_range="bytes 2-5/10", length=4)
    review.storage.download_range = download
    response = review.client.get("/items/1/media", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.data == b"abcd"
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert response.headers["Content-Length"] == "4" and response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Type"] == "audio/mpeg" and response.headers["Cache-Control"] == "no-store"
    assert seen == [(review.item.media_s3_key, "bytes=2-5")]
    page = BeautifulSoup(review.client.get("/items/1/classification").text, "html.parser")
    assert len(page.select("audio")) == 1 and page.select("[data-seek]")
    assert "private.mp3" not in str(page)
    review.storage.media_info = lambda key: {"content_type": "text/html", "length": 10}
    assert not BeautifulSoup(review.client.get("/items/1/classification").text, "html.parser").select("audio")


@pytest.mark.parametrize("header", [None, "", "bytes=1-2,4-5", "bytes=-4", "bytes=5-2", "bytes=0-8388608"])
def test_media_invalid_range_is_416_without_storage_read(review, header):
    review.item.media_s3_key = "items/podcast/episode/audio/private.mp3"
    db.session.commit()
    headers = {} if header is None else {"Range": header}
    assert review.client.get("/items/1/media", headers=headers).status_code == 416


@pytest.mark.parametrize("end", [10, 20])
def test_explicit_media_range_past_eof_returns_generic_416(review, monkeypatch, end):
    from io import BytesIO
    from web import routes
    from tests.test_s3_utils import manager
    storage, client = manager()
    body = BytesIO(b"23456789")
    client.get_object.return_value = dict(Body=body, ContentLength=8,
        ContentRange="bytes 2-9/10", ContentType="audio/mpeg",
        ResponseMetadata={"HTTPStatusCode": 206})
    review.item.media_s3_key = "items/podcast/episode/audio/private.mp3"
    db.session.commit()
    monkeypatch.setattr(routes, "get_s3_manager", lambda *args: storage)
    response = review.client.get("/items/1/media", headers={"Range": f"bytes=2-{end}"},
        content_type="application/json")
    assert response.status_code == 416 and response.json == {"error": "Request failed"}
    assert "private.mp3" not in response.text and body.closed


def test_duplicate_json_fields_and_nonobject_payloads_are_generic_400(review):
    data = json.dumps(correction(review))[:-1] + ', "label": "editorial"}'
    assert review.client.post("/api/items/1/classification/overrides", data=data,
        content_type="application/json").status_code == 400
    for value in ([], None, "private secret"):
        response = review.client.post("/api/items/1/classification/overrides", data=json.dumps(value),
            content_type="application/json")
        assert response.status_code == 400 and response.json == {"error": "Request could not be accepted"}


def test_running_state_is_closed_aggregate_metadata(review):
    revision = review.service.ensure_revision(1, "policy_upgrade", idempotency_key="running")
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == revision)
        .values(state="running", error_category=None))
    db.session.commit()
    assert review.client.get("/api/items/1").json["classification"]["state"] == "Running"


def test_missing_media_does_not_hide_failed_review_or_retry_form(review, monkeypatch):
    from web import routes
    # The classification can fail before any active policy exists.
    review.item.active_classification_revision_id = None
    review.item.media_s3_key = "items/podcast/episode/audio/private.mp3"
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == review.active)
        .values(state="failed", error_category="claude_timeout"))
    db.session.commit()
    def unavailable(*args):
        raise RuntimeError("secret provider failure")
    monkeypatch.setattr(routes, "get_s3_manager", unavailable)
    response = review.client.get("/items/1/classification")
    assert response.status_code == 200 and "Retry classification" in response.text
    assert "secret" not in response.text


def test_transcript_html_is_escaped_and_never_reaches_json(review, monkeypatch):
    item, transcript = add_item(review, external_id="escaped", texts=("Visible &lt;script&gt;alert('private')&lt;/script&gt;",))
    review.item, review.transcript = item, transcript
    finish(review)
    response = review.client.get(f"/items/{item.id}/classification")
    assert response.status_code == 200 and "Visible &lt;script&gt;" in response.text
    assert "<script>alert('private')" not in response.text
    assert "Visible" not in review.client.get(f"/api/items/{item.id}").text


def test_successful_html_correction_works_without_javascript_and_redirects(review, monkeypatch):
    monkeypatch.setattr(tasks, "publish_override", lambda *a: None)
    data = correction(review)
    data["active_revision_id"] = str(data["active_revision_id"])
    response = review.client.post("/items/1/classification/overrides", data=data)
    assert response.status_code == 303 and response.headers["Location"] == "/items/1/classification"
    assert m.SegmentOverride.query.one().label == "paid_commercial"


@pytest.mark.parametrize("publication", ["unknown", "fast_worker"])
def test_real_publication_handoff_preserves_both_outbox_generations(review, app, monkeypatch, publication):
    app.config["CLASSIFICATION_ENABLED"] = True
    def send(name, *, args, **kwargs):
        if publication == "unknown":
            raise RuntimeError("private broker output")
        if name == "tasks.classify_item":
            db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == args[0])
                .values(attempts=2, error_category="dispatch_pending"))
        else:
            db.session.execute(sa.update(m.ClassificationSweep).where(m.ClassificationSweep.id == args[0])
                .values(items_scanned=1, cursor_item_id=1, celery_task_id=f"source-rule-{args[0]}-after-1",
                    error_category="dispatch_pending"))
        db.session.commit()
    monkeypatch.setattr(tasks.celery_app, "send_task", send)
    response = review.client.post("/api/items/1/classification/overrides", json=correction(review))
    assert response.status_code == 202
    revision = db.session.get(m.ClassificationRevision, response.json["revision_id"])
    sweep = m.ClassificationSweep.query.one()
    assert revision.error_category == sweep.error_category == "dispatch_pending"
    if publication == "fast_worker":
        assert revision.attempts == 2 and sweep.items_scanned == 1


def test_review_queue_and_page_classification_queries_are_bounded(review):
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    sa.event.listen(db.engine, "before_cursor_execute", capture)
    try:
        assert review.client.get("/classification/review").status_code == 200
        assert review.client.get("/sources/1/items").status_code == 200
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", capture)
    revision_reads = [statement for statement in statements if "classification_revisions" in statement]
    assert len(revision_reads) <= 5
    assert any("count(" in statement.lower() for statement in revision_reads)
    assert any("LIMIT" in statement for statement in revision_reads)


def test_service_rechecks_revision_race_and_route_returns_409(review, monkeypatch):
    def race(**kwargs):
        db.session.execute(sa.update(m.ContentItem).where(m.ContentItem.id == 1)
            .values(active_classification_revision_id=None))
        db.session.commit()
        raise review.module.ClassificationServiceError("invalid_policy")
    monkeypatch.setattr(review.service, "record_override", race)
    assert review.client.post("/api/items/1/classification/overrides", json=correction(review)).status_code == 409


def test_keyset_continues_after_anchor_leaves_review_queue(review, monkeypatch):
    from web import routes
    monkeypatch.setattr(routes, "REVIEW_PAGE_SIZE", 1)
    item, _ = add_item(review, external_id="later-failure")
    failed = review.service.ensure_revision(item.id, "backfill")
    db.session.execute(sa.update(m.ClassificationRevision).where(m.ClassificationRevision.id == failed)
        .values(state="failed", error_category="claude_timeout"))
    db.session.commit()
    page = BeautifulSoup(review.client.get("/classification/review").text, "html.parser")
    following = page.find("a", string="Next reviews")["href"]
    # Completing a correction replaces the active uncertain policy, so the
    # immutable old revision disappears from review eligibility.
    outcome = review.service.record_override(item_id=1, admin_id=review.owner.id, **correction(review))
    prepared = review.service.prepare_revision(outcome.revision_id)
    for ordinal in prepared.block_ordinals:
        review.service.run_block(outcome.revision_id, ordinal)
    review.service.finalize_revision(outcome.revision_id)
    response = review.client.get(following)
    assert response.status_code == 200
    rows = BeautifulSoup(response.text, "html.parser").select("[data-review-revision]")
    assert [int(row["data-review-revision"]) for row in rows] == [failed]
