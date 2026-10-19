# Docker Podcast and Video Learning Platform Design

**Status:** Approved with YouTube amendment
**Date:** 2026-10-19  
**Primary URL:** `https://podcasts.ennovocon.com`  
**Installation path:** `/home/wdphillips/PodcastTranscriptionWeb`  
**Canonical artifact bucket:** `s3://podcast-transcriptions-web-2025`

## 1. Goal

Turn PodcastTranscriptionWeb into an admin-only podcast and public-YouTube learning workspace that runs on this machine with Docker Compose. The application will let the administrator add podcast feeds, YouTube videos, channels, and playlists; discover and transcribe their items; extract lessons with the locally authenticated Claude CLI; search and learn from the resulting knowledge; and automatically publish every completed item to Lamad, Ebenezer, and Agent Factory.

The system is not merely a transcript archive. It must support a closed learning loop:

```text
podcast or YouTube source
  -> timestamped transcript
  -> structured claims, teachings, and exercises
  -> governed memory and learning intake
  -> reusable Agent Factory capabilities
  -> cited lessons and implementation plans
  -> administrator feedback and outcomes
  -> evaluated learning
```

## 2. Product Decisions

The following decisions are fixed for this design:

- Deployment uses Docker Compose on this machine, not Heroku.
- The application listens on `127.0.0.1:8005` only.
- The administrator will configure a Cloudflare Tunnel for `podcasts.ennovocon.com` that targets `http://localhost:8005`.
- The application has exactly one administrator and no public registration.
- Stripe and all subscription/payment behavior are removed.
- Mailgun and email notifications are removed for this release.
- Amazon Transcribe remains the speech-to-text provider.
- `s3://podcast-transcriptions-web-2025` is the canonical audio, transcript, and learning-bundle store.
- Claude Code runs in non-interactive `claude -p` mode for summaries and structured teaching extraction.
- PostgreSQL plus Celery provides the publication outbox. S3 Events, SQS, and Lambda are not required.
- Every completed podcast episode or YouTube video is automatically submitted to Lamad, Ebenezer, and Agent Factory.
- Automatic submission does not silently convert a speaker's claim or a model inference into verified truth.
- Agent Factory may automatically ingest candidate evidence, but trusted skill or agent releases remain versioned and governed.

## 3. Scope

### 3.1 In scope

- Production-ready Docker Compose deployment.
- PostgreSQL, Redis, web, worker, and scheduler services.
- Single-administrator provisioning and login.
- Apple Podcasts URL and RSS-feed intake.
- Public YouTube video, channel, and playlist intake without cookies or account authentication.
- Periodic podcast-episode and YouTube-video discovery.
- Initial intake of the newest 25 items plus controlled historical backfill by date range, item count, or explicit item URL.
- Amazon Transcribe integration using the designated S3 bucket.
- Timestamped transcript normalization.
- Restricted Claude CLI summarization and teaching extraction.
- Immutable, versioned content-item learning bundles.
- A browsable source, item, transcript, lesson, and knowledge interface.
- An Ask & Learn interface that produces cited lessons and implementation plans.
- Reliable automatic publishing to Lamad, Ebenezer, and Agent Factory.
- Per-destination delivery state, retries, and administrator-visible failures.
- Feedback/outcome capture for lessons and plans.
- Tests, health checks, backups, and operating documentation.

### 3.2 Out of scope

- Heroku deployment or compatibility work.
- Public account creation or multi-tenant billing.
- Stripe, Mailgun, or subscription management.
- S3 event notifications, SQS, Lambda, or a second cloud orchestrator.
- Automatically asserting that podcast advice is scientifically or factually correct.
- Automatically publishing copyrighted full transcripts to Git or a public endpoint.
- Silently modifying a trusted Agent Factory skill without versioning, evaluation, and promotion.
- Authenticated, private, members-only, age-restricted, or cookie-dependent YouTube intake.

## 4. Existing Repository Findings

The repository already contains a Flask application, PostgreSQL models, Celery tasks, Redis configuration, a Dockerfile, and a Compose file. The current deployment is not production-ready and requires focused repair:

- The Compose startup command calls `flask db upgrade`, but no usable migration history is present.
- `migrations.py` imports a removed `Subscription` model.
- PostgreSQL and Redis are published on host ports already used by services on this machine.
- There is no Celery Beat service even though periodic discovery is configured.
- Celery invocation and task signatures are inconsistent in several places.
- Transcription and summarization mix local filesystem paths with S3 URLs.
- The transcription task references an undefined `whisper_model` variable.
- The summary endpoint and task signatures disagree.
- The current dependency list and implementation disagree about OpenAI support.
- Docker and documentation still contain Heroku, Stripe, Mailgun, and OpenAI remnants that no longer describe the desired system.
- The landing and login pages still advertise public registration and email delivery.

These are installation blockers and will be repaired as part of this design. Unrelated refactoring is excluded.

## 5. System Architecture

```text
Cloudflare Tunnel
        |
        v
127.0.0.1:8005
        |
        v
  Flask/Gunicorn web
        |
        +---------------------------+
        |                           |
   PostgreSQL                    Redis
 application state          Celery broker/backend
 publication outbox
        |                           |
        +-------------+-------------+
                      |
                 Celery workers
       discovery / ingestion / transcription /
          extraction / publication
                      |
       +--------------+-------------------+
       |              |                   |
Amazon Transcribe / yt-dlp captions / Claude CLI / downstream adapters
       |              |             /        |          \
       +------ S3 learning bundle -+      Ebenezer    Agent Factory
                                      Lamad
```

### 5.1 Compose services

| Service | Responsibility | Host exposure |
| --- | --- | --- |
| `web` | Flask UI/API under Gunicorn | `127.0.0.1:8005` |
| `worker` | Celery ingestion, transcription, extraction, and publication queues | None |
| `beat` | Scheduled RSS and YouTube discovery | None |
| `postgres` | Durable application state and outbox | None |
| `redis` | Celery broker and result backend | None |
| `migrate` | One-shot versioned database migration | None |

The web, worker, and beat services use the same application image. Only the worker receives the Claude credential mount. Database and Redis ports remain internal to the Compose network.

### 5.2 Persistent state

- PostgreSQL uses a named Docker volume.
- Redis is operational queue state, not the source of truth.
- Audio, transcripts, summaries, teaching artifacts, and learning bundles live in the designated S3 bucket.
- Temporary audio files live in container-local temporary storage and are deleted after upload/processing.
- Claude credentials live in a dedicated host configuration directory mounted into the worker; they are never copied into the image or repository.

## 6. Administrator and Application Security

Public registration is removed at the route, template, and navigation levels. The unauthenticated landing page offers login only.

The administrator is provisioned with an interactive command:

```text
docker compose run --rm web flask admin create
```

The command prompts for email and password without persisting the plaintext password in `.env`. It refuses to create a second account. Password replacement is a separate authenticated or interactive administrative operation.

Security controls include:

- Secure, HTTP-only, same-site session cookies in production.
- CSRF protection for state-changing browser requests.
- Login throttling and generic authentication errors.
- A generated production `SECRET_KEY` stored outside Git.
- No direct host exposure for PostgreSQL or Redis.
- `.env` and credential directories excluded from Git and restricted to the owner.
- Log redaction for credentials, signed URLs, transcript bodies, and authorization headers.
- Explicit request timeouts and response-size limits for remote fetches.
- Server-side authorization checks on every source, item, artifact, and lesson route.

Cloudflare supplies public TLS. Cloudflare Access may be added as defense in depth, but application login remains mandatory.

## 7. Content Processing Workflow

### 7.1 Discovery

The administrator adds an Apple Podcasts URL, RSS URL, public YouTube video, public YouTube channel, or public YouTube playlist. Podcast URLs resolve to a canonical feed. YouTube URLs resolve through the official YouTube Data API to canonical public identities. Celery Beat checks enabled sources on a configurable interval and creates idempotent items using feed-plus-GUID identity for podcast episodes and YouTube video ID identity for videos.

New sources import the newest 25 items. Historical backfill accepts a date range, requested item count, or explicit episode/video URL and runs in bounded, cancellable batches. A YouTube video that appears in both a channel and a playlist is stored and processed once while retaining both source memberships.

### 7.2 Processing states

A content item uses explicit states:

```text
discovered
-> downloading
-> transcribing
-> extracting
-> bundling
-> completed
```

Failures retain the last successful stage and a redacted error. Retrying resumes from the last valid immutable artifact rather than repeating all prior work.

Downstream delivery is separate from item completion:

```text
pending -> delivering -> delivered
                      -> retryable_failure
                      -> permanent_failure
```

A Lamad outage therefore does not erase a valid transcript or prevent Ebenezer and Agent Factory delivery.

### 7.3 Transcript acquisition

Podcast audio is downloaded to a bounded temporary file, validated, and stored in S3. YouTube transcript acquisition uses creator-provided captions first, YouTube automatic captions second, and bounded audio extraction plus Amazon Transcribe only as fallback. The worker records `creator_caption`, `automatic_caption`, or `amazon_transcribe` as transcript origin, normalizes every result into stable timestamped segments, and calculates content hashes. YouTube segments retain video ID and timestamp citation targets.

`AWS_REGION` is required configuration. Startup validation checks that the configured region and account can read/write the exact bucket and invoke Amazon Transcribe. Configuration fails closed rather than silently using a different bucket.

`YOUTUBE_API_KEY` is required for public channel and playlist discovery. The worker image contains `yt-dlp[default]==2026.8.19`, its matching EJS solver dependency, and Deno `2.9.7`; upgrades require a reviewed image change. No remote solver download, runtime self-update, YouTube OAuth token, browser cookie, or authenticated account is allowed.

### 7.4 Claude extraction

Claude Code is installed at a pinned version in the worker image. Authentication uses a dedicated writable configuration mount derived from the administrator's Claude account, so token refresh can occur without exposing the main host configuration tree.

The worker invokes Claude with controls equivalent to:

```text
claude -p
  --restricted
  --tools ""
  --disable-slash-commands
  --no-session-persistence
  --output-format json
  --json-schema /app/schemas/learning-media-bundle-v1.json
```

The system prompt states that the transcript is untrusted quoted data and that instructions contained inside it must never be followed. Claude receives no shell, filesystem, browser, MCP, skill, or network tools. Output must validate against the learning-bundle schema before it can be published.

Each derived artifact records:

- Claude version and selected model.
- Prompt/template version and hash.
- Transcript hash.
- Processing timestamp.
- Schema version.
- Explicit-versus-inferred status for each item.

## 8. Canonical Episode Learning Bundle

Each completed item produces an immutable bundle under a content-addressed S3 prefix. A later correction creates a new bundle version and supersession link; it does not overwrite prior evidence.

```text
manifest.json
transcript.json
summary.md
teachings.json
claims.jsonl
evidence.jsonl
program-routes.json
bundle.json
```

### 8.1 `manifest.json`

Contains stable item and source identity, canonical item/source URLs, creator/publisher, guests when known, publication date, duration, acquisition time, language, rights/access notes, retention rules, raw-media/transcript retention flags, and hashes for every retained artifact.

### 8.2 `transcript.json`

Contains ordered segments with stable segment IDs, start/end timestamps, text, speaker label when available, and segment hash. The full transcript is private by default.

### 8.3 `teachings.json`

Contains structured topic segments and teaching material:

- Topic and subtopic identifiers.
- Thesis and supporting teaching points.
- Frameworks and ordered steps.
- Exercises and suggested actions.
- Stories and examples.
- Exceptions, boundary conditions, and anti-patterns.
- Guest/speaker attribution.
- Source segment references.
- Whether the item is explicit, inferred, interpreted, or unknown.
- Confidence and verification state.

### 8.4 `claims.jsonl` and `evidence.jsonl`

These preserve source-to-claim ancestry and distinguish:

- Evidence that a speaker said something.
- External evidence that the statement is correct.
- Model-derived synthesis.
- Repeated claims that trace to the same original source.

A guest statement is primary evidence that the guest made the statement, not independent verification that the statement is true. Model-generated summaries do not count as additional corroboration.

### 8.5 `program-routes.json`

Contains proposed Agent Factory routes with a reason, confidence, and evidence references. A route is an intake suggestion, not authority to modify a released skill.

Initial routes include:

| Program | Relevant podcast material |
| --- | --- |
| ASP-10 | Sermon/teaching architecture, delivery, illustrations, application, and closing patterns |
| ASP-50 | Attention, hooks, distribution, calls to action, and portability checks |
| ASP-80 | Brand principles and content strategy as external research; never a substitute for the administrator's own approved voice corpus |
| ASP-90 | Health, exercise, behavior, and habit material subject to health evidence and safety escalation rules |
| ASP-100 | Topic decomposition, teaching points, frameworks, exercises, and cross-episode synthesis from Mel Robbins and other teaching podcasts |
| ASP-120 | Book research, claims, stories, examples, frameworks, and possibility-board inputs |
| ASP-130 | Podcast structure, spoken teaching, guest intelligence, interview patterns, retention, post-interview learning, and production insights |

The GaryVee Audio Experience is an initial source for ASP-50 and external research supporting ASP-80. Audio can support analysis of spoken hooks, framing, stories, calls to action, and structure. Claims about visual hooks, edit density, or platform performance require corresponding video/platform evidence.

## 9. Publication Outbox

When a bundle transaction commits, the application creates one delivery record per destination. A Celery publisher processes the records with exponential backoff and bounded retries.

The idempotency identity is derived from:

```text
content-item identity + transcript hash + bundle schema version + destination
```

Every downstream API receives the idempotency identity. Replaying an identical delivery returns the prior result; changing content under the same identity is rejected.

The administrator can view:

- Current state for each destination.
- Last successful delivery.
- Attempt count and next retry.
- Redacted error category.
- Downstream record identifier.
- Manual retry action.

## 10. Lamad Integration

Lamad owns governed learning state, evaluation, datasets, training orchestration, and promotion. It does not replace the source artifact store or inference interface.

The Lamad integration adds a versioned learning-media intake contract. The contract accepts the item type, bundle reference, content hashes, structured examples, provenance, rights/retention classification, and proposed capabilities. It does not embed an uncontrolled duplicate of the raw transcript in a cognitive-outcome envelope.

Every completed item is registered automatically with initial `unassessed` learning state. Structured items may be available for provisional, provenance-visible retrieval immediately, while promotion into approved datasets, durable methods, or released capabilities follows Lamad evaluation policy.

The intake supports:

- Podcast/YouTube source artifact registration.
- Topic and teaching-example registration.
- Cross-item clustering and contradiction detection.
- Verification and applicability state.
- Candidate learning generation.
- Links to Agent Factory skill candidates and releases.
- Human or automated evaluation evidence.
- Feedback and outcome events from Ask & Learn sessions.

Automatic intake means Lamad sees all information. It does not mean every assertion becomes a trusted lesson or training target.

## 11. Ebenezer Integration

Ebenezer owns durable memory, provenance, retention, correction, supersession, and retrieval. The integration adds `podcast` and `youtube` as explicit source systems and enables their governed event projections.

Each bundle produces:

- Artifact references for the transcript, summary, and teaching bundle.
- Evidence records for explicit source claims.
- Lesson-memory candidates for derived frameworks and practices.
- Relationships linking lessons and claims to exact podcast/video segments.
- Supersession records when corrected transcripts or interpretations replace older versions.

The append-only records are canonical memory. Semantic/vector indexes are derived and rebuildable. Retrieval returns authority, verification, freshness, and source reasons rather than unlabelled text.

Podcast and YouTube memory is available to Ask & Learn as soon as it is admitted, but unverified claims remain visibly unverified.

## 12. Agent Factory Integration

Agent Factory builds portable external skills, not an uncontrolled second memory or model-training system. The integration adds `podcast_episode` and `youtube_video` source adapters and uses existing corpus, evidence, claim, source-quality, freshness, challenge, and publication contracts.

For every completed item, the matching adapter:

1. Adds or updates the corpus manifest item.
2. Records rights/access and retention state.
3. Imports timestamped evidence and claim ancestry.
4. Routes proposed material to applicable skill programs.
5. Preserves explicit, inferred, interpreted, and unknown distinctions.
6. Makes candidate material available to skills and agents.
7. Produces versioned candidate skill artifacts when a program's synthesis criteria are met.

The raw transcript remains private in S3 and is accessed through an authorized source adapter. It is not committed automatically to Agent Factory Git. Canonical published artifacts contain lawful derived material, source references, hashes, and provenance.

A podcast or YouTube source cannot silently rewrite a trusted skill. New evidence can trigger a candidate skill revision; Lamad evaluation and the Agent Factory release workflow govern promotion.

## 13. Ask & Learn

The initial learning interface lives at `podcasts.ennovocon.com`.

An administrator question follows this path:

```text
question
-> classify intent and relevant programs
-> retrieve Ebenezer memories and exact transcript segments
-> retrieve Lamad evaluation/promotion state
-> select applicable Agent Factory skills
-> generate a cited lesson and implementation plan with restricted Claude
-> save the learning session
-> collect progress and outcome feedback
-> publish feedback to Ebenezer and Lamad
```

For a question such as “Teach me how to implement a habit discussed by a Mel Robbins guest,” the answer must:

- Identify the source episode/video, speaker/guest/creator, and timestamps.
- Separate the guest's claim from external verification.
- Prefer evaluated or corroborated guidance while retaining useful provisional material with labels.
- Produce concrete steps, boundaries, exceptions, and follow-up checks.
- Apply ASP-90 safety constraints when the topic becomes health-related.
- Allow the administrator to record whether the plan was attempted, completed, useful, ineffective, or harmful.

Learning outcomes become episodic memory in Ebenezer and governed evidence in Lamad. They never retroactively rewrite the original source record.

## 14. User Interface

### 14.1 Podcast Library

- Add Apple Podcasts or RSS URL.
- Enable, pause, refresh, or remove a subscription from active discovery.
- Show canonical source, last check, item counts, and health.

### 14.2 Episode Queue

- Filter by podcast, date, processing state, or delivery state.
- Show current stage, progress, duration, and errors.
- Retry the failed stage without duplicating completed work.

### 14.3 Episode Workspace

- Read synchronized transcript segments.
- Browse summary, topics, frameworks, exercises, actions, examples, and claims.
- Jump from a lesson or claim to its timestamped source.
- See verification and inference labels.
- See Lamad, Ebenezer, and Agent Factory delivery records.

### 14.4 Learning Library

- Search by topic, podcast, speaker/guest, framework, program, verification state, and date.
- Browse cross-item topic clusters and contradictions.
- Distinguish provisional memory from promoted learning.

### 14.5 Ask & Learn

- Ask free-form questions.
- Receive cited lessons and implementation plans.
- Save plans and record progress/outcomes.
- Inspect every source and evaluation label used in an answer.

### 14.6 Operations

- View service and integration health.
- View and retry delivery failures.
- Inspect redacted job logs.
- Run documented backup and restore workflows.

## 15. Domain Model Changes

The podcast-only source model is replaced with a source-neutral model. Versioned migrations add or revise the following concepts:

- `Admin`: the single authorized account, enforced by application and database rules.
- `ContentSource`: RSS podcast, YouTube channel, YouTube playlist, or individual YouTube video with canonical identity and discovery state.
- `ContentItem`: podcast episode or YouTube video with globally stable platform identity and processing state.
- `SourceItem`: many-to-many membership that permits one YouTube video to belong to a channel and playlists without duplicate processing.
- `BackfillRequest`: selector, cursor, bounded batch progress, cancellation, and redacted failure state.
- `ItemArtifact`: immutable S3 URI, hash, media type, transcript origin, schema version, and supersession link.
- `TranscriptSegment`: stable timestamped segment identity and source hash, or an indexed projection of the canonical S3 object.
- `TeachingItem`: structured derived material with source references and epistemic state.
- `LearningBundle`: immutable bundle identity, version, hashes, and completion state.
- `PublicationDelivery`: destination, idempotency identity, state, attempts, downstream ID, and redacted failure.
- `LearningSession`: administrator question, retrieved record references, generated response artifact, and prompt/model lineage.
- `LearningOutcome`: progress, result, rating, notes, timestamp, and publication state.

Large transcript bodies remain in S3. PostgreSQL stores operational metadata, indexes, small structured records, and downstream identifiers.

## 16. Reliability and Failure Handling

- Each stage commits durable state before scheduling the next stage.
- Celery tasks are idempotent and safe to retry after worker termination.
- External calls have timeouts, bounded response sizes, retry classification, and exponential backoff.
- A failed downstream delivery never marks the source transcript invalid.
- A malformed Claude result is rejected before bundle creation and may be retried with the same prompt/schema version.
- Repeated permanent failures surface in the administrator UI and stop automatic retries.
- A corrected transcript produces a new version and supersession record.
- Container recreation preserves PostgreSQL state and all canonical S3 artifacts.
- Redis loss may delay jobs but cannot erase canonical application or artifact state.

## 17. Health, Observability, and Operations

The application exposes loopback-accessible liveness and readiness endpoints. Readiness checks cover:

- PostgreSQL migration state.
- Redis connectivity.
- Celery worker heartbeat.
- Celery Beat schedule presence.
- S3 bucket access and configured region.
- Amazon Transcribe client initialization.
- YouTube Data API public-access validation.
- Pinned `yt-dlp` binary/version availability.
- Claude binary availability and authentication status.
- Lamad, Ebenezer, and Agent Factory adapter health.

Logs are structured and correlate source, item, job, bundle, and delivery IDs. Metrics include queue age, stage duration, discovery quota/throttling failures, failures by class, delivery lag, Claude extraction failures, and downstream success rates.

Operations documentation includes:

- Initial installation and configuration.
- Administrator creation and password reset.
- Starting, stopping, upgrading, and rolling back Compose.
- Cloudflare Tunnel target configuration.
- PostgreSQL backup and restore.
- Credential rotation.
- Reprocessing and republishing an item safely.
- Running, monitoring, cancelling, and resuming historical backfills.
- Diagnosing each health-check failure.

## 18. Testing Strategy

### 18.1 Unit tests

- Apple URL and RSS resolution.
- YouTube video/channel/playlist URL classification and canonical resolution.
- YouTube API pagination, newest-25 selection, and scheduled discovery.
- Date-range, item-count, and explicit-URL backfill selection.
- Podcast and YouTube item identity, source membership, and cross-source deduplication.
- Creator-caption, automatic-caption, and Amazon Transcribe fallback priority.
- Transcript normalization and content hashing.
- Learning-bundle schema validation.
- Explicit/inferred epistemic classification.
- Program routing.
- Idempotency identities and retry decisions.
- Authentication, single-admin enforcement, CSRF, and authorization.
- Secret and signed-URL redaction.

### 18.2 Security tests

- Public registration is unavailable.
- Transcript prompt injection cannot obtain tools or host access.
- Restricted or signed source URLs are not published.
- Raw transcripts are not written to public Agent Factory artifacts.
- Cross-user access is impossible even if a second user row is inserted manually.
- Logs do not contain credentials or transcript bodies.

### 18.3 Integration and contract tests

- PostgreSQL and Redis startup/health behavior.
- Amazon Transcribe and S3 adapters with recorded or stubbed responses.
- YouTube Data API and pinned `yt-dlp` adapters with deterministic fixtures.
- An opt-in live public-YouTube smoke test that is excluded from the normal suite.
- Claude JSON contract with a deterministic fixture and a live opt-in smoke test.
- Lamad learning-media intake contract and idempotent replay.
- Ebenezer podcast/YouTube memory admission, retrieval, correction, and supersession.
- Agent Factory `podcast_episode` and `youtube_video` corpus/evidence import and routing.
- Partial downstream outage and independent recovery.

### 18.4 End-to-end tests

- Add a feed and a YouTube channel, discover items, acquire transcripts through captions and Amazon Transcribe fallback, extract bundles, publish to all destinations, retrieve them, and generate a cited lesson.
- Recreate all application containers and verify the item, bundle, delivery state, and learning session remain available.
- Record a lesson outcome and verify its Lamad and Ebenezer lineage.

## 19. Delivery Phases

### Phase 1: Docker foundation

- Repair the existing processing pipeline and dependencies.
- Replace ad hoc table creation with versioned migrations.
- Remove Heroku, Stripe, Mailgun, registration, and obsolete OpenAI behavior.
- Add single-admin provisioning.
- Harden Compose and bind only `127.0.0.1:8005`.
- Add public YouTube video, channel, and playlist discovery through the YouTube Data API.
- Add creator-caption, automatic-caption, and `yt-dlp` audio-fallback ingestion.
- Add newest-25 intake and controlled historical backfills for podcast and YouTube sources.
- Validate AWS, S3, YouTube, Redis, PostgreSQL, and basic content-item processing.

**Exit condition:** The admin can log in; add a podcast feed, YouTube video, channel, or playlist; import the newest 25 items; run a controlled historical backfill; process an item with the correct transcript-source priority; and retain state across container recreation.

### Phase 2: Media learning workspace

- Add restricted Claude extraction.
- Define and validate the learning-bundle schema.
- Store versioned bundles in S3.
- Add item learning views, knowledge library, and publication outbox.

**Exit condition:** Every completed podcast episode or YouTube video has a valid, browsable learning bundle and durable delivery jobs.

### Phase 3: Learning-system adapters

- Add the Lamad learning-media intake contract for podcast episodes and YouTube videos.
- Add the Ebenezer podcast/YouTube source projections and retrieval paths.
- Add the Agent Factory `podcast_episode` and `youtube_video` adapters and program routing.
- Enable automatic, idempotent publishing to all three.

**Exit condition:** A completed podcast episode or YouTube video is visible with intact lineage in Lamad, Ebenezer, and Agent Factory, and replays create no duplicates.

### Phase 4: Ask & Learn feedback loop

- Add governed multi-system retrieval.
- Add cited lesson and implementation-plan generation.
- Add saved sessions, progress, and outcomes.
- Publish feedback to Lamad and Ebenezer.

**Exit condition:** The admin can ask a cross-podcast question, receive a source-linked lesson, record an outcome, and inspect the resulting learning and memory lineage.

Each phase receives its own executable implementation plan and verification gate. Cross-repository changes in Phase 3 are planned and reviewed in their owning repositories.

## 20. Acceptance Criteria

The complete design is delivered when:

1. `https://podcasts.ennovocon.com` serves the application through the administrator-managed Cloudflare Tunnel.
2. Only the single provisioned administrator can log in; no registration, payment, or email-delivery workflow remains.
3. Docker Compose starts cleanly without conflicting host PostgreSQL or Redis ports.
4. A podcast can be added from an Apple Podcasts or RSS URL, and a public YouTube video, channel, or playlist can be added and checked periodically.
5. A new channel or playlist imports the newest 25 videos, automatically discovers later uploads, and supports date-range, count, and explicit-URL historical backfill.
6. YouTube transcript acquisition uses creator captions, then automatic captions, then audio extraction plus Amazon Transcribe, and records the selected origin.
7. A podcast episode or YouTube video can be converted into a valid learning bundle with restricted Claude CLI extraction.
8. Canonical artifacts are private and durable in `podcast-transcriptions-web-2025`.
9. Every completed item creates and completes idempotent deliveries to Lamad, Ebenezer, and Agent Factory.
10. The UI exposes transcript, lesson, provenance, verification, inference, and delivery state.
11. Ask & Learn returns actionable answers with exact episode/video segment citations and visible trust state.
12. Administrator feedback becomes traceable Lamad learning evidence and Ebenezer memory without altering the original source.
13. Tests and live health checks pass, and durable state survives container recreation.

## 21. Key Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Podcast claim is mistaken for verified fact | Preserve speaker assertion, inference, and external verification as separate fields |
| Model-generated summaries inflate corroboration | Keep model output as derived ancestry, never an independent source |
| Copyrighted transcripts are redistributed | Keep raw transcripts private in S3; publish references and permitted derivatives only |
| Prompt injection inside a transcript | Restricted Claude mode, no tools, no skills, no sessions, explicit untrusted-data prompt, schema validation |
| One downstream system is unavailable | Transactional outbox, independent delivery states, bounded retries |
| Duplicate items or deliveries | Stable platform identities/content hashes and destination-scoped idempotency identities |
| The same YouTube video appears in several sources | Global YouTube video identity plus `SourceItem` membership; process once |
| YouTube quota or throttling interrupts discovery | Official API pagination, bounded polling, exponential retry, and visible per-source state |
| YouTube extraction behavior changes | Pinned `yt-dlp`, adapter contract tests, opt-in live smoke test, deliberate upgrade procedure |
| Public video availability is mistaken for redistribution permission | Keep captions, audio, and transcripts private; publish citations and permitted derivatives only |
| Low-quality advice becomes a trusted skill | Immediate provisional retrieval with labels; governed evaluation and versioned promotion |
| Health advice causes harm | ASP-90 evidence hierarchy, safety escalation, explicit limits, and non-medical framing |
| Claude OAuth material leaks into an image or repo | Dedicated runtime mount, secret exclusions, no credential copying into the image |
| Memory index loss causes amnesia | Append-only canonical records and S3 artifacts; vector/search indexes remain rebuildable |

