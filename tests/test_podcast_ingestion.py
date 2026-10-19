from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from ingestion_types import (DiscoveryPage, DownloadedMedia, IngestionError,
    ItemDescriptor, SourceDescriptor, normalize_public_url, redact_url)
from feed_resolver import resolve_podcast_source
from episode_fetcher import (discover_podcast_items, download_podcast_audio,
    download_audio, filter_items, generate_episode_id, list_audio_items, slugify)


FEED = Path(__file__).with_name("fixtures").joinpath("feed.xml").read_bytes()
URL = "https://feeds.example/show/feed.xml"
REQUEST = requests.sessions.Session.request


class Response:
    def __init__(self, body=b"", *, url=URL, status=200, headers=None, failure=None):
        self.body, self.url, self.status_code = body, url, status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.failure, self.closed, self.chunks = failure, False, []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("unsafe ?token=secret user:password")

    def iter_content(self, chunk_size):
        self.chunks.append(chunk_size)
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]
        if self.failure:
            raise self.failure

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["timeout"] == (3, 7)
        assert kwargs["stream"] is True
        assert "Authorization" not in kwargs.get("headers", {})
        assert self.responses, "Unexpected HTTP request"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Real network is forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])


@pytest.fixture
def settings():
    return SimpleNamespace(HTTP_CONNECT_TIMEOUT_SECONDS=3,
        HTTP_READ_TIMEOUT_SECONDS=7, RSS_MAX_BYTES=100_000,
        MAX_AUDIO_BYTES=200_000, PODCAST_INITIAL_ITEM_LIMIT=25)


@pytest.fixture
def source():
    return SourceDescriptor(source_type="podcast_rss", external_id=sha256(URL.encode()).hexdigest(),
        canonical_url=URL, title="Example")


def test_contracts_are_immutable_and_pages_copy_items(source, tmp_path):
    item = ItemDescriptor(source_type="podcast_rss", source_external_id=source.external_id,
        external_id="item", title="Episode", published_at=datetime(2026, 1, 1, 2,
        tzinfo=timezone(timedelta(hours=2))), media_url="https://cdn.example/e.mp3")
    items = [item]
    page = DiscoveryPage(items=items)
    items.clear()
    media = DownloadedMedia(path=tmp_path / "e.mp3", size_bytes=1, content_type="audio/mpeg")
    for value in (source, item, page, media):
        with pytest.raises(FrozenInstanceError):
            value.extra = "changed"
    assert page.items == (item,)
    assert item.published_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert item.published_at.tzinfo is timezone.utc
    naive = replace(item, published_at=datetime(2026, 1, 1))
    assert naive.published_at.tzinfo is timezone.utc


def test_contracts_freeze_nested_metadata_and_expose_neutral_fields(source):
    metadata = {"uploads_playlist_id": "UUexample", "nested": {"languages": ["en"]}}
    channel = replace(source, source_type="youtube_channel", provider_metadata=metadata)
    metadata["nested"]["languages"].append("fr")
    assert channel.provider_metadata["nested"]["languages"] == ("en",)
    with pytest.raises(TypeError):
        channel.provider_metadata["uploads_playlist_id"] = "changed"
    with pytest.raises(TypeError):
        channel.provider_metadata["nested"]["languages"] = ()
    video = ItemDescriptor(source_type="youtube_channel", source_external_id="channel",
        external_id="video", title="Video", published_at=None, media_url=None,
        platform="youtube", item_type="youtube_video", description="Description",
        creator_id="creator", creator_name="Creator", channel_id="channel",
        channel_name="Channel", channel_url="https://www.youtube.com/channel/UCexample",
        thumbnail_url="https://images.example/video.jpg", duration_seconds=60,
        provider_metadata={"languages": ["en"]})
    assert video.provider_metadata["languages"] == ("en",)
    assert video.platform == "youtube" and video.item_type == "youtube_video"
    assert DiscoveryPage(items=()).etag is None
    assert DiscoveryPage(items=(), etag="etag").etag == "etag"


def test_url_normalization_redacts_query_credentials_and_fragment():
    raw = "HTTPS://user:password@Feeds.Example:443/feed.xml?token=secret#secret"
    assert normalize_public_url(raw) == "https://feeds.example/feed.xml"
    assert redact_url(raw) == "https://feeds.example/feed.xml"
    assert redact_url("malformed secret") == "[invalid URL]"


def test_direct_feed_head_and_bounded_get(settings):
    head = Response(headers={"Content-Type": "application/rss+xml"})
    body = Response(FEED)
    session = Session(head, body)
    result = resolve_podcast_source("HTTPS://Feeds.Example:443/show/feed.xml#fragment", settings, session)
    assert result.external_id == sha256(URL.encode()).hexdigest()
    assert result.canonical_url == URL
    assert result.title == "Example Podcast"
    assert [c[0] for c in session.calls] == ["HEAD", "GET"]
    assert head.closed and body.closed
    assert body.chunks == [65536]


@pytest.mark.parametrize("head", [Response(status=405), Response(status=501), requests.Timeout("token=secret")])
def test_head_unsupported_falls_back_to_feed_get(head, settings):
    assert resolve_podcast_source(URL, settings, Session(head, Response(FEED))).title == "Example Podcast"


def test_html_relative_feed_link_uses_final_response_url(settings):
    page = Response(b'<html><link rel="alternate" type="application/rss+xml" href="../rss/feed.xml"></html>',
        url="https://feeds.example/podcast/show/", headers={"Content-Type": "text/html"})
    feed = Response(FEED, url="https://feeds.example/podcast/rss/feed.xml")
    session = Session(Response(headers={"Content-Type": "text/html"}), page, feed)
    result = resolve_podcast_source("https://feeds.example/show", settings, session)
    assert session.calls[-1][1] == "https://feeds.example/podcast/rss/feed.xml"
    assert result.canonical_url == "https://feeds.example/podcast/rss/feed.xml"


def test_apple_lookup_is_bounded_then_resolves_feed(settings):
    lookup = Response(json.dumps({"results": [{"feedUrl": URL}]}).encode())
    session = Session(lookup, Response(FEED))
    result = resolve_podcast_source("https://podcasts.apple.com/us/podcast/show/id123456789?i=1", settings, session)
    assert result.canonical_url == URL
    assert session.calls[0][1] == "https://itunes.apple.com/lookup?id=123456789"
    assert lookup.chunks == [65536] and lookup.closed


@pytest.mark.parametrize("body", [b"not json", b'{"results": []}', b'{"results": [{"feedUrl":"file:///secret"}]}'])
def test_apple_resolution_safe_failures(body, settings):
    with pytest.raises(IngestionError) as error:
        resolve_podcast_source("https://podcasts.apple.com/podcast/id123456789?token=secret", settings, Session(Response(body)))
    assert "secret" not in str(error.value)


def test_discovery_normalizes_dates_ids_order_and_relative_urls(source, settings):
    body = Response(FEED)
    page = discover_podcast_items(source, settings, Session(body))
    assert [item.title for item in page.items] == ["Newest", "Older", "No GUID"]
    newest, older, fallback = page.items
    assert newest.published_at == datetime(2026, 10, 13, 8, tzinfo=timezone.utc)
    assert newest.external_id == sha256((source.external_id + "episode-new").encode()).hexdigest()
    assert older.media_url == "https://feeds.example/show/audio/old.mp3"
    assert newest.media_url == "https://cdn.example/new.mp3"
    assert newest.canonical_url == "https://feeds.example/show/episodes/new"
    assert fallback.guid == "https://feeds.example/show/audio/fallback.mp3"
    assert fallback.external_id == sha256((source.external_id + fallback.guid).encode()).hexdigest()
    assert fallback.published_at.tzinfo is timezone.utc
    assert page.exhausted and page.next_cursor is None
    assert body.chunks == [65536] and body.closed


def many_items(count=30):
    entries = "".join(f'<item><title>Episode {n}</title><guid>{n}</guid><pubDate>2026-10-{n:02}T12:00:00Z</pubDate><enclosure url="https://cdn.example/{n}.mp3" type="audio/mpeg"/></item>' for n in range(1, count + 1))
    return f"<rss><channel><title>Many</title>{entries}</channel></rss>".encode()


def test_initial_25_and_explicit_count(source, settings):
    page = discover_podcast_items(source, settings, Session(Response(many_items())))
    assert len(page.items) == 25
    assert page.items[0].title == "Episode 30"
    assert page.items[-1].title == "Episode 6"
    assert not page.exhausted
    assert page.next_cursor == "25"
    page = discover_podcast_items(source, settings, Session(Response(many_items())), limit=28)
    assert len(page.items) == 28


def test_explicit_date_range_filters_before_count(source, settings):
    page = discover_podcast_items(source, settings, Session(Response(many_items())), limit=2,
        published_after=datetime(2026, 10, 10, tzinfo=timezone.utc),
        published_before=datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert [item.title for item in page.items] == ["Episode 12", "Episode 11"]


def test_missing_invalid_dates_and_duplicate_guids(source, settings):
    body = b'<rss><channel><item><title>Unknown date</title><guid>x</guid><pubDate>bad</pubDate><enclosure url="a.mp3" type="audio/mpeg"/></item><item><guid>x</guid><enclosure url="a.mp3" type="audio/mpeg"/></item></channel></rss>'
    page = discover_podcast_items(source, settings, Session(Response(body)))
    assert len(page.items) == 1 and page.items[0].published_at is None


@pytest.mark.parametrize("declared", [True, False])
def test_feed_body_limit_applies_to_resolve_and_discovery(declared, source, settings):
    settings.RSS_MAX_BYTES = 10
    for resolving in (True, False):
        body = Response(FEED, headers={"Content-Length": str(len(FEED))} if declared else {})
        responses = [Response(), body] if resolving else [body]
        with pytest.raises(IngestionError, match="response_too_large"):
            if resolving:
                resolve_podcast_source(URL, settings, Session(*responses))
            else:
                discover_podcast_items(source, settings, Session(*responses))
        assert body.closed


@pytest.mark.parametrize("body", [b"<html>not a feed</html>", b"not xml", b""])
def test_malformed_feed_safe_failure(body, source, settings):
    with pytest.raises(IngestionError, match="invalid_feed"):
        discover_podcast_items(source, settings, Session(Response(body)))


def test_audio_streams_and_returns_hash(source, settings, tmp_path):
    audio = b"a" * 70_000
    body = Response(audio, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(audio))})
    result = download_podcast_audio("https://cdn.example/a.mp3?signature=secret", tmp_path / "nested/a.mp3", settings, Session(body))
    assert result.path.read_bytes() == audio
    assert result.size_bytes == len(audio)
    assert result.sha256 == sha256(audio).hexdigest()
    assert result.content_type == "audio/mpeg"
    assert body.chunks == [65536] and body.closed


@pytest.mark.parametrize("kind", ["declared", "streamed", "interrupted", "empty", "truncated", "http"])
def test_failed_audio_cleans_partial_and_preserves_existing(kind, settings, tmp_path, caplog):
    settings.MAX_AUDIO_BYTES = 5
    destination = tmp_path / "audio.mp3"
    destination.write_bytes(b"previous")
    headers = {"Content-Length": "10"} if kind == "declared" else {}
    if kind == "truncated":
        headers["Content-Length"] = "5"
    response = Response(b"123456" if kind == "streamed" else b"" if kind == "empty" else b"123",
        headers=headers, status=500 if kind == "http" else 200,
        failure=requests.Timeout("?token=secret user:password") if kind == "interrupted" else None)
    with pytest.raises(IngestionError) as error:
        download_podcast_audio("https://cdn.example/a.mp3?token=secret", destination, settings, Session(response))
    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [destination]
    assert response.closed
    assert "secret" not in str(error.value) + caplog.text
    assert "password" not in str(error.value) + caplog.text


def test_resolution_http_failure_does_not_echo_sensitive_error(settings, caplog):
    with pytest.raises(IngestionError) as error:
        resolve_podcast_source(URL + "?token=secret", settings,
            Session(Response(status=405), requests.ConnectionError("user:password token=secret")))
    assert "secret" not in str(error.value) + caplog.text
    assert "password" not in str(error.value) + caplog.text


def test_redirects_are_bounded_and_credentials_never_forwarded(settings):
    redirect = Response(status=302, headers={"Location": "https://user:secret@other.example/feed"})
    with pytest.raises(IngestionError):
        resolve_podcast_source(URL, settings, Session(redirect))
    assert redirect.closed


def test_selection_validation_happens_before_http(source, settings):
    with pytest.raises(ValueError):
        discover_podcast_items(source, settings, Session(), limit=-1)
    with pytest.raises(ValueError):
        discover_podcast_items(source, settings, Session(),
            published_after=datetime(2026, 10, 13), published_before=datetime(2026, 10, 10))


def test_public_requests_remove_inherited_authentication(settings, monkeypatch):
    prepared = []

    def send(_session, request, **kwargs):
        prepared.append(request)
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.raw = io.BytesIO(FEED)
        return response

    # Exercise requests preparation, replacing only the actual network boundary.
    monkeypatch.setattr(requests.Session, "request", REQUEST)
    monkeypatch.setattr(requests.Session, "send", send)
    with requests.Session() as session:
        session.auth = ("user", "secret")
        session.headers["Authorization"] = "Bearer secret"
        session.headers["Proxy-Authorization"] = "Basic secret"
        session.cookies.set("session", "secret")
        resolve_podcast_source(URL, settings, session)
    for request in prepared:
        assert "Authorization" not in request.headers
        assert "Proxy-Authorization" not in request.headers
        assert "Cookie" not in request.headers


def test_signed_guid_is_hashed_exactly_but_not_exposed(source, settings):
    guid = "https://example.org/id?signature=secret"
    body = f'<rss><channel><item><guid isPermaLink="false">{guid}</guid><enclosure url="e.mp3" type="audio/mpeg"/></item></channel></rss>'.encode()
    item = discover_podcast_items(source, settings, Session(Response(body))).items[0]
    assert item.external_id == sha256((source.external_id + guid).encode()).hexdigest()
    assert "secret" not in repr(item)


def test_cursor_reads_same_snapshot_and_invalid_cursor_is_rejected(source, settings):
    page = discover_podcast_items(source, settings, Session(Response(many_items())), limit=10, cursor="25")
    assert len(page.items) == 5 and page.exhausted
    assert page.items[0].title == "Episode 5"
    for cursor in ("invalid", "-1"):
        with pytest.raises(ValueError):
            discover_podcast_items(source, settings, Session(), cursor=cursor)


def test_redirect_loop_is_bounded_and_closes_every_response(settings):
    redirects = [Response(status=302, headers={"Location": "/loop"}) for _ in range(6)]
    session = Session(*redirects)
    with pytest.raises(IngestionError, match="redirect_failed"):
        resolve_podcast_source(URL, settings, session)
    assert len(session.calls) == 6 and all(response.closed for response in redirects)


def test_apple_spoof_is_not_sent_to_lookup(settings):
    session = Session(Response(), Response(FEED))
    resolve_podcast_source("https://podcasts.apple.com.attacker.example/feed", settings, session)
    assert all("itunes.apple.com" not in url for _, url, _ in session.calls)


def test_empty_feed_is_successful_discovery(source, settings):
    page = discover_podcast_items(source, settings, Session(Response(b'<rss version="2.0"><channel><title>Empty</title></channel></rss>')))
    assert page.items == () and page.exhausted


@pytest.mark.parametrize("length", ["bad", "-1"])
def test_malformed_content_length_is_safe(length, settings, tmp_path):
    response = Response(b"audio", headers={"Content-Length": length})
    with pytest.raises(IngestionError, match="invalid_content_length"):
        download_podcast_audio(URL, tmp_path / "a.mp3", settings, Session(response))
    assert list(tmp_path.iterdir()) == []


def test_duration_is_seconds_not_enclosure_byte_length(source, settings):
    body = b'<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><item><itunes:duration>1:02:03</itunes:duration><enclosure url="a.mp3" type="audio/mpeg" length="999999"/></item></channel></rss>'
    item = discover_podcast_items(source, settings, Session(Response(body))).items[0]
    assert item.duration_seconds == 3723


def test_legacy_entry_points_delegate_to_bounded_intake(settings, tmp_path):
    from feed_resolver import resolve_feed
    settings.BACKFILL_REQUEST_MAX_ITEMS = 5000
    assert resolve_feed(" ", settings, Session()) is None
    assert resolve_feed(URL, settings, Session(Response(), Response(FEED))) == URL
    items = list_audio_items(URL, settings, Session(Response(FEED)))
    assert len(items) == 3 and items[0]["title"] == "Newest"
    assert len(filter_items(items, days=1, now=datetime(2026, 10, 14, tzinfo=timezone.utc))) == 1
    assert filter_items(items, last_n=1) == items[:1]
    assert download_audio(URL, str(tmp_path / "a.mp3"), settings, Session(Response(b"audio"))) == str(tmp_path / "a.mp3")
    assert generate_episode_id("guid", "unused") == sha256(b"guid").hexdigest()
    assert len(generate_episode_id(None, URL)) == 64
    assert slugify("Hello, Podcast World!") == "hello-podcast-world"


def test_partial_publication_date_does_not_depend_on_wall_clock(source, settings):
    body = b'<rss><channel><item><pubDate>October</pubDate><enclosure url="a.mp3" type="audio/mpeg"/></item></channel></rss>'
    item = discover_podcast_items(source, settings, Session(Response(body))).items[0]
    assert item.published_at == datetime(1970, 10, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("metadata", [["not a mapping"], {1: "nontext key"}, {"nested": {1: "nontext key"}}, {"object": object()}])
def test_metadata_rejects_non_mapping_or_mutable_values(source, metadata):
    with pytest.raises(TypeError):
        replace(source, provider_metadata=metadata)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "224.0.0.1", "0.0.0.0", "240.0.0.1", "[::1]", "[::]",
    "[fc00::1]", "[fe80::1]", "[ff02::1]", "[::ffff:127.0.0.1]"])
def test_private_ip_feed_and_audio_fail_before_http(host, settings, tmp_path):
    for download in (False, True):
        session = Session()
        with pytest.raises(IngestionError, match="unsafe_destination"):
            if download:
                download_podcast_audio(f"http://{host}/audio.mp3", tmp_path / "a.mp3", settings, session)
            else:
                resolve_podcast_source(f"http://{host}/feed", settings, session)
        assert session.calls == []


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::1"], []])
def test_dns_names_require_every_answer_to_be_public(addresses, settings, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs:
        [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses])
    session = Session()
    with pytest.raises(IngestionError, match="unsafe_destination"):
        resolve_podcast_source("https://internal.example/feed", settings, session)
    assert session.calls == []


def test_dns_failure_is_safe(settings, monkeypatch):
    def failed(*args, **kwargs):
        raise socket.gaierror("token=secret")
    monkeypatch.setattr(socket, "getaddrinfo", failed)
    with pytest.raises(IngestionError, match="unsafe_destination"):
        resolve_podcast_source(URL, settings, Session())


def test_redirect_to_private_address_is_rejected_before_second_request(settings):
    redirect = Response(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    session = Session(redirect)
    with pytest.raises(IngestionError, match="unsafe_destination"):
        resolve_podcast_source(URL, settings, session)
    assert len(session.calls) == 1 and redirect.closed


def test_public_ip_literal_is_allowed(settings):
    session = Session(Response(url="https://93.184.216.34/feed"), Response(FEED, url="https://93.184.216.34/feed"))
    assert resolve_podcast_source("https://93.184.216.34/feed", settings, session).canonical_url == "https://93.184.216.34/feed"
