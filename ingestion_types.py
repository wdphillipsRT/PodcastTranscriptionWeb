"""Immutable, persistence-safe values shared by synchronous source adapters."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class IngestionError(ValueError):
    """A safe category, never an underlying HTTP exception or provider payload."""


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def sensitive_query_key(key: str) -> bool:
    """Case-insensitive denylist shared by public URLs and provider metadata.

    Separators are ignored so api_key, API-KEY, and apiKey follow one policy.
    Public selector keys not covered by this policy are retained as identity.
    """
    key = re.sub(r"[^a-z0-9]", "", key.casefold())
    return key.startswith(("xamz", "xgoog")) or key in {
        "token", "accesstoken", "apikey", "key", "signature", "sig", "auth",
        "authorization", "password", "passwd", "secret", "credential", "credentials",
        "securitytoken", "session", "sessionid", "authkey", "authtoken", "clientsecret",
        "jwt", "hmac", "policy", "keypairid", "awsaccesskeyid", "googleaccessid",
        "hdnts", "hdnea",
    }


def validated_http_url(value: str) -> str:
    """Validate a raw locator before any normalization can hide URL userinfo."""
    try:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
        if parts.scheme not in {"http", "https"} or not host or any(c.isspace() for c in host):
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise ValueError
        return value.strip()
    except (TypeError, ValueError, UnicodeError):
        raise IngestionError("invalid_url") from None


def has_sensitive_query(value: str) -> bool:
    parts = urlsplit(validated_http_url(value))
    return any(sensitive_query_key(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True))


def normalize_public_url(value: str) -> str:
    """Retain public query pair order and duplicates; reject access secrets.

    Query order can select a resource, particularly when keys repeat. Only the
    encoding is normalized; pairs are never sorted for identity or acquisition.
    """
    try:
        parts = urlsplit(validated_http_url(value))
        host, port = parts.hostname, parts.port
        if has_sensitive_query(value):
            raise IngestionError("sensitive_url")
        host = host.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parts.scheme, port) not in {("https", 443), ("http", 80)}:
            host += f":{port}"
        query = urlencode(parse_qsl(parts.query, keep_blank_values=True))
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", query, ""))
    except (TypeError, ValueError, UnicodeError):
        raise IngestionError("invalid_url") from None


MAX_PERSON_NAME_CHARS = 300
# Zero-width (non-)joiners spell real Persian and Indic names; other format
# characters (bidi overrides, zero-width spaces) are dropped with the name.
_NAME_JOINERS = frozenset("\u200c\u200d")


def person_names(values, *, limit: int) -> tuple[str, ...]:
    """Display names: whitespace collapsed, empty/oversized/control- or
    format-character names dropped, case-insensitive duplicates removed, order
    kept, bounded by a positive limit."""
    if isinstance(values, (str, bytes)):
        raise TypeError("person names must be an iterable of strings, not a string")
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    result, seen = [], set()
    for value in values or ():
        if not isinstance(value, str):
            raise TypeError("person names must be strings")
        name = " ".join(value.split())
        if (not name or len(name) > MAX_PERSON_NAME_CHARS
                or any(unicodedata.category(char) in ("Cc", "Cf") and char not in _NAME_JOINERS
                       for char in name)):
            continue
        if name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
        if len(result) == limit:
            break
    return tuple(result)


def redact_url(value: str) -> str:
    try:
        # Redaction deliberately removes *every* query, independent of identity.
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        return normalize_public_url(urlunsplit((parts.scheme, host, parts.path, "", "")))
    except (TypeError, ValueError):
        return "[invalid URL]"


class TransientMediaLocator:
    """An acquisition-only capability, intentionally outside descriptor values.

    Its raw URL is available only to the downloader boundary. No dataclass, dict,
    printable URL, pickle state, or JSON value is exposed by this contract.
    """

    __slots__ = ("__url",)

    def __init__(self, url: str):
        object.__setattr__(self, "_TransientMediaLocator__url", validated_http_url(url))

    def __setattr__(self, name, value):
        raise TypeError("transient media locator is immutable")

    def __repr__(self):
        return "<TransientMediaLocator redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("transient media locator cannot be serialized")

    def __getstate__(self):
        raise TypeError("transient media locator cannot be serialized")

    def _for_download(self):
        return self.__url


def _freeze(value):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("provider_metadata keys must be strings")
        if any(sensitive_query_key(key) for key in value):
            raise TypeError("provider_metadata must not contain access credentials")
        return MappingProxyType({key: _freeze(entry) for key, entry in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(entry) for entry in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(entry) for entry in value)
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return normalize_public_url(value)
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
    hosts: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "canonical_url", normalize_public_url(self.canonical_url))
        object.__setattr__(self, "hosts", person_names(self.hosts, limit=10))
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
    requires_reacquisition: bool = False
    provider_metadata: Mapping = field(default_factory=dict)
    guests: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "published_at", utc_datetime(self.published_at))
        object.__setattr__(self, "guests", person_names(self.guests, limit=20))
        if self.guid and self.guid.lower().startswith(("http://", "https://")):
            try:
                guid = normalize_public_url(self.guid)
            except IngestionError:
                guid = "sha256:" + sha256(self.guid.encode("utf-8")).hexdigest()
            object.__setattr__(self, "guid", guid)
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
    # YouTube identities observed in this page but not currently usable. Keep
    # them separate from ItemDescriptor so no unavailable video can be queued.
    unavailable_ids: tuple[str, ...] = ()
    # Hosts the source itself declares on this fetch; None when not reported.
    source_hosts: tuple[str, ...] | None = None

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))
        if self.source_hosts is not None:
            object.__setattr__(self, "source_hosts", person_names(self.source_hosts, limit=10))
        object.__setattr__(self, "unavailable_ids", tuple(self.unavailable_ids))


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    size_bytes: int
    content_type: str | None = None
    sha256: str = ""

    def __post_init__(self):
        object.__setattr__(self, "path", Path(self.path))
