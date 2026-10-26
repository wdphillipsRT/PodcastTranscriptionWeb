"""People a podcast feed declares: hosts for the show and guests per episode.

feedparser drops <podcast:person> attributes and merges <itunes:author> into
<itunes:owner>, so these few elements are read from the same bounded feed body
with the standard XML parser. A document with a DTD or entity declaration is
refused before parsing when it is in an ASCII-compatible encoding; any other
encoding (UTF-16/32) is contained by expat's own entity-amplification limits.
Any parse or decoding failure simply declares nobody: this lookup must never
break discovery, which feedparser handles on its own.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
import xml.etree.ElementTree as ElementTree

from ingestion_types import person_names


ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
PODCAST = "{https://podcastindex.org/namespace/1.0}"
HOST_ROLES = frozenset({"host", "co-host", "cohost"})
UTF8_BOM = b"\xef\xbb\xbf"
MAX_HOSTS = 10
MAX_GUESTS = 20


@dataclass(frozen=True)
class FeedPeople:
    hosts: tuple[str, ...] = ()
    guests_by_guid: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))

    def __eq__(self, other):
        return (isinstance(other, FeedPeople) and self.hosts == other.hosts
                and dict(self.guests_by_guid) == dict(other.guests_by_guid))


def _text(element):
    return element.text or "" if element is not None else ""


def _people(parent, roles):
    # The podcast namespace defines "host" as the default person role.
    return [_text(person) for person in parent.findall(PODCAST + "person")
            if (person.get("role") or "host").strip().lower() in roles]


def parse_feed_people(body: bytes) -> FeedPeople:
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        return FeedPeople()
    # expat rejects anything before the XML declaration; feedparser does not.
    bom = UTF8_BOM if body.startswith(UTF8_BOM) else b""
    body = bom + body[len(bom):].lstrip(b" \t\r\n")
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError, LookupError):
        # ValueError: multi-byte encodings (GBK, Big5, Shift_JIS, EUC-KR);
        # LookupError: an unknown encoding label.
        return FeedPeople()
    channel = root.find("channel")
    if channel is None:
        return FeedPeople()
    hosts = person_names(_people(channel, HOST_ROLES), limit=MAX_HOSTS)
    if not hosts:
        # A single author/owner name is a seed for the administrator to confirm.
        candidates = (_text(channel.find(ITUNES + "author")),
                      _text(channel.find(f"{ITUNES}owner/{ITUNES}name")))
        hosts = person_names(candidates, limit=1)
    guests = {}
    for item in channel.findall("item"):
        guid = _text(item.find("guid")).strip()
        names = person_names(_people(item, {"guest"}), limit=MAX_GUESTS)
        if guid and names:
            # Discovery keeps the first item with a given guid; so do its guests.
            guests.setdefault(guid, names)
    return FeedPeople(hosts, MappingProxyType(guests))
