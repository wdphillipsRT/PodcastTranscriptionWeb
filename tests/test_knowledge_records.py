"""Exporter record shapes: a podcast.episode plus editorial passages (golden fixture)."""

from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from integrations.knowledge.contract import validate_podcast_batch, write_podcast_batch
from integrations.knowledge.records import (
    PASSAGE_MAX_CHARS, build_records, episode_facts, passages, payload_sha256, publisher_name,
    timestamp,
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
    revision = SimpleNamespace(id=4, included_milliseconds=27000, uncertain_segments=0, policy_version="commercial-policy/v1")
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
                       "uncertain_segments": 0, "policy_version": "commercial-policy/v1"},
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
                       "segment_ids": [f"seg-000041-{'a' * 16}", f"seg-000042-{'a' * 16}"],
                       "uncertain_segment_ids": []},
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


def test_passage_ends_never_move_backwards_and_bound_the_duration_check():
    groups = passages((segment(1, 0.0, 50.0, "a"), segment(2, 10.0, 20.0, "b"), segment(3, 20.0, 30.0, "c"),
                       segment(4, 100.0, 170.0, "d"), segment(5, 105.0, 110.0, "e")))
    assert [(g.segment_ids[0][4:10], len(g.segment_ids), g.start_ms, g.end_ms) for g in groups] == [
        ("000001", 3, 0, 50000), ("000004", 1, 100000, 170000), ("000005", 1, 105000, 110000)]


def test_a_single_segment_is_never_split_even_beyond_both_limits():
    text = "x" * (PASSAGE_MAX_CHARS + 500)
    groups = passages((segment(1, 0.0, 90.0, text), segment(2, 90.0, 91.0, "y")))
    assert [(len(g.segment_ids), g.start_ms, g.end_ms, len(g.text)) for g in groups] == [
        (1, 0, 90000, PASSAGE_MAX_CHARS + 500), (1, 90000, 91000, 1)]


def test_timestamps_require_an_aware_datetime():
    assert timestamp(datetime(2026, 10, 22, 3, 15, 0, 500, tzinfo=UTC)) == "2026-10-22T03:15:00Z"
    assert timestamp(None) is None
    with pytest.raises(ValueError):
        timestamp(datetime(2026, 10, 22, 3, 15))


def test_payload_identity_tracks_only_exported_facts():
    base = payload_sha256(facts())
    assert base == payload_sha256(facts())
    assert base != payload_sha256(facts(title="Renamed"))
    assert base != payload_sha256(facts(description="<p>New summary</p>"))


@pytest.mark.parametrize("changes, expected", [
    ({}, "The Mel Robbins Podcast"),
    ({"platform": "youtube", "channel_name": "Channel"}, "Channel"),
    ({"platform": "youtube", "channel_name": None}, "The Mel Robbins Podcast"),
    ({"platform": "youtube", "channel_name": " \t\n "}, "The Mel Robbins Podcast"),
    ({"platform": "youtube", "channel_name": "  My \t Channel "}, "My Channel"),
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


def test_uncertain_segments_join_their_neighbours_and_are_marked():
    segments = (segment(1, 0.0, 5.0, "Editorial opening"), segment(2, 5.0, 9.0, "Uncertain bridge"),
                segment(3, 9.0, 14.0, "Editorial lesson"), segment(8, 40.0, 44.0, "Uncertain coda"))
    uncertain = (segments[1].id, segments[3].id)
    groups = passages(segments, uncertain)
    assert [(len(g.segment_ids), g.uncertain_segment_ids, g.label) for g in groups] == [
        (3, (segments[1].id,), "mixed"), (1, (segments[3].id,), "uncertain")]
    records, _ = build_records(facts(), generation=1, supersedes=None, segments=segments,
                               uncertain_segment_ids=uncertain)
    assert [(r["attributes"]["label"], r["attributes"]["uncertain_segment_ids"]) for r in records[1:]] == [
        ("mixed", [segments[1].id]), ("uncertain", [segments[3].id])]
    assert records[1]["content"]["bytes"] == len("Editorial opening\nUncertain bridge\nEditorial lesson".encode())


def test_the_episode_counts_its_uncertain_segments_and_the_count_is_a_fact():
    item = SimpleNamespace(id=42, platform="podcast", title="T", description=None, canonical_url="https://example.com/e",
                           channel_name=None, published_at=None, created_at=datetime(2026, 10, 21, tzinfo=UTC),
                           duration_seconds=None, language=None)
    revision = SimpleNamespace(id=4, included_milliseconds=1, uncertain_segments=2, policy_version="p")
    built = episode_facts(item, SimpleNamespace(name="Show"), revision, PEOPLE,
                          observed_at=datetime(2026, 10, 22, tzinfo=UTC))
    records, _ = build_records(built, generation=1, supersedes=None, segments=SEGMENTS)
    assert records[0]["attributes"]["uncertain_segments"] == 2
    assert payload_sha256(built) != payload_sha256(facts())


def test_passages_record_the_model_label_of_their_thresholded_uncertain_segments():
    segments = (segment(1, 0.0, 5.0, "Editorial opening"), segment(2, 5.0, 9.0, "Probable advert"),
                segment(3, 9.0, 14.0, "Borderline lesson"), segment(4, 14.0, 18.0, "Model said uncertain"),
                segment(9, 50.0, 55.0, "Editorial close"))
    uncertain = (segments[1].id, segments[2].id, segments[3].id)
    labels = {segments[1].id: "paid_commercial", segments[2].id: "editorial"}
    groups = passages(segments, uncertain, labels)
    assert [g.uncertain_segment_labels for g in groups] == [
        ((segments[1].id, "paid_commercial"), (segments[2].id, "editorial")), ()]
    records, _ = build_records(facts(), generation=1, supersedes=None, segments=segments,
                               uncertain_segment_ids=uncertain, uncertain_segment_labels=labels)
    assert records[1]["attributes"]["uncertain_segment_labels"] == labels
    assert set(records[1]["attributes"]["uncertain_segment_labels"]) < set(records[1]["attributes"]["uncertain_segment_ids"])
    assert "uncertain_segment_labels" not in records[2]["attributes"]
    assert all("uncertain_segment_labels" not in record["attributes"] for record in records[:1])


def test_passages_without_model_labels_omit_the_attribute():
    segments = (segment(1, 0.0, 5.0, "Editorial opening"), segment(2, 5.0, 9.0, "Uncertain bridge"))
    records, _ = build_records(facts(), generation=1, supersedes=None, segments=segments,
                               uncertain_segment_ids=(segments[1].id,))
    assert records[1]["attributes"]["uncertain_segment_ids"] == [segments[1].id]
    assert "uncertain_segment_labels" not in records[1]["attributes"]


@pytest.mark.parametrize("labels", ["editorial_segment", "unknown_segment"])
def test_model_labels_are_only_for_uncertain_segments(labels):
    segments = (segment(1, 0.0, 5.0, "Editorial opening"), segment(2, 5.0, 9.0, "Uncertain bridge"))
    key = segments[0].id if labels == "editorial_segment" else segment(7, 0.0, 1.0, "x").id
    with pytest.raises(ValueError):
        passages(segments, (segments[1].id,), {key: "paid_commercial"})
