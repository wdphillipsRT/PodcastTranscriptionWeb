"""Classify public intake URLs without network access."""

from urllib.parse import urlsplit

from ingestion_types import IngestionError, normalize_public_url


def classify_source_url(url: str) -> str:
    normalize_public_url(url)
    parsed = urlsplit(url.strip())
    if parsed.username is not None or parsed.password is not None:
        raise IngestionError("invalid_url")
    return "podcast_rss"
