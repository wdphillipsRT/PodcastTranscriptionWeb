# Local Whisper Primary Transcription Design

**Date:** 2026-10-19

**Status:** Approved

**Parent specification:** `2026-10-19-docker-podcast-learning-platform-design.md`

## 1. Purpose

Podcast and YouTube audio that lacks usable creator or automatic captions will
be transcribed locally with Whisper instead of being sent directly to Amazon
Transcribe. The deployment will use the English-optimized `medium.en` model
through `faster-whisper` on the current CPU-only Docker host. Amazon Transcribe
remains an automatic fallback after bounded retryable local failures.

This addendum changes only the audio-transcription portion of the ingestion
pipeline. Discovery, caption preference, private S3 artifact storage,
commercial classification, learning projections, and future delivery to Lamad,
Ebenezer, and Agent Factory continue to consume the canonical transcript
contract.

## 2. Decisions

- Local Whisper is the primary audio transcription engine.
- The runtime is `faster-whisper` with `medium.en`, CPU execution, and INT8
  computation.
- Whisper runs in a dedicated Docker service at concurrency one.
- Creator-provided YouTube captions remain first choice; YouTube automatic
  captions remain second choice. Whisper is primary only when audio
  transcription is required.
- Amazon Transcribe runs automatically after three retryable Whisper failures.
- Invalid or unsafe media does not trigger paid fallback work.
- Existing completed Amazon transcripts are preserved. Whisper applies to new
  items and items explicitly retried after a processing failure.
- Every provider artifact and canonical transcript records immutable input and
  provider lineage.

## 3. Goals

- Keep routine podcast and YouTube audio transcription on this machine.
- Preserve timestamps suitable for citations, commercial review, and learning
  extraction.
- Isolate CPU-heavy inference from the web, discovery, download, classifier,
  and AWS fallback workers.
- Make every retry and fallback idempotent and recoverable after process or host
  interruption.
- Retain Amazon Transcribe as a bounded reliability path rather than a parallel
  or default provider.
- Make the active provider, model, version, compute type, and fallback state
  visible without exposing private storage identities or provider payloads.
- Permit a later move to a GPU host without changing upstream acquisition or
  downstream transcript consumers.

## 4. Non-goals

- Re-transcribing completed Amazon, creator-caption, or automatic-caption
  transcripts.
- Running more than one local Whisper inference concurrently on the initial
  host.
- Supporting non-English transcription in the initial release.
- Speaker diarization, voice identification, or speaker-name inference.
- Using an external hosted Whisper API.
- Replacing creator or automatic YouTube captions with Whisper when the caption
  artifact remains valid.
- Sending corrupt, empty, oversized, unsupported, or unsafe media to Amazon as
  a way to bypass local validation.

## 5. Provider Order and Data Flow

Podcast episodes follow this path:

```text
discover -> download audio -> local Whisper -> canonical transcript
         -> commercial classification -> eligible learning projection
```

YouTube videos follow this path:

```text
discover -> creator captions
             | unavailable
             v
          automatic captions
             | unavailable
             v
          extract audio -> local Whisper -> canonical transcript
```

When local Whisper exhausts its retryable attempt budget, both platforms follow
this bounded fallback path:

```text
local Whisper exhausted -> Amazon Transcribe -> canonical transcript
```

Audio acquisition continues to validate and store a content-addressed private
S3 artifact before transcription. The Whisper worker downloads that verified
artifact into its bounded temporary filesystem, verifies the audio SHA-256,
runs inference, validates the complete result, writes a content-addressed raw
provider artifact to S3, and only then commits its durable stage transition.
Canonical normalization reads the immutable provider artifact and produces the
same private text and segment artifacts used by the existing classifier.

No transcript body, source URL, S3 key, media locator, model output, or
credential is sent through Celery. Task messages contain integer item IDs and
deterministic task identities only.

## 6. Components

### 6.1 Whisper adapter

A provider-neutral adapter wraps `faster-whisper`. Its public operation accepts
a verified local audio path plus bounded public metadata and returns a typed,
timestamped result. It owns:

- loading the pinned `medium.en` model from the local cache;
- CPU INT8 inference;
- fixed English language selection rather than language auto-detection;
- conservative silence filtering;
- segment and word timestamps;
- bounded segment, word, text, duration, and serialized-result sizes;
- conversion of library exceptions into closed safe categories; and
- deterministic serialization of the provider result.

The adapter never reads application database rows, writes S3 objects, dispatches
tasks, or decides whether Amazon fallback is allowed.

### 6.2 Whisper artifact normalizer

A dedicated normalizer converts the validated provider JSON into
`media-transcript-segments/v1`. It applies the existing canonical limits and
stable segment-ID construction. Its lineage uses:

- origin `local_whisper`;
- provider `faster-whisper`;
- the pinned `faster-whisper` version;
- the pinned model name and immutable model revision;
- compute type `int8`;
- language `en`; and
- the raw provider-artifact SHA-256.

Model identity and compute type are part of the provider artifact and durable
database provenance. The canonical `provider_version` is the bounded composite
identity
`1.2.1+medium.en.a29b04bd15381511a9af671baec01072039215e3.int8`,
so the canonical transcript hash also changes when any approved inference
component changes. The same values participate in the deterministic Whisper
job identity so a model change cannot silently reuse output from another model.

### 6.3 Processing pipeline stages

The existing provider-neutral `transcribe` stage becomes the local Whisper
stage for new and retried audio. A new `amazon_transcribe` stage represents the
fallback provider explicitly. Historical completed `transcribe` jobs remain
valid because completed-item verification uses the recorded transcript origin
and artifact lineage, not an assumption that the stage always means Whisper.

Task routing is:

- `transcribe` -> `transcribe_item_audio` on the `whisper` queue;
- `amazon_transcribe` -> a distinct fallback task on the existing
  `transcription` queue; and
- caption acquisition, audio acquisition, and normalization remain on their
  existing queues.

The stage planner follows recorded artifact lineage. A valid completed Amazon
transcript is reused. A new or retried item with valid audio but no valid
provider transcript enters `transcribe`. A valid Whisper provider result whose
canonical artifact needs repair is normalized without rerunning inference.

### 6.4 Dedicated Docker service

Compose adds a `whisper` worker that consumes only the `whisper` queue with
Celery concurrency one and prefetch one. It does not consume discovery,
download, classification, default, or Amazon fallback work.

The worker receives a 10-CPU container limit, an 8 GiB memory limit, no more
than 10 inference threads, and a 1 GiB temporary filesystem. These limits leave
capacity for the web, database, Redis, downloader, beat scheduler, and
classifier on the current 16-CPU, 27 GiB host. The operations guide records
these values and the safe procedure for changing them after measurement.

A named Docker volume stores the model snapshot. Deployment resolves
`medium.en` to an immutable model revision and persists that revision in the
dependency/configuration lock; the runtime never follows a mutable model
branch. Startup fails closed when the configured snapshot is missing, differs
from the pinned revision, or cannot load as CPU INT8.

Temporary audio and intermediate output use a bounded `tmpfs`. Cleanup occurs
in `finally` paths after success, retry, fallback, timeout, and worker failure.
No model cache or media directory is exposed through the web container.

## 7. Durable Provenance and Schema

The content-item transcript origin enumeration adds `local_whisper`. The
content item retains the existing provider and artifact fields and adds bounded
fields for:

- transcription provider version;
- transcription model name;
- immutable model revision;
- compute type; and
- safe fallback reason, when Amazon produced the active transcript after local
  exhaustion.

The new `amazon_transcribe` processing stage is added to the database stage
constraint. Migrations preserve all existing item states, job rows, active
classification pointers, and artifacts. No existing Amazon row is rewritten to
Whisper provenance.

The raw Whisper provider artifact uses schema `local-whisper-result/v1`. It is
deterministic JSON containing only the schema identifier, input audio SHA-256,
provider/version/model identity,
language, duration, validated timestamped segments and words, and bounded
inference metadata needed for audit. It excludes local paths, S3 keys, source
URLs, credentials, hostnames, exception text, and arbitrary library objects.

Provider artifacts use the existing private item namespace and immutable
content-addressed naming. Canonical text and segment objects retain their
existing key and hash rules. Completion is valid only when database lineage,
provider JSON, canonical JSON, text bytes, hashes, model identity, and input
audio identity all agree.

## 8. Idempotency and Recovery

The Whisper job identity is derived from:

- item ID;
- verified audio SHA-256;
- provider and pinned provider version;
- `medium.en` plus immutable model revision;
- `int8` compute type; and
- normalizer schema/version.

The identity contains no secret or private locator. Redelivery first verifies
durable provider and canonical artifacts. Valid artifacts are reused; incomplete
or mismatched tuples are repaired from the earliest invalid stage. A process
crash before a durable commit causes the same task identity to be redelivered.
A process crash after the provider artifact commit reuses that immutable object
instead of rerunning inference.

Manual item retry continues to preserve valid completed stages and artifacts.
It routes failed audio transcription back through local Whisper. It never
replaces a valid completed Amazon transcript merely because the administrator
opened or viewed the item.

## 9. Failures, Retries, and Amazon Fallback

Whisper errors cross persistence boundaries only as closed categories.

Retryable local categories include:

- temporary S3 read/write failure;
- worker soft timeout;
- temporary model/runtime failure;
- temporary resource exhaustion; and
- incomplete but otherwise valid provider execution.

Each local delivery increments the durable Whisper stage attempt count. The
same deterministic task identity retries with bounded exponential delay and
jitter. After the third failed local attempt, the Whisper job becomes failed
with safe category `whisper_exhausted`, the item remains `transcribing`, and an
`amazon_transcribe` outbox is committed in the same transaction. Only after
that commit may the Amazon task be published.

Permanent local categories include:

- corrupt or empty audio;
- unsupported media after verified decoding;
- audio or decoded duration beyond configured limits;
- invalid model/configuration identity;
- malformed, oversized, non-monotonic, or empty Whisper output; and
- artifact lineage or hash mismatch.

Permanent input and integrity failures stop for administrator review without
invoking Amazon. Model installation/configuration errors fail the Whisper
health check and pause local consumption rather than creating paid fallback
work for every queued episode.

Amazon fallback retains its current bounded retry behavior. The completed item
records origin `amazon_transcribe` and a safe fallback reason identifying local
retry exhaustion. It does not retain raw exception text. If Amazon also fails,
the existing deferred/failed item behavior and manual retry action apply.

## 10. Configuration and Dependency Policy

The production dependency is pinned to an exact `faster-whisper` release and
its transitive lock state. The initial approved provider release is
`faster-whisper==1.2.1`; upgrades require a new model smoke test and transcript
identity review.

Required configuration includes:

- `TRANSCRIPTION_PRIMARY=faster_whisper`;
- `FASTER_WHISPER_EXPECTED_VERSION=1.2.1`;
- `WHISPER_MODEL=medium.en`;
- `WHISPER_MODEL_REPOSITORY=Systran/faster-whisper-medium.en`;
- `WHISPER_MODEL_REVISION=a29b04bd15381511a9af671baec01072039215e3`;
- `WHISPER_COMPUTE_TYPE=int8`;
- `WHISPER_CPU_THREADS=10`;
- `WHISPER_MAX_ATTEMPTS=3`;
- `WHISPER_SOFT_TIME_LIMIT_SECONDS=21600`;
- `WHISPER_HARD_TIME_LIMIT_SECONDS=21900`; and
- the existing bounded audio and transcript limits plus the 1 GiB temporary
  filesystem limit.

Only the immutable revision value comes from the checked-in model lock; runtime
configuration must equal it. `TRANSCRIPTION_PRIMARY=amazon_transcribe` is the
documented operational rollback switch. Changing that switch affects only new
or explicitly retried work and never rewrites completed transcripts.

## 11. Security and Privacy

- Whisper inference is local; transcript text is not sent to a new external
  provider.
- The model snapshot is fetched only during an explicit deployment preparation
  step and verified against the model lock.
- Runtime containers keep dropped capabilities, no-new-privileges, non-root
  execution, bounded temporary storage, and read-only model access.
- The Whisper worker receives only the S3 permissions already needed for owned
  private item artifacts. It receives no Claude, YouTube, admin, or Cloudflare
  credentials.
- Logs and metrics contain item/source IDs, stage, duration, attempt, provider,
  model identity, and safe category only. They exclude transcript text, audio,
  titles, URLs, S3 keys, model output, and credentials.
- Provider JSON is parsed as data. No pickle, arbitrary object deserialization,
  shell interpolation, or model-generated command is allowed.

## 12. UI and Operations

Item and source views distinguish:

- `Whisper · medium.en` for local completion;
- `Amazon Transcribe · Whisper fallback` for fallback completion;
- `Whisper queued`, `Whisper running`, and safe retry/failure states; and
- caption origins unchanged.

No UI action starts a duplicate transcription for a completed item. The
existing retry action remains available only for failed, deferred, or
unavailable processing states.

Readiness reports only component status. The detailed local operator check
verifies model revision, CPU INT8 loading, model-cache readability, bounded
temporary-space availability, queue connectivity, S3 access, and a short
compiled-in inference fixture. Queue depth, per-item stage duration, retry
category, fallback count, and aggregate real-time factor are recorded as safe
operational measurements.

The operations guide documents model preparation, health checks, canary
execution, queue inspection, rollback to Amazon primary, cache recovery, and
resource tuning. It explicitly prohibits deleting processing rows or private
artifacts to force a retry.

## 13. Testing Strategy

Unit and contract tests cover:

- deterministic Whisper provider serialization;
- timestamp, text, language, duration, and size validation;
- `local_whisper` canonical normalization and lineage;
- provider/model/version/revision/compute provenance;
- content-addressed provider and canonical artifacts;
- stage planning for podcasts and YouTube caption fallback;
- preservation of valid completed Amazon and caption transcripts;
- Whisper retry classification and the three-attempt boundary;
- atomic Amazon fallback outbox creation and replay;
- permanent failures that must not invoke Amazon;
- worker termination, duplicate delivery, and partial-artifact repair;
- owner-scoped, safe UI status;
- migrations from the current production schema; and
- Compose isolation, queue ownership, non-root execution, limits, and model
  volume permissions.

Most tests use a typed fake adapter and deterministic provider fixtures. A
separate opt-in live smoke test loads the real pinned `medium.en` snapshot and
transcribes a short non-private English fixture. It asserts bounded runtime,
non-empty monotonic timestamps, expected phrases, deterministic serialization,
and exact provider identity. It does not use production media or AWS.

The full existing ingestion, transcript, classification, learning-projection,
security, repository-hygiene, and Docker contract suites must remain green.

## 14. Rollout

1. Add and verify schema, adapter, normalizer, routing, tests, Compose service,
   model lock, and operations documentation with automatic Whisper routing
   disabled.
2. Populate the named model volume from the pinned model lock and run the real
   local smoke test.
3. Deploy the Whisper worker with queue consumption paused; verify web,
   discovery, media, classifier, database, Redis, S3, and Amazon health remain
   unchanged.
4. Enable local routing for exactly one new or explicitly retried production
   item. Verify provider artifact, canonical lineage, classification handoff,
   cleanup, runtime, and UI provenance.
5. Enable `TRANSCRIPTION_PRIMARY=faster_whisper` for normal new and retried
   work at concurrency one.
6. Observe CPU, memory, queue depth, real-time factor, retry categories, and
   fallback count before changing resource limits or concurrency.

At no point does rollout enqueue existing completed items. Rollback changes the
primary-provider switch for future work, stops the Whisper queue consumer, and
leaves every completed Whisper transcript and classification intact.

## 15. Acceptance Criteria

- New podcast audio without a transcript is completed by local
  `faster-whisper` `medium.en` on CPU INT8.
- YouTube creator and automatic captions still outrank audio transcription.
- New YouTube audio fallback uses local Whisper before Amazon.
- Existing completed Amazon and caption transcripts remain unchanged.
- A valid manual retry of a failed audio-transcription item returns to Whisper.
- Three retryable local failures produce one durable Amazon fallback handoff;
  no earlier attempt invokes Amazon.
- Permanent input, integrity, or configuration failures do not invoke Amazon.
- Every completed transcript passes exact audio, provider, model, artifact, and
  canonical-lineage validation.
- Whisper work cannot starve web, discovery, download, classification, or
  Amazon fallback queues.
- No transcript text, private locator, S3 key, provider payload, or credential
  appears in Celery messages, logs, metrics, health output, or public errors.
- The real pinned-model smoke test, full automated suite, Docker health checks,
  and one-item production canary pass before normal routing is enabled.

## 16. Primary References

- OpenAI Whisper model descriptions and English-model guidance:
  <https://github.com/openai/whisper>
- `faster-whisper` runtime, CPU INT8 usage, and benchmarks:
  <https://github.com/SYSTRAN/faster-whisper>
- Pinned `faster-whisper` 1.2.1 release:
  <https://github.com/SYSTRAN/faster-whisper/releases/tag/v1.2.1>
- Pinned `medium.en` model repository:
  <https://huggingface.co/Systran/faster-whisper-medium.en/tree/a29b04bd15381511a9af671baec01072039215e3>

