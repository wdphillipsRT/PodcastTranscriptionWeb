# Private media transcription workspace

Ingest Apple Podcasts pages, podcast RSS feeds, and public YouTube videos,
Shorts, channels, handles, users, and playlists. One administrator manages
sources, requests historical backfills, and reads transcripts.
YouTube processing prefers creator captions, then automatic captions, and
falls back to audio transcription with Amazon Transcribe. Artifacts stay in
the private external S3 bucket; PostgreSQL stores source and processing state.

The only supported deployment is the six-service local Docker Compose stack
(`postgres`, `redis`, `migrate`, `web`, `worker`, `beat`). Web binds to host
loopback port 8005. The administrator's external Cloudflare Tunnel publishes
`https://podcasts.ennovocon.com` with origin `http://localhost:8005`.

Follow the [operations guide](docs/operations.md) for prerequisites, secrets,
installation, administrator provisioning, source use, recovery, and backups.
Start with that guide before running Compose: valid AWS credentials and a
YouTube Data API key are required by startup checks.

Phase 1 implements ingestion and transcription. Claude learning bundles and
delivery to Lamad, Ebenezer, or Agent Factory are deferred to later phases.
Their mandatory filtered-input gateway is implemented in `learning_projection.py`:
future Phase 2 bundle extraction and Phase 3 publishers must call
`load_active_learning_projection(session=..., storage=..., item_id=...)` and
validate cited segment IDs with the returned projection's `assert_citable()`.
The immutable result contains only exact canonical segments labeled `editorial`;
commercials, promotions, housekeeping, and `uncertain` segments are ineligible.
Missing, corrupt, stale, failed, or superseded inputs raise
`LearningProjectionUnavailable`; there is no full-transcript fallback.
This gateway does not publish to Lamad, Ebenezer, or Agent Factory.

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

Controlled provider checks require explicit file selection and
`RUN_LIVE_YOUTUBE=1`; they skip until each check's required settings are supplied.
See [controlled live verification](docs/operations.md#controlled-live-verification)
for the bounded inputs, additional AWS opt-in, costs, and remaining manual gates.
