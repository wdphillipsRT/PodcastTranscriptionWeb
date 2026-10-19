"""Owner-scoped historical navigation and public timestamp citations."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest

from extensions import db
from models import ContentItem, ContentSource, SourceItem
from tests.test_web_sources import workspace
from transcripts import normalize_vtt


def html(response):
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    return BeautifulSoup(response.text, "html.parser")


def test_all_backfilled_items_are_reachable_with_stable_bounded_navigation(workspace, monkeypatch):
    from web import routes
    with workspace.app.app_context():
        for number in range(405):
            item = ContentItem(admin_id=1, item_type="youtube_video", platform="youtube",
                external_id=f"older{number:06}", canonical_url=f"https://www.youtube.com/watch?v=older{number:06}",
                title=f"Historical {number}", published_at=(datetime(2025, 1, 1, tzinfo=timezone.utc)
                    + timedelta(days=number // 100)) if number >= 10 else None)
            db.session.add(item)
            db.session.flush()
            db.session.add(SourceItem(source_id=1, item_id=item.id))
        db.session.commit()
        expected = list(db.session.scalars(db.select(ContentItem.id).order_by(
            ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())))
    monkeypatch.setattr(routes, "get_s3_manager", lambda *a: SimpleNamespace(
        download_text_content=lambda *a, **k: "Oldest private transcript"))
    summary = html(workspace.client.get("/sources/1"))
    assert summary.find("a", string="Older items")
    page = html(workspace.client.get("/sources/1/items"))
    seen, pages = [], []
    while True:
        rows = page.select("tbody tr th a")
        assert 0 < len(rows) <= 200
        seen.extend(int(link["href"].rsplit("/", 1)[1]) for link in rows)
        pages.append(page)
        older = page.find("a", string="Older items")
        if not older:
            break
        assert len(pages) < 4
        page = html(workspace.client.get(older["href"]))
    assert seen == expected and seen[-1] == 1
    assert len(pages) == 3
    for expected_page in reversed(pages[:-1]):
        page = html(workspace.client.get(page.find("a", string="Newer items")["href"]))
        assert [link["href"] for link in page.select("tbody tr th a")] == [
            link["href"] for link in expected_page.select("tbody tr th a")]
    assert not page.find("a", string="Newer items")
    oldest = html(workspace.client.get("/items/1"))
    transcript = oldest.find("a", string="Read transcript")["href"]
    assert "Oldest private transcript" in workspace.client.get(transcript).text
    monkeypatch.setattr(routes, "current_user", SimpleNamespace(id=999))
    for path in ("/sources/1", "/sources/1/items", pages[0].find("a", string="Older items")["href"],
                 "/items/1", transcript):
        assert workspace.client.get(path).status_code == 404


@pytest.mark.parametrize("query", ["before=0", "before=-1", "before=abc", "before=1.5",
    "before=", "before=9223372036854775808", "before=1&after=1", "before=1&before=2", "after=%3Cscript%3E"])
def test_invalid_history_cursors_are_safe(workspace, query):
    for path in ("/sources/1", "/sources/1/items"):
        response = workspace.client.get(f"{path}?{query}")
        assert response.status_code == 400
        assert response.headers["Cache-Control"] == "no-store"


def test_history_cursor_must_belong_to_selected_source(workspace):
    with workspace.app.app_context():
        source = ContentSource(admin_id=1, source_type="podcast_rss", external_id="other-feed",
            canonical_url="https://example.com/feed", name="Other")
        db.session.add(source)
        db.session.commit()
        source_id = source.id
    for query in ("before=1", "after=1", "before=999"):
        assert workspace.client.get(f"/sources/{source_id}/items?{query}").status_code == 404


def test_older_navigation_is_stable_when_new_discovery_arrives(workspace):
    with workspace.app.app_context():
        for number in range(205):
            item = ContentItem(admin_id=1, item_type="youtube_video", platform="youtube", external_id=f"stable{number:05}",
                canonical_url=f"https://www.youtube.com/watch?v=stable{number:05}", title=f"Stable {number}",
                published_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
            db.session.add(item)
            db.session.flush()
            db.session.add(SourceItem(source_id=1, item_id=item.id))
        db.session.commit()
    page = html(workspace.client.get("/sources/1/items"))
    cursor = page.find("a", string="Older items")["href"]
    before = html(workspace.client.get(cursor))
    with workspace.app.app_context():
        item = ContentItem(admin_id=1, item_type="youtube_video", platform="youtube", external_id="newest00000",
            canonical_url="https://www.youtube.com/watch?v=newest00000", title="New discovery",
            published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        db.session.add(item)
        db.session.flush()
        db.session.add(SourceItem(source_id=1, item_id=item.id))
        db.session.commit()
    after = html(workspace.client.get(cursor))
    assert [row["href"] for row in before.select("tbody tr th a")] == [row["href"] for row in after.select("tbody tr th a")]


def segment_storage(workspace, monkeypatch, *, platform="youtube", canonical_url=None, mutate=None):
    from web import routes
    with workspace.app.app_context():
        row = db.session.get(ContentItem, 1)
        row.platform = platform
        row.item_type = "youtube_video" if platform == "youtube" else "podcast_episode"
        row.canonical_url = canonical_url or ("https://www.youtube.com/watch?v=abcdefghijk" if platform == "youtube"
            else "https://media.example.com/episode.mp3?episode=123")
        row.segments_s3_key = "items/private/segments.json"
        payload = normalize_vtt("WEBVTT\n\n00:00:01.234 --> 00:00:02.567\nPrivate <script>words</script>.\n",
            platform=platform, external_id=row.external_id, item_id=row.id, source_id=1,
            origin="creator_caption", language="en", provider_version="test", canonical_url=(
                row.canonical_url if canonical_url is None else None)).to_dict()
        if mutate:
            mutate(payload)
        db.session.commit()
    reads = []
    def download(key, *, max_bytes):
        assert 0 < max_bytes <= 10 * 1024 * 1024
        reads.append(key)
        return json.dumps(payload) if key.endswith("segments.json") else "Private <script>words</script>."
    monkeypatch.setattr(routes, "get_s3_manager", lambda *a: SimpleNamespace(download_text_content=download))
    return reads


@pytest.mark.parametrize("platform,expected", [
    ("youtube", "https://www.youtube.com/watch?v=abcdefghijk&t=1s"),
    ("podcast", "https://media.example.com/episode.mp3?episode=123#t=1.234,2.567")])
def test_item_and_transcript_expose_derived_citations_only(workspace, monkeypatch, platform, expected):
    segment_storage(workspace, monkeypatch, platform=platform,
        mutate=lambda payload: payload["segments"][0].update(citation="javascript:alert('secret')"))
    for path in ("/items/1", "/items/1/transcript"):
        response = workspace.client.get(path)
        page = html(response)
        link = page.select_one('section[aria-labelledby="citations-heading"] a')
        assert link and link["href"] == expected
        assert {"noopener", "noreferrer"} <= set(link["rel"])
        for secret in ("javascript:", "segments.json", "private.txt", "s3://", "transcript_sha256", "<script>words"):
            assert secret not in response.text
        if path.endswith("transcript"):
            assert "&lt;script&gt;words&lt;/script&gt;" in response.text
        else:
            assert "words&lt;/script&gt;" not in response.text
    assert "citation" not in workspace.client.get("/api/items/1").text


@pytest.mark.parametrize("changes", [
    {"start": -1}, {"start": True}, {"start": "1"}, {"start": float("nan")},
    {"end": float("inf")}, {"end": 0}, {"start": 10 ** 1000},
])
def test_invalid_segment_times_never_become_links(workspace, monkeypatch, changes):
    segment_storage(workspace, monkeypatch, mutate=lambda p: p["segments"][0].update(changes))
    assert not html(workspace.client.get("/items/1")).select('section[aria-labelledby="citations-heading"] a')


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://private:secret@example.com/file.mp3",
    "https://example.com/file.mp3?token=secret", "https://bucket.s3.amazonaws.com/private-key",
    "https://bucket.s3.cn-north-1.amazonaws.com.cn/private-key"])
@pytest.mark.parametrize("platform", ["youtube", "podcast"])
def test_invalid_canonical_locator_never_becomes_citation(workspace, monkeypatch, url, platform):
    segment_storage(workspace, monkeypatch, canonical_url=url, platform=platform)
    response = workspace.client.get("/items/1")
    assert not html(response).select('section[aria-labelledby="citations-heading"] a')
    assert "secret" not in response.text and "private-key" not in response.text


def test_mismatched_canonical_youtube_identity_never_becomes_citation(workspace, monkeypatch):
    segment_storage(workspace, monkeypatch, canonical_url="https://www.youtube.com/watch?v=other000000")
    assert not html(workspace.client.get("/items/1")).select('section[aria-labelledby="citations-heading"] a')


def test_citations_are_owner_scoped_before_storage_reads(workspace, monkeypatch):
    from web import routes
    reads = segment_storage(workspace, monkeypatch)
    monkeypatch.setattr(routes, "current_user", SimpleNamespace(id=999))
    assert workspace.client.get("/items/1").status_code == 404
    assert workspace.client.get("/items/1/transcript").status_code == 404
    assert not reads


@pytest.mark.parametrize("corrupt", ["private storage exception", "not-json", "[]", '{"segments": []}'])
def test_citation_storage_failures_are_private_and_do_not_break_transcript(workspace, monkeypatch, corrupt):
    from web import routes
    segment_storage(workspace, monkeypatch)
    def download(key, *, max_bytes):
        if key.endswith("segments.json"):
            if corrupt == "private storage exception":
                raise RuntimeError("secret https://signed.invalid?token=private")
            return corrupt
        return "Escaped transcript <private>"
    monkeypatch.setattr(routes, "get_s3_manager", lambda *a: SimpleNamespace(download_text_content=download))
    for path in ("/items/1", "/items/1/transcript"):
        response = workspace.client.get(path)
        assert not html(response).select('section[aria-labelledby="citations-heading"] a')
        assert "secret" not in response.text and "segments.json" not in response.text
        if path.endswith("transcript"):
            assert "Escaped transcript &lt;private&gt;" in response.text


@pytest.mark.parametrize("identity", [{"item_id": 2}, {"platform": "podcast"}, {"external_id": "other000000"}])
def test_segment_lineage_must_match_authorized_item(workspace, monkeypatch, identity):
    segment_storage(workspace, monkeypatch, mutate=lambda payload: payload.update(identity))
    assert not html(workspace.client.get("/items/1")).select('section[aria-labelledby="citations-heading"] a')
