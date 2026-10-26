"""Public, API-key-only YouTube discovery with bounded and safe HTTP I/O.

Only official Data API endpoints are contacted. No web pages, OAuth, cookies or
media locators are consumed. Missing videos cannot be reliably distinguished as
private, deleted or members-only by this API; media acquisition must recheck
availability. Region restrictions are conservatively rejected without a region.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import re

import requests

from ingestion_types import DiscoveryPage, IngestionError, ItemDescriptor, SourceDescriptor
from source_classifier import CHANNEL_ID, PLAYLIST_ID, VIDEO_ID, youtube_locator


API_BASE = "https://www.googleapis.com/youtube/v3"
_PARTS = {"videos": "snippet,contentDetails,status,liveStreamingDetails",
          "channels": "snippet,contentDetails", "playlists": "snippet,contentDetails,status",
          "playlistItems": "snippet,contentDetails,status"}
_RETRYABLE = frozenset({"transport_error", "quota_exhausted", "rate_limited", "upstream_error", "upcoming", "live"})
_UNAVAILABLE = frozenset({"private", "unlisted", "deleted", "unavailable", "private_or_deleted",
                          "upcoming", "live", "region_restricted", "age_restricted", "members_only"})


class YouTubeDiscoveryError(IngestionError):
    """Safe stable category; retryable means a later attempt may succeed.

    Quota exhaustion requires waiting for quota reset; live/upcoming requires
    waiting for publication/completion. No automatic retries spend extra quota.
    """

    def __init__(self, category: str):
        self.category = category
        self.retryable = category in _RETRYABLE
        super().__init__(category)


def _fail(category):
    raise YouTubeDiscoveryError(category)


def _text(value, *, required=False):
    if not isinstance(value, str) or (required and not value):
        _fail("invalid_response")
    return value


def _mapping(value):
    if not isinstance(value, dict):
        _fail("invalid_response")
    return value


def _identity(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail("invalid_response")
    return value


def _thumbnail(snippet):
    thumbnails = _mapping(snippet.get("thumbnails", {}))
    for size in ("maxres", "standard", "high", "medium", "default"):
        if size in thumbnails:
            return _text(_mapping(thumbnails[size]).get("url"), required=True)
    return None


def _published(value):
    try:
        value = _text(value, required=True)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail("invalid_response")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail("invalid_response")


def _duration(value):
    # YouTube documents days plus hours/minutes/seconds, not calendar months.
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", _text(value))
    if not match or not any(match.groups()) or value.endswith("T"):
        _fail("invalid_response")
    try:
        days, hours, minutes, seconds = (float(v or 0) for v in match.groups())
        return int(days * 86400 + hours * 3600 + minutes * 60 + seconds)
    except (ValueError, OverflowError):
        _fail("invalid_response")


def _public_api_auth(request):
    """Override session/netrc auth and strip any session account cookies."""
    request.headers.pop("Authorization", None)
    request.headers.pop("Cookie", None)
    return request


class _RedactTransport(logging.Filter):
    """urllib3's debug request line includes query authentication otherwise."""

    def __init__(self, secret):
        super().__init__()
        self._secret = secret

    def filter(self, record):
        message = record.getMessage()
        record.msg = re.sub(r"([?&]key=)[^&\s\"']+", r"\1[redacted]", message.replace(self._secret, "[redacted]"))
        record.args = ()
        return True

    def __repr__(self):
        return "<_RedactTransport redacted>"


@contextmanager
def _safe_transport_logging(secret):
    filter_ = _RedactTransport(secret)
    loggers = [logging.getLogger(name) for name in
               ("urllib3.connectionpool", "urllib3.util.retry",
                "requests.packages.urllib3.connectionpool", "requests.packages.urllib3.util.retry")]
    for logger in loggers:
        logger.addFilter(filter_)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(filter_)


class YouTubeDiscoveryClient:
    """Synchronous injected-session client, safe to represent in diagnostics."""

    def __init__(self, api_key: str, session=None, *, timeout=(5, 20), max_response_bytes=2 * 1024 * 1024):
        if not isinstance(api_key, str) or not api_key.strip():
            _fail("missing_api_key")
        if (not isinstance(timeout, tuple) or len(timeout) != 2
                or any(type(v) not in {int, float} or not 0 < v <= 60 for v in timeout)
                or type(max_response_bytes) is not int or not 0 < max_response_bytes <= 8 * 1024 * 1024):
            _fail("invalid_configuration")
        self.__api_key = api_key
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._max_bytes = max_response_bytes

    def __repr__(self):
        return "<YouTubeDiscoveryClient credentials=redacted>"

    def _request(self, resource, **params):
        # Resource is selected exclusively by internal callers, never URLs.
        if resource not in _PARTS:
            _fail("invalid_source")
        params = {"part": _PARTS[resource], **params, "key": self.__api_key}
        response = None
        try:
            with _safe_transport_logging(self.__api_key):
                response = self._session.get(API_BASE + "/" + resource, params=params,
                                             timeout=self._timeout, stream=True, allow_redirects=False,
                                             auth=_public_api_auth, headers={"Accept": "application/json"})
                status = response.status_code
                failed_status = not 200 <= status < 300
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    if failed_status:
                        self._http_error(status, {})
                    _fail("invalid_response")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    if len(body) + len(chunk) > self._max_bytes:
                        if failed_status:
                            self._http_error(status, {})
                        _fail("response_too_large")
                    body.extend(chunk)
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    if failed_status:
                        self._http_error(status, {})
                    _fail("invalid_response")
                if failed_status:
                    # Error bodies are optional hints, never the authority for
                    # whether an HTTP failure can be retried. Proxies commonly
                    # return empty, malformed or scalar bodies for 429/5xx.
                    self._http_error(status, data if isinstance(data, dict) else {})
                data = _mapping(data)
                if "error" in data:
                    _fail("invalid_response")
                items = data.get("items")
                if not isinstance(items, list) or len(items) > 50 or any(not isinstance(v, dict) for v in items):
                    _fail("invalid_response")
                for key in ("etag", "nextPageToken"):
                    if key in data:
                        _text(data[key], required=True)
                        if len(data[key]) > 4096 or any(ord(c) < 32 for c in data[key]):
                            _fail("invalid_response")
                return data
        except requests.RequestException:
            raise YouTubeDiscoveryError("transport_error") from None
        finally:
            if response is not None:
                # requests.Response.close() is local cleanup, never another request.
                try:
                    with _safe_transport_logging(self.__api_key):
                        response.close()
                except (requests.RequestException, OSError):
                    raise YouTubeDiscoveryError("transport_error") from None

    @staticmethod
    def _http_error(status, data):
        error = data.get("error", {})
        errors = error.get("errors", []) if isinstance(error, dict) else []
        reasons = {v.get("reason") for v in errors if isinstance(v, dict) and isinstance(v.get("reason"), str)} if isinstance(errors, list) else set()
        for known, category in (({"quotaExceeded", "dailyLimitExceeded"}, "quota_exhausted"),
                                ({"rateLimitExceeded", "userRateLimitExceeded"}, "rate_limited"),
                                ({"membersOnly", "membershipRequired"}, "members_only"),
                                ({"ageRestricted"}, "age_restricted"),
                                ({"regionRestriction", "regionRestricted"}, "region_restricted")):
            if reasons & known:
                _fail(category)
        if status == 429:
            _fail("rate_limited")
        if 500 <= status <= 599:
            _fail("upstream_error")
        _fail({400: "bad_request", 401: "forbidden", 403: "forbidden", 404: "not_found"}.get(status, "invalid_response"))

    @staticmethod
    def _locator(url):
        try:
            return youtube_locator(url)
        except (IngestionError, ValueError, TypeError):
            raise YouTubeDiscoveryError("invalid_url") from None

    @staticmethod
    def _single(data, identity=None):
        items = data["items"]
        if not items:
            _fail("not_found")
        if len(items) != 1 or (identity is not None and items[0].get("id") != identity):
            _fail("invalid_response")
        return items[0]

    def resolve_source(self, url: str) -> SourceDescriptor:
        kind, selector, identity = self._locator(url)
        if kind == "youtube_video":
            data = self._request("videos", id=identity)
            item = self._videos(data, [identity], kind, identity)[0]
            return SourceDescriptor(kind, identity, item.canonical_url, item.title, item.description,
                                    item.thumbnail_url, {"channel_id": item.channel_id},
                                    hosts=(item.channel_name,) if item.channel_name else ())
        resource = "channels" if kind == "youtube_channel" else "playlists"
        data = self._request(resource, **{selector: identity})
        entry = self._single(data, identity if selector == "id" else None)
        identity = _identity(entry.get("id"), CHANNEL_ID if kind == "youtube_channel" else PLAYLIST_ID)
        snippet = _mapping(entry.get("snippet"))
        metadata = {}
        if kind == "youtube_channel":
            details = _mapping(entry.get("contentDetails"))
            playlists = _mapping(details.get("relatedPlaylists"))
            metadata["uploads_playlist_id"] = _identity(playlists.get("uploads"), PLAYLIST_ID)
            canonical = f"https://www.youtube.com/channel/{identity}"
        else:
            self._public_status(entry)
            metadata = {"channel_id": _identity(snippet.get("channelId"), CHANNEL_ID),
                        "channel_name": _text(snippet.get("channelTitle"))}
            canonical = f"https://www.youtube.com/playlist?list={identity}"
        try:
            title = _text(snippet.get("title"), required=True)
            # The channel owner is the host: the channel itself, or the playlist's channel.
            owner = title if kind == "youtube_channel" else metadata.get("channel_name")
            return SourceDescriptor(kind, identity, canonical, title, _text(snippet.get("description", "")),
                                    _thumbnail(snippet), metadata, hosts=(owner,) if owner else ())
        except IngestionError:
            raise YouTubeDiscoveryError("invalid_response") from None

    def resolve_video(self, url: str) -> ItemDescriptor:
        kind, _, identity = self._locator(url)
        if kind != "youtube_video":
            _fail("invalid_url")
        return self._videos(self._request("videos", id=identity), [identity], kind, identity)[0]

    def source_contains_video(self, source: SourceDescriptor, video_id: str) -> bool:
        """Check one exact playlist membership with one bounded official call.

        A filtered lookup needs no broad playlist scan or pagination. Video
        availability/ownership metadata is validated separately by resolve_video.
        """
        if not isinstance(source, SourceDescriptor) or source.source_type != "youtube_playlist":
            _fail("invalid_source")
        playlist_id = _identity(source.external_id, PLAYLIST_ID)
        video_id = _identity(video_id, VIDEO_ID)
        data = self._request("playlistItems", playlistId=playlist_id, videoId=video_id, maxResults=1)
        if not data["items"]:
            return False
        if len(data["items"]) != 1:
            _fail("invalid_response")
        entry = data["items"][0]
        self._public_status(entry)
        details = _mapping(entry.get("contentDetails"))
        snippet = _mapping(entry.get("snippet"))
        resource = _mapping(snippet.get("resourceId"))
        if (details.get("videoId") != video_id or resource.get("kind") != "youtube#video"
                or resource.get("videoId") != video_id or snippet.get("playlistId", playlist_id) != playlist_id):
            _fail("invalid_response")
        return True

    @staticmethod
    def _public_status(entry):
        status = _mapping(entry.get("status"))
        privacy = _text(status.get("privacyStatus"), required=True)
        if privacy == "private":
            _fail("private")
        if privacy == "unlisted":
            _fail("unlisted")
        if privacy != "public":
            _fail("invalid_response")
        upload = _text(status.get("uploadStatus", "processed"), required=True)
        if upload == "deleted":
            _fail("deleted")
        if upload in {"failed", "rejected"}:
            _fail("unavailable")
        if upload != "processed":
            _fail("upcoming")

    def _videos(self, data, identities, source_type, source_id, *, skip_unavailable=False):
        entries = {}
        for entry in data["items"]:
            identity = _identity(entry.get("id"), VIDEO_ID)
            if identity not in identities or identity in entries:
                _fail("invalid_response")
            entries[identity] = entry
        items = []
        for identity in identities:
            if identity not in entries:
                if skip_unavailable:
                    continue
                _fail("private_or_deleted")
            entry = entries[identity]
            try:
                items.append(self._video(entry, identity, source_type, source_id))
            except YouTubeDiscoveryError as error:
                if not skip_unavailable or error.category not in _UNAVAILABLE:
                    raise
        return tuple(items)

    def _video(self, entry, identity, source_type, source_id):
        self._public_status(entry)
        snippet = _mapping(entry.get("snippet"))
        live = _text(snippet.get("liveBroadcastContent", "none"), required=True)
        if live in {"live", "upcoming"}:
            _fail(live)
        if live != "none":
            _fail("invalid_response")
        live_details = _mapping(entry.get("liveStreamingDetails", {}))
        if live_details.get("scheduledStartTime") and not live_details.get("actualStartTime"):
            _fail("upcoming")
        if live_details.get("actualStartTime") and not live_details.get("actualEndTime"):
            _fail("live")
        details = _mapping(entry.get("contentDetails"))
        restriction = _mapping(details.get("regionRestriction", {}))
        if restriction:
            _fail("region_restricted")
        rating = _mapping(details.get("contentRating", {}))
        if rating.get("ytRating") == "ytAgeRestricted":
            _fail("age_restricted")
        channel = _identity(snippet.get("channelId"), CHANNEL_ID)
        channel_name = _text(snippet.get("channelTitle"))
        try:
            return ItemDescriptor(source_type=source_type, source_external_id=source_id, external_id=identity,
                                  title=_text(snippet.get("title"), required=True),
                                  published_at=_published(snippet.get("publishedAt")),
                                  canonical_url=f"https://www.youtube.com/watch?v={identity}",
                                  description=_text(snippet.get("description", "")),
                                  platform="youtube", item_type="youtube_video", creator_id=channel,
                                  creator_name=channel_name, channel_id=channel, channel_name=channel_name,
                                  channel_url=f"https://www.youtube.com/channel/{channel}",
                                  thumbnail_url=_thumbnail(snippet), duration_seconds=_duration(details.get("duration")),
                                  requires_reacquisition=True)
        except IngestionError:
            raise YouTubeDiscoveryError("invalid_response") from None

    def discover_page(self, source: SourceDescriptor, page_token: str | None = None) -> DiscoveryPage:
        if not isinstance(source, SourceDescriptor):
            _fail("invalid_source")
        if page_token is not None and (not isinstance(page_token, str) or not page_token
                                       or len(page_token) > 4096 or any(ord(c) < 32 for c in page_token)):
            _fail("invalid_page_token")
        if source.source_type == "youtube_video":
            if not VIDEO_ID.fullmatch(source.external_id) or page_token is not None:
                _fail("invalid_source")
            data = self._request("videos", id=source.external_id)
            return DiscoveryPage(self._videos(data, [source.external_id], source.source_type, source.external_id),
                                 etag=data.get("etag"))
        if source.source_type == "youtube_channel":
            if not CHANNEL_ID.fullmatch(source.external_id):
                _fail("invalid_source")
            playlist = source.provider_metadata.get("uploads_playlist_id")
        elif source.source_type == "youtube_playlist":
            playlist = source.external_id
        else:
            _fail("invalid_source")
        if not isinstance(playlist, str) or not PLAYLIST_ID.fullmatch(playlist):
            _fail("invalid_source")
        params = {"playlistId": playlist, "maxResults": 50}
        if page_token is not None:
            params["pageToken"] = page_token
        data = self._request("playlistItems", **params)
        identities = []
        unavailable = []
        for entry in data["items"]:
            blocked = False
            try:
                self._public_status(entry)
            except YouTubeDiscoveryError as error:
                if error.category not in _UNAVAILABLE:
                    raise
                blocked = True
            if blocked:
                # Private/deleted placeholders may omit contentDetails or even
                # the video identity. Retain any exposed identity for revisits;
                # an unidentifiable placeholder must not stop usable siblings.
                details = _mapping(entry.get("contentDetails", {}))
                snippet = _mapping(entry.get("snippet", {}))
                resource_id = _mapping(snippet.get("resourceId", {}))
                exposed = [_identity(value, VIDEO_ID) for value in
                           (details.get("videoId"), resource_id.get("videoId")) if value is not None]
                if len(set(exposed)) > 1:
                    _fail("invalid_response")
                if not exposed:
                    continue
                identity = exposed[0]
                if identity not in unavailable:
                    unavailable.append(identity)
                continue
            details = _mapping(entry.get("contentDetails"))
            identity = _identity(details.get("videoId"), VIDEO_ID)
            snippet = _mapping(entry.get("snippet"))
            resource_id = _mapping(snippet.get("resourceId"))
            if resource_id.get("kind") != "youtube#video" or resource_id.get("videoId") != identity:
                _fail("invalid_response")
            if identity not in identities:
                identities.append(identity)
        items = ()
        if identities:
            videos = self._request("videos", id=",".join(identities))
            items = self._videos(videos, identities, source.source_type, source.external_id, skip_unavailable=True)
            usable = {item.external_id for item in items}
            unavailable.extend(identity for identity in identities if identity not in usable and identity not in unavailable)
        cursor = data.get("nextPageToken")
        return DiscoveryPage(items, next_cursor=cursor, exhausted=cursor is None, etag=data.get("etag"),
                             unavailable_ids=tuple(unavailable))
