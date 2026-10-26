"""Exporter record shapes: a podcast.episode plus editorial passages (golden fixture)."""

from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from integrations.knowledge.contract import validate_podcast_batch, write_podcast_batch
from integrations.knowledge.records import (
    PASSAGE_MAX_CHARS, build_records, episode_facts, passages, payload_sha256, publisher_name,
)
from transcripts import TranscriptSegment

UTC = timezone.utc
PEOPLE = [{"name": "Ada Host", "role": "host", "basis": "feed_declared", "confidence": 1, "entity_id": "person:ada-host"},
          {"name": "James Clear", "role": "guest", "basis": "inferred", "confidence": 0.92,
           "entity_id": "person:james-clear"}]


def segment(ordinal, start, end, text):
    return TranscriptSegment(f"seg-{ordinal:06d}-{'a' * 16}", sha256(text.encode()).hexdigest(), start, end, text,
                             "https://example.com/episode#t=0")


def facts(**item_changes):
    item = SimpleNamespace(id=42, platform="podcast", title="  Building   habits that stick ",
        description="<p>Episode 42 on habit formation with James Clear.</p>",
        canonical_url="https://example.com/podcast/episode-42", channel_name=None,
        published_at=datetime(2026, 10, 20, 8, tzinfo=UTC), created_at=datetime(2026, 10, 21, 2, tzinfo=UTC),
        duration_seconds=3120, language="en")
    item.__dict__.update(item_changes)
    source = SimpleNamespace(name="The Mel Robbins Podcast")
    revision = SimpleNamespace(id=4, included_milliseconds=27000, policy_version="commercial-policy/v1")
    return episode_facts(item, source, revision, PEOPLE, observed_at=datetime(2026, 10, 22, 3, 15, 0, 500, tzinfo=UTC))


SEGMENTS = (
    segment(41, 724.0, 731.5, "The habit loop has three parts:"),
    segment(42, 731.5, 751.0, "cue, routine, reward."),
    segment(57, 900.0, 905.0, "Make it obvious."),
)


def test_episode_and_passage_records_match_the_golden_shape():
    records, blobs = build_records(facts(), generation=1, supersedes="podcast:podcast.episode:item-42/rev-3/e1",
                                   segments=SEGMENTS)
    text = "The habit loop has three parts:\ncue, routine, reward.".encode()
    digest = sha256(text).hexdigest()
    rights = {"basis": "personal_research", "redistribution": "none", "training": "requires_approval"}
    source = {"uri": "https://example.com/podcast/episode-42", "publisher": "The Mel Robbins Podcast",
              "published_at": "2026-10-20T08:00:00Z", "retrieved_at": "2026-10-21T02:00:00Z"}
    assert records[0] == {
        "schema_version": "knowledge-record/v1", "record_id": "podcast:podcast.episode:item-42/rev-4/e1",
        "kind": "podcast.episode", "title": "Building habits that stick",
        "summary": "Episode 42 on habit formation with James Clear.", "content": None, "source": source,
        "observed_at": "2026-10-22T03:15:00Z", "participants": PEOPLE,
        "attributes": {"platform": "podcast", "item_id": 42, "classification_revision_id": 4,
                       "export_generation": 1, "editorial_passages": 2, "duration_seconds": 3120,
                       "language": "en", "editorial_milliseconds": 27000,
                       "policy_version": "commercial-policy/v1"},
        "relations": [], "supersedes": "podcast:podcast.episode:item-42/rev-3/e1",
        "rights": rights, "sensitivity": "internal"}
    assert records[1] == {
        "schema_version": "knowledge-record/v1",
        "record_id": f"podcast:podcast.segment:item-42/rev-4/e1/seg-000041-{'a' * 16}",
        "kind": "podcast.segment", "title": "Building habits that stick, 12:04-12:31",
        "summary": "The habit loop has three parts: cue, routine, reward.",
        "content": {"sha256": digest, "media_type": "text/plain; charset=utf-8", "bytes": len(text)},
        "source": source, "observed_at": "2026-10-22T03:15:00Z", "participants": PEOPLE,
        "attributes": {"platform": "podcast", "start_ms": 724000, "end_ms": 751000, "label": "editorial",
                       "segment_ids": [f"seg-000041-{'a' * 16}", f"seg-000042-{'a' * 16}"]},
        "relations": [{"type": "part_of", "target_record_id": "podcast:podcast.episode:item-42/rev-4/e1"}],
        "supersedes": None, "rights": rights, "sensitivity": "internal"}
    assert records[2]["title"] == "Building habits that stick, 15:00-15:05"
    assert set(blobs) == {digest, sha256(b"Make it obvious.").hexdigest()} and blobs[digest] == text


def test_records_are_accepted_by_the_vendored_oracle(tmp_path):
    records, blobs = build_records(facts(platform="youtube", channel_name="Example Channel"), generation=2,
                                   supersedes=None, segments=SEGMENTS)
    batch = write_podcast_batch(tmp_path, batch_id="podcast-20261022-001", producer_version="test",
                                created_at="2026-10-22T04:00:00Z", records=records, blobs=blobs)
    summary = validate_podcast_batch(batch)
    assert summary.record_count == 3
    assert {record["attributes"]["platform"] for record in summary.records} == {"youtube"}
    assert summary.records[0]["source"]["publisher"] == "Example Channel"
    assert summary.records[0]["record_id"].endswith("/e2")


def test_passages_split_at_gaps_duration_and_size():
    long_text = "x" * (PASSAGE_MAX_CHARS - 10)
    groups = passages((segment(1, 0.0, 30.0, "a"), segment(2, 30.0, 59.0, "b"), segment(3, 59.0, 61.0, "c"),
                       segment(5, 70.0, 71.0, "d"), segment(6, 71.0, 72.0, long_text), segment(7, 72.0, 73.0, "e" * 20)))
    assert [(g.segment_ids[0][4:10], len(g.segment_ids), g.start_ms, g.end_ms) for g in groups] == [
        ("000001", 2, 0, 59000), ("000003", 1, 59000, 61000), ("000005", 2, 70000, 72000), ("000007", 1, 72000, 73000)]
    assert groups[0].text == "a\nb"


def test_payload_identity_tracks_only_exported_facts():
    base = payload_sha256(facts())
    assert base == payload_sha256(facts())
    assert base != payload_sha256(facts(title="Renamed"))
    assert base != payload_sha256(facts(description="<p>New summary</p>"))


@pytest.mark.parametrize("changes, expected", [
    ({}, "The Mel Robbins Podcast"),
    ({"platform": "youtube", "channel_name": "Channel"}, "Channel"),
    ({"platform": "youtube", "channel_name": None}, "The Mel Robbins Podcast"),
])
def test_publisher_is_the_show_or_youtube_channel(changes, expected):
    item = SimpleNamespace(platform="podcast", channel_name=None)
    item.__dict__.update(changes)
    assert publisher_name(item, SimpleNamespace(name="The Mel Robbins Podcast")) == expected


def test_long_titles_summaries_and_empty_descriptions_stay_within_the_schema():
    built = facts(title="T" * 900, description=None)
    assert len(built["title"]) == 500 and built["summary"] == "T" * 499 + "… (The Mel Robbins Podcast)"
    long = facts(description="word " * 1000)
    assert len(long["summary"]) <= 2000 and long["summary"].endswith("…")
    records, _ = build_records(built, generation=1, supersedes=None,
                               segments=(segment(1, 3600.0, 3605.0, ("word " * 100).strip()),))
    assert len(records[1]["title"]) <= 500 and records[1]["title"].endswith(", 1:00:00-1:00:05")
    assert len(records[1]["summary"]) <= 300 and records[1]["summary"].endswith("…")
