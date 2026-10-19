# Public YouTube Source Ingestion Design

**Status:** Approved  
**Date:** 2026-10-19  
**Parent design:** `docs/superpowers/specs/2026-10-19-docker-podcast-learning-platform-design.md`

## 1. Purpose and precedence

This document adds public YouTube videos, channels, and playlists to the PodcastTranscriptionWeb learning platform before Phase 1 implementation begins. It supersedes podcast-only source, item, discovery, and Phase 1 assumptions in the parent design. Docker deployment, single-admin access, S3 storage, restricted Claude extraction, Lamad delivery, Ebenezer delivery, Agent Factory delivery, and Ask & Learn remain unchanged except that they now operate on both podcast episodes and YouTube videos.

## 2. Fixed product decisions

- Accept public YouTube video, channel, and playlist URLs.
- Do not use YouTube OAuth, an authenticated Google account, browser cookies, or private-content access.
- Use the YouTube Data API for canonical discovery and pagination.
- Use `yt-dlp[default]==2026.8.19` only for subtitle inventory/download and bounded audio extraction.
- Bundle the matching `yt-dlp-ejs` dependency and Deno `2.9.7`; do not download remote challenge components or self-update at runtime.
- Import the newest 25 items when a channel, playlist, or podcast source is first added.
- Automatically discover new podcast episodes and YouTube uploads.
- Permit controlled historical backfill by date range, requested item count, or explicit episode/video URL.
- Prefer creator-provided captions, then YouTube automatic captions, then audio extraction plus Amazon Transcribe.
- Record transcript origin and timestamp lineage for every item.
- Store public-source artifacts privately; public availability does not authorize transcript or media redistribution.

## 3. Selected approach

The system uses a hybrid adapter instead of relying entirely on scraped YouTube pages.

`YouTubeDiscoveryClient` uses the official YouTube Data API to resolve source type, canonical identifiers, channel upload playlists, playlist membership, pagination, publication dates, and public availability. Public API-key requests require no user account or OAuth grant.

`YouTubeMediaClient` invokes the pinned `yt-dlp` entry point with argument arrays, never a shell string. It inventories creator and automatic captions, downloads the selected caption file, or extracts an audio-only file when captions are unusable or absent. The image includes the matching EJS solver package and pinned Deno runtime required for current YouTube challenge handling. Every subprocess has a deadline, bounded output capture, temporary working directory, fixed language preference, and redacted diagnostics.

Alternatives were rejected:

- `yt-dlp` alone would remove the API key but make scheduled discovery, pagination, and metadata more dependent on YouTube page extraction behavior.
- Channel RSS plus `yt-dlp` cannot satisfy playlist discovery, newest-25 intake, or flexible historical backfill.

Primary references:

- YouTube playlist/channel upload pagination: `https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list`
- `yt-dlp` subtitle and audio options: `https://github.com/yt-dlp/yt-dlp/blob/master/README.md`
- `yt-dlp` EJS/runtime requirements: `https://github.com/yt-dlp/yt-dlp/wiki/EJS`

## 4. Source-neutral domain model

Phase 1 uses these operational records:

### `ContentSource`

- `source_type`: `podcast_rss`, `youtube_channel`, `youtube_playlist`, or `youtube_video`.
- Canonical platform identifier and canonical URL.
- Administrator-supplied display name plus provider metadata.
- Enabled state, discovery cursor, last-success timestamp, next-check timestamp, and redacted error category.
- Initial-import limit fixed at 25 unless a specific backfill selector is supplied.

### `ContentItem`

- `item_type`: `podcast_episode` or `youtube_video`.
- Stable platform identity.
- Title, description, canonical public URL, publication timestamp, duration, and creator/channel metadata.
- Processing stage, last successful stage, and safe error state.
- Transcript origin: `creator_caption`, `automatic_caption`, or `amazon_transcribe`.
- Private S3 keys and hashes for media, provider transcript, canonical transcript, and normalized segments.

Podcast item identity is the canonical feed identity plus episode GUID, with a deterministic fallback derived from the enclosure URL. YouTube item identity is the YouTube video ID.

### `SourceItem`

A unique source/item join preserves membership without duplicate processing. A video found through both its channel and one or more playlists has one `ContentItem`, multiple `SourceItem` rows, and one canonical transcript pipeline.

### `BackfillRequest`

- Source ID and exactly one selector: date range, requested count, or explicit item URL.
- Cursor, items scanned, items accepted, items queued, state, cancellation flag, and safe failure category.
- Bounded page and item limits per worker invocation so backfills yield to ordinary discovery and can resume safely.

## 5. Discovery and backfill behavior

The Add Source form classifies and validates the submitted URL before persisting it. Canonical resolution rejects unsupported hosts, ambiguous handles that cannot be resolved, authenticated/private sources, and non-media pages.

Initial discovery imports the newest 25 unique public items. Scheduled discovery requests only enough pages to reach already-known items, records a durable cursor where supported, and queues only new identities after committing them.

Historical backfill supports:

- Inclusive start/end publication dates, such as a five-year-old range.
- A requested number of older items.
- One explicit podcast episode or YouTube video URL.

The administrator can inspect progress and cancel a pending/running backfill. Cancellation stops future pages and does not delete already discovered items or valid artifacts. Replaying the same backfill is idempotent.

## 6. Transcript acquisition priority

For a YouTube item, the worker performs this ordered decision:

1. Inventory creator-provided captions in the preferred language.
2. If absent or invalid, inventory YouTube automatic captions in the preferred language.
3. If neither produces a valid timed transcript, extract bounded audio and use Amazon Transcribe.

A caption is valid only when it contains timed textual cues, stays under the configured byte limit, and parses without unsupported external references. Caption files are treated as untrusted input.

All origins normalize to the same segment contract:

```text
segment_id
start_milliseconds
end_milliseconds
text
speaker_if_known
source_item_id
transcript_hash
citation_target
```

For YouTube, `citation_target` is the canonical video URL plus a timestamp derived from `start_milliseconds`. The canonical transcript records the chosen origin, caption language, provider/tool versions, acquisition timestamp, and hashes. The fallback audio file is uploaded to the private S3 bucket before Amazon Transcribe and removed from local temporary storage afterward.

## 7. UI behavior

The existing Add Podcast interaction becomes Add Source and accepts all approved source URLs. The source list shows type, canonical identity, enabled state, item count, last check, next check, and safe error status.

Each source page offers a Backfill action with mutually exclusive controls for date range, item count, or explicit item URL. The page shows queued/running/completed/cancelled/failed state and progress counts.

Each item view shows source memberships, publication metadata, processing state, transcript origin, language, transcript availability, and citations. It never reveals private S3 keys, signed URLs, API keys, raw command output, or local file paths.

## 8. Configuration and security

Phase 1 adds:

```text
YOUTUBE_API_KEY
YOUTUBE_INITIAL_ITEM_LIMIT=25
YOUTUBE_DISCOVERY_INTERVAL_SECONDS=3600
YOUTUBE_MAX_PAGES_PER_TASK=5
YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK=100
BACKFILL_REQUEST_MAX_ITEMS=5000
YOUTUBE_PREFERRED_CAPTION_LANGUAGES=en.*,en
YTDLP_EXPECTED_VERSION=2026.08.19
DENO_EXPECTED_VERSION=2.9.7
YTDLP_TIMEOUT_SECONDS=1800
YOUTUBE_CAPTION_MAX_BYTES=52428800
```

The API key lives only in the ignored deployment environment and is redacted from logs. The application performs public-data API calls only. It does not request write scopes or OAuth consent.

`yt-dlp` receives validated canonical YouTube URLs and fixed arguments. Shell invocation, user-supplied output templates, arbitrary extractor arguments, configuration-file discovery, plugins, browser-cookie reading, credential flags, remote components, and runtime self-update are disabled. Downloads use isolated temporary directories and bounded files. The container runs as a non-root user. Readiness verifies the expected `yt-dlp` and Deno versions.

## 9. Failure classification

- API quota exhaustion: retry after the provider reset window and surface `quota_exhausted`.
- Transient API, DNS, or rate-limit failure: exponential retry with jitter and a bounded attempt count.
- Deleted or private video: terminal `unavailable` for that item.
- Upcoming premiere: deferred until its scheduled time.
- Active livestream: deferred until a stable recording/caption source is available.
- Region-blocked, age-restricted, members-only, or cookie-dependent content: terminal `unsupported_public_access` for the initial release.
- Creator-caption failure: fall through to automatic captions.
- Automatic-caption failure: fall through to audio plus Amazon Transcribe.
- Audio extraction or transcription failure: retain the last valid stage and a redacted category for manual retry.

A failed source never stops discovery or processing for other sources. A valid existing transcript prevents reacquisition on retry unless the administrator explicitly requests a new artifact version.

## 10. Testing

Unit and contract tests cover:

- URL classification for videos, shorts, channels, handles, playlists, and unsupported pages.
- Canonical YouTube identity resolution.
- YouTube Data API pagination and channel upload-playlist traversal.
- Newest-25 ordering and scheduled-new-item discovery.
- Date-range, count, and explicit-URL backfills.
- Cancellation and resumption.
- Cross-channel/playlist deduplication through `SourceItem`.
- Creator-caption priority over automatic captions.
- Automatic-caption priority over audio fallback.
- Caption parsing, byte bounds, timestamp citations, and origin lineage.
- Bounded `yt-dlp` invocation without shell, cookies, plugins, or user configuration.
- Retry and terminal-failure classification.
- Private-artifact and log-redaction guarantees.
- Fully stubbed end-to-end ingestion for one video, one channel, and one playlist.

The normal test suite makes no live YouTube or AWS calls. A separately invoked live smoke test uses one short public video and a low-cost API request; it is never part of default CI or development tests.

## 11. Phase 1 acceptance criteria

Phase 1 cannot complete until:

1. The administrator can add a public video, channel, and playlist.
2. Channel and playlist initial discovery stores the newest 25 unique videos.
3. Scheduled discovery adds later uploads without duplicates.
4. Date-range, item-count, and explicit-URL backfills work for both podcasts and YouTube where the selector applies.
5. A video shared by a channel and playlist has one item, multiple memberships, and one transcript.
6. Creator captions, automatic captions, and Amazon Transcribe fallback each pass deterministic contract tests in the required order.
7. Transcript origin, tool/provider version, hash, and timestamp citations are retained.
8. Unsupported public access fails safely without cookies or authentication.
9. YouTube API and `yt-dlp` readiness checks pass in the running Docker stack.
10. The source/item/backfill state survives application-container recreation.

## 12. Downstream learning impact

Phase 2 applies the same restricted `claude -p` learning-bundle extraction to both item types. YouTube metadata and timestamp citations become bundle provenance, not evidence that a claim is true.

Phase 3 adds `podcast_episode` and `youtube_video` intake variants to Lamad, Ebenezer, and Agent Factory. A downstream outage never invalidates the source transcript, and duplicate source memberships never create duplicate downstream deliveries.

Phase 4 retrieves across podcast and YouTube material while preserving item type, speaker/creator attribution, transcript origin, epistemic state, and exact timestamp citations.
