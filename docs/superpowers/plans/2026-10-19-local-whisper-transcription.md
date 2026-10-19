# Local Whisper Primary Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcribe new and explicitly retried podcast/YouTube audio locally with pinned `faster-whisper` `medium.en` on CPU INT8, preserving caption priority and completed transcripts, with an automatic Amazon Transcribe fallback after exactly three retryable local failures.

**Architecture:** Keep the provider-neutral `transcribe` stage but make it a dedicated local Whisper stage on its own one-process queue; add an explicit `amazon_transcribe` fallback stage on the existing transcription queue. The local worker streams a verified private S3 audio object to bounded tmpfs, runs a pinned process-local model, writes an immutable raw provider artifact, and hands canonical normalization back to the existing download worker. A provider-aware planner reuses exact artifacts and completed historical Amazon/caption transcripts, while a durable outbox commits the third-failure Amazon handoff before publishing it.

**Tech Stack:** Python 3.11, Flask/SQLAlchemy/Alembic, Celery/Redis, PostgreSQL, private Amazon S3, `faster-whisper==1.2.1`, CTranslate2 CPU INT8, FFmpeg/PyAV, Docker Compose, pytest.

## Global Constraints

- The approved design in `docs/superpowers/specs/2026-10-19-local-whisper-transcription-design.md` is authoritative.
- Creator-provided YouTube captions remain first choice, automatic captions second, local Whisper third, and Amazon Transcribe only the bounded fallback or explicit operational rollback primary.
- Do not enqueue, rewrite, or re-transcribe any completed caption or Amazon transcript. Only new items and administrator-retried failed/deferred/unavailable items enter the new path.
- Celery messages contain only integer item IDs and deterministic non-secret task IDs. Never put audio, transcript text, URLs, S3 keys, model output, credentials, or provider payloads in task messages, logs, metrics, health responses, or database error fields.
- Keep raw and canonical transcript objects private and content-addressed. Preserve unknown/old S3 objects during repair; clear only database references proven invalid.
- Preserve commit-before-publish outboxes, per-item lifetime locks, caption priority, exact artifact verification, and the rule that classification cannot downgrade valid media completion.
- The Whisper service consumes only `whisper` at concurrency one/prefetch one. It receives S3-only AWS credentials, a non-secret YouTube sentinel, no Claude config mount, and no Amazon Transcribe permission.
- Runtime model resolution is local-only. Ordinary service startup must never follow a mutable model branch or download model files.
- Use a 1 GiB tmpfs without materializing decoded PCM. Stream the bounded S3 audio object directly to a temporary encoded-media file while hashing it.
- All automated tests are offline. The real model smoke is opt-in and uses only the checked-in public-domain fixture.
- Every task follows red-green-refactor: write the named failing test, run it and confirm the expected failure, implement the smallest complete change, rerun the focused suite, then commit atomically.
- Do not deploy the semantic switch while old `tasks.transcribe_item_audio` messages remain active, reserved, scheduled, or queued on `transcription`. The rollout task contains a hard drain gate.

## Fixed Runtime and Artifact Contracts

Use these exact code-owned values in `whisper_transcribe.py` and validate configuration against them:

```python
PROVIDER = "faster-whisper"
PROVIDER_VERSION = "1.2.1"
MODEL = "medium.en"
MODEL_REPOSITORY = "Systran/faster-whisper-medium.en"
MODEL_REVISION = "a29b04bd15381511a9af671baec01072039215e3"
COMPUTE_TYPE = "int8"
LANGUAGE = "en"
RESULT_SCHEMA = "local-whisper-result/v1"
MODEL_LOCK_SCHEMA = "whisper-model-lock/v1"
MODEL_MANIFEST_SCHEMA = "whisper-model-manifest/v1"
CANONICAL_PROVIDER_VERSION = "1.2.1+medium.en.a29b04bd15381511a9af671baec01072039215e3.int8"
MAX_SEGMENTS = 100_000
MAX_WORDS = 2_000_000
MAX_RESULT_BYTES = 52_428_800
MAX_AUDIO_DURATION_SECONDS = 21_600
```

The raw provider JSON is canonical JSON with exactly these top-level fields:

```text
schema, job_name, audio_sha256, provider, provider_version,
model, model_repository, model_revision, compute_type, language,
duration, inference, segments
```

`inference` contains only `beam_size`, `vad_filter`, and `word_timestamps`. Each segment contains `start`, `end`, `text`, and `words`; each word contains `start`, `end`, `word`, and `probability`. Reject unknown fields, non-finite numbers, out-of-order timestamps, empty text/results, duration over 21,600 seconds, limit violations, and identity mismatches. The canonical transcript keeps segment timestamps/text but not word rows.

Use these closed local error categories:

```python
WHISPER_RETRYABLE = (
    "storage_transient", "worker_timeout",
    "whisper_runtime_transient", "resource_exhausted",
)
WHISPER_PERMANENT = (
    "invalid_audio", "unsupported_media", "duration_too_large",
    "whisper_configuration", "invalid_whisper_output", "artifact_mismatch",
)
```

The deterministic Whisper job identity hashes item ID, audio SHA-256, provider/version, model/repository/revision, compute type, language, and canonical normalizer version. The raw object key is `items/{platform}/{external_id}/provider/{raw_sha256}.json`. After inference, persist that expected key/hash and provenance while the job remains `running`, commit, then perform the conditional immutable upload. This ordering makes a crash after upload reusable without abandoning content-addressed naming.

---

### Task 1: Lock configuration, Python dependencies, and model identity

**Files:**

- Modify: `config.py`
- Modify: `.env.example`
- Create: `requirements-whisper.in`
- Create: `requirements-whisper.lock`
- Create: `whisper-model.lock.json`
- Modify: `tests/test_config.py`
- Modify: `tests/test_repository_hygiene.py`

- [ ] **Step 1: Write failing configuration and lock-contract tests**

Add assertions for these defaults and exact validators:

```text
TRANSCRIPTION_PRIMARY=amazon_transcribe
FASTER_WHISPER_EXPECTED_VERSION=1.2.1
WHISPER_MODEL=medium.en
WHISPER_MODEL_REPOSITORY=Systran/faster-whisper-medium.en
WHISPER_MODEL_REVISION=a29b04bd15381511a9af671baec01072039215e3
WHISPER_COMPUTE_TYPE=int8
WHISPER_CPU_THREADS=10
WHISPER_MAX_ATTEMPTS=3
WHISPER_SOFT_TIME_LIMIT_SECONDS=21600
WHISPER_HARD_TIME_LIMIT_SECONDS=21900
WHISPER_MAX_AUDIO_DURATION_SECONDS=21600
```

Reject any other primary/provider/model/repository/revision/compute value, threads outside `1..10`, attempts other than `3`, hard timeout not greater than soft timeout, and duration above the hard timeout. Assert the model-lock JSON has the fixed identity plus required files `config.json`, `model.bin`, `tokenizer.json`, and `vocabulary.json`. Assert every resolved Whisper dependency is exactly pinned in `requirements-whisper.lock` and the Docker build will be able to install it with hashes.

- [ ] **Step 2: Run the tests and confirm they fail because the fields/locks do not exist**

Run: `pytest -q tests/test_config.py tests/test_repository_hygiene.py`

Expected: failures naming missing Whisper settings and lock files, not unrelated failures.

- [ ] **Step 3: Add validated settings and reproducible locks**

Add typed settings to `Settings`; parse `TRANSCRIPTION_PRIMARY` explicitly and reuse the existing positive-integer loader for numeric fields. Keep Amazon as the checked-in/default rollout primary. Add the same values to `.env.example` without real credentials, plus:

```text
WHISPER_AWS_ACCESS_KEY_ID=
WHISPER_AWS_SECRET_ACCESS_KEY=
WHISPER_AWS_SESSION_TOKEN=
```

Put only `faster-whisper==1.2.1` in `requirements-whisper.in`. Generate a Python 3.11/Linux lock with transitive versions and SHA-256 hashes in `requirements-whisper.lock`; do not add the heavy runtime to `requirements.txt`. Add `whisper-model.lock.json` with the fixed schema/identity/required-file list.

- [ ] **Step 4: Run the focused tests**

Run: `pytest -q tests/test_config.py tests/test_repository_hygiene.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.py .env.example requirements-whisper.in requirements-whisper.lock whisper-model.lock.json tests/test_config.py tests/test_repository_hygiene.py
git commit -m "build: lock local whisper runtime"
```

---

### Task 2: Build the pure Whisper adapter and pinned model preparation

**Files:**

- Create: `whisper_transcribe.py`
- Create: `scripts/cache-whisper-model.py`
- Create: `tests/test_whisper_transcribe.py`
- Create: `tests/fixtures/whisper-provider-result.json`
- Create: `assets/whisper-smoke.flac`
- Create: `assets/WHISPER-SMOKE-ATTRIBUTION.md`
- Modify: `.dockerignore`

- [ ] **Step 1: Write failing adapter tests with a fake model**

Test lazy process-local singleton loading; exact repository/revision/local-only/model/CPU-thread/INT8 arguments; fixed English, word timestamps, conservative VAD, and bounded beam settings; complete generator materialization; deterministic job identity and JSON bytes; segment/word ordering; finite probabilities; limit checks; and closed exception categories. Assert no Flask, SQLAlchemy, Celery, boto3, S3, database, or shell imports occur in the adapter.

The fake model must cover a successful result, empty result, malformed timestamp, NaN/Infinity, oversized text/segment/word counts, duration over 21,600 seconds, decode rejection, memory/resource exhaustion, and generic runtime failure.

- [ ] **Step 2: Run the adapter tests and confirm the module is missing**

Run: `pytest -q tests/test_whisper_transcribe.py`

Expected: collection/import failure for `whisper_transcribe`.

- [ ] **Step 3: Implement the adapter and result contract**

Use frozen slot dataclasses for words, segments, and the full result; a `WhisperError(category, retryable)` boundary; canonical JSON serialization; and an injected model factory for tests. Import `faster_whisper` only inside the default factory. Cache one model per worker process keyed by PID and exact model identity. Load from the prepared local model directory only—never repository/network fallback at runtime.

- [ ] **Step 4: Implement deterministic model preparation**

`scripts/cache-whisper-model.py` must read `whisper-model.lock.json`, call `huggingface_hub.snapshot_download()` with the exact repository and immutable revision into `/models/medium.en-a29b04bd15381511a9af671baec01072039215e3`, verify required files, hash every regular snapshot file, and atomically write `whisper-model-manifest.json` containing only relative names, byte sizes, hashes, revision, and lock hash. Refuse symlinks escaping the snapshot, unexpected special files, or mutable revisions.

Add the 1,152,693-byte upstream Whisper `jfk.flac` fixture as `assets/whisper-smoke.flac`; assert SHA-256 `63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715`. Record its public-domain JFK speech provenance in the attribution file. Add only the narrow `.dockerignore` exception required for this asset.

- [ ] **Step 5: Run focused tests**

Run: `pytest -q tests/test_whisper_transcribe.py tests/test_repository_hygiene.py`

Expected: PASS without loading/downloading the real model.

- [ ] **Step 6: Commit**

```bash
git add whisper_transcribe.py scripts/cache-whisper-model.py tests/test_whisper_transcribe.py tests/fixtures/whisper-provider-result.json assets/whisper-smoke.flac assets/WHISPER-SMOKE-ATTRIBUTION.md .dockerignore tests/test_repository_hygiene.py
git commit -m "feat: add pinned local whisper adapter"
```

---

### Task 3: Normalize local Whisper output into the canonical transcript contract

**Files:**

- Modify: `transcripts.py`
- Modify: `tests/test_transcripts.py`

- [ ] **Step 1: Write failing local-normalizer tests**

Using `tests/fixtures/whisper-provider-result.json`, assert `normalize_local_whisper()`:

- accepts only schema `local-whisper-result/v1` and exact provider/model identity;
- verifies the supplied audio SHA-256 and raw artifact SHA-256;
- builds stable segment IDs/citations with origin `local_whisper`, provider `faster-whisper`, language `en`, and the composite canonical provider version;
- preserves monotonic segment timestamps/text and excludes word rows from canonical JSON;
- produces deterministic canonical text/segments hashes;
- rejects unknown fields, identity mismatches, empty/malformed output, timestamp regressions, non-finite values, limit violations, and private locators.

Extend parse/lineage tests so `local_whisper` is valid and historical caption/Amazon fixtures remain byte-for-byte unchanged.

- [ ] **Step 2: Run and confirm the expected failure**

Run: `pytest -q tests/test_transcripts.py`

Expected: failures for the missing normalizer/origin.

- [ ] **Step 3: Implement local normalization**

Share the existing `_build()` canonical construction and limits. Parse provider JSON as data, validate its closed schema, and pass the raw canonical JSON SHA-256 as `source_sha256`. Do not make `transcripts.py` import the heavy runtime.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest -q tests/test_transcripts.py tests/test_whisper_transcribe.py`

Expected: PASS.

```bash
git add transcripts.py tests/test_transcripts.py
git commit -m "feat: normalize local whisper transcripts"
```

---

### Task 4: Stream private S3 audio safely into bounded local storage

**Files:**

- Modify: `s3_utils.py`
- Modify: `tests/test_s3_utils.py`

- [ ] **Step 1: Write failing streaming-download tests**

Add `download_file_verified(s3_key, local_path, *, expected_sha256, max_bytes)` coverage for chunked writes without `get_bytes()`, exact content length/hash validation, empty/oversized/truncated bodies, hash mismatch, transient S3 failures, invalid destination types, removal of partial files, response-body closure, and no leakage of keys/paths in raised errors.

- [ ] **Step 2: Run and confirm the method is missing**

Run: `pytest -q tests/test_s3_utils.py`

Expected: failure for missing `download_file_verified`.

- [ ] **Step 3: Implement bounded streaming**

Reuse `_object_chunks()` so S3 redirect, size, and stream-closure protections remain centralized. Open only the caller-supplied file inside its already-created temporary directory, update SHA-256 while writing, `flush()`/`fsync()` before success, and unlink a partial regular file on every failure. Return a small typed result containing only verified byte count and digest.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest -q tests/test_s3_utils.py`

Expected: PASS.

```bash
git add s3_utils.py tests/test_s3_utils.py
git commit -m "feat: stream verified audio from private storage"
```

---

### Task 5: Add durable provider provenance and the Amazon fallback stage

**Files:**

- Modify: `models.py`
- Create: `migrations/versions/0003_local_whisper_transcription.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_migrations.py`

- [ ] **Step 1: Write failing model and migration tests**

Add `local_whisper` to transcript origins and `amazon_transcribe` to processing stages. Add nullable bounded strings:

```text
transcription_provider_version VARCHAR(128)
transcription_model VARCHAR(128)
transcription_model_revision VARCHAR(64)
transcription_compute_type VARCHAR(32)
transcription_fallback_reason VARCHAR(64)
```

Seed migration tests with a completed Amazon item/job, completed caption item, source membership, and active classification revision. Assert upgrade preserves every existing row and pointer, backfills Amazon provider version `2017-10-26` and caption provider version `2026.08.19`, leaves model/compute/fallback null, accepts new origin/stage values, and passes `foreign_key_check`/`compare_metadata`. Assert downgrade/upgrade reversibility and PostgreSQL named-constraint order.

- [ ] **Step 2: Run and confirm enum/column failures**

Run: `pytest -q tests/test_models.py tests/test_migrations.py`

Expected: failures naming absent values/columns/migration head.

- [ ] **Step 3: Implement the ORM and migration**

Revision `0003_local_whisper_transcription` has down-revision `0002_commercial_classification`. Rebuild all four affected checks:

```text
ck_content_items_processing_stage
ck_content_items_last_successful_stage
ck_content_items_transcript_origin
ck_processing_jobs_stage
```

Use the same SQLite foreign-key-off/outside-transaction batch strategy as migration `0002` so rebuilding `content_items` cannot cascade-delete memberships, jobs, or classification rows. Do not rename historical completed `transcribe` jobs or rewrite completed origins.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest -q tests/test_models.py tests/test_migrations.py tests/test_system_checks.py`

Expected: PASS.

```bash
git add models.py migrations/versions/0003_local_whisper_transcription.py tests/test_models.py tests/test_migrations.py tests/test_system_checks.py
git commit -m "feat: store transcription provider provenance"
```

---

### Task 6: Route new audio through local Whisper and reuse exact artifacts

**Files:**

- Modify: `tasks.py`
- Modify: `celeryconfig.py`
- Modify: `tests/test_processing_tasks.py`
- Modify: `tests/test_discovery_tasks.py`
- Modify: `tests/test_phase1_smoke.py`

- [ ] **Step 1: Extend the processing fake and write failing route/reuse tests**

Add an injected fake Whisper adapter/storage stream. Cover:

- new podcast audio routes `podcast_audio -> transcribe -> normalize -> completed`;
- YouTube still tries creator then automatic captions before `youtube_audio -> transcribe`;
- `transcribe_item_audio` is on `whisper`, while a new `amazon_transcribe_item_audio` task is on `transcription`;
- `TRANSCRIPTION_PRIMARY=amazon_transcribe` routes new/retried audio directly to `amazon_transcribe` during rollout/rollback;
- existing completed Amazon and caption items remain complete and are never dispatched;
- valid local raw artifacts with missing canonical output route directly to `normalize`;
- provider reuse verifies raw bytes, input audio hash, job identity, exact model provenance, and content-addressed key;
- model/revision/compute/audio/hash mismatches rerun from `transcribe` without deleting S3 objects;
- task IDs include the deterministic stage/input/model identity but no locator/key/title/text;
- a crash after the committed raw pointer plus successful upload reuses the raw object rather than rerunning inference.

- [ ] **Step 2: Run and confirm existing Amazon-only expectations fail**

Run: `pytest -q tests/test_processing_tasks.py tests/test_discovery_tasks.py tests/test_phase1_smoke.py`

Expected: failures for queue/stage/provider assumptions.

- [ ] **Step 3: Implement provider-aware planning and local execution**

Add `whisper` to the declared Celery queues and exact routes. Split the current `_transcribe()`:

- `_transcribe()` verifies/downloads the audio into `TemporaryDirectory(dir="/tmp")`, invokes the lazy adapter, computes the raw bytes/hash/key, persists the running provider pointer/provenance and commits, conditionally uploads immutable raw JSON, and returns `normalize`;
- `_amazon_transcribe()` contains the current Amazon adapter/validation behavior and adds provider version/model/fallback provenance;
- `_normalize()` branches between caption VTT and local Whisper provider JSON while retaining identical canonical upload rules.

Extend clearing helpers so changing audio/provider clears stale provider-version/model/revision/compute/fallback fields together. Make `_plan()`, `_verification_stage()`, and `_completed_valid()` provider-aware rather than equating `transcribe` with Amazon. Use `TRANSCRIPTION_PRIMARY` only when no reusable/persisted provider path already decides the stage.

Map stages exactly:

```text
transcribe -> tasks.transcribe_item_audio -> whisper
amazon_transcribe -> tasks.amazon_transcribe_item_audio -> transcription
normalize -> tasks.normalize_item_transcript -> downloads
```

Give only the Whisper task `soft_time_limit=21600` and `time_limit=21900`; do not widen global Celery limits.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest -q tests/test_processing_tasks.py tests/test_discovery_tasks.py tests/test_phase1_smoke.py tests/test_transcripts.py`

Expected: PASS.

```bash
git add tasks.py celeryconfig.py tests/test_processing_tasks.py tests/test_discovery_tasks.py tests/test_phase1_smoke.py
git commit -m "feat: route audio through local whisper"
```

---

### Task 7: Enforce three local attempts and atomically hand off to Amazon

**Files:**

- Modify: `tasks.py`
- Modify: `web/routes.py`
- Modify: `tests/test_processing_tasks.py`
- Modify: `tests/test_web_sources.py`
- Modify: `tests/test_web_navigation.py`

- [ ] **Step 1: Write failing retry/fallback/manual-retry tests**

Cover all boundaries:

- retryable local failures at attempts one and two return bounded Celery retries and never create an Amazon job;
- the third retryable failure commits `transcribe=failed/whisper_exhausted` plus `amazon_transcribe=pending/dispatch_pending` and keeps the item `transcribing` before broker publication;
- unknown broker outcome leaves exactly one replayable fallback outbox;
- replay does not duplicate the fallback job or reset a newer generation;
- a worker killed after committing attempt three is redelivered into fallback without a fourth inference;
- permanent input/integrity/config/output failures never invoke Amazon;
- Amazon retains the existing six-attempt behavior and final failed/deferred semantics;
- a completed fallback records origin/provider `amazon_transcribe` and fallback reason `whisper_exhausted`;
- direct Amazon rollback primary leaves fallback reason null;
- administrator retry of a failed/deferred audio item resets the local `transcribe` attempt budget to zero, marks any failed Amazon fallback as `manual_retry_superseded`, and replans through Whisper;
- administrator viewing/retrying cannot alter a completed transcript.

- [ ] **Step 2: Run and confirm the generic six-attempt handler fails these cases**

Run: `pytest -q tests/test_processing_tasks.py tests/test_web_sources.py tests/test_web_navigation.py`

Expected: failures at the third-attempt and manual-reset assertions.

- [ ] **Step 3: Implement stage-specific failure handling**

Before claiming local inference, detect an exhausted running/retrying `transcribe` job with no reusable raw artifact and create the fallback outbox. In the exception path, use `< WHISPER_MAX_ATTEMPTS` for retries; on exhaustion, change both jobs/item in one transaction and return the normal committed dispatch tuple. Use only closed categories; never persist exception strings.

Update the owner-scoped retry route under the item lock. Reset `ProcessingJob.attempts` only for the local `transcribe` job on an explicit retry, preserve valid media/provider artifacts, and force the planner—not the route—to decide whether acquisition, local normalization, or local inference is needed. Preserve CSRF, source-enabled, duplicate-request, and outbox guards.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest -q tests/test_processing_tasks.py tests/test_web_sources.py tests/test_web_navigation.py`

Expected: PASS.

```bash
git add tasks.py web/routes.py tests/test_processing_tasks.py tests/test_web_sources.py tests/test_web_navigation.py
git commit -m "feat: fall back after bounded whisper retries"
```

---

### Task 8: Isolate the Whisper Docker runtime and add safe operator checks

**Files:**

- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `system_checks.py`
- Modify: `app.py`
- Create: `transcription_cli.py`
- Create: `scripts/start-whisper.sh`
- Modify: `tests/test_compose_contract.py`
- Modify: `tests/test_system_checks.py`
- Modify: `tests/test_app_factory.py`
- Modify: `tests/test_repository_hygiene.py`

- [ ] **Step 1: Write failing Compose/startup/readiness/CLI tests**

Assert:

- the ordinary `app` image remains free of faster-whisper dependencies;
- a `whisper` Docker target installs `requirements-whisper.lock` with hashes;
- a profile-only `whisper-model` service has no `.env`, database, Redis, AWS, YouTube, or Claude credentials and mounts `whisper_models:/models` read-write;
- runtime `whisper` mounts the model read-only, consumes only `whisper`, uses concurrency/prefetch one, runs as UID/GID 1000, drops all capabilities, enables no-new-privileges, uses exactly 10 CPUs/8 GiB/1 GiB tmpfs, and receives the separate Whisper AWS variables plus a fixed non-secret YouTube sentinel;
- the general worker continues consuming Amazon `transcription` and never consumes `whisper`;
- `start-whisper.sh` pins `PYTHONPATH`, checks database/Redis/S3 plus model lock/manifest/load and tmpfs capacity, and does not probe YouTube, yt-dlp, Deno, Claude, or Amazon Transcribe;
- public readiness reports a named `whisper_worker` status by pinging only `whisper@whisper-worker`, without importing/loading the model; when Amazon is configured as primary, that optional component reports healthy without requiring a local worker;
- `flask whisper verify-model` checks exact manifest/file hashes and a CPU INT8 load, while `flask whisper smoke` runs the fixture, checks the expected phrase/timestamps/identity, and prints only safe aggregate status—never decoded text or paths.

- [ ] **Step 2: Run and confirm runtime contracts fail**

Run: `pytest -q tests/test_compose_contract.py tests/test_system_checks.py tests/test_app_factory.py tests/test_repository_hygiene.py`

Expected: failures for the absent target/services/script/CLI/component.

- [ ] **Step 3: Implement the multi-target image and Compose isolation**

Keep the current image as Docker target `app`. Add `FROM app AS whisper`, install the hashed Whisper lock, create `/models` owned by UID/GID 1000, and copy only the smoke asset needed by the CLI. Configure:

```text
whisper-model: profile model-prep, restart no, RW model volume, cache script
whisper: target whisper, hostname whisper-worker, queue whisper,
         concurrency 1, prefetch 1, 10 CPUs, 8 GiB, 1 GiB /tmp tmpfs,
         RO model volume, start-whisper.sh
```

Override standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional `AWS_SESSION_TOKEN` from `WHISPER_AWS_*`; override `YOUTUBE_API_KEY=disabled-for-whisper-worker`; do not mount `.runtime/claude`. Add named volume `whisper_models`.

- [ ] **Step 4: Implement safe readiness and operator commands**

Register a `whisper` Flask CLI group. Keep model verification/smoke local to the Whisper image; `/readyz` performs only the named worker ping with the existing isolated timeout. Model configuration failures must make the startup script exit before Celery consumes work.

- [ ] **Step 5: Run focused tests and build both images**

Run:

```bash
pytest -q tests/test_compose_contract.py tests/test_system_checks.py tests/test_app_factory.py tests/test_repository_hygiene.py
docker compose --project-name podcasttranscriptionweb config --quiet
docker build --target app -t podcast-transcription:local .
docker build --target whisper -t podcast-transcription-whisper:local .
```

Expected: all commands succeed; the build does not download the model.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml system_checks.py app.py transcription_cli.py scripts/start-whisper.sh tests/test_compose_contract.py tests/test_system_checks.py tests/test_app_factory.py tests/test_repository_hygiene.py
git commit -m "feat: isolate whisper worker runtime"
```

---

### Task 9: Expose safe provenance and complete operations documentation

**Files:**

- Modify: `web/routes.py`
- Modify: `templates/item.html`
- Modify: `templates/source_items.html`
- Modify: `tests/test_web_navigation.py`
- Modify: `tests/test_web_sources.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `tests/test_repository_hygiene.py`

- [ ] **Step 1: Write failing UI/documentation contract tests**

Assert owner-scoped views show only:

```text
Whisper · medium.en
Amazon Transcribe · Whisper fallback
Whisper queued
Whisper running
```

Expose bounded provider version/model/compute/fallback labels only when internally valid. Never expose model paths, S3 keys, job names, hashes, provider JSON, source URLs, or error text. Preserve caption labels and retry visibility rules.

Repository tests must require exact commands for model prep/verification/smoke, queue/worker inspection, separate IAM policy, canary, rollback, cache rebuild, resource tuning, backup/restore, and all service-count/start/stop changes.

- [ ] **Step 2: Run and confirm missing labels/docs fail**

Run: `pytest -q tests/test_web_navigation.py tests/test_web_sources.py tests/test_repository_hygiene.py`

Expected: failures for missing safe projections and runbook sections.

- [ ] **Step 3: Implement UI DTOs and operations guide**

Add only safe allowlisted values to `item_view()`/source rows. Document this S3-only Whisper policy, then insert the new access-key values only into the private `.env` at deployment time:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BucketMetadata",
      "Effect": "Allow",
      "Action": "s3:GetBucketLocation",
      "Resource": "arn:aws:s3:::podcast-transcriptions-web-2025"
    },
    {
      "Sid": "PodcastArtifacts",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": "arn:aws:s3:::podcast-transcriptions-web-2025/items/*"
    }
  ]
}
```

Explicitly state that this identity has no `transcribe:*` permission. Document model disk/RAM/CPU expectations, `docker compose --project-name podcasttranscriptionweb --profile model-prep run --rm whisper-model`, local verify/smoke, named-worker ping, canary selection, Amazon-primary rollback, and the prohibition on deleting processing rows/S3 objects to force retry.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest -q tests/test_web_navigation.py tests/test_web_sources.py tests/test_repository_hygiene.py`

Expected: PASS.

```bash
git add web/routes.py templates/item.html templates/source_items.html tests/test_web_navigation.py tests/test_web_sources.py README.md docs/operations.md tests/test_repository_hygiene.py
git commit -m "docs: add whisper provenance and operations"
```

---

### Task 10: Verify, deploy behind Amazon primary, canary, and enable Whisper

**Files:**

- Modify only if verification exposes a defect; every fix needs its own regression test and atomic commit.

- [ ] **Step 1: Run the complete offline verification**

Run:

```bash
pytest -q
docker compose --project-name podcasttranscriptionweb config --quiet
docker build --target app -t podcast-transcription:local .
docker build --target whisper -t podcast-transcription-whisper:local .
git diff --check
git status --short
```

Expected: all tests/builds pass, no whitespace errors, and only intentional plan-tracking changes remain.

- [ ] **Step 2: Back up and enforce the old-message drain gate**

Follow `docs/operations.md` to back up PostgreSQL. Keep `TRANSCRIPTION_PRIMARY=amazon_transcribe`. Stop `worker` and `beat`, then verify both conditions before deploying code that changes `tasks.transcribe_item_audio` semantics:

- Celery inspection reports no active, reserved, or scheduled `tasks.transcribe_item_audio` deliveries.
- PostgreSQL reports zero `transcribe` jobs in `pending`, `running`, or `retrying` state and zero `transcribe` jobs with `error_category='dispatch_pending'`.

If either count is nonzero, restart the old worker to drain it and repeat. Do not purge Redis and do not edit/delete processing rows.

- [ ] **Step 3: Deploy schema/code with Amazon still primary**

Run the documented Compose pull/build/up sequence with:

```text
CLAUDE_CONFIG_HOST_PATH=/home/wdphillips/PodcastTranscriptionWeb/.runtime/claude
COMPOSE_PROJECT_NAME=podcasttranscriptionweb
TRANSCRIPTION_PRIMARY=amazon_transcribe
```

Verify migration head `0003_local_whisper_transcription`, public `/healthz`, `/readyz`, discovery, classifier, existing completed item counts, and that no completed transcript was queued or changed.

- [ ] **Step 4: Create the S3-only IAM credentials and prepare the pinned model**

Create a dedicated IAM user/access key with exactly the policy in `docs/operations.md`; place only its values in `WHISPER_AWS_*` inside the private `.env`. Then run the profile-only model preparation command. Verify its manifest and load from the Whisper image. The model-prep service must be stopped/removed after completion; the named volume remains.

- [ ] **Step 5: Start Whisper without routing production work and run the real smoke**

Start/recreate the `whisper` service while Amazon remains primary. Run the documented `verify-model` and `smoke` commands. Expected smoke result: exact model/provider identity, non-empty monotonic segments, bounded duration, and the JFK phrase check; output contains no transcript text/path.

- [ ] **Step 6: Run exactly one explicit production canary**

Stop `beat`, drain discovery/download/transcription work, and verify no non-canary media job is pending or dispatchable. Select one failed/deferred/unavailable item with valid owned audio and no completed transcript. Temporarily set `TRANSCRIPTION_PRIMARY=faster_whisper`, recreate the configuration-reading services needed for the canary while leaving `beat` stopped, request that one item retry, and observe it through local raw artifact, canonical normalization, classification creation, cleanup, and safe UI provenance. Confirm no other completed item changed and no unrelated failed item was enqueued.

- [ ] **Step 7: Enable normal local routing or roll back safely**

If the canary passes, leave `TRANSCRIPTION_PRIMARY=faster_whisper`, recreate `web worker beat classifier whisper`, and verify `whisper_worker` readiness plus all existing services. If it fails, set primary back to `amazon_transcribe`, recreate configuration-reading services, stop Whisper consumption, and retain every database/S3 artifact for diagnosis.

- [ ] **Step 8: Final verification and push**

Run the complete offline suite once more after any canary-derived fix, inspect `git log --oneline --decorate -12`, confirm `master` is clean/ahead only by reviewed commits, and push `master` to `origin`. Record the deployed commit and safe aggregate canary outcome in the handoff; never include credentials, private locators, S3 keys, transcript text, or model output.

---

## Plan Self-Review Checklist

- [ ] Every approved design goal and non-goal maps to at least one task/test.
- [ ] Caption priority and preservation of completed Amazon/caption transcripts are explicit regression tests.
- [ ] The three-attempt boundary, hard-death recovery, atomic fallback outbox, and manual retry reset are all covered.
- [ ] S3 download is streaming and bounded; model/runtime never download implicitly.
- [ ] Migration preserves memberships, jobs, classifications, and legacy provider identity.
- [ ] Queue ownership, CPU/RAM/tmpfs bounds, credential isolation, model volume permissions, and safe readiness are contract-tested.
- [ ] Rollout starts Amazon-primary, drains old messages, prepares/verifies the model, uses exactly one canary, and has a reversible switch.
- [ ] The plan contains no unresolved implementation marker or unspecified decision.
