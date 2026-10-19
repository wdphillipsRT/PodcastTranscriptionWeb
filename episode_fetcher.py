"""Deterministic podcast discovery and bounded, atomic media acquisition."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin

from dateutil import parser as dtparse
import requests

from config import Settings
from feed_resolver import bounded_chunks, fetch_body, http_response, parse_feed
from ingestion_types import (DiscoveryPage, DownloadedMedia, IngestionError,
    ItemDescriptor, SourceDescriptor, normalize_public_url, utc_datetime)


def _publication(entry):
    value = entry.get("published") or entry.get("updated")
    try:
        return utc_datetime(dtparse.parse(value, default=datetime(1970, 1, 1))) if value else None
    except (ValueError, TypeError, OverflowError):
        return None


def _duration(value):
    if value is None:
        return None
    try:
        parts = [int(part) for part in str(value).split(":")]
        if not 1 <= len(parts) <= 3 or any(part < 0 for part in parts):
            return None
        result = 0
        for part in parts:
            result = result * 60 + part
        return result
    except ValueError:
        return None


def discover_podcast_items(source, settings, session, *, limit=None,
        published_after=None, published_before=None, cursor=None) -> DiscoveryPage:
    """Select a bounded feed snapshot inclusively by UTC date, then by count.

    RSS offsets apply only to an unchanged feed snapshot. Orchestration must use
    durable identities to deduplicate between fetches, not offset checkpoints.
    """
    limit = settings.PODCAST_INITIAL_ITEM_LIMIT if limit is None else limit
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    after, before = utc_datetime(published_after), utc_datetime(published_before)
    if after is not None and before is not None and after > before:
        raise ValueError("invalid publication range")
    try:
        offset = int(cursor) if cursor is not None else 0
        if offset < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("invalid discovery cursor") from None
    body, feed_url = fetch_body(source.canonical_url, settings, session)
    parsed = parse_feed(body)
    items, seen = [], set()
    for entry in parsed.entries:
        enclosure = next((enc for enc in entry.get("enclosures", ())
            if enc.get("href") and enc.get("type", "").lower().startswith("audio/")), None)
        if not enclosure:
            continue
        try:
            media_url = normalize_public_url(urljoin(feed_url, enclosure["href"]))
            canonical_url = normalize_public_url(urljoin(feed_url, entry.get("link") or media_url))
        except IngestionError:
            continue
        guid = str(entry.get("id") or entry.get("guid") or media_url)
        external_id = sha256((source.external_id + guid).encode("utf-8")).hexdigest()
        if external_id in seen:
            continue
        seen.add(external_id)
        items.append(ItemDescriptor(source_type="podcast_rss", source_external_id=source.external_id,
            external_id=external_id, title=entry.get("title") or "Untitled Episode",
            published_at=_publication(entry), media_url=media_url, canonical_url=canonical_url,
            description=entry.get("summary", ""), guid=guid,
            creator_name=entry.get("author"), duration_seconds=_duration(entry.get("itunes_duration"))))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda item: (item.published_at or epoch, item.external_id), reverse=True)
    if after is not None:
        items = [item for item in items if item.published_at is not None and item.published_at >= after]
    if before is not None:
        items = [item for item in items if item.published_at is not None and item.published_at <= before]
    selected = items[offset:offset + limit]
    exhausted = offset + len(selected) >= len(items)
    return DiscoveryPage(items=selected, exhausted=exhausted,
        next_cursor=None if exhausted else str(offset + len(selected)))


def download_podcast_audio(url, destination, settings, session) -> DownloadedMedia:
    """Stream into a unique sibling file; replace the destination on success."""
    destination = Path(destination)
    partial = None
    try:
        with http_response(session, "GET", url, settings) as response:
            response.raise_for_status()
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, size = sha256(), 0
            with tempfile.NamedTemporaryFile(dir=destination.parent,
                    prefix=f".{destination.name}.", suffix=".part", delete=False) as handle:
                partial = Path(handle.name)
                for chunk in bounded_chunks(response, settings.MAX_AUDIO_BYTES):
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if not size:
                raise IngestionError("empty_media")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or None
            os.replace(partial, destination)
            partial = None
            return DownloadedMedia(path=destination, size_bytes=size,
                content_type=content_type, sha256=digest.hexdigest())
    except OSError:
        raise IngestionError("media_write_failed") from None
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)


def list_audio_items(feed_url, settings=None, session=None):
    """Legacy dictionary projection with the same bounded network intake."""
    settings = settings or Settings
    source = SourceDescriptor(source_type="podcast_rss",
        external_id=sha256(normalize_public_url(feed_url).encode()).hexdigest(),
        canonical_url=feed_url, title="Podcast")
    if session is None:
        with requests.Session() as client:
            client.trust_env = False
            return list_audio_items(feed_url, settings, client)
    page = discover_podcast_items(source, settings, session, limit=settings.BACKFILL_REQUEST_MAX_ITEMS)
    return [{"title": item.title, "link": item.canonical_url, "published": item.published_at,
        "audio_url": item.media_url, "guid": item.guid, "description": item.description,
        "duration": item.duration_seconds} for item in page.items]


def filter_items(items, last_n=None, days=None, *, now=None):
    if days is not None:
        cutoff = utc_datetime(now or datetime.now(timezone.utc)) - timedelta(days=days)
        items = [item for item in items if item["published"] and utc_datetime(item["published"]) >= cutoff]
    return items[:last_n] if last_n is not None else items


def download_audio(audio_url, dest_path, settings=None, session=None):
    if session is not None:
        return str(download_podcast_audio(audio_url, dest_path, settings or Settings, session).path)
    with requests.Session() as client:
        client.trust_env = False
        return str(download_podcast_audio(audio_url, dest_path, settings or Settings, client).path)


def generate_episode_id(guid, audio_url):
    return sha256((guid or normalize_public_url(audio_url)).encode("utf-8")).hexdigest()


def slugify(text):
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", text.lower())).strip("-")[:50]
