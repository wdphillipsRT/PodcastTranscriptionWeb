"""Classify public intake URLs without network access."""

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from ingestion_types import IngestionError, normalize_public_url


YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"})
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
PLAYLIST_ID = re.compile(r"(?:PL|UU|OLAK5uy_)[A-Za-z0-9_-]{10,56}\Z")


def youtube_locator(url: str) -> tuple[str, str, str]:
    """Return kind, official API selector and validated identity, without I/O.

    Video-plus-playlist URLs select two different sources and are rejected.
    Account playlists (WL/LL), vanity /c/ aliases and arbitrary paths are not
    public canonical sources. Query decoration never changes the identity.
    """
    normalized = normalize_public_url(url)
    parts = urlsplit(normalized)
    if parts.hostname not in YOUTUBE_HOSTS or parts.port not in {None, 80, 443}:
        raise IngestionError("invalid_url")
    if parts.port is not None and (parts.scheme, parts.port) not in {("https", 443), ("http", 80)}:
        raise IngestionError("invalid_url")
    query = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in {"v", "list"}:
            if key in query and query[key] != value:
                raise IngestionError("invalid_url")
            query[key] = value
    path = unquote(parts.path).rstrip("/")
    short_host = parts.hostname in {"youtu.be", "www.youtu.be"}
    if short_host or path.startswith("/shorts/"):
        identity = path.removeprefix("/") if short_host else path.removeprefix("/shorts/")
        if "list" in query or ("v" in query and query["v"] != identity) or not VIDEO_ID.fullmatch(identity):
            raise IngestionError("invalid_url")
        return "youtube_video", "id", identity
    if path == "/watch":
        identity = query.get("v", "")
        if "list" not in query and VIDEO_ID.fullmatch(identity):
            return "youtube_video", "id", identity
    elif path == "/playlist":
        identity = query.get("list", "")
        if "v" not in query and PLAYLIST_ID.fullmatch(identity):
            return "youtube_playlist", "id", identity
    elif not ({"v", "list"} & query.keys()):
        if path.startswith("/channel/") and CHANNEL_ID.fullmatch(path[9:]):
            return "youtube_channel", "id", path[9:]
        if re.fullmatch(r"/@[\w.\-·]{1,100}", path):
            return "youtube_channel", "forHandle", path[1:]
        if re.fullmatch(r"/user/[A-Za-z0-9_-]{1,100}", path):
            return "youtube_channel", "forUsername", path[6:]
    raise IngestionError("invalid_url")


def classify_source_url(url: str) -> str:
    normalized = normalize_public_url(url)
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise IngestionError("invalid_url")
    if parsed.hostname in YOUTUBE_HOSTS:
        return youtube_locator(url)[0]
    if parsed.hostname in {"studio.youtube.com", "accounts.youtube.com"}:
        raise IngestionError("invalid_url")
    return "podcast_rss"
