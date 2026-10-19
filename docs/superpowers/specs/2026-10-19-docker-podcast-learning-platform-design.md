# Docker Podcast Learning Platform Design

**Status:** Approved  
**Date:** 2026-10-19  
**Primary URL:** `https://podcasts.ennovocon.com`  
**Installation path:** `/home/wdphillips/PodcastTranscriptionWeb`  
**Canonical artifact bucket:** `s3://podcast-transcriptions-web-2025`

## 1. Goal

Turn PodcastTranscriptionWeb into an admin-only podcast learning workspace that runs on this machine with Docker Compose. The application will let the administrator add podcasts, discover and transcribe episodes, extract lessons with the locally authenticated Claude CLI, search and learn from the resulting knowledge, and automatically publish every completed episode to Lamad, Ebenezer, and Agent Factory.

The system is not merely a transcript archive. It must support a closed learning loop:

```text
podcast source
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
- Every completed episode is automatically submitted to Lamad, Ebenezer, and Agent Factory.
- Automatic submission does not silently convert a speaker's claim or a model inference into verified truth.
- Agent Factory may automatically ingest candidate evidence, but trusted skill or agent releases remain versioned and governed.

## 3. Scope

### 3.1 In scope

- Production-ready Docker Compose deployment.
- PostgreSQL, Redis, web, worker, and scheduler services.
- Single-administrator provisioning and login.
- Apple Podcasts URL and RSS-feed intake.
- Periodic episode discovery.
- Amazon Transcribe integration using the designated S3 bucket.
- Timestamped transcript normalization.
- Restricted Claude CLI summarization and teaching extraction.
- Immutable, versioned episode learning bundles.
- A browsable podcast, episode, transcript, lesson, and knowledge interface.
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
          ingestion / transcription /
          extraction / publication
                      |
       +--------------+-------------------+
       |              |                   |
 Amazon Transcribe  Claude CLI       downstream adapters
       |              |             /        |          \
       +------ S3 learning bundle -+      Ebenezer    Agent Factory
                                      Lamad
```

### 5.1 Compose services

| Service | Responsibility | Host exposure |
| --- | --- | --- |
| `web` | Flask UI/API under Gunicorn | `127.0.0.1:8005` |
| `worker` | Celery ingestion, transcription, extraction, and publication queues | None |
| `beat` | Scheduled RSS discovery | None |
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
- Server-side authorization checks on every podcast, episode, artifact, and lesson route.

Cloudflare supplies public TLS. Cloudflare Access may be added as defense in depth, but application login remains mandatory.

## 7. Episode Processing Workflow

### 7.1 Discovery

The administrator adds either an Apple Podcasts URL or an RSS URL. The application resolves and stores the canonical feed URL. Celery Beat checks enabled feeds on a configurable interval and creates idempotent episode records using the feed identity and episode GUID.

### 7.2 Processing states

An episode uses explicit states:

```text
discovered
-> downloading
-> transcribing
-> extracting
-> bundling
-> completed
```

Failures retain the last successful stage and a redacted error. Retrying resumes from the last valid immutable artifact rather than repeating all prior work.

Downstream delivery is separate from episode completion:

```text
pending -> delivering -> delivered
                      -> retryable_failure
                      -> permanent_failure
```

A Lamad outage therefore does not erase a valid transcript or prevent Ebenezer and Agent Factory delivery.

### 7.3 Audio and transcription

Audio is downloaded to a bounded temporary file, validated, and stored in S3. Amazon Transcribe reads the S3 object and writes its result to the same bucket. The worker normalizes the provider result into stable timestamped segments and calculates content hashes.

`AWS_REGION` is required configuration. Startup validation checks that the configured region and account can read/write the exact bucket and invoke Amazon Transcribe. Configuration fails closed rather than silently using a different bucket.

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
  --json-schema /app/schemas/podcast-learning-bundle-v1.json
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

Each completed episode produces an immutable bundle under a content-addressed S3 prefix. A later correction creates a new bundle version and supersession link; it does not overwrite prior evidence.

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

Contains stable episode and show identity, feed and episode URLs, creator/publisher, guests when known, publication date, duration, acquisition time, language, rights/access notes, retention rules, raw-media/transcript retention flags, and hashes for every retained artifact.

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
episode GUID + transcript hash + bundle schema version + destination
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

The Lamad integration adds a versioned podcast-learning intake contract. The contract accepts the bundle reference, content hashes, structured examples, provenance, rights/retention classification, and proposed capabilities. It does not embed an uncontrolled duplicate of the raw transcript in a cognitive-outcome envelope.

Every completed episode is registered automatically with initial `unassessed` learning state. Structured items may be available for provisional, provenance-visible retrieval immediately, while promotion into approved datasets, durable methods, or released capabilities follows Lamad evaluation policy.

The intake supports:

- Podcast/source artifact registration.
- Topic and teaching-example registration.
- Cross-episode clustering and contradiction detection.
- Verification and applicability state.
- Candidate learning generation.
- Links to Agent Factory skill candidates and releases.
- Human or automated evaluation evidence.
- Feedback and outcome events from Ask & Learn sessions.

Automatic intake means Lamad sees all information. It does not mean every assertion becomes a trusted lesson or training target.

## 11. Ebenezer Integration

Ebenezer owns durable memory, provenance, retention, correction, supersession, and retrieval. The integration adds `podcast` as an explicit source system and enables its governed event projection.

Each bundle produces:

- Artifact references for the transcript, summary, and teaching bundle.
- Evidence records for explicit source claims.
- Lesson-memory candidates for derived frameworks and practices.
- Relationships linking lessons and claims to exact episode segments.
- Supersession records when corrected transcripts or interpretations replace older versions.

The append-only records are canonical memory. Semantic/vector indexes are derived and rebuildable. Retrieval returns authority, verification, freshness, and source reasons rather than unlabelled text.

Podcast memory is available to Ask & Learn as soon as it is admitted, but unverified claims remain visibly unverified.

## 12. Agent Factory Integration

Agent Factory builds portable external skills, not an uncontrolled second memory or model-training system. The integration adds a `podcast_episode` source adapter and uses existing corpus, evidence, claim, source-quality, freshness, challenge, and publication contracts.

For every completed episode, the adapter:

1. Adds or updates the corpus manifest item.
2. Records rights/access and retention state.
3. Imports timestamped evidence and claim ancestry.
4. Routes proposed material to applicable skill programs.
5. Preserves explicit, inferred, interpreted, and unknown distinctions.
6. Makes candidate material available to skills and agents.
7. Produces versioned candidate skill artifacts when a program's synthesis criteria are met.

The raw transcript remains private in S3 and is accessed through an authorized source adapter. It is not committed automatically to Agent Factory Git. Canonical published artifacts contain lawful derived material, source references, hashes, and provenance.

A podcast cannot silently rewrite a trusted skill. New evidence can trigger a candidate skill revision; Lamad evaluation and the Agent Factory release workflow govern promotion.

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

- Identify the source episode, speaker/guest, and timestamps.
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
- Show canonical feed, last check, episode counts, and health.

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
- Browse cross-episode topic clusters and contradictions.
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

The existing user, podcast, episode, and processing-job models remain where suitable. Versioned migrations add or revise the following concepts:

- `Admin`: the single authorized account, enforced by application and database rules.
- `EpisodeArtifact`: immutable S3 URI, hash, media type, schema version, and supersession link.
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
- Claude binary availability and authentication status.
- Lamad, Ebenezer, and Agent Factory adapter health.

Logs are structured and correlate podcast, episode, job, bundle, and delivery IDs. Metrics include queue age, stage duration, failures by class, delivery lag, Claude extraction failures, and downstream success rates.

Operations documentation includes:

- Initial installation and configuration.
- Administrator creation and password reset.
- Starting, stopping, upgrading, and rolling back Compose.
- Cloudflare Tunnel target configuration.
- PostgreSQL backup and restore.
- Credential rotation.
- Reprocessing and republishing an episode safely.
- Diagnosing each health-check failure.

## 18. Testing Strategy

### 18.1 Unit tests

- Apple URL and RSS resolution.
- Episode identity and deduplication.
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
- Claude JSON contract with a deterministic fixture and a live opt-in smoke test.
- Lamad podcast-intake contract and idempotent replay.
- Ebenezer podcast memory admission, retrieval, correction, and supersession.
- Agent Factory `podcast_episode` corpus/evidence import and routing.
- Partial downstream outage and independent recovery.

### 18.4 End-to-end tests

- Add a feed, discover one episode, transcribe it, extract a bundle, publish to all destinations, retrieve it, and generate a cited lesson.
- Recreate all application containers and verify the episode, bundle, delivery state, and learning session remain available.
- Record a lesson outcome and verify its Lamad and Ebenezer lineage.

## 19. Delivery Phases

### Phase 1: Docker foundation

- Repair the existing processing pipeline and dependencies.
- Replace ad hoc table creation with versioned migrations.
- Remove Heroku, Stripe, Mailgun, registration, and obsolete OpenAI behavior.
- Add single-admin provisioning.
- Harden Compose and bind only `127.0.0.1:8005`.
- Validate AWS, S3, Redis, PostgreSQL, and basic episode processing.

**Exit condition:** The admin can log in, add a feed, process an episode, and retain state across container recreation.

### Phase 2: Podcast learning workspace

- Add restricted Claude extraction.
- Define and validate the learning-bundle schema.
- Store versioned bundles in S3.
- Add episode learning views, knowledge library, and publication outbox.

**Exit condition:** Every completed episode has a valid, browsable learning bundle and durable delivery jobs.

### Phase 3: Learning-system adapters

- Add the Lamad podcast-learning intake contract.
- Add the Ebenezer podcast source/projection and retrieval path.
- Add the Agent Factory `podcast_episode` adapter and program routing.
- Enable automatic, idempotent publishing to all three.

**Exit condition:** A completed episode is visible with intact lineage in Lamad, Ebenezer, and Agent Factory, and replays create no duplicates.

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
4. A podcast can be added from an Apple Podcasts or RSS URL and checked periodically.
5. An episode can be downloaded, transcribed with Amazon Transcribe, and converted into a valid learning bundle with restricted Claude CLI extraction.
6. Canonical artifacts are private and durable in `podcast-transcriptions-web-2025`.
7. Every completed episode creates and completes idempotent deliveries to Lamad, Ebenezer, and Agent Factory.
8. The UI exposes transcript, lesson, provenance, verification, inference, and delivery state.
9. Ask & Learn returns actionable answers with exact episode/segment citations and visible trust state.
10. Administrator feedback becomes traceable Lamad learning evidence and Ebenezer memory without altering the original source.
11. Tests and live health checks pass, and durable state survives container recreation.

## 21. Key Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Podcast claim is mistaken for verified fact | Preserve speaker assertion, inference, and external verification as separate fields |
| Model-generated summaries inflate corroboration | Keep model output as derived ancestry, never an independent source |
| Copyrighted transcripts are redistributed | Keep raw transcripts private in S3; publish references and permitted derivatives only |
| Prompt injection inside a transcript | Restricted Claude mode, no tools, no skills, no sessions, explicit untrusted-data prompt, schema validation |
| One downstream system is unavailable | Transactional outbox, independent delivery states, bounded retries |
| Duplicate episodes or deliveries | Stable GUID/content hashes and destination-scoped idempotency identities |
| Low-quality advice becomes a trusted skill | Immediate provisional retrieval with labels; governed evaluation and versioned promotion |
| Health advice causes harm | ASP-90 evidence hierarchy, safety escalation, explicit limits, and non-medical framing |
| Claude OAuth material leaks into an image or repo | Dedicated runtime mount, secret exclusions, no credential copying into the image |
| Memory index loss causes amnesia | Append-only canonical records and S3 artifacts; vector/search indexes remain rebuildable |

