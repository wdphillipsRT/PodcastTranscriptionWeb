"""Builds the KNOWLEDGE-INTAKE-0001 golden batches.

Run from the repository root to regenerate the committed fixtures:

    python tests/fixtures/knowledge/build_golden.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "reference"))

from knowledge_contract import person_entity_id, write_batch  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden"
RIGHTS = {"basis": "personal_research", "redistribution": "none", "training": "requires_approval"}
CREATED_AT = "2026-10-22T10:00:00Z"


def _content(text: str, media_type: str = "text/plain; charset=utf-8") -> tuple[dict, bytes]:
    data = text.encode("utf-8")
    return {"sha256": hashlib.sha256(data).hexdigest(), "media_type": media_type, "bytes": len(data)}, data


def _participant(name: str, role: str, basis: str, confidence: float) -> dict:
    return {"name": name, "role": role, "basis": basis, "confidence": confidence,
            "entity_id": person_entity_id(name)}


def podcast_batch() -> dict:
    segment_ref, segment_blob = _content("The habit loop has three parts: cue, routine, reward.")
    participants = [
        _participant("Mel Robbins", "host", "admin_set", 1),
        _participant("James Clear", "guest", "inferred", 0.92),
    ]
    source = {"uri": "https://example.com/podcast/episode-42", "publisher": "The Mel Robbins Podcast",
              "published_at": "2026-10-20T08:00:00Z", "retrieved_at": "2026-10-21T02:00:00Z"}
    episode = {
        "schema_version": "knowledge-record/v1",
        "record_id": "podcast:podcast.episode:item-42/rev-3",
        "kind": "podcast.episode",
        "title": "Building habits that stick",
        "summary": "Episode 42 on habit formation with James Clear.",
        "content": None,
        "source": source,
        "observed_at": "2026-10-21T03:15:00Z",
        "participants": participants,
        "attributes": {"platform": "podcast", "duration_seconds": 3120, "editorial_segments": 1},
        "relations": [],
        "supersedes": None,
        "rights": RIGHTS,
        "sensitivity": "internal",
    }
    segment = {
        "schema_version": "knowledge-record/v1",
        "record_id": "podcast:podcast.segment:item-42/rev-3/seg-0007",
        "kind": "podcast.segment",
        "title": "Building habits that stick, 12:04-12:31",
        "summary": "James Clear describes the habit loop.",
        "content": segment_ref,
        "source": source,
        "observed_at": "2026-10-21T03:15:00Z",
        "participants": participants,
        "attributes": {"platform": "podcast", "start_ms": 724000, "end_ms": 751000, "label": "editorial"},
        "relations": [{"type": "part_of", "target_record_id": episode["record_id"]}],
        "supersedes": None,
        "rights": RIGHTS,
        "sensitivity": "internal",
    }
    return {"producer": "podcast", "batch_id": "podcast-20261022-001",
            "records": [episode, segment], "blobs": {segment_ref["sha256"]: segment_blob}}


def issachar_batch() -> dict:
    article_ref, article_blob = _content("# Central bank holds rates\n\nThe bank held rates at 4.25%.",
                                         "text/markdown; charset=utf-8")
    article = {
        "schema_version": "knowledge-record/v1",
        "record_id": "issachar:issachar.article:3f2a9c1b7d4e",
        "kind": "issachar.article",
        "title": "Central bank holds rates",
        "summary": "Rates held at 4.25 percent.",
        "content": article_ref,
        "source": {"uri": "https://news.example.com/rates", "publisher": "Example News",
                   "published_at": "2026-10-21T14:00:00Z", "retrieved_at": "2026-10-21T22:05:00Z"},
        "observed_at": "2026-10-21T22:05:00Z",
        "attributes": {"ingest_id": "3f2a9c1b7d4e", "extraction_method": "trafilatura"},
        "relations": [],
        "supersedes": None,
        "rights": RIGHTS,
        "sensitivity": "internal",
    }
    forecast = {
        "schema_version": "knowledge-record/v1",
        "record_id": "issachar:issachar.forecast:fx-0123456789abcdef",
        "kind": "issachar.forecast",
        "title": "Will the bank cut rates by 2026-12-31?",
        "summary": "P(yes)=0.3500 after the October hold.",
        "content": None,
        "source": {"uri": "issachar://forecasts/rate-cut-2026/fx-0123456789abcdef", "publisher": "Issachar",
                   "published_at": None, "retrieved_at": "2026-10-21T23:40:00Z"},
        "observed_at": "2026-10-21T23:40:00Z",
        "valid_from": "2026-10-21T23:40:00Z",
        "valid_until": "2026-12-31T23:59:59Z",
        "attributes": {"contract_id": "fc-rate-cut-2026-v1", "p_yes": 0.35, "p_no": 0.65},
        "relations": [{"type": "cites", "target_record_id": article["record_id"]}],
        "supersedes": None,
        "rights": RIGHTS,
        "sensitivity": "internal",
    }
    return {"producer": "issachar", "batch_id": "issachar-20261022-001",
            "records": [article, forecast], "blobs": {article_ref["sha256"]: article_blob}}


def podcast_followup_batch() -> dict:
    """The next night's podcast export: a reclassified episode and a YouTube item.

    Covers what podcast-20261022-001 does not: a YouTube item, one blob shared by
    two records, and a non-null `supersedes` naming a record of the earlier batch.
    """
    podcast_ref, podcast_blob = _content("The habit loop has three parts: cue, routine, reward.")
    shared_ref, shared_blob = _content("Thanks for having me.")
    podcast_participants = [
        _participant("Mel Robbins", "host", "admin_set", 1),
        _participant("James Clear", "guest", "feed_declared", 1),
    ]
    youtube_participants = [
        _participant("Ada Example", "host", "feed_declared", 1),
        _participant("Grace Hopper", "guest", "inferred", 0.81),
    ]
    podcast_source = {"uri": "https://example.com/podcast/episode-42", "publisher": "The Mel Robbins Podcast",
                      "published_at": "2026-10-20T08:00:00Z", "retrieved_at": "2026-10-21T02:00:00Z"}
    youtube_source = {"uri": "https://www.youtube.com/watch?v=abcdefghijk", "publisher": "Example Science Channel",
                      "published_at": "2026-10-19T15:30:00Z", "retrieved_at": "2026-10-20T01:00:00Z"}
    podcast_episode = {
        "schema_version": "knowledge-record/v1",
        "record_id": "podcast:podcast.episode:item-42/rev-4/e1",
        "kind": "podcast.episode",
        "title": "Building habits that stick",
        "summary": "Episode 42 on habit formation with James Clear.",
        "content": None,
        "source": podcast_source,
        "observed_at": "2026-10-22T03:15:00Z",
        "participants": podcast_participants,
        "attributes": {"platform": "podcast", "item_id": 42, "classification_revision_id": 4,
                       "export_generation": 1, "duration_seconds": 3120, "editorial_passages": 1},
        "relations": [],
        "supersedes": "podcast:podcast.episode:item-42/rev-3",
        "rights": RIGHTS,
        "sensitivity": "internal",
    }
    podcast_segment = {
        "schema_version": "knowledge-record/v1",
        "record_id": "podcast:podcast.segment:item-42/rev-4/e1/seg-000041-0a1b2c3d4e5f6071",
        "kind": "podcast.segment",
        "title": "Building habits that stick, 12:04-12:31",
        "summary": "The habit loop has three parts: cue, routine, reward.",
        "content": podcast_ref,
        "source": podcast_source,
        "observed_at": "2026-10-22T03:15:00Z",
        "participants": podcast_participants,
        "attributes": {"platform": "podcast", "start_ms": 724000, "end_ms": 751000, "label": "editorial",
                       "segment_ids": ["seg-000041-0a1b2c3d4e5f6071"]},
        "relations": [{"type": "part_of", "target_record_id": podcast_episode["record_id"]}],
        "supersedes": None,
        "rights": RIGHTS,
        "sensitivity": "internal",
    }
    youtube_episode = {
        "schema_version": "knowledge-record/v1",
        "record_id": "podcast:podcast.episode:item-7/rev-12/e1",
        "kind": "podcast.episode",
        "title": "How compilers changed computing",
        "summary": "A conversation with Grace Hopper about the first compilers.",
        "content": None,
        "source": youtube_source,
        "observed_at": "2026-10-22T03:20:00Z",
        "participants": youtube_participants,
        "attributes": {"platform": "youtube", "item_id": 7, "classification_revision_id": 12,
                       "export_generation": 1, "duration_seconds": 1800, "editorial_passages": 2},
        "relations": [],
        "supersedes": None,
        "rights": RIGHTS,
        "sensitivity": "internal",
    }

    def youtube_segment(segment_id: str, start_ms: int, end_ms: int, span: str) -> dict:
        return {
            "schema_version": "knowledge-record/v1",
            "record_id": f"podcast:podcast.segment:item-7/rev-12/e1/{segment_id}",
            "kind": "podcast.segment",
            "title": f"How compilers changed computing, {span}",
            "summary": "Thanks for having me.",
            "content": shared_ref,
            "source": youtube_source,
            "observed_at": "2026-10-22T03:20:00Z",
            "participants": youtube_participants,
            "attributes": {"platform": "youtube", "start_ms": start_ms, "end_ms": end_ms,
                           "label": "editorial", "segment_ids": [segment_id]},
            "relations": [{"type": "part_of", "target_record_id": youtube_episode["record_id"]}],
            "supersedes": None,
            "rights": RIGHTS,
            "sensitivity": "internal",
        }

    records = [
        podcast_episode, podcast_segment, youtube_episode,
        youtube_segment("seg-000003-1b2c3d4e5f607182", 4000, 6000, "0:04-0:06"),
        youtube_segment("seg-000210-2c3d4e5f60718293", 1795000, 1797000, "29:55-29:57"),
    ]
    blobs = {podcast_ref["sha256"]: podcast_blob, shared_ref["sha256"]: shared_blob}
    return {"producer": "podcast", "batch_id": "podcast-20261023-001", "records": records, "blobs": blobs}


def build(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    built = []
    for batch in (podcast_batch(), issachar_batch(), podcast_followup_batch()):
        built.append(write_batch(
            target, producer=batch["producer"], batch_id=batch["batch_id"],
            producer_version="golden-1", created_at=CREATED_AT,
            records=batch["records"], blobs=batch["blobs"],
        ))
    return built


if __name__ == "__main__":
    shutil.rmtree(GOLDEN, ignore_errors=True)
    for path in build(GOLDEN):
        print(path.relative_to(ROOT))
