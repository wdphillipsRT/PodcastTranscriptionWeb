"""Immutable, persistence-safe values shared by synchronous source adapters."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class IngestionError(ValueError):
    """A safe category, never an underlying HTTP exception or provider payload."""


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_public_url(value: str) -> str:
    """Keep stable public identity; remove credentials and transient URL data."""
    try:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
        if parts.scheme not in {"http", "https"} or not host or any(c.isspace() for c in host):
            raise ValueError
        host = host.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parts.scheme, port) not in {("https", 443), ("http", 80)}:
            host += f":{port}"
        query = ""
        if parts.hostname.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in {"v", "list"}])
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", query, ""))
    except (TypeError, ValueError, UnicodeError):
        raise IngestionError("invalid_url") from None


def redact_url(value: str) -> str:
    try:
        return normalize_public_url(value)
    except IngestionError:
        return "[invalid URL]"


def _freeze(value):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("provider_metadata keys must be strings")
        return MappingProxyType({key: _freeze(entry) for key, entry in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(entry) for entry in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(entry) for entry in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError("provider_metadata must contain immutable data values")


def _freeze_metadata(value):
    if not isinstance(value, Mapping):
        raise TypeError("provider_metadata must be a mapping")
    return _freeze(value)


@dataclass(frozen=True)
class SourceDescriptor:
    source_type: str
    external_id: str
    canonical_url: str
    title: str
    description: str = ""
    image_url: str | None = None
    provider_metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "canonical_url", normalize_public_url(self.canonical_url))
        if self.image_url:
            object.__setattr__(self, "image_url", normalize_public_url(self.image_url))
        object.__setattr__(self, "provider_metadata", _freeze_metadata(self.provider_metadata))

    @property
    def name(self):
        return self.title


@dataclass(frozen=True)
class ItemDescriptor:
    source_type: str
    source_external_id: str
    external_id: str
    title: str
    published_at: datetime | None = None
    media_url: str | None = None
    canonical_url: str | None = None
    description: str = ""
    guid: str | None = None
    platform: str = "podcast"
    item_type: str = "podcast_episode"
    creator_id: str | None = None
    creator_name: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    channel_url: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    provider_metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "published_at", utc_datetime(self.published_at))
        if self.guid and self.guid.lower().startswith(("http://", "https://")):
            object.__setattr__(self, "guid", normalize_public_url(self.guid))
        for name in ("media_url", "canonical_url", "channel_url", "thumbnail_url"):
            value = getattr(self, name)
            if value:
                object.__setattr__(self, name, normalize_public_url(value))
        object.__setattr__(self, "provider_metadata", _freeze_metadata(self.provider_metadata))

    @property
    def remote_media_url(self):
        return self.media_url


@dataclass(frozen=True)
class DiscoveryPage:
    items: tuple[ItemDescriptor, ...]
    next_cursor: str | None = None
    exhausted: bool = True
    etag: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    size_bytes: int
    content_type: str | None = None
    sha256: str = ""

    def __post_init__(self):
        object.__setattr__(self, "path", Path(self.path))
