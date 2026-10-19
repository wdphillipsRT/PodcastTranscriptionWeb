"""Offline contract tests for canonical YouTube discovery."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from unittest.mock import Mock
import traceback

import pytest
import requests

from ingestion_types import SourceDescriptor
from source_classifier import classify_source_url
from youtube_discovery import YouTubeDiscoveryClient, YouTubeDiscoveryError


VIDEO = "dQw4w9WgXcQ"
CHANNEL = "UCabcdefghijklmnopqrstuv"
PLAYLIST = "PLabcdefghijklmnopqrstuv"
UPLOADS = "UUabcdefghijklmnopqrstuv"
KEY = "secret-test-api-key"
BASE = "https://www.googleapis.com/youtube/v3"


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / "youtube" / f"{name}.json").read_text())


def response(payload=None, status=200, content_type="application/json", body=None):
    result = Mock()
    result.status_code = status
    result.headers = {"Content-Type": content_type}
    result.iter_content.return_value = [body if body is not None else json.dumps(payload).encode()]
    result.json.side_effect = AssertionError("must read bounded bytes before parsing JSON")
    return result


def client_with(*responses, **kwargs):
    session = Mock()
    session.get.side_effect = responses
    return YouTubeDiscoveryClient(KEY, session=session, **kwargs), session


@pytest.mark.parametrize("url,kind", [
    (f"https://youtu.be/{VIDEO}?t=2", "youtube_video"),
    (f"https://www.youtube.com/watch?v={VIDEO}", "youtube_video"),
    (f"https://m.youtube.com/shorts/{VIDEO}", "youtube_video"),
    (f"https://youtube.com/channel/{CHANNEL}", "youtube_channel"),
    ("https://www.youtube.com/@teacher", "youtube_channel"),
    ("https://www.youtube.com/user/Teacher", "youtube_channel"),
    (f"https://youtube.com/playlist?list={PLAYLIST}", "youtube_playlist"),
])
def test_classifier_recognizes_only_supported_public_shapes(url, kind):
    assert classify_source_url(url) == kind


@pytest.mark.parametrize("url", [
    "https://youtube.com/feed/history", "https://youtube.com/feed/subscriptions",
    "https://youtube.com/account", "https://studio.youtube.com/", "https://youtube.com/c/Teacher",
    "https://youtube.com/watch?v=bad", "https://youtube.com/channel/UCshort",
    "https://youtube.com/playlist?list=WL", "https://youtube.com/playlist?list=LL",
    f"https://youtube.com/watch?v={VIDEO}&v=abcdefghijk",
    f"https://youtube.com/watch?v={VIDEO}&list={PLAYLIST}",
    f"https://youtube.com/watch?v={VIDEO}&token=private",
    f"https://youtu.be/{VIDEO}?v=abcdefghijk",
    f"https://youtube.com:444/watch?v={VIDEO}",
    "https://youtube.com/@", "https://youtube.com/redirect?q=https://example.com",
])
def test_rejects_unsupported_or_ambiguous_youtube_urls_before_http(url):
    client, session = client_with()
    with pytest.raises(YouTubeDiscoveryError) as error:
        client.resolve_source(url)
    assert error.value.category == "invalid_url"
    assert not error.value.retryable
    session.get.assert_not_called()
    with pytest.raises(ValueError):
        classify_source_url(url)


@pytest.mark.parametrize("url", [f"https://youtube.com.evil.test/watch?v={VIDEO}",
    f"https://example.com/watch?v={VIDEO}", f"https://youtube-nocookie.com/embed/{VIDEO}"])
def test_client_rejects_non_youtube_hosts(url):
    client, session = client_with()
    with pytest.raises(YouTubeDiscoveryError, match="invalid_url"):
        client.resolve_source(url)
    session.get.assert_not_called()


@pytest.mark.parametrize("url", [f"https://youtu.be/{VIDEO}", f"https://youtube.com/watch?v={VIDEO}",
    f"https://youtube.com/shorts/{VIDEO}"])
def test_resolves_canonical_video_and_normalizes_metadata(url):
    result = response(fixture("public_video"))
    client, session = client_with(result)
    item = client.resolve_video(url)
    assert item.external_id == VIDEO
    assert item.source_external_id == VIDEO
    assert item.canonical_url == f"https://www.youtube.com/watch?v={VIDEO}"
    assert item.platform == "youtube" and item.item_type == "youtube_video"
    assert item.duration_seconds == 3723
    assert item.published_at == datetime(2026, 10, 18, 12, 30, tzinfo=timezone.utc)
    assert item.creator_id == item.channel_id == CHANNEL
    assert item.creator_name == item.channel_name == "Teacher"
    assert item.channel_url == f"https://www.youtube.com/channel/{CHANNEL}"
    assert item.media_url is None and item.requires_reacquisition
    args, kwargs = session.get.call_args
    assert args == (BASE + "/videos",)
    assert kwargs["params"]["key"] == KEY
    assert set(kwargs["params"]["part"].split(",")) == {"snippet", "contentDetails", "status", "liveStreamingDetails"}
    assert kwargs["timeout"] == (5, 20)
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    result.close.assert_called_once()


@pytest.mark.parametrize("path,selector,value", [
    (f"channel/{CHANNEL}", "id", CHANNEL), ("@teacher", "forHandle", "@teacher"),
    ("user/Teacher", "forUsername", "Teacher"),
])
def test_channel_resolution_stores_canonical_id_and_uploads(path, selector, value):
    client, session = client_with(response(fixture("channel")))
    source = client.resolve_source("https://youtube.com/" + path)
    assert source.source_type == "youtube_channel" and source.external_id == CHANNEL
    assert source.canonical_url == f"https://www.youtube.com/channel/{CHANNEL}"
    assert source.provider_metadata["uploads_playlist_id"] == UPLOADS
    assert session.get.call_args.args == (BASE + "/channels",)
    assert session.get.call_args.kwargs["params"][selector] == value
    assert session.get.call_args.kwargs["params"]["part"] == "snippet,contentDetails"


def test_playlist_source_resolution():
    client, session = client_with(response(fixture("playlist")))
    source = client.resolve_source(f"https://youtube.com/playlist?list={PLAYLIST}")
    assert source.source_type == "youtube_playlist" and source.external_id == PLAYLIST
    assert source.canonical_url == f"https://www.youtube.com/playlist?list={PLAYLIST}"
    assert session.get.call_args.args == (BASE + "/playlists",)
    assert session.get.call_args.kwargs["params"]["part"] == "snippet,contentDetails,status"


@pytest.mark.parametrize("kind,external_id,metadata", [
    ("youtube_playlist", PLAYLIST, {}),
    ("youtube_channel", CHANNEL, {"uploads_playlist_id": UPLOADS}),
])
def test_paginated_memberships_are_hydrated_in_order(kind, external_id, metadata):
    first = fixture("playlist_page")
    second = deepcopy(first)
    del second["nextPageToken"]
    client, session = client_with(response(first), response(fixture("public_video")),
                                  response(second), response(fixture("public_video")))
    source = SourceDescriptor(kind, external_id, "https://youtube.com/", "Lessons", provider_metadata=metadata)
    page = client.discover_page(source, None)
    assert isinstance(page.items, tuple) and len(page.items) == 1
    assert page.items[0].external_id == VIDEO and page.items[0].source_external_id == external_id
    assert page.next_cursor == "CAUQAA" and not page.exhausted
    assert page.etag == "membership-page-etag"
    params = session.get.call_args_list[0].kwargs["params"]
    assert params["playlistId"] == (UPLOADS if kind == "youtube_channel" else PLAYLIST)
    assert params["maxResults"] == 50
    assert "pageToken" not in params
    final = client.discover_page(source, page.next_cursor)
    assert final.exhausted and final.next_cursor is None
    assert session.get.call_args_list[2].kwargs["params"]["pageToken"] == "CAUQAA"


def test_video_source_has_one_page_and_preserves_etag():
    client, session = client_with(response(fixture("public_video")), response(fixture("public_video")))
    source = client.resolve_source(f"https://youtu.be/{VIDEO}")
    assert source.source_type == "youtube_video" and source.external_id == VIDEO
    page = client.discover_page(source, None)
    assert page.exhausted and page.etag == "video-page-etag" and len(page.items) == 1


@pytest.mark.parametrize("status,reason,category,retryable", [
    (400, "badRequest", "bad_request", False), (403, "forbidden", "forbidden", False),
    (404, "videoNotFound", "not_found", False),
    (403, "quotaExceeded", "quota_exhausted", True),
    (403, "dailyLimitExceeded", "quota_exhausted", True),
    (429, "rateLimitExceeded", "rate_limited", True), (500, "backendError", "upstream_error", True),
    (503, "backendError", "upstream_error", True),
    (403, "membersOnly", "members_only", False), (403, "ageRestricted", "age_restricted", False),
    (403, "regionRestriction", "region_restricted", False),
])
def test_typed_http_failures_never_include_provider_messages(status, reason, category, retryable):
    payload = {"error": {"code": status, "message": BASE + "/videos?key=" + KEY,
                         "errors": [{"reason": reason}]}}
    client, _ = client_with(response(payload, status=status))
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert caught.value.category == category and caught.value.retryable is retryable
    assert KEY not in str(caught.value) + repr(caught.value) + repr(client)


@pytest.mark.parametrize("mutation,category,retryable", [
    (lambda v: v["status"].update(privacyStatus="private"), "private", False),
    (lambda v: v["status"].update(uploadStatus="deleted"), "deleted", False),
    (lambda v: v["snippet"].update(liveBroadcastContent="upcoming"), "upcoming", True),
    (lambda v: v["snippet"].update(liveBroadcastContent="live"), "live", True),
    (lambda v: v["contentDetails"].update(regionRestriction={"blocked": ["US"]}), "region_restricted", False),
    (lambda v: v["contentDetails"].update(contentRating={"ytRating": "ytAgeRestricted"}), "age_restricted", False),
])
def test_video_availability_restrictions(mutation, category, retryable):
    payload = fixture("public_video")
    mutation(payload["items"][0])
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert (caught.value.category, caught.value.retryable) == (category, retryable)


def test_omitted_private_deleted_or_members_only_video_is_unavailable():
    client, _ = client_with(response({"items": []}))
    with pytest.raises(YouTubeDiscoveryError, match="private_or_deleted"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")


@pytest.mark.parametrize("payload", [[], {}, {"items": {}}, {"items": [None]},
    {"items": [{"id": "invalid"}]}, {"items": [], "nextPageToken": []}])
def test_rejects_malformed_json_shapes(payload):
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError, match="invalid_response"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")


@pytest.mark.parametrize("body,content_type", [(b"<html>error</html>", "text/html"),
    (b"{invalid", "application/json"), (b"[]", "application/json")])
def test_rejects_non_json_and_invalid_body(body, content_type):
    result = response(body=body, content_type=content_type)
    client, _ = client_with(result)
    with pytest.raises(YouTubeDiscoveryError, match="invalid_response"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    result.close.assert_called_once()


def test_response_size_is_bounded():
    result = response(body=b" " * 129)
    client, _ = client_with(result, max_response_bytes=128)
    with pytest.raises(YouTubeDiscoveryError, match="response_too_large"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    result.close.assert_called_once()


def test_transport_exception_does_not_retain_secret_in_traceback(caplog):
    session = Mock()
    session.get.side_effect = requests.Timeout(BASE + "/videos?key=" + KEY)
    client = YouTubeDiscoveryClient(KEY, session=session)
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert caught.value.category == "transport_error" and caught.value.retryable
    assert KEY not in "".join(traceback.format_exception(caught.value)) + caplog.text + repr(client)


def test_sensitive_query_credentials_never_reach_transport():
    client, session = client_with()
    with pytest.raises(YouTubeDiscoveryError):
        client.resolve_video(f"https://youtu.be/{VIDEO}?API_Key={KEY}")
    session.get.assert_not_called()


@pytest.mark.parametrize("path,value", [
    (("status",), []), (("status", "uploadStatus"), []),
    (("snippet", "liveBroadcastContent"), []), (("snippet", "publishedAt"), "yesterday"),
    (("snippet", "publishedAt"), "2026-10-18T12:30:00"),
    (("snippet", "channelId"), "UCinvalid"), (("snippet", "title"), None),
    (("snippet", "thumbnails"), []), (("contentDetails", "duration"), "PT"),
    (("contentDetails", "duration"), "P1M"), (("contentDetails", "duration"), "PT" + "9" * 400 + "S"),
    (("contentDetails", "regionRestriction"), []), (("contentDetails", "contentRating"), []),
])
def test_nested_malformed_video_data_always_has_a_typed_failure(path, value):
    payload = fixture("public_video")
    target = payload["items"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError, match="invalid_response"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")


def test_debug_transport_logs_redact_query_credentials(caplog):
    session = Mock()
    def get(*args, **kwargs):
        logging.getLogger("urllib3.connectionpool").debug('GET /youtube/v3/videos?key=%s HTTP/1.1', KEY)
        return response(fixture("public_video"))
    session.get.side_effect = get
    with caplog.at_level(logging.DEBUG):
        YouTubeDiscoveryClient(KEY, session=session).resolve_video(f"https://youtu.be/{VIDEO}")
    assert KEY not in caplog.text and "[redacted]" in caplog.text


def test_query_auth_does_not_use_session_cookies_or_account_auth():
    client, session = client_with(response(fixture("public_video")))
    client.resolve_video(f"https://youtu.be/{VIDEO}")
    kwargs = session.get.call_args.kwargs
    prepared = requests.Request("GET", BASE + "/videos", auth=kwargs["auth"],
                                headers=kwargs["headers"]).prepare()
    assert not prepared.headers.get("Authorization")
    assert not prepared.headers.get("Cookie")
    assert kwargs["auth"] is not None
    session_with_account = requests.Session()
    session_with_account.auth = ("account", "password")
    session_with_account.headers["Authorization"] = "Bearer private-token"
    session_with_account.cookies.set("account", "private-cookie")
    inherited = session_with_account.prepare_request(requests.Request(
        "GET", BASE + "/videos", auth=kwargs["auth"], headers=kwargs["headers"]))
    assert "Authorization" not in inherited.headers and "Cookie" not in inherited.headers


def test_empty_playlist_does_not_make_a_video_api_request():
    client, session = client_with(response({"etag": "empty", "items": []}))
    source = SourceDescriptor("youtube_playlist", PLAYLIST, f"https://www.youtube.com/playlist?list={PLAYLIST}", "Empty")
    page = client.discover_page(source)
    assert page.items == () and page.exhausted and page.etag == "empty"
    assert session.get.call_count == 1


def test_batch_videos_reordered_by_provider_keep_playlist_order():
    memberships = fixture("playlist_page")
    second = deepcopy(memberships["items"][0])
    second["contentDetails"]["videoId"] = "abcdefghijk"
    second["snippet"]["resourceId"]["videoId"] = "abcdefghijk"
    memberships["items"].append(second)
    videos = fixture("public_video")
    another = deepcopy(videos["items"][0])
    another["id"] = "abcdefghijk"
    videos["items"].insert(0, another)
    client, session = client_with(response(memberships), response(videos))
    source = SourceDescriptor("youtube_playlist", PLAYLIST, "https://youtube.com/", "Lessons")
    assert [v.external_id for v in client.discover_page(source).items] == [VIDEO, "abcdefghijk"]
    assert session.get.call_args.kwargs["params"]["id"] == VIDEO + ",abcdefghijk"


@pytest.mark.parametrize("source,token", [
    (SourceDescriptor("podcast_rss", "id", "https://example.com/", "Invalid"), None),
    (SourceDescriptor("youtube_channel", CHANNEL, "https://youtube.com/", "Missing uploads"), None),
    (SourceDescriptor("youtube_video", "bad", "https://youtube.com/", "Bad video"), None),
    (SourceDescriptor("youtube_playlist", PLAYLIST, "https://youtube.com/", "Bad cursor"), "\nsecret"),
])
def test_invalid_source_or_cursor_is_rejected_before_http(source, token):
    client, session = client_with()
    with pytest.raises(YouTubeDiscoveryError):
        client.discover_page(source, token)
    session.get.assert_not_called()


def test_unavailable_playlist_member_has_typed_error_without_advancing_cursor():
    client, _ = client_with(response(fixture("playlist_page")), response({"items": []}))
    source = SourceDescriptor("youtube_playlist", PLAYLIST, "https://youtube.com/", "Lessons")
    with pytest.raises(YouTubeDiscoveryError, match="private_or_deleted"):
        client.discover_page(source)


def test_close_transport_failure_is_safe():
    result = response(fixture("public_video"))
    result.close.side_effect = requests.ConnectionError(BASE + "/videos?key=" + KEY)
    client, _ = client_with(result)
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert caught.value.category == "transport_error"
    assert KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("payload,status", [({"items": []}, 302), ({"items": []}, 307)])
def test_api_redirects_are_never_followed(payload, status):
    result = response(payload, status=status)
    result.headers["Location"] = "https://attacker.example/collect"
    client, session = client_with(result)
    with pytest.raises(YouTubeDiscoveryError, match="invalid_response"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert session.get.call_count == 1 and session.get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("duration,seconds", [("PT0S", 0), ("PT45M", 2700),
    ("P1DT2H3M4S", 93784), ("PT1.5S", 1)])
def test_iso8601_duration_formats(duration, seconds):
    payload = fixture("public_video")
    payload["items"][0]["contentDetails"]["duration"] = duration
    client, _ = client_with(response(payload))
    assert client.resolve_video(f"https://youtu.be/{VIDEO}").duration_seconds == seconds


@pytest.mark.parametrize("payload,category", [
    ({"items": []}, "not_found"),
    ({"items": [{"id": CHANNEL, "snippet": {"title": "Teacher"}, "contentDetails": {}}]}, "invalid_response"),
])
def test_missing_or_invalid_channel_resolution(payload, category):
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError, match=category):
        client.resolve_source("https://youtube.com/@teacher")


def test_private_playlist_is_rejected():
    payload = fixture("playlist")
    payload["items"][0]["status"]["privacyStatus"] = "private"
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError, match="private"):
        client.resolve_source(f"https://youtube.com/playlist?list={PLAYLIST}")


@pytest.mark.parametrize("status,category,retryable", [
    (429, "rate_limited", True), (500, "upstream_error", True), (503, "upstream_error", True),
    (400, "bad_request", False), (401, "forbidden", False),
    (403, "forbidden", False), (404, "not_found", False),
])
@pytest.mark.parametrize("body", [b"", b"{malformed", b"[]", b'"scalar"', b"null", b"42",
                                  b'{"error": {"errors": "invalid"}}'])
def test_http_status_classification_survives_invalid_error_body(status, category, retryable, body, caplog):
    result = response(body=body, status=status)
    client, _ = client_with(result)
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert (caught.value.category, caught.value.retryable) == (category, retryable)
    assert KEY not in "".join(traceback.format_exception(caught.value)) + caplog.text
    result.close.assert_called_once()


@pytest.mark.parametrize("body", [b"", b"{malformed", b"[]", b'"scalar"', b"null"])
def test_successful_status_still_requires_strict_json_object(body):
    result = response(body=body)
    client, _ = client_with(result)
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert caught.value.category == "invalid_response" and not caught.value.retryable
    result.close.assert_called_once()


@pytest.mark.parametrize("status,category", [(429, "rate_limited"), (503, "upstream_error"),
                                             (403, "forbidden")])
def test_oversized_error_body_stops_reading_and_falls_back_to_http_status(status, category):
    result = response(status=status)
    def chunks(**kwargs):
        yield b"x" * 129
        raise AssertionError("must stop after the byte ceiling")
    result.iter_content.side_effect = chunks
    client, _ = client_with(result, max_response_bytes=128)
    with pytest.raises(YouTubeDiscoveryError) as caught:
        client.resolve_video(f"https://youtu.be/{VIDEO}")
    assert caught.value.category == category
    result.close.assert_called_once()


@pytest.mark.parametrize("privacy,category", [("unlisted", "unlisted"), (None, "invalid_response"),
                                            ("unknown", "invalid_response"), ("", "invalid_response")])
@pytest.mark.parametrize("operation", ["resolve_video", "resolve_source", "discover_page"])
def test_discovery_requires_explicit_public_video_privacy(privacy, category, operation):
    payload = fixture("public_video")
    payload["items"][0]["status"]["privacyStatus"] = privacy
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError) as caught:
        if operation == "discover_page":
            source = SourceDescriptor("youtube_video", VIDEO, f"https://youtube.com/watch?v={VIDEO}", "Public video")
            client.discover_page(source)
        else:
            getattr(client, operation)(f"https://youtu.be/{VIDEO}")
    assert caught.value.category == category and not caught.value.retryable


def test_discovery_missing_video_privacy_fails_closed():
    payload = fixture("public_video")
    del payload["items"][0]["status"]["privacyStatus"]
    client, _ = client_with(response(payload))
    with pytest.raises(YouTubeDiscoveryError, match="^invalid_response$"):
        client.resolve_video(f"https://youtu.be/{VIDEO}")


def test_public_completed_livestream_remains_discoverable():
    payload = fixture("public_video")
    payload["items"][0]["liveStreamingDetails"] = {
        "actualStartTime": "2026-10-18T11:00:00Z", "actualEndTime": "2026-10-18T12:00:00Z"}
    client, _ = client_with(response(payload))
    assert client.resolve_video(f"https://youtu.be/{VIDEO}").external_id == VIDEO


@pytest.mark.parametrize("present", [True, False])
def test_exact_playlist_membership_lookup_is_one_bounded_official_request(present):
    payload = fixture("playlist_page")
    if not present:
        payload["items"] = []
    client, session = client_with(response(payload))
    source = SourceDescriptor("youtube_playlist", PLAYLIST, f"https://youtube.com/playlist?list={PLAYLIST}", "List")
    assert client.source_contains_video(source, VIDEO) is present
    assert session.get.call_count == 1
    assert session.get.call_args.args == (BASE + "/playlistItems",)
    params = session.get.call_args.kwargs["params"]
    assert params["playlistId"] == PLAYLIST and params["videoId"] == VIDEO and params["maxResults"] == 1
    assert "pageToken" not in params


@pytest.mark.parametrize("mutation", ["wrong_video", "wrong_playlist", "too_many", "private"])
def test_playlist_membership_lookup_fails_closed_for_wrong_or_unsafe_response(mutation):
    payload = fixture("playlist_page")
    if mutation == "wrong_video":
        payload["items"][0]["contentDetails"]["videoId"] = "abcdefghijk"
    elif mutation == "wrong_playlist":
        payload["items"][0]["snippet"]["playlistId"] = "PLzyxwvutsrqponmlkjihgfe"
    elif mutation == "too_many":
        payload["items"].append(deepcopy(payload["items"][0]))
    else:
        payload["items"][0]["status"]["privacyStatus"] = "private"
    client, session = client_with(response(payload))
    source = SourceDescriptor("youtube_playlist", PLAYLIST, f"https://youtube.com/playlist?list={PLAYLIST}", "List")
    with pytest.raises(YouTubeDiscoveryError):
        client.source_contains_video(source, VIDEO)
    assert session.get.call_count == 1


def test_playlist_membership_lookup_rejects_invalid_selectors_before_http():
    client, session = client_with()
    source = SourceDescriptor("youtube_playlist", "invalid", "https://youtube.com/", "List")
    with pytest.raises(YouTubeDiscoveryError):
        client.source_contains_video(source, VIDEO)
    session.get.assert_not_called()
