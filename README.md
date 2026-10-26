# Private media transcription workspace

Ingest Apple Podcasts pages, podcast RSS feeds, and public YouTube videos,
Shorts, channels, handles, users, and playlists. One administrator manages
sources, requests historical backfills, and reads transcripts.
YouTube processing prefers creator captions, then automatic captions, and
falls back to audio transcription with Amazon Transcribe. Artifacts stay in
the private external S3 bucket; PostgreSQL stores source and processing state.

The only supported deployment is the seven-service local Docker Compose stack
(`postgres`, `redis`, `migrate`, `web`, `worker`, `beat`, `classifier`). Web binds to host
loopback port 8005. The administrator's external Cloudflare Tunnel publishes
`https://podcasts.ennovocon.com` with origin `http://localhost:8005`.

Follow the [operations guide](docs/operations.md) for prerequisites, secrets,
installation, administrator provisioning, source use, recovery, and backups.
Start with that guide before running Compose: valid AWS credentials and a
YouTube Data API key are required by startup checks.

Phase 1 implements ingestion, transcription, and commercial-segment classification
through the Bezalel Platform Model Router (an isolated classifier worker) with owner
review/corrections. Learning bundles and Agent Factory delivery are deferred to later phases.
Their mandatory filtered-input gateway is implemented in `learning_projection.py`:
future Phase 2 bundle extraction and Phase 3 publishers must call
`load_active_learning_projection(session=..., storage=..., item_id=...)` and
validate cited segment IDs with the returned projection's `assert_citable()`.
The immutable result contains only exact canonical segments labeled `editorial`;
commercials, promotions, housekeeping, and `uncertain` segments are ineligible.
Missing, corrupt, stale, failed, or superseded inputs raise
`LearningProjectionUnavailable`; there is no full-transcript fallback.
The optional `exporter` service reads the separate knowledge projection
(`load_active_knowledge_projection`): it writes nightly KNOWLEDGE-INTAKE-0001
batches of episode and passage records, editorial with uncertain segments
marked, naming
each show's hosts and each episode's guests, for the Platform knowledge intake,
which delivers them to Lamad and Ebenezer. See
[knowledge intake export](docs/operations.md#knowledge-intake-export).

New bundle construction requires the latest revision by default, including
when a newer revision is pending, retrying, running, or failed. An already
published prior bundle may stay active until its replacement is validated.
Explicit promotional research instead calls `load_promotional_projection()`
with a nonempty purpose and only `self_promotion`/`cross_promotion` labels;
its separate `PromotionalProjection` type cannot use the general citation API.
See the [filtered-input contract](docs/operations.md#filtered-learning-and-delivery-inputs)
for artifact validation and integration requirements.

For offline development verification, install `requirements-dev.txt` in a
Python 3.11 virtual environment, then run:

```bash
.venv/bin/python -m pytest -q
```

The default tests use synthetic credentials and make no live provider calls.
The integrated acceptance proof is `tests/test_phase1_smoke.py`: it exercises
admin login, RSS and YouTube intake, newest-25 discovery, all backfill selectors,
caption priority, private transcript retrieval, and idempotent replay.

Commercial exclusion has a separate offline release gate:

```bash
.venv/bin/python -m pytest -q tests/test_commercial_classification_smoke.py tests/eval/test_commercial_policy_eval.py
```

It exercises both completed podcast and YouTube canonical transcripts through
the final-stage hook, durable classification, immutable artifacts, owner review,
correction, and filtered loading. Crash recovery and replay preserve completed block
checkpoints; learned rules remain source-scoped. Synthetic evaluation covers all
six labels and confidence/boundary cases, blocks any editorial false exclusion,
and binds expected count metrics to the policy version. This tests the future
consumer input contract; bundle extraction and destination delivery remain deferred.
See [acceptance, evaluation, and safe observations](docs/operations.md#commercial-acceptance-evaluation-and-observations)
for metric definitions, the full release gate, and private real-evaluation storage.

Controlled provider checks require explicit file selection and
`RUN_LIVE_YOUTUBE=1`; they skip until each check's required settings are supplied.
See [controlled live verification](docs/operations.md#controlled-live-verification)
for the bounded inputs, additional AWS opt-in, costs, and remaining manual gates.
