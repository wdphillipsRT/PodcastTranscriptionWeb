"""Who an exported record names: the show's hosts and the episode's guests.

Hosts belong to the content source: a non-empty administrator list wins over
the feed-declared (or YouTube channel owner) list. Guests belong to the item:
an administrator list (possibly empty) wins, then <podcast:person> guests, then
a completed restricted Claude extraction for the item's current title and
description. Only names with an identifier (`person_entity_id`) are exported.
"""

from hashlib import sha256
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from ingestion_types import person_names
from integrations.knowledge.contract import ContractError, canonical_json, person_entity_id


GUEST_PROMPT_VERSION = "episode-guest-extraction/v1"
INFERRED_GUEST_MIN_CONFIDENCE = 0.5
MAX_PARTICIPANTS = 50
MAX_ADMIN_TEXT_CHARS = 10_000
HOST_ROLES = ("host", "co_host")


def source_hosts(source):
    """Return (names, basis) for a content source; basis is None without hosts."""
    if source.admin_hosts:
        return tuple(source.admin_hosts), "admin_set"
    if source.feed_hosts:
        return tuple(source.feed_hosts), "feed_declared"
    return (), None


def item_guests(item, extraction):
    """Return (name, basis, confidence) guests for one item."""
    if item.admin_guests is not None:
        return tuple((name, "admin_set", 1) for name in item.admin_guests)
    if item.feed_guests:
        return tuple((name, "feed_declared", 1) for name in item.feed_guests)
    if extraction is not None and extraction.state == "completed":
        return tuple((guest["name"], "inferred", guest["confidence"]) for guest in extraction.guests
                     if guest["confidence"] >= INFERRED_GUEST_MIN_CONFIDENCE)
    return ()


def _identity(name):
    try:
        return person_entity_id(name)
    except ContractError:
        return None


def participants(hosts, host_basis, guests):
    """Contract participants: first nameable host is `host`, the rest `co_host`."""
    result, seen = [], set()

    def add(name, role, basis, confidence):
        entity_id = _identity(name)
        if entity_id is None or entity_id in seen or len(result) == MAX_PARTICIPANTS:
            return
        seen.add(entity_id)
        result.append({"name": name, "role": role, "basis": basis, "confidence": confidence,
                       "entity_id": entity_id})

    for name in hosts:
        add(name, "co_host" if any(p["role"] == "host" for p in result) else "host", host_basis, 1)
    for name, basis, confidence in guests:
        add(name, "guest", basis, confidence)
    return result


def has_host(people):
    return any(person["role"] in HOST_ROLES for person in people)


def parse_admin_names(text, *, limit):
    """Administrator names, one per line; ValueError on anything unexportable."""
    if not isinstance(text, str) or len(text) > MAX_ADMIN_TEXT_CHARS:
        raise ValueError("names must be text")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > limit:
        raise ValueError("too many names")
    names = person_names(lines, limit=limit)
    if len(names) != len({" ".join(line.split()).casefold() for line in lines}):
        raise ValueError("names must be 1 to 300 printable characters")
    if any(_identity(name) is None for name in names):
        raise ValueError("names need letters or digits")
    return names


def plain_text(value):
    """Visible text of feed HTML with whitespace collapsed."""
    if not value:
        return ""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        text = BeautifulSoup(value, "html.parser").get_text(" ")
    return " ".join(text.split())


def guest_input_sha256(item):
    """Identity of a guest-extraction input; a changed title or description re-extracts."""
    payload = {"prompt_version": GUEST_PROMPT_VERSION, "title": " ".join(item.title.split()),
               "description": plain_text(item.description)}
    return sha256(canonical_json(payload)).hexdigest()
