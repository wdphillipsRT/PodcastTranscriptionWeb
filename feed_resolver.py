"""Bounded podcast feed resolution. No parser-initiated HTTP or hidden retries."""

from contextlib import contextmanager
from hashlib import sha256
import json
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import feedparser
import requests

from config import Settings
from ingestion_types import IngestionError, SourceDescriptor, normalize_public_url
from source_classifier import classify_source_url


CHUNK_SIZE = 64 * 1024
MAX_REDIRECTS = 5
APPLE_HOSTS = {"podcasts.apple.com", "itunes.apple.com"}
APPLE_ID_RE = re.compile(r"(?:^|/)id(\d+)(?:/|$)")


class _PublicRequestAuth(requests.auth.AuthBase):
    """Prevent session/netrc credentials and cookies entering public requests."""

    def __call__(self, request):
        for header in ("Authorization", "Proxy-Authorization", "Cookie"):
            request.headers.pop(header, None)
        return request


_PUBLIC_AUTH = _PublicRequestAuth()


def _public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not (address.is_multicast or address.is_unspecified
        or address.is_reserved or address.is_loopback or address.is_link_local)


def _validate_destination(parts, resolver=None):
    """Reject any non-public answer. Egress policy must also prevent rebinding.

    Validation and requests' connection resolution are distinct operations; this
    check does not claim to pin DNS or eliminate the DNS time-of-check window.
    The resolver is replaceable so offline tests never perform real DNS queries.
    """
    try:
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            resolver = resolver or socket.getaddrinfo
            answers = resolver(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM)
            if not answers or not all(_public_address(answer[4][0]) for answer in answers):
                raise ValueError
        else:
            if not _public_address(str(address)):
                raise ValueError
    except (OSError, ValueError):
        raise IngestionError("unsafe_destination") from None


def _request_url(url):
    # Reject userinfo before requests can derive an Authorization header.
    normalize_public_url(url)
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise IngestionError("invalid_url")
    _validate_destination(parts)
    return url


@contextmanager
def http_response(session, method, url, settings):
    """Follow bounded, validated redirects and close every response."""
    response = None
    try:
        for redirect in range(MAX_REDIRECTS + 1):
            response = session.request(method, _request_url(url), stream=True,
                allow_redirects=False,
                auth=_PUBLIC_AUTH,
                timeout=(settings.HTTP_CONNECT_TIMEOUT_SECONDS, settings.HTTP_READ_TIMEOUT_SECONDS),
                headers={"User-Agent": "PodcastTranscriptionBot/1.0"})
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("Location")
            response.close()
            response = None
            if not location or redirect == MAX_REDIRECTS:
                raise IngestionError("redirect_failed")
            url = _request_url(urljoin(url, location))
        yield response
    except requests.RequestException:
        raise IngestionError("http_failed") from None
    finally:
        if response is not None:
            response.close()


def bounded_chunks(response, maximum):
    """Bound decoded bytes and reject an oversized declared wire length early."""
    try:
        declared = int(response.headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
        raise IngestionError("invalid_content_length") from None
    if declared < 0:
        raise IngestionError("invalid_content_length")
    if declared > maximum:
        raise IngestionError("response_too_large")
    size = 0
    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
        if chunk:
            size += len(chunk)
            if size > maximum:
                raise IngestionError("response_too_large")
            yield chunk
    if declared and not response.headers.get("Content-Encoding") and size != declared:
        raise IngestionError("incomplete_response")


def fetch_body(url, settings, session):
    with http_response(session, "GET", url, settings) as response:
        response.raise_for_status()
        body = b"".join(bounded_chunks(response, settings.RSS_MAX_BYTES))
        return body, normalize_public_url(response.url)


def parse_feed(body):
    parsed = feedparser.parse(body)
    if not parsed.get("version") or not (parsed.get("feed") or parsed.get("entries")):
        raise IngestionError("invalid_feed")
    return parsed


def resolve_podcast_source(url, settings, session) -> SourceDescriptor:
    """Resolve an Apple page, feed, or HTML alternate link to a canonical feed."""
    classify_source_url(url)
    url = normalize_public_url(url)
    parts = urlsplit(url)
    if parts.hostname in APPLE_HOSTS:
        match = APPLE_ID_RE.search(parts.path)
        if not match:
            raise IngestionError("invalid_apple_url")
        body, _ = fetch_body(f"https://itunes.apple.com/lookup?id={match.group(1)}", settings, session)
        try:
            lookup = json.loads(body)
            url = normalize_public_url(lookup["results"][0]["feedUrl"])
        except (ValueError, TypeError, KeyError, IndexError):
            raise IngestionError("apple_lookup_failed") from None
    else:
        try:
            with http_response(session, "HEAD", url, settings) as response:
                if response.status_code < 400:
                    url = normalize_public_url(response.url)
        except IngestionError as error:
            if str(error) != "http_failed":
                raise
    body, final_url = fetch_body(url, settings, session)
    try:
        parsed = parse_feed(body)
    except IngestionError:
        soup = BeautifulSoup(body, "html.parser")
        link = next((link for link in soup.find_all("link", rel="alternate")
            if link.get("href") and any(kind in link.get("type", "").lower()
            for kind in ("rss", "atom", "xml"))), None)
        if link is None:
            raise IngestionError("feed_not_found") from None
        url = normalize_public_url(urljoin(final_url, link["href"]))
        body, final_url = fetch_body(url, settings, session)
        parsed = parse_feed(body)
    feed = parsed.feed
    return SourceDescriptor(source_type="podcast_rss",
        external_id=sha256(final_url.encode("utf-8")).hexdigest(), canonical_url=final_url,
        title=feed.get("title") or "Untitled Podcast", description=feed.get("subtitle", ""))


def resolve_feed(url, settings=None, session=None):
    """Legacy entry point, delegated to the bounded adapter."""
    if not url or not url.strip():
        return None
    if session is not None:
        return resolve_podcast_source(url, settings or Settings, session).canonical_url
    with requests.Session() as client:
        client.trust_env = False
        return resolve_podcast_source(url, settings or Settings, client).canonical_url
