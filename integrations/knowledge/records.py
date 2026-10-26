"""Knowledge records for one exported item: a podcast.episode and its passages.

Record identifiers (`podcast:<kind>:<native-id>`) are stable per item,
classification revision, export generation, and passage:

    podcast.episode  item-<item id>/rev-<classification revision id>/e<generation>
    podcast.segment  <episode native id>/<first projection segment id>

A passage is a run of consecutive knowledge-projection segments, editorial or
uncertain alike, so caption-sized cues become citable passages and an uncertain
stretch joins the content around it. Each passage lists its uncertain segments
in `attributes.uncertain_segment_ids` and is labelled `editorial`, `uncertain`
(all uncertain) or `mixed`. Where the confidence threshold made a segment
uncertain and the policy recorded the model's own label, the passage maps that
segment to it in `attributes.uncertain_segment_labels` (e.g. `paid_commercial`
or `editorial`); the attribute is omitted when no segment in the passage has one. A segment joins the current passage only while the
passage stays within 60 seconds (measured to the latest segment end so far)
and 6,000 characters; a single segment is never split, so a passage of one
segment may exceed either limit.
Every fact that can change an episode's record is in `episode_facts`, which is
read from the database only; its hash detects changes without storage reads.
"""

from dataclasses import dataclass
from datetime import timezone
from hashlib import sha256

from integrations.knowledge.contract import canonical_json
from integrations.knowledge.people import plain_text


RIGHTS = {"basis": "personal_research", "redistribution": "none", "training": "requires_approval"}
TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"
PASSAGE_MAX_MS = 60_000
PASSAGE_MAX_CHARS = 6_000
TITLE_CHARS = 500
SUMMARY_CHARS = 2_000
PASSAGE_SUMMARY_CHARS = 300


@dataclass(frozen=True)
class Passage:
    segment_ids: tuple[str, ...]
    start_ms: int
    end_ms: int
    text: str
    uncertain_segment_ids: tuple[str, ...] = ()
    uncertain_segment_labels: tuple[tuple[str, str], ...] = ()

    @property
    def label(self):
        if not self.uncertain_segment_ids:
            return "editorial"
        return "uncertain" if len(self.uncertain_segment_ids) == len(self.segment_ids) else "mixed"


def timestamp(value):
    if value is not None and value.tzinfo is None:
        raise ValueError("knowledge timestamps require a timezone-aware datetime")
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip() + "…"


def _clock(milliseconds):
    hours, rest = divmod(milliseconds // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def publisher_name(item, source):
    """The show: the YouTube channel for videos, else the content source's name."""
    channel = " ".join((item.channel_name or "").split()) if item.platform == "youtube" else ""
    if channel:
        return channel
    return " ".join(source.name.split())


def episode_facts(item, source, revision, people, *, observed_at):
    """Every database fact an episode record carries; hashed for change detection."""
    publisher = publisher_name(item, source)
    title = _shorten(" ".join(item.title.split()), TITLE_CHARS) or "Untitled episode"
    summary = _shorten(plain_text(item.description), SUMMARY_CHARS) or _shorten(f"{title} ({publisher})", SUMMARY_CHARS)
    attributes = {"duration_seconds": item.duration_seconds, "language": item.language,
                  "editorial_milliseconds": revision.included_milliseconds,
                  "uncertain_segments": revision.uncertain_segments,
                  "policy_version": revision.policy_version}
    return {
        "item_id": item.id, "classification_revision_id": revision.id, "platform": item.platform,
        "title": title, "summary": summary,
        "source": {"uri": item.canonical_url, "publisher": publisher,
                   "published_at": timestamp(item.published_at), "retrieved_at": timestamp(item.created_at)},
        "observed_at": timestamp(observed_at), "participants": list(people),
        "attributes": {key: value for key, value in attributes.items() if value is not None},
    }


def payload_sha256(facts):
    return sha256(canonical_json(facts)).hexdigest()


def episode_native_id(item_id, revision_id, generation):
    return f"item-{item_id}/rev-{revision_id}/e{generation}"


def passages(segments, uncertain_segment_ids=(), uncertain_segment_labels=None):
    """Group consecutive projection segments, whatever their label, into bounded passages.

    `uncertain_segment_labels` maps uncertain segment IDs to the model's own label.
    """
    uncertain = frozenset(uncertain_segment_ids)
    labels = dict(uncertain_segment_labels or {})
    if not labels.keys() <= uncertain or not labels.keys() <= {segment.id for segment in segments}:
        raise ValueError("model labels are only for exported uncertain segments")
    groups, current, previous = [], None, None
    for segment in segments:
        ordinal = int(segment.id[4:10])
        start, end = round(segment.start * 1000), round(segment.end * 1000)
        if (current is None or ordinal != previous + 1 or max(current["end"], end) - current["start"] > PASSAGE_MAX_MS
                or len(current["text"]) + 1 + len(segment.text) > PASSAGE_MAX_CHARS):
            current = {"ids": [], "start": start, "end": end, "text": None}
            groups.append(current)
        current["ids"].append(segment.id)
        current["end"] = max(current["end"], end)
        current["text"] = segment.text if current["text"] is None else current["text"] + "\n" + segment.text
        previous = ordinal
    return [Passage(tuple(group["ids"]), group["start"], group["end"], group["text"],
                    tuple(identity for identity in group["ids"] if identity in uncertain),
                    tuple((identity, labels[identity]) for identity in group["ids"] if identity in labels))
            for group in groups]


def build_records(facts, *, generation, supersedes, segments, uncertain_segment_ids=(), uncertain_segment_labels=None):
    """Return (records, blobs) for one item: the episode first, then its passages."""
    native = episode_native_id(facts["item_id"], facts["classification_revision_id"], generation)
    episode_id = f"podcast:podcast.episode:{native}"
    common = {"schema_version": "knowledge-record/v1", "source": facts["source"], "observed_at": facts["observed_at"],
              "participants": facts["participants"], "rights": RIGHTS, "sensitivity": "internal"}
    groups = passages(segments, uncertain_segment_ids, uncertain_segment_labels)
    records = [{
        **common, "record_id": episode_id, "kind": "podcast.episode", "title": facts["title"],
        "summary": facts["summary"], "content": None,
        "attributes": {"platform": facts["platform"], "item_id": facts["item_id"],
                       "classification_revision_id": facts["classification_revision_id"],
                       "export_generation": generation, "editorial_passages": len(groups), **facts["attributes"]},
        "relations": [], "supersedes": supersedes,
    }]
    blobs = {}
    for group in groups:
        data = group.text.encode("utf-8")
        digest = sha256(data).hexdigest()
        blobs[digest] = data
        span = f", {_clock(group.start_ms)}-{_clock(group.end_ms)}"
        records.append({
            **common, "record_id": f"podcast:podcast.segment:{native}/{group.segment_ids[0]}",
            "kind": "podcast.segment", "title": _shorten(facts["title"], TITLE_CHARS - len(span)) + span,
            "summary": _shorten(" ".join(group.text.split()), PASSAGE_SUMMARY_CHARS) or facts["title"],
            "content": {"sha256": digest, "media_type": TEXT_MEDIA_TYPE, "bytes": len(data)},
            "attributes": {"platform": facts["platform"], "start_ms": group.start_ms, "end_ms": group.end_ms,
                           "label": group.label, "segment_ids": list(group.segment_ids),
                           "uncertain_segment_ids": list(group.uncertain_segment_ids),
                           **({"uncertain_segment_labels": dict(group.uncertain_segment_labels)}
                              if group.uncertain_segment_labels else {})},
            "relations": [{"type": "part_of", "target_record_id": episode_id}], "supersedes": None,
        })
    return records, blobs
