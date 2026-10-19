# Podcast and YouTube Docker Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run an admin-only Docker Compose application that ingests podcast feeds and public YouTube videos, channels, and playlists into one durable, private, timestamped transcript pipeline.

**Architecture:** A Flask application factory serves the private UI and CLI; PostgreSQL stores source-neutral operational state; Redis carries Celery jobs; worker and beat containers discover and process media. The YouTube Data API provides canonical public discovery, pinned `yt-dlp` plus pinned Deno acquires captions or audio, Amazon Transcribe provides speech-to-text fallback, and private immutable artifacts live in `podcast-transcriptions-web-2025`. A one-shot migration service owns schema changes, and loopback-only Gunicorn is the sole host-published service.

**Tech Stack:** Python 3.11, Flask 3, SQLAlchemy, Alembic/Flask-Migrate, Flask-Login, Flask-WTF, Flask-Limiter, PostgreSQL 15, Redis 7, Celery 5, boto3, Amazon Transcribe, YouTube Data API v3, `yt-dlp[default]==2026.8.19`, Deno 2.9.7, FFmpeg, Gunicorn, Docker Compose, pytest.

## Global Constraints

- This plan replaces `docs/superpowers/plans/2026-10-19-docker-foundation.md` and implements Phase 1 from both approved design documents.
- Accept Apple Podcasts pages, RSS/Atom feeds, and public YouTube video, channel, handle, and playlist URLs.
- Do not use YouTube OAuth, Google account login, browser cookies, private content, remote `yt-dlp` components, plugins, runtime self-update, or shell command construction.
- Initial source intake is the newest 25 items. Historical backfill supports exactly one selector per request: inclusive date range, requested item count, or explicit episode/video URL.
- Transcript acquisition order for YouTube is creator captions, automatic captions, then bounded audio extraction plus Amazon Transcribe.
- A YouTube video appearing through several sources has one `ContentItem`, multiple `SourceItem` memberships, and one transcript pipeline.
- Do not add Claude extraction, learning bundles, Lamad delivery, Ebenezer delivery, Agent Factory delivery, or Ask & Learn in Phase 1.
- Do not add Stripe, Mailgun, public registration, OpenAI, OpenRouter, Heroku, S3 events, SQS, Lambda, or public S3 objects.
- Bind only `web` to `127.0.0.1:8005`; publish no PostgreSQL or Redis host ports.
- Store private S3 keys and hashes in PostgreSQL. Never store or log presigned URLs, API keys, authorization headers, transcript bodies, or `yt-dlp` command output.
- Protect every state-changing browser request with CSRF and every source/item route with server-side authorization.
- Bound every HTTP request, response body, caption file, subprocess, temporary file, discovery page, and backfill worker invocation.
- Make every Celery stage idempotent and commit durable state before dispatching the next task.
- Use only `podcast-transcriptions-web-2025`; startup checks fail closed when S3, Transcribe, YouTube API, pinned `yt-dlp`, pinned Deno, PostgreSQL, or Redis validation fails.
- Preserve unrelated user changes. Inspect `git status --short` before every commit and stage only task-owned files.

---

## File and Responsibility Map

| Path | Responsibility after Phase 1 |
| --- | --- |
| `config.py` | Strict environment parsing and exact Phase 1 limits |
| `extensions.py` | Unbound Flask extensions |
| `app.py` | Application factory and blueprint/CLI registration |
| `wsgi.py` | Production WSGI construction boundary |
| `models.py` | Admin, source, item, membership, discovery, backfill, and job state |
| `admin_cli.py` | Single-admin create/reset commands |
| `ingestion_types.py` | Immutable source/item/page descriptors shared by adapters |
| `source_classifier.py` | Apple/RSS/YouTube URL classification |
| `feed_resolver.py` | Bounded podcast source resolution |
| `episode_fetcher.py` | Bounded podcast discovery and audio download |
| `youtube_discovery.py` | YouTube Data API canonical resolution and pagination |
| `youtube_media.py` | Safe pinned `yt-dlp` caption inventory/download and audio extraction |
| `transcripts.py` | VTT/Amazon normalization, hashes, stable segments, citations |
| `s3_utils.py` | Private object operations and bucket validation |
| `transcribe.py` | Deterministic Amazon Transcribe job execution |
| `discovery_service.py` | Candidate staging, newest-item selection, membership, and backfill rules |
| `tasks.py` | Celery discovery, backfill, acquisition, retry, and periodic tasks |
| `celeryconfig.py` | Broker, queues, reliability, limits, and beat schedule |
| `system_checks.py` | Readiness checks and `flask system check` |
| `web/routes.py` | Authenticated source/item/backfill API and pages |
| `templates/` and `static/` | Private Add Source, source, item, and backfill interface |
| `migrations/` | Initial source-neutral versioned schema |
| `scripts/` | Checked web and worker entrypoints |
| `Dockerfile` | Non-root app image with pinned Deno and Python dependencies |
| `docker-compose.yml` | Internal database/queue plus migrate/web/worker/beat topology |
| `tests/` | Unit, security, adapter, pipeline, Compose, and smoke contracts |

---

### Task 1: Establish exact dependencies, configuration, and the test harness

**Files:**
- Replace: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Replace: `.env.example`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: deployment environment or an injected mapping.
- Produces: `ConfigError`; `load_settings(environment: Mapping[str, str] | None = None) -> Settings`.

- [ ] **Step 1: Write failing settings tests**

Test required secrets/URLs, the exact bucket, `YOUTUBE_API_KEY`, secure-cookie defaults, numeric positivity, newest-25 defaults, backfill/task bounds, `yt-dlp`/Deno expected versions, and mapping `DATABASE_URL` to `SQLALCHEMY_DATABASE_URI`. Use a `valid_environment()` fixture containing no real secret.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_config.py -q`.

Expected: collection fails because `config` does not exist.

- [ ] **Step 3: Define runtime and development dependencies**

`requirements.txt` must contain bounded releases for BeautifulSoup, boto3, Celery Redis support, email-validator, feedparser, Flask, Flask-Limiter Redis support, Flask-Login, Flask-Migrate, Flask-SQLAlchemy, Flask-WTF, Gunicorn, psycopg2-binary, python-dateutil, python-dotenv, requests, tenacity, WebVTT parsing, and the exact line:

```text
yt-dlp[default]==2026.8.19
```

`requirements-dev.txt` includes `-r requirements.txt`, pytest, pytest-mock, and responses. Do not retain pandas, rich, rapidfuzz, pydub, OpenAI, or HTTP clients unused by Phase 1.

- [ ] **Step 4: Implement strict settings**

Use these exact defaults:

```text
SESSION_COOKIE_SECURE=true
MAX_CONTENT_LENGTH=1048576
MAX_AUDIO_BYTES=536870912
RSS_MAX_BYTES=10485760
YOUTUBE_CAPTION_MAX_BYTES=52428800
HTTP_CONNECT_TIMEOUT_SECONDS=10
HTTP_READ_TIMEOUT_SECONDS=60
PODCAST_INITIAL_ITEM_LIMIT=25
YOUTUBE_INITIAL_ITEM_LIMIT=25
DISCOVERY_INTERVAL_SECONDS=3600
YOUTUBE_MAX_PAGES_PER_TASK=5
YOUTUBE_MAX_ITEMS_PER_BACKFILL_TASK=100
BACKFILL_REQUEST_MAX_ITEMS=5000
YOUTUBE_PREFERRED_CAPTION_LANGUAGES=en.*,en
YTDLP_EXPECTED_VERSION=2026.08.19
DENO_EXPECTED_VERSION=2.9.7
YTDLP_TIMEOUT_SECONDS=1800
TRANSCRIBE_POLL_SECONDS=10
TRANSCRIBE_TIMEOUT_SECONDS=14400
```

Require `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `AWS_REGION`, `AWS_S3_BUCKET_NAME`, and `YOUTUBE_API_KEY` outside tests. Reject any bucket other than `podcast-transcriptions-web-2025` and any known development secret.

- [ ] **Step 5: Replace the environment contract and ignore secrets**

Document only Phase 1 settings in `.env.example`, with blank secrets and bucket fixed to `podcast-transcriptions-web-2025`. Ignore `.env`, `.aws/`, `.claude/`, `.superpowers/`, caches, coverage, local data, and `.worktrees/`.

- [ ] **Step 6: Verify GREEN and commit**

Run `python -m pip install -r requirements-dev.txt` and `python -m pytest tests/test_config.py -q`.

Expected: all settings tests pass.

Commit:

```bash
git add requirements.txt requirements-dev.txt config.py tests/__init__.py tests/test_config.py .env.example .gitignore
git commit -m "test: establish media ingestion configuration"
```

---

### Task 2: Create an import-safe Flask factory and extension boundary

**Files:**
- Create: `extensions.py`
- Replace: `app.py`
- Create: `wsgi.py`
- Create: `web/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_app_factory.py`

**Interfaces:**
- Consumes: `load_settings()`.
- Produces: `create_app(config_override: Mapping | None = None) -> Flask`; production `wsgi:app`.

- [ ] **Step 1: Write failing factory tests**

Assert that `/healthz` returns exactly `{"status": "ok"}`, importing `app` performs no database or network work, no tables are created implicitly, ProxyFix trusts one proxy hop, and test overrides can disable secure cookies, CSRF, and rate limiting.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_app_factory.py -q`.

Expected: failure because the factory contract is absent.

- [ ] **Step 3: Implement extensions and the minimal factory**

`extensions.py` exports unbound `db`, `migrate`, `login_manager`, `csrf`, and `limiter`. `create_app` loads settings, applies overrides, initializes extensions, and registers the health route. Later tasks add CLI and web-blueprint registration only after those modules exist. The factory must not create a production app at module import and must not call `db.create_all()`.

`wsgi.py` contains only:

```python
from app import create_app

app = create_app()
```

- [ ] **Step 4: Verify GREEN and commit**

Run `python -m pytest tests/test_config.py tests/test_app_factory.py -q`.

Expected: all tests pass.

Commit:

```bash
git add extensions.py app.py wsgi.py web/__init__.py tests/conftest.py tests/test_app_factory.py
git commit -m "refactor: add flask application factory"
```

---

### Task 3: Add the source-neutral relational model and initial migration

**Files:**
- Replace: `models.py`
- Delete: `migrations.py`
- Create: `migrations/alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_media_foundation.py`
- Create: `tests/test_models.py`
- Create: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `extensions.db` and `extensions.migrate`.
- Produces: `Admin`, `ContentSource`, `ContentItem`, `SourceItem`, `DiscoveryRun`, `DiscoveryCandidate`, `BackfillRequest`, and `ProcessingJob`.

- [ ] **Step 1: Write failing invariant tests**

Cover one-admin database enforcement; unique `(source_type, external_id)` sources; unique `(platform, external_id)` items; unique source/item membership; one processing row per item/stage; one candidate per discovery run/item; exactly one valid backfill selector; timezone-aware timestamps; cascades; and explicit state check constraints.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_models.py -q`.

Expected: missing model and field failures.

- [ ] **Step 3: Implement the models**

Use these controlled values:

```text
source_type: podcast_rss, youtube_channel, youtube_playlist, youtube_video
item_type: podcast_episode, youtube_video
platform: podcast, youtube
item status: discovered, acquiring, transcribing, completed, failed, deferred, unavailable
transcript_origin: creator_caption, automatic_caption, amazon_transcribe
discovery run type: initial, scheduled, backfill
discovery state: pending, scanning, finalizing, completed, cancelled, failed
backfill selector: date_range, count, explicit_url
backfill state: pending, running, completed, cancelled, failed
job stage: podcast_audio, youtube_captions, youtube_audio, transcribe, normalize
job state: pending, running, retrying, completed, failed
```

`ContentItem` stores canonical URL, safe remote media URL, publication time, creator/channel metadata, duration, transcript lineage, private S3 keys, and hashes. `DiscoveryCandidate` stores normalized descriptor JSON only until finalization. `BackfillRequest` enforces selector-specific fields in both model validation and database checks.

- [ ] **Step 4: Create and test migration `0001_media_foundation`**

The migration creates all eight tables, foreign keys, uniqueness/check constraints, and indexes for enabled sources, item status/publication time, discovery state, backfill state, and job state. Downgrade removes them in reverse order. A temporary SQLite migration test runs upgrade, inspects tables/version, downgrades to base, and confirms removal.

- [ ] **Step 5: Verify GREEN and commit**

Run `python -m pytest tests/test_models.py tests/test_migrations.py -q`.

Expected: all schema and migration tests pass.

Commit:

```bash
git add models.py migrations tests/test_models.py tests/test_migrations.py
git rm migrations.py
git commit -m "feat: add source neutral media schema"
```

---

### Task 4: Enforce one administrator and secure authentication

**Files:**
- Create: `admin_cli.py`
- Create: `web/auth.py`
- Modify: `app.py`
- Create: `tests/test_admin_cli.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Consumes: `Admin`, Flask-Login, CSRF, Limiter.
- Produces: `flask admin create`; `flask admin reset-password`; `/login`; POST-only `/logout`.

- [ ] **Step 1: Write failing CLI/authentication tests**

Cover normalized validated email, hidden confirmed password, 14-character minimum, database/application refusal of a second admin, reset-password, generic invalid-login response, five attempts per minute, safe relative `next`, CSRF logout, and 404 for both methods on `/register`.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_admin_cli.py tests/test_auth.py -q`.

Expected: missing command and route failures.

- [ ] **Step 3: Implement the commands and routes**

Catch `IntegrityError`, always rollback, never echo passwords, and load the sole Admin through Flask-Login. Browser requests redirect to login; unauthenticated API requests return JSON 401. Logout accepts POST only.

- [ ] **Step 4: Verify GREEN and commit**

Run `python -m pytest tests/test_admin_cli.py tests/test_auth.py -q`.

Expected: all tests pass.

Commit:

```bash
git add admin_cli.py web/auth.py app.py tests/test_admin_cli.py tests/test_auth.py
git commit -m "feat: enforce single administrator access"
```

---

### Task 5: Define common ingestion contracts and bounded podcast intake

**Files:**
- Create: `ingestion_types.py`
- Create: `source_classifier.py`
- Replace: `feed_resolver.py`
- Replace: `episode_fetcher.py`
- Create: `tests/fixtures/feed.xml`
- Create: `tests/test_source_classifier.py`
- Create: `tests/test_podcast_ingestion.py`

**Interfaces:**
- Produces: `SourceDescriptor`, `ItemDescriptor`, `DiscoveryPage`; `classify_source_url(url: str) -> str`; `resolve_podcast_source(url, settings, session) -> SourceDescriptor`; `discover_podcast_items(source, settings, session) -> DiscoveryPage`; `download_podcast_audio(url, destination, settings, session) -> DownloadedMedia`.

- [ ] **Step 1: Write failing contract and podcast tests**

Test immutable descriptor fields, Apple and direct-feed classification, Apple lookup resolution, direct RSS GET fallback when HEAD is unsupported, relative feed links, safe failures, timezone normalization, SHA-256 GUID fallback, newest-first ordering, 25-item initial slicing, explicit date/count selection, bounded RSS/audio streaming, partial-file cleanup, and URL query redaction.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_source_classifier.py tests/test_podcast_ingestion.py -q`.

Expected: descriptor imports fail and current unbounded/MD5 behavior fails.

- [ ] **Step 3: Implement contracts and bounded podcast adapters**

All HTTP calls use `(10, 60)` default timeouts and 64 KiB streaming chunks. `SourceDescriptor.external_id` for podcasts is SHA-256 of normalized canonical feed URL. `ItemDescriptor.external_id` is SHA-256 of source external ID plus RSS GUID. Remove aria2 and subprocess fallback.

- [ ] **Step 4: Verify GREEN and commit**

Run `python -m pytest tests/test_source_classifier.py tests/test_podcast_ingestion.py -q`.

Expected: all tests pass without real network access.

Commit:

```bash
git add ingestion_types.py source_classifier.py feed_resolver.py episode_fetcher.py tests/fixtures/feed.xml tests/test_source_classifier.py tests/test_podcast_ingestion.py
git commit -m "feat: add bounded podcast ingestion contracts"
```

---

### Task 6: Add canonical YouTube Data API discovery

**Files:**
- Create: `youtube_discovery.py`
- Modify: `source_classifier.py`
- Create: `tests/fixtures/youtube/`
- Create: `tests/test_youtube_discovery.py`

**Interfaces:**
- Consumes: `SourceDescriptor`, `ItemDescriptor`, `DiscoveryPage`, API key, injected requests session.
- Produces: `YouTubeDiscoveryClient.resolve_source(url: str) -> SourceDescriptor`; `YouTubeDiscoveryClient.discover_page(source, page_token: str | None) -> DiscoveryPage`; `YouTubeDiscoveryClient.resolve_video(url: str) -> ItemDescriptor`.

- [ ] **Step 1: Write failing URL and API tests**

Cover `youtu.be`, `watch?v=`, `/shorts/`, `/channel/UC…`, `/@handle`, and `playlist?list=`. Reject private/account URLs, `/c/` aliases that cannot be canonically resolved, malformed IDs, and non-YouTube hosts. Mock `videos.list`, `channels.list` with `id`, `forHandle`, and `forUsername`, channel uploads playlists, `playlists.list`, and paginated `playlistItems.list` with `maxResults=50`.

Assert typed errors for 400/403/404, quota exhaustion, transient 429/5xx, private/deleted items, upcoming/live items, and region/age/member restrictions.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_youtube_discovery.py -q`.

Expected: module import fails.

- [ ] **Step 3: Implement the injected API client**

Use `https://www.googleapis.com/youtube/v3` with API key query authentication. Never log the prepared URL. Channel sources store the uploads playlist ID in provider metadata. Each page returns normalized item descriptors plus a next-page token and ETag. YouTube item identity is the 11-character video ID.

- [ ] **Step 4: Verify GREEN and commit**

Run `python -m pytest tests/test_youtube_discovery.py -q`.

Expected: all pagination, resolution, and classification tests pass.

Commit:

```bash
git add youtube_discovery.py source_classifier.py tests/fixtures/youtube tests/test_youtube_discovery.py
git commit -m "feat: add youtube data api discovery"
```

---

### Task 7: Add safe caption-first YouTube media acquisition

**Files:**
- Create: `youtube_media.py`
- Create: `tests/fixtures/youtube/captions.vtt`
- Create: `tests/test_youtube_media.py`

**Interfaces:**
- Produces: `CaptionCandidate`; `YouTubeMediaClient.inventory(video_url) -> CaptionInventory`; `select_caption(inventory, preferred_languages) -> CaptionCandidate | None`; `download_caption(video_url, candidate, directory) -> Path`; `extract_audio(video_url, directory) -> DownloadedMedia`.

- [ ] **Step 1: Write failing safety and priority tests**

Test creator captions winning over automatic captions, preferred-language ordering, automatic fallback, no-caption result, VTT output discovery, audio-only MP3 extraction, timeout, maximum file size, and redacted failures. Inspect the subprocess argument array and assert `shell=False`, `--no-config`, `--no-plugin-dirs`, `--no-playlist`, `--js-runtimes deno`, no cookies/credentials, no remote components, fixed output templates, and canonical YouTube URL only.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_youtube_media.py -q`.

Expected: module import fails.

- [ ] **Step 3: Implement the safe wrapper**

Inventory uses `--dump-single-json --skip-download`. Read `subtitles` and `automatic_captions` separately. Caption download enables only the selected kind/language and VTT format. Audio extraction uses `bestaudio/best`, FFmpeg MP3 conversion, `--max-filesize 536870912`, a 1,800-second deadline, an isolated temporary directory, and post-download size verification. Capture at most 64 KiB of diagnostic output and redact URLs/tokens before storing an error category.

- [ ] **Step 4: Verify GREEN and commit**

Run `python -m pytest tests/test_youtube_media.py -q`.

Expected: all caption priority and command-safety tests pass.

Commit:

```bash
git add youtube_media.py tests/fixtures/youtube/captions.vtt tests/test_youtube_media.py
git commit -m "feat: add safe youtube caption and audio acquisition"
```

---

### Task 8: Add private S3 storage and deterministic transcript normalization

**Files:**
- Replace: `s3_utils.py`
- Replace: `transcribe.py`
- Create: `transcripts.py`
- Create: `tests/fixtures/transcribe-result.json`
- Create: `tests/test_s3_utils.py`
- Create: `tests/test_transcripts.py`
- Create: `tests/test_transcribe.py`

**Interfaces:**
- Produces: private `S3Manager`; `normalize_vtt`; `normalize_amazon_transcribe`; `CanonicalTranscript`; deterministic `transcribe_audio_from_s3`.

- [ ] **Step 1: Write failing storage/normalization tests**

Assert key-returning encrypted S3 puts, bounded gets, exact-region probe validation, no HTTP URLs, stable transcript/segment hashes, punctuation handling, stable segment IDs, YouTube timestamp citations, podcast timestamp citations, transcript-origin lineage, deterministic Amazon job names, conflict resume, completed-job reuse, timeout, failed-job redaction, and private provider-result reads.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_s3_utils.py tests/test_transcripts.py tests/test_transcribe.py -q`.

Expected: existing URL-returning storage and random-job behavior fail.

- [ ] **Step 3: Implement private storage and the canonical transcript contract**

Artifact keys use `items/{platform}/{external_id}/{kind}/{filename}`. All writes specify content type and AES256 server-side encryption. Canonical segment JSON contains schema `media-transcript-segments/v1`, source/item identity, origin, tool/provider versions, language, hashes, timed segments, and citation targets. Serialize hashed JSON with sorted keys and compact separators.

- [ ] **Step 4: Implement deterministic Amazon Transcribe**

Use job name `media-{item_id}-{audio_sha256_prefix}-v1`, direct private S3 input/output, bounded polling, conflict resume, and provider JSON read through `S3Manager`. Return only private keys, hashes, language, duration, provider, and job name.

- [ ] **Step 5: Verify GREEN and commit**

Run `python -m pytest tests/test_s3_utils.py tests/test_transcripts.py tests/test_transcribe.py -q`.

Expected: all tests pass.

Commit:

```bash
git add s3_utils.py transcribe.py transcripts.py tests/fixtures/transcribe-result.json tests/test_s3_utils.py tests/test_transcripts.py tests/test_transcribe.py
git commit -m "feat: normalize private media transcripts"
```

---

### Task 9: Implement idempotent source discovery and controlled backfills

**Files:**
- Create: `discovery_service.py`
- Replace: `celeryconfig.py`
- Replace: `tasks.py`
- Create: `tests/test_discovery_service.py`
- Create: `tests/test_discovery_tasks.py`
- Create: `tests/test_backfills.py`

**Interfaces:**
- Produces Celery tasks: `discover_source`, `continue_discovery`, `finalize_discovery`, `run_backfill`, `cancel_backfill`, `check_all_sources`.

- [ ] **Step 1: Write failing discovery tests**

Cover podcast newest-25 selection, channel chronological newest-25 selection, playlist full-pagination candidate staging before newest-25 finalization, page-bound continuation after five pages, scheduled new-item discovery, duplicate source memberships, commit-before-dispatch, disabled-source skipping, and independent source failures.

- [ ] **Step 2: Write failing backfill tests**

Cover inclusive date filtering, count filtering, explicit URL resolution, one-selector enforcement, 5,000-item request maximum, 100 accepted items per worker invocation, durable cursor resume, progress counts, cancellation, replay idempotency, and provider-availability limits for old RSS items.

- [ ] **Step 3: Verify RED**

Run `python -m pytest tests/test_discovery_service.py tests/test_discovery_tasks.py tests/test_backfills.py -q`.

Expected: missing service/task failures.

- [ ] **Step 4: Implement candidate staging and task routing**

Initial YouTube playlist scans stage candidates across bounded tasks, then finalization selects the newest 25 by publication time and creates memberships. Channel upload playlists use the same contract. Scheduled playlist and channel discovery also scans bounded pages to completion before queuing only unseen items, so arbitrary playlist ordering cannot hide new uploads. Podcast discovery uses the normalized page directly. Backfill tasks process at most 100 accepted items before rescheduling themselves.

Configure queues `default`, `discovery`, `downloads`, and `transcription`; JSON serialization; UTC; late acknowledgements; reject-on-worker-lost; prefetch one; startup broker retry; explicit soft/hard limits; result expiry; and hourly `check_all_sources` beat schedule.

- [ ] **Step 5: Verify GREEN and commit**

Run `python -m pytest tests/test_discovery_service.py tests/test_discovery_tasks.py tests/test_backfills.py -q`.

Expected: all discovery, deduplication, backfill, and cancellation tests pass.

Commit:

```bash
git add discovery_service.py celeryconfig.py tasks.py tests/test_discovery_service.py tests/test_discovery_tasks.py tests/test_backfills.py
git commit -m "feat: add durable source discovery and backfills"
```

---

### Task 10: Implement the caption-priority item processing pipeline

**Files:**
- Modify: `tasks.py`
- Create: `tests/test_processing_tasks.py`

**Interfaces:**
- Produces Celery tasks: `process_item`, `acquire_podcast_audio`, `acquire_youtube_transcript`, `transcribe_item_audio`, and `normalize_item_transcript`.

- [ ] **Step 1: Write failing podcast processing tests**

Assert bounded download, audio SHA-256, deterministic private key, temporary cleanup, durable stage commits, Amazon transcription, completed state, repeat-task artifact reuse, retry classification, and redacted failure state.

- [ ] **Step 2: Write failing YouTube processing tests**

Assert creator caption success skips automatic/audio, creator failure tries automatic, automatic failure extracts audio and transcribes, valid existing transcript skips all acquisition, duplicate memberships queue once, unavailable/deferred provider states map correctly, and every origin/tool/hash/citation field is persisted.

- [ ] **Step 3: Verify RED**

Run `python -m pytest tests/test_processing_tasks.py -q`.

Expected: task imports or pipeline assertions fail.

- [ ] **Step 4: Implement idempotent processing**

Use a `ProcessingJob` upsert per item/stage. Pass integer IDs only through Celery. Commit the item and job before scheduling the next task. Retry transient HTTP/AWS/provider errors with exponential backoff and jitter; stop on invalid input or unsupported public access. Never reacquire a valid hashed artifact unless an explicit new version is requested.

- [ ] **Step 5: Verify GREEN and commit**

Run `python -m pytest tests/test_processing_tasks.py -q` and `python -m pytest -q`.

Expected: all tests pass without live network calls.

Commit:

```bash
git add tasks.py tests/test_processing_tasks.py
git commit -m "feat: add caption priority processing pipeline"
```

---

### Task 11: Build the private Add Source, item, and backfill interface

**Files:**
- Create: `web/routes.py`
- Modify: `app.py`
- Replace: `templates/base.html`
- Replace: `templates/index.html`
- Replace: `templates/login.html`
- Replace: `templates/dashboard.html`
- Replace: `templates/episodes.html` with `templates/source_items.html`
- Create: `templates/source.html`
- Create: `templates/item.html`
- Modify: `static/js/app.js`
- Delete: `templates/register.html`
- Delete: `templates/subscription.html`
- Delete: `templates/emails/summary_ready.html`
- Delete: `templates/emails/transcript_ready.html`
- Delete: `templates/emails/welcome.html`
- Create: `tests/test_web_sources.py`
- Create: `tests/test_web_security.py`

**Interfaces:**
- Produces authenticated routes for dashboard, add source, source items, item detail/transcript, create/cancel backfill, retry item, and POST logout.

- [ ] **Step 1: Write failing source/UI tests**

Cover Add Source classification for every supported URL, commit-before-discovery dispatch, duplicate-source response, newest-25 status display, source memberships, transcript origin, backfill selector validation/progress/cancel, safe retry, and 404 ownership isolation.

- [ ] **Step 2: Write failing security tests**

Cover no registration/payment/email claims, global CSRF for form and JSON POSTs, generic errors, transcript `Cache-Control: no-store`, no private keys/URLs/command output in HTML or JSON, POST-only logout, and unauthenticated API JSON 401.

- [ ] **Step 3: Verify RED**

Run `python -m pytest tests/test_web_sources.py tests/test_web_security.py -q`.

Expected: missing routes and public-product remnants fail.

- [ ] **Step 4: Implement routes and the private UI**

Use one Add Source form. Show source type, canonical identity, health, item counts, and discovery/backfill state. Backfill controls are mutually exclusive. Route every JSON mutation through a shared `apiRequest` that reads the rendered CSRF meta token. Item JSON exposes availability booleans and lineage labels, never storage keys.

- [ ] **Step 5: Verify GREEN and commit**

Run `python -m pytest tests/test_web_sources.py tests/test_web_security.py tests/test_auth.py -q`.

Expected: all tests pass.

Commit:

```bash
git add app.py web/routes.py templates static/js/app.js tests/test_web_sources.py tests/test_web_security.py
git rm templates/register.html templates/subscription.html templates/emails/summary_ready.html templates/emails/transcript_ready.html templates/emails/welcome.html templates/episodes.html
git commit -m "feat: add private media source workspace"
```

---

### Task 12: Add readiness checks and the hardened Docker topology

**Files:**
- Create: `system_checks.py`
- Modify: `app.py`
- Replace: `Dockerfile`
- Replace: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `scripts/start-web.sh`
- Create: `scripts/start-worker.sh`
- Create: `tests/test_system_checks.py`
- Create: `tests/test_compose_contract.py`

**Interfaces:**
- Produces: `/readyz`; `flask system check --services`; six-service Compose deployment.

- [ ] **Step 1: Write failing readiness tests**

Mock and test PostgreSQL migration state, Redis ping, S3 access probe/region, Transcribe authenticated list call, YouTube low-cost public API validation, `yt-dlp --version == 2026.08.19`, `deno --version == 2.9.7`, Celery worker ping, and beat schedule presence. Readiness output contains only component and `ok`/`failed`.

- [ ] **Step 2: Write failing Compose/image contract tests**

Assert the exact services `postgres`, `redis`, `migrate`, `web`, `worker`, and `beat`; only loopback web port; named PostgreSQL volume; one-shot migration; dependency health; worker queues; beat; non-root image; worker tmpfs bounded to 768 MiB; pinned Deno multi-stage source; exact `yt-dlp` requirement; and absence of prohibited providers/queues.

- [ ] **Step 3: Verify RED**

Run `python -m pytest tests/test_system_checks.py tests/test_compose_contract.py -q`.

Expected: missing checks/services and published database ports fail.

- [ ] **Step 4: Implement checks and startup commands**

`/healthz` remains dependency-free. `/readyz` caches full check results for 15 seconds. Web/worker startup runs database, Redis, S3, Transcribe, YouTube, `yt-dlp`, and Deno checks before exec. Beat startup checks database/Redis and its configured schedule.

- [ ] **Step 5: Replace the image and Compose**

Use:

```dockerfile
FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.11-slim
COPY --from=deno /deno /usr/local/bin/deno
```

Install only ca-certificates and FFmpeg system packages, create UID/GID 1000 app user, copy with ownership, and run non-root. Compose keeps PostgreSQL/Redis internal, runs migrations before app services, binds `127.0.0.1:8005:8005`, and gives only the worker a 768 MiB `/tmp` tmpfs.

- [ ] **Step 6: Verify GREEN and commit**

Run:

```bash
python -m pytest tests/test_system_checks.py tests/test_compose_contract.py -q
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps web python -m compileall -q /app
```

Expected: tests, rendered configuration, image build, and compilation pass.

Commit:

```bash
git add system_checks.py app.py Dockerfile docker-compose.yml .dockerignore scripts tests/test_system_checks.py tests/test_compose_contract.py
git commit -m "feat: harden the media ingestion stack"
```

---

### Task 13: Remove obsolete code and write Docker operations documentation

**Files:**
- Delete: `Procfile`, `app.json`, `runtime.txt`, `deploy.sh`, `heroku-setup.sh`
- Delete: `summarize.py`, `prompts.py`, `storage.py`, `podcast_pipeline.py`
- Delete: `check_redis_queue.py`, `debug_episodes.py`, `trigger_discovery.py`
- Delete: `DEPLOYMENT.md`, `DEPLOY_NOW.md`, `LAUNCH.md`, `NETWORK_ACCESS.md`, `TROUBLESHOOTING.md`
- Replace: `README.md`
- Create: `docs/operations.md`
- Create: `tests/test_repository_hygiene.py`

**Interfaces:**
- Produces: one supported Docker deployment and operations path.

- [ ] **Step 1: Write a failing hygiene test**

Scan runtime Python, templates, image, Compose, environment example, and executable root scripts for Heroku, Stripe, Mailgun, OpenAI, OpenRouter, registration/subscription routes, email tasks, summary queues, cookie flags, OAuth, remote yt-dlp components, and shell-form media execution. Exclude approved specs/plans and documentation sentences that explicitly state an integration is absent.

- [ ] **Step 2: Verify RED and remove legacy assets**

Run `python -m pytest tests/test_repository_hygiene.py -q`.

Expected: current legacy files fail the scan. Delete exactly the listed files and repair any remaining runtime import before proceeding.

- [ ] **Step 3: Write installation and operations documentation**

Document `.env`, generated secrets, AWS credentials, YouTube API key creation/restriction, build/start, migration, admin creation/reset, Cloudflare origin `http://localhost:8005`, source/backfill use, health diagnosis, `yt-dlp`/Deno reviewed upgrades, logs, retries, PostgreSQL backup/restore, secret rotation, and persistence. Warn that `docker compose down -v` deletes PostgreSQL data.

- [ ] **Step 4: Verify GREEN and commit**

Run `python -m pytest tests/test_repository_hygiene.py -q` and `python -m pytest -q`.

Expected: all tests pass.

Commit:

```bash
git add README.md docs/operations.md tests/test_repository_hygiene.py
git add -u
git commit -m "docs: replace legacy deployment with media operations"
```

---

### Task 14: Prove the Phase 1 exit condition

**Files:**
- Create: `tests/test_phase1_smoke.py`
- Create: `tests/live/test_youtube_live.py`
- Modify: `README.md`
- Modify: `docs/operations.md`

**Interfaces:**
- Consumes: complete Phase 1 stack.
- Produces: deterministic podcast/YouTube smoke evidence and opt-in live verification instructions.

- [ ] **Step 1: Write the fully stubbed end-to-end smoke test**

Exercise admin creation/login; one RSS source; one YouTube video, channel, and playlist; newest-25 selection; cross-source duplicate video membership; creator-caption success; automatic-caption success; audio/Amazon fallback; date/count/explicit backfills; authorized transcript retrieval; and idempotent replay. Assert private keys, origins, hashes, citations, row counts, and no duplicate processing.

- [ ] **Step 2: Verify the automated suite**

Run:

```bash
python -m pytest -q
docker compose config --quiet
docker compose build
```

Expected: all tests pass and the image builds.

- [ ] **Step 3: Run the real migration and persistence gate**

With real ignored `.env` values:

```bash
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose exec -T postgres psql -U podcasts -d podcasts -tAc "select version_num from alembic_version"
```

Expected: `0001_media_foundation`.

Create the admin if absent, start the full stack, verify `docker compose ps --all`, `/healthz`, and `/readyz`, record table counts, run `docker compose down`, run `docker compose up -d`, and confirm counts are unchanged.

- [ ] **Step 4: Run controlled live source checks**

Use one short public YouTube video and one small podcast feed. Run the opt-in YouTube test explicitly, never as part of the default suite. Verify the video takes the highest available caption origin, a fixture/no-caption video exercises Amazon fallback only when intentionally selected, channel/playlist discovery imports 25 or all available when fewer, historical backfill progresses, and no key/private URL appears in UI or logs.

- [ ] **Step 5: Commit the smoke proof**

```bash
git add tests/test_phase1_smoke.py tests/live/test_youtube_live.py README.md docs/operations.md
git commit -m "test: prove podcast and youtube phase one flow"
```

---

## Phase 1 Completion Gate

- [ ] `python -m pytest -q` passes with no live network calls.
- [ ] `docker compose config --quiet` and `docker compose build` pass.
- [ ] Fresh PostgreSQL migrates to `0001_media_foundation`.
- [ ] Only `web` publishes `127.0.0.1:8005`.
- [ ] Liveness and full readiness return 200 with the stack running.
- [ ] The admin CLI creates one account and refuses a second.
- [ ] Apple/RSS plus public YouTube video/channel/playlist sources work.
- [ ] New channel/playlist sources select the newest 25 unique items.
- [ ] Scheduled discovery and every backfill selector are idempotent.
- [ ] Creator captions, automatic captions, and Amazon fallback follow the required order and preserve origin/citations.
- [ ] Shared YouTube videos have multiple memberships but one item/transcript.
- [ ] PostgreSQL state survives application-container recreation.
- [ ] Registration, payment, email, hosted-provider, cookie/OAuth, and runtime downloader-update paths are absent.
- [ ] Private artifacts remain in `podcast-transcriptions-web-2025`; no secret, transcript body, storage key, or signed URL leaks.
- [ ] Cloudflare can target `http://localhost:8005`; tunnel/DNS configuration remains administrator-owned.

## Deferred Plans

1. Phase 2: restricted `claude -p` extraction, schema-validated learning-media bundles, knowledge UI, and publication outbox.
2. Phase 3: Lamad, Ebenezer, and Agent Factory `podcast_episode`/`youtube_video` adapters with independent delivery.
3. Phase 4: cited Ask & Learn retrieval and feedback-driven learning outcomes.
