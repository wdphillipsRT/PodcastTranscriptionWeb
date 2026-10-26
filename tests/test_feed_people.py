"""Hosts and guests declared by podcast feeds and YouTube owners, offline."""

from hashlib import sha256

import pytest

from episode_fetcher import discover_podcast_items
from feed_people import FeedPeople, parse_feed_people
from feed_resolver import resolve_podcast_source
from ingestion_types import DiscoveryPage, ItemDescriptor, SourceDescriptor, person_names
from tests.test_podcast_ingestion import URL, Response, Session, no_real_network, settings, source  # noqa: F401
from tests.test_youtube_discovery import PLAYLIST, VIDEO, client_with, fixture, response


PEOPLE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <title>People Show</title>
  <itunes:author>Author Name</itunes:author>
  <itunes:owner><itunes:name>Owner Media LLC</itunes:name><itunes:email>owner@example.com</itunes:email></itunes:owner>
  <podcast:person role="host">Ada Host</podcast:person>
  <podcast:person>  Grace   Cohost </podcast:person>
  <podcast:person role="producer">Not A Host</podcast:person>
  <item><title>With guests</title><guid isPermaLink="false"> guest-episode </guid>
    <podcast:person role="Guest">James Clear</podcast:person>
    <podcast:person role="guest">james clear</podcast:person>
    <podcast:person role="guest">Zo\xc3\xab O'Brien</podcast:person>
    <podcast:person role="host">Ada Host</podcast:person>
    <enclosure url="https://cdn.example/guests.mp3" type="audio/mpeg"/>
  </item>
  <item><title>Solo</title><guid>solo-episode</guid>
    <enclosure url="https://cdn.example/solo.mp3" type="audio/mpeg"/>
  </item>
</channel></rss>"""


def feed(channel_people):
    return (b'<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
            b"<channel><title>Show</title>" + channel_people + b"</channel></rss>")


def test_channel_hosts_and_item_guests_come_from_podcast_person_tags():
    people = parse_feed_people(PEOPLE_FEED)
    assert people.hosts == ("Ada Host", "Grace Cohost")
    assert dict(people.guests_by_guid) == {"guest-episode": ("James Clear", "Zoë O'Brien")}


@pytest.mark.parametrize("channel, hosts", [
    (b"<itunes:author>Mel Robbins</itunes:author><itunes:owner><itunes:name>Mel Media</itunes:name></itunes:owner>",
     ("Mel Robbins",)),
    (b"<itunes:owner><itunes:name>Mel Media</itunes:name></itunes:owner>", ("Mel Media",)),
    (b"<itunes:author>   </itunes:author>", ()),
    (b"", ()),
])
def test_itunes_author_then_owner_seed_a_single_host(channel, hosts):
    assert parse_feed_people(feed(channel)).hosts == hosts


@pytest.mark.parametrize("body", [
    b"not xml",
    b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><itunes:author>&a;</itunes:author></channel></rss>',
    b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title></feed>',
])
def test_unparseable_dtd_or_non_rss_feeds_declare_nobody(body):
    assert parse_feed_people(body) == FeedPeople()


def test_person_names_are_normalized_deduplicated_and_bounded():
    names = ["  Ada\tLovelace ", "ADA LOVELACE", "", "x" * 301, "Bad\x00Name", *[f"Guest {n}" for n in range(30)]]
    assert person_names(names, limit=5) == ("Ada Lovelace", "Guest 0", "Guest 1", "Guest 2", "Guest 3")
    with pytest.raises(TypeError):
        person_names([42])


def test_descriptors_normalize_people():
    assert SourceDescriptor("podcast_rss", "feed", URL, "Show", hosts=[" Ada  Host ", "ada host"]).hosts == ("Ada Host",)
    item = ItemDescriptor(source_type="podcast_rss", source_external_id="feed", external_id="e", title="E",
                          guests=["James Clear"])
    assert item.guests == ("James Clear",)
    assert DiscoveryPage(items=[]).source_hosts is None
    assert DiscoveryPage(items=[], source_hosts=["Ada Host", "ADA HOST"]).source_hosts == ("Ada Host",)


def test_discovery_page_reports_feed_hosts_and_item_guests(source, settings):
    page = discover_podcast_items(source, settings, Session(Response(PEOPLE_FEED)))
    assert page.source_hosts == ("Ada Host", "Grace Cohost")
    guests = {item.title: item.guests for item in page.items}
    assert guests == {"With guests": ("James Clear", "Zoë O'Brien"), "Solo": ()}
    assert {item.external_id for item in page.items} == {
        sha256((source.external_id + guid).encode()).hexdigest() for guid in ("guest-episode", "solo-episode")}


def test_resolved_podcast_source_carries_feed_hosts(settings):
    result = resolve_podcast_source(URL, settings, Session(Response(), Response(PEOPLE_FEED)))
    assert result.title == "People Show" and result.hosts == ("Ada Host", "Grace Cohost")


def test_youtube_sources_name_the_channel_owner_as_host():
    channel, _ = client_with(response(fixture("channel")))
    assert channel.resolve_source("https://youtube.com/@teacher").hosts == ("Teacher",)
    playlist, _ = client_with(response(fixture("playlist")))
    assert playlist.resolve_source(f"https://youtube.com/playlist?list={PLAYLIST}").hosts == ("Teacher",)
    video, _ = client_with(response(fixture("public_video")))
    assert video.resolve_source(f"https://youtu.be/{VIDEO}").hosts == ("Teacher",)


GBK_FEED = ('<?xml version="1.0" encoding="GBK"?><rss version="2.0"><channel><title>节目</title>'
            '<item><title>一</title><guid>g</guid><enclosure url="https://cdn.example/a.mp3" type="audio/mpeg"/></item>'
            '</channel></rss>').encode("gbk")


@pytest.mark.parametrize("body", [
    GBK_FEED,
    b'<?xml version="1.0" encoding="x-nope"?><rss version="2.0"><channel><title>Show</title></channel></rss>',
])
def test_encodings_expat_cannot_read_declare_nobody(body):
    assert parse_feed_people(body) == FeedPeople()


def test_gbk_feed_discovery_still_yields_its_items(source, settings):
    page = discover_podcast_items(source, settings, Session(Response(GBK_FEED)))
    assert [item.title for item in page.items] == ["一"]


@pytest.mark.parametrize("role", ["host", "Co-Host", "cohost", " COHOST "])
def test_co_hosts_count_as_hosts(role):
    body = feed(b'<podcast:person xmlns:podcast="https://podcastindex.org/namespace/1.0" role="'
                + role.encode() + b'">Pat Partner</podcast:person>')
    assert parse_feed_people(body).hosts == ("Pat Partner",)


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
def test_leading_whitespace_before_the_declaration_is_ignored(prefix):
    people = parse_feed_people(prefix + b" \r\n\t" + PEOPLE_FEED)
    assert people.hosts == ("Ada Host", "Grace Cohost")
    assert parse_feed_people(prefix + PEOPLE_FEED).hosts == ("Ada Host", "Grace Cohost")


def test_duplicate_guids_keep_the_first_items_guests():
    body = feed(b'<item xmlns:podcast="https://podcastindex.org/namespace/1.0"><guid>same</guid>'
                b'<podcast:person role="guest">First Guest</podcast:person></item>'
                b'<item xmlns:podcast="https://podcastindex.org/namespace/1.0"><guid>same</guid>'
                b'<podcast:person role="guest">Second Guest</podcast:person></item>')
    assert dict(parse_feed_people(body).guests_by_guid) == {"same": ("First Guest",)}


def test_person_names_rejects_a_bare_string():
    with pytest.raises(TypeError):
        person_names("Ada Lovelace", limit=5)


@pytest.mark.parametrize("limit", [0, -1])
def test_person_names_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        person_names(["Ada Lovelace"], limit=limit)


@pytest.mark.parametrize("name", ["Bad\x80Name", "Bad\u202eName", "Bad\u200bName", "Bad\x9bName"])
def test_person_names_drop_format_and_c1_control_characters(name):
    assert person_names([name, "Kept"], limit=5) == ("Kept",)


def test_person_names_keep_zero_width_joiners():
    names = ["\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645", "\u0915\u094d\u200d\u0937"]
    assert person_names(names, limit=5) == tuple(names)
