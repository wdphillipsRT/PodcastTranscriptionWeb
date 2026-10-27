# Docker operations

## Scope and prerequisites

Run commands from the repository root in a Bash SSH session. The only supported
installation is `docker-compose.yml`: `postgres`, `redis`, `migrate`, `web`,
`worker`, `beat`, and `classifier`. The `migrate` service exits successfully after
applying schema migrations; the other six services stay running after credentials
are configured. Keep one media worker, one classifier, and one beat service
unless a reviewed scaling change is made. The classifier alone consumes the
`classification` queue; the media worker consumes exactly
`default,discovery,downloads,transcription`.

Use a Linux host with Docker Engine, the Docker Compose v2 plugin, Git,
Python 3 for secret generation, curl, and an editor. Allow enough disk for the
database/images and RAM for PostgreSQL, two worker processes, and the worker's
768 MiB temporary media filesystem, plus one classifier process and its 256 MiB
temporary filesystem. The non-root image supplies Python 3.11, FFmpeg, yt-dlp
and Deno; no host media tools are needed. The host needs outbound HTTPS to AWS,
Google/YouTube, Apple, public feed/media hosts, and image/package registries.
The podcast Compose network (`192.168.144.0/20`, pinned in `docker-compose.yml`)
must reach the Bezalel Platform Model Router at `https://192.168.68.200:18443`;
see [Platform Model Router](#platform-model-router). Amazon Transcribe usage and
provider quotas can incur costs; router calls do not.

The administrator owns the Cloudflare Tunnel and DNS configuration for
`podcasts.ennovocon.com`. Run the tunnel connector on this host so its origin
`http://localhost:8005` reaches the published loopback port. Tunnel containers
have a different localhost; they are outside this stack. No tunnel credentials
or DNS automation are included here. Do not publish the application port on
all host interfaces: the app trusts one forwarding proxy.

Phase 1 supports ingestion and transcription. Editorial knowledge reaches Lamad
and Ebenezer only through the optional knowledge intake exporter below.
Learning bundles and Agent Factory delivery are deferred to later phases.

## Filtered learning and delivery inputs

`learning_projection.py` provides the single transcript input gateway for
future Phase 2 bundle extraction and Phase 3 Lamad, Ebenezer, and Agent Factory
publishers. This task implements that boundary; it does not create learning
bundles or publish to any of those systems. Downstream code must not read
`ContentItem.segments_s3_key` or `transcript_s3_key` directly. A repository
contract test scans `learning_*.py`, `publishers/`, and `integrations/` for
direct artifact-field references outside the gateway.

Use committed item/revision inputs and the existing private storage client:

```python
from learning_projection import load_active_learning_projection

projection = load_active_learning_projection(
    session=db.session, storage=storage, item_id=item_id,
)
segment = projection.assert_citable(segment_id)
```

The immutable result has the canonical `learning-transcript-projection/v1`
schema and exact canonical editorial records, including their IDs, hashes,
timings, text, and citations. It exposes no storage client, full transcript,
or raw policy. Only `editorial` is eligible; `uncertain` is always excluded.
The method and the module's `assert_citable(projection, segment_id)` both
reject excluded, uncertain, unknown, and promotional references with the same
safe exception, without disclosing whether an excluded segment exists.

The knowledge intake exporter alone uses a separate loader,
`load_active_knowledge_projection`, which returns editorial and `uncertain`
segments in transcript order with the uncertain ones listed in
`uncertain_segment_ids`. It applies every check above, also reads the stored
editorial projection byte for byte and requires it to equal the knowledge
projection without its uncertain segments, and returns a type that
`assert_citable` rejects. Excluded labels (paid, self or cross promotion,
housekeeping) never appear in either projection.

Loading requires a completed item and its active completed classification
revision matching the current transcript and canonical-object hashes. All
objects are read with the existing byte limit. Canonical JSON bytes, schemas,
content-addressed keys, embedded hashes, item/source ownership and membership,
classifier provenance, policy lineage, revision totals, and exact editorial
projection equality are verified. Policy/projection digests follow their
canonical schema convention: each hashes the artifact without its own digest
field. The canonical transcript object also has its full stored-byte hash.
After storage reads, the gateway rechecks the active revision, transcript
identity, source membership, and newer revisions before returning.

The default `require_latest=True` blocks new bundle construction while any
higher revision is pending, retrying, running, or failed, or a higher completed
revision has not become the active pointer. A higher superseded attempt does
not block the valid active revision. Previously published bundles may remain
active until replacement validation; loading does not unpublish them.
`require_latest=False` is reserved for explicit inspection of the still-active
prior result. It never bypasses artifact checks and must not be used for new
bundle extraction or delivery.

Any missing, corrupt, stale, failed, or superseded active input raises
`LearningProjectionUnavailable` with the message `learning projection unavailable`. Consumers
must stop extraction/delivery and use the existing classification recovery
workflow; they must never retry with a full/raw transcript. The gateway makes
no database writes, commits no caller changes, and logs no transcript text,
artifact keys, or provider diagnostics. Its short database read sessions end
before storage I/O. A returned DTO is a snapshot, so future publishers must
also validate their bundle/revision identity when activating a replacement.

Purpose-specific promotional research is an explicit separate path:

```python
from learning_projection import load_promotional_projection

promotion = load_promotional_projection(
    session=db.session, storage=storage, item_id=item_id,
    labels=("self_promotion", "cross_promotion"),
    purpose="asp-50-marketing-research",
)
```

Purpose is a nonempty policy-valid slug (up to 128 characters); labels must
contain only `self_promotion` and/or `cross_promotion`, without duplicates.
This returns the distinct immutable `promotional-analysis-projection/v1`
`PromotionalProjection`, which is rejected by the general citation API. It
reconstructs only the requested promotion records on demand from the
hash-validated canonical transcript and active policy, always with latest
revision gating. No promotional artifact is stored in the revision, and the
stored learning projection is not read for this research path. Neither path
loads the full text artifact as a fallback.

## Knowledge intake export

The optional `exporter` service is the KNOWLEDGE-INTAKE-0001 producer
`podcast`. Each night at 04:00 UTC it writes new and changed items as batches
into the Platform spool inbox `/srv/bezalel/intake/podcast/inbox/`, which the
Platform knowledge intake catalogs and delivers to Lamad and Ebenezer. Every
batch is validated before publication with the vendored contract oracle and a
real JSON Schema validator (`integrations/knowledge/vendor/`, pinned by `PIN`).

An item is exported when it is completed, its active classification revision is
completed, and its classifying source names a host. Each item becomes one
`podcast.episode` record and one `podcast.segment` record per passage of
consecutive editorial or uncertain segments. A segment joins the current
passage only while the passage stays within 60 seconds (from its first start to
the latest segment end so far) and 6,000 characters; a single segment is never
split, so a one-segment passage may exceed either limit. Transcript text is read
only through `load_active_knowledge_projection`, so commercials, promotions and
housekeeping never leave the application. Uncertain segments are exported
inside their passages and marked: each passage lists them in
`attributes.uncertain_segment_ids` (empty when none) and is labelled
`editorial`, `uncertain` or `mixed`, and each episode carries
`attributes.uncertain_segments`, the revision's count. When the confidence
threshold (editorial below 0.80, any other label below 0.95) made a segment
uncertain, the policy keeps the model's own label as the range's optional
`model_label`, and the passage maps that segment to it in
`attributes.uncertain_segment_labels`, for example
`{"seg-…": "paid_commercial", "seg-…": "editorial"}`, so a probable advert can be
told from a borderline editorial segment. Its keys are always a subset of
`uncertain_segment_ids`. The attribute is omitted when no uncertain segment in
the passage has a recorded label: one the model itself called `uncertain`, a
conflict between source rules, an override, or any policy stored before model
labels were kept (Claude-era and earlier router revisions; their labels are never
inferred, and a later reclassification records them). `model_label` is optional
under the unchanged `commercial-segment-policy/v1`, omitted from the stored JSON
when absent, so existing policies keep their bytes and hashes and no revision
becomes non-current; it is covered by the policy hash and the revision row like
every other range field, and one inconsistent with the range's confidence fails
closed. Code from before `model_label` rejects a policy that has one, so rolling
back past it fails those episodes' validation like the `model` rollback below.
Release check: exported content holds no excluded segment, every
uncertain segment in it is marked, and model labels name only marked segments
(`tests/test_knowledge_exporter.py::test_release_check_no_excluded_segment_leaves_and_every_uncertain_one_is_marked`). A newer
pending or failed revision postpones the item. Records name the show as
`source.publisher` (the YouTube channel for videos), set
`attributes.platform`, and list participants with their basis. A host or guest
correction, or a new classification revision, emits a new record that
supersedes the earlier one; record identifiers are never reused. Export state is
kept in `knowledge_export_batches` and `knowledge_item_exports`.

The contract requires `source.uri` on every record. It is the episode's own
link; an episode without one uses its enclosure URL, else the source's feed
URL. URLs whose query string carries a recognised credential parameter are
never stored, but a token in the URL path is not detected, so do not enable the
export while a private feed with a token in its path is subscribed: the token
would reach the Platform catalog.

Hosts come from the feed (`<podcast:person role="host">`, else
`<itunes:author>`, else `<itunes:owner>`) or the YouTube channel owner, and
podcast feeds refresh them on every discovery run. The administrator can name
hosts on the source page; administrator hosts win, and saving an empty list
restores the feed's. A source without a host is flagged on its page and its
items are not exported. Seed YouTube sources added before this feature once:

```bash
docker compose exec web flask --app app:create_app knowledge seed-hosts
```

Guests come from the administrator's list on the item page, else
`<podcast:person role="guest">` tags, else (when enabled) a restricted
model-router extraction (route `podcast.guests`) over the title and description
that runs on the classifier. The
extraction keeps only names that appear as whole words in the title or, taken
separately, in the description (compared after NFKC normalisation and case
folding), stores them as that text spells them, and marks them `inferred` with
a confidence; inferred guests below 0.5 are shown but not exported. The item
page's guest form is prefilled only with declared guests (administrator or
feed), never inferred ones, so saving it unchanged never turns a guess into an
administrator guest; saving an empty list records that the episode has no
guests. Saving the form does turn feed guests into administrator guests, even
unchanged: their basis becomes administrator-set and the item is exported
again. Saving the same administrator list again, or resetting an item that has
no administrator list, changes nothing. While extraction is enabled, an item
without declared guests waits (`awaiting_guests`) until its current title and
description have been extracted. An item whose extraction is still being
retried after a router failure (below) also waits, so it is not exported
without guests and then superseded; it is released once extraction completes or
is given up.

Extraction runs every `GUEST_EXTRACTION_INTERVAL_SECONDS` (default and minimum
3600: configuration validation rejects less, and `celeryconfig` refuses to load
with it) for up to `GUEST_EXTRACTION_MAX_ITEMS_PER_RUN` items. A PostgreSQL
advisory lock keeps runs from overlapping (a run that finds one active stops as
`busy`), and a run starts no call that could outlast its 10-minute deadline
(each call is bounded by `GUEST_EXTRACTION_TIMEOUT_SECONDS`, default 120, plus
the client's 10-second connect timeout and 90-second read margin; a timeout so
long that one call needs more than 10 minutes allows exactly one call per run).
Each title and description is extracted once. Invalid output
(`model_invalid_output`) is recorded as `failed` and never retried for that
input; the run moves on. A busy router (`router_busy`: in progress, rate
limited, timed out, unreachable, or no eligible model) stops the run without a
record, so the item is tried again next hour. A router failure reported by the
router itself (for example a 504 model timeout on an input the model never
finishes, or a model server that is down) is recorded as `failed` with
category `router_busy` and stops the run; the client reasons `router_failure`
and `provider_unavailable` are both recorded this way. That input is retried on
later runs, after every input never tried, so it cannot block them, and is given up after
three router failures (`MAX_ROUTER_FAILURES`, counted in
`guest_extractions.router_failures`). Authentication, configuration and
model-mismatch errors stop the run without a record. Before each call the run
re-reads today's (UTC) recorded calls and stops (`daily_limit`) at
`GUEST_EXTRACTION_MAX_CALLS_PER_DAY` (default 200, at most 2400); the one
unrecorded call that stops a run is not counted. Router calls cost nothing;
rows record `cost_microusd` 0 and the router model. Extraction runs on the
classifier's `classification` queue (concurrency 1), so each run can
delay classification by up to about 10 minutes (its run deadline). Only items whose
source names a host the exporter can identify are extracted, newest first.

Inputs that failed under Claude stay `failed` and are never tried again,
because each title and description is extracted once. To retry them with the
router, delete the Claude-era failed rows (their `model` is the old `claude-...`
model name; router rows record `ROUTER_EXPECTED_MODEL`). Run it inside the web
container, which reads the database URL from its own environment, so the URL
never appears on a command line:

```bash
docker compose exec -T web flask shell <<'PY'
from extensions import db
import sqlalchemy as sa
deleted = db.session.execute(sa.text(
    "DELETE FROM guest_extractions WHERE state = 'failed' AND model LIKE 'claude-%'")).rowcount
db.session.commit()
print("deleted", deleted)
PY
```

Rows deleted today no longer count toward today's call limit. Completed
Claude-era rows are kept; their guests stay valid until the title or
description changes.

Enable it with `GUEST_EXTRACTION_ENABLED=true`, then
recreate `beat`, `classifier`, and `exporter` (if it is running); the exporter
reads the same setting to decide whether items wait for guests. Once it is
enabled, every item otherwise ready to export that has no administrator or
feed guests waits in `awaiting_guests` until it has been extracted. Extraction
works newest first, at most `GUEST_EXTRACTION_MAX_ITEMS_PER_RUN` items an hour,
and at most `GUEST_EXTRACTION_MAX_CALLS_PER_DAY` calls a day, so an existing
library would wait for days. Run the
initial backfill (`knowledge export`, below) before setting `GUEST_EXTRACTION_ENABLED=true`,
or leave extraction off. While extraction is enabled, a run stopped by an
authentication, configuration, or model-mismatch error records nothing, so
waiting items, including every new one, stay in `awaiting_guests` until the
error is fixed; the hourly `knowledge_guest_extraction` log entry names the
reason in `stopped`. Turning extraction off (and recreating `exporter`)
releases waiting items on the next export run; those never extracted are
exported without inferred guests.

`knowledge status` does not count waiting items. Each export run reports them:
the nightly run's `knowledge_export_completed` (or `knowledge_export_failed`)
log entry has `skipped.awaiting_guests`, and `knowledge export` prints
`awaiting_guests=` with the count when there are any (it exports the other
eligible items as it does so).

To enable the export after the Platform has created the spool, find the
numeric GID of `intake-podcast`, set it and `KNOWLEDGE_EXPORT_ENABLED=true` in
`.env` (with `PODCAST_INTAKE_INBOX_HOST_PATH` if the inbox lives elsewhere),
then recreate `beat` and start the exporter:

```bash
getent group intake-podcast
docker compose up -d --force-recreate beat
COMPOSE_PROFILES=knowledge docker compose up -d exporter
docker compose exec exporter flask --app app:create_app knowledge status
```

Turn `KNOWLEDGE_EXPORT_ENABLED` (the beat schedule) and the `knowledge`
Compose profile (the `exporter` worker, the only consumer of the `knowledge`
queue) on and off together. With the schedule on and no exporter, beat queues
one message a night that waits unconsumed in Redis and is discarded as expired
(six hours) only when a worker finally receives it. `knowledge status` prints
batch counts by state, the number of exported items, and the number of sources
without a host.

The exporter mounts only the inbox, joins `PODCAST_INTAKE_GID`, and writes with
group read/write so the intake can claim batches. If the export is disabled,
the inbox is missing, is not a setgid directory of one of the exporter's groups,
is not writable, or its lock file cannot be opened, or another run holds the
lock, the run is skipped (`disabled`, `inbox_missing`, `inbox_group`,
`inbox_unwritable`, `busy`) with a `knowledge_export_skipped` warning and
nothing is written; transcription and classification never wait on it. Each
report counts the items it passed over by reason: `unchanged`, `no_host`,
`publisher_unnamed`, `awaiting_guests`, `projection_unavailable`,
`changed_during_export`, `too_large`, and `invalid_record` (an item whose
records fail the contract; it is logged as `knowledge_export_invalid_record`
and the rest of the batch is still written). A failed run
(`knowledge_export_failed`, `status=failed`) reports one of three reasons. A
contract error from the batch writer (`contract_error`) or a filesystem error
while writing (`write_failed`) marks its batch `failed`, except that a batch
already renamed into the inbox before the error stays `pending`.
`sequence_exhausted` means the next batch name for that UTC date would pass
sequence 999 (sequences continue after the highest one recorded, skipping names
present in the inbox); no batch is created, and the next UTC date starts again
from 001. Batches the run already wrote stay written. At the start of every run a `pending` batch found in the inbox becomes `written`; any other
becomes `abandoned` and its items are exported again. An unexpected error in the
nightly task is logged as `knowledge_export_failed` with its `error_type`.

To backfill the existing library in ordinary sequenced batches, run the export
command; `--max-batches` defaults to 20 per run, independent of
`KNOWLEDGE_EXPORT_MAX_BATCHES_PER_RUN` (default 5), which caps only the
nightly run:

```bash
COMPOSE_PROFILES=knowledge docker compose run --rm exporter flask --app app:create_app knowledge export --since 2015-01-01 --until 2026-12-31 --max-batches 20
```

The window filters on publication date. Without `--since`/`--until` every
eligible item is a candidate, including undated ones. Repeat until it reports
`batches=0`. Stop `exporter` with the other writers during restores and
upgrades (`docker compose stop exporter`).

## Commercial acceptance, evaluation, and observations

Run the offline commercial gate before promoting a classification policy:

```bash
.venv/bin/python -m pytest -q tests/test_commercial_classification_smoke.py tests/eval/test_commercial_policy_eval.py
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
git diff --check
```

No live model router, credentials, AWS, RSS, YouTube, Redis, or broker is needed.
The smoke test injects only external storage/classifier/broker/provider boundaries
and exercises real final-stage hooks, SQLite outboxes, worker checkpoints, artifact
validation, owner review routes, correction sweeps, and filtered loaders. It covers
both podcast and YouTube completed canonical items without reacquiring media.
The crash case kills the task after block 1 commits and before publication, then
reconciles/replays a multi-block revision without another model call for block 1.

All ten section-13 invariants are checked at their implemented boundaries:
canonical bytes/IDs/times/hashes remain unchanged; coverage is exact; citations
require editorial IDs; general payloads omit all excluded/uncertain content;
promotional research has a separate type rejected by the citation gateway;
rules stay source-scoped; corrections retain prior artifacts and revision lineage;
retries select one shared active revision; failures never return a full transcript;
and backlog classification never invokes media/providers. Summaries, teachings,
claims, evidence indexes, embeddings, immutable bundles, and actual Lamad,
Ebenezer, and Agent Factory publishers are deferred to Phases 2/3. Their eventual
release gates must extend these assertions through actual extraction and delivery.
The authorized owner review page intentionally reads canonical segments with a
validated policy; it is an audit interface, not a general-learning consumer.

`tests/fixtures/commercial-eval.json` is wholly synthetic, with independent truth
labels and supplied classifier outputs for every label, adjacent/repeated ads,
editorial introductions/teaching calls to action/closing summaries, and exact
confidence thresholds and ambiguous boundaries. The deterministic harness builds
real policies and projections; it measures policy behavior, not live-model accuracy.
Metrics are nested by numeric source ID and label. Precision is correct predicted
segments / predicted segments; recall is correct predicted segments / truth
segments. False-exclusion rate is truth-editorial segments withheld (including
uncertain) / truth-editorial segments. Uncertain-duration rate is uncertain segment
milliseconds / all segment milliseconds (gaps are excluded). A boundary is an
adjacent-segment label transition; accuracy uses matching boundaries / the union
of truth and predicted boundaries, also computed per label for its incident edges.
The source report retains truth/predicted boundary counts for inspection.
Reports contain only integer numerators/denominators and label/source identifiers;
divide counts to obtain rates and treat a zero denominator as undefined.
The release fixture allows zero editorial false exclusions. A policy-version
change fails until `expected_metrics.policy_version` and reviewed expected metrics
are updated together. Mutation tests check imperfect metrics and prevent changing
expected counts from accepting an editorial false exclusion.

Any later opt-in, administrator-reviewed real evaluation corpus must live only
under the Git-ignored `.runtime/eval/` directory with restricted local access.
Never commit its transcripts, annotations, classifier outputs, prompts, or
per-example results; never print or log them, including on test failure. Export
only aggregate counts by label/source ID. Keep real evaluation opt-in and separate
from the synthetic default suite; do not substitute private data into Git fixtures.

Task observations use the `tasks` logger at INFO with fixed event messages
`commercial_classification_prepare`, `_block`, `_finalize`, `_failed`, and
`_source_rule`. Configure a structured log handler to serialize only the
`commercial_event` extra field and the fixed event name. That field allows only
`item_id`, `source_id`, `revision_id`, `policy_version`, `stage_duration_ms`,
`label_counts`, `duration_ratios`, `review_required`, `retry_category`, and
`rule_match_counts`. The encoder takes integer item/source/revision identities,
uses the running code's policy version, and omits unknown retry categories and
unexpected count fields. Finalization counts use `editorial`, aggregated `excluded`
(the four noneditorial/nonuncertain labels), and `uncertain`; duration ratios use
`included`, `excluded`, and `uncertain`. Source-rule counts are cumulative `scanned`
and `matched` (items queued by that sweep); its revision ID identifies the
originating reviewed policy. Stage duration uses a monotonic clock. Events can
repeat on replay and are observations, not a billing ledger or activation signal.
Use durable revision/block state for timing and completion. Log-sink failure cannot
fail an otherwise committed checkpoint.

Never add transcript bodies, segment IDs, prompts, model output, source URLs,
private artifact keys, hashes, credentials, provider diagnostics, exception text,
or tracebacks to these events. The acceptance tests inspect log records and
structured fields against the allowlist and synthetic leak sentinels.

## Credentials and environment

Protect the checkout, Docker access, backups, and `.env` as administrator data.
Docker access can reveal container environment variables. Never commit `.env`,
paste secrets into command arguments, run `set -x` during setup, or share the
output of `docker compose config` / container inspection with secrets present.

For a new checkout, generate `.env` exclusively: this command refuses to
overwrite an existing file. Values are generated in the process and are never
printed or recorded as literal secrets in shell history.

```bash
umask 077
python3 - <<'PY'
import os
from pathlib import Path
import secrets
from urllib.parse import quote

password = secrets.token_urlsafe(36)
values = {
    'SECRET_KEY': secrets.token_urlsafe(48),
    'POSTGRES_PASSWORD': password,
    'DATABASE_URL': 'postgresql+psycopg2://podcasts:' + quote(password, safe='') + '@postgres:5432/podcasts',
}
lines = Path('.env.example').read_text().splitlines()
content = '\n'.join(
    line.split('=', 1)[0] + '=' + values[line.split('=', 1)[0]]
    if '=' in line and line.split('=', 1)[0] in values else line
    for line in lines
) + '\n'
descriptor = os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as destination:
    destination.write(content)
PY
chmod 600 .env
"${EDITOR:-vi}" .env
```

`chmod 600 .env` allows only its owner to read/write it. Preserve this permission
after editor replacement or restore. Keep `POSTGRES_DB=podcasts`,
`POSTGRES_USER=podcasts`, the internal database host `postgres:5432`,
`REDIS_URL=redis://redis:6379/0`, and
`RATELIMIT_STORAGE_URI=redis://redis:6379/1`. Do not source `.env` in a shell:
it is Compose environment syntax and contains unquoted values with spaces.
If using your own database password, percent-encode it with
`urllib.parse.quote(password, safe='')` only in `DATABASE_URL`; keep the raw
password in `POSTGRES_PASSWORD`. The generated URL-safe password avoids Compose
interpolation and URL punctuation problems. `.env.example` documents limits
and timeouts; leave the reviewed media versions unchanged during setup.

Configure these provider values in the editor:

- `AWS_S3_BUCKET_NAME=podcast-transcriptions-web-2025` is the only accepted
  bucket. It is an external private S3 bucket, never a Compose service. Keep S3
  Block Public Access enabled. `AWS_REGION=us-east-1` is the example setting;
  confirm the bucket's actual region in AWS and use that exact region for S3
  and Amazon Transcribe. Region mismatch fails closed. Do not substitute a
  different bucket or public endpoint.
- Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, with
  `AWS_SESSION_TOKEN` for temporary credentials. Boto3 uses its normal
  [credential chain](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html).
  Host `~/.aws` files are not mounted into the image; a host
  CLI login alone does not configure these containers. An available workload
  or instance role may supply credentials through the normal SDK chain; verify
  that it is reachable from the containers. Avoid competing credential sources
  and rotate temporary credentials before expiry.
- The AWS principal needs bucket location/list access, read/write access to
  the bucket's `items/*` objects, and Amazon Transcribe start/get/list job
  permissions in the configured region. Configure the bucket and principal so
  Amazon Transcribe can read the private input and write its result in the same
  bucket. Include encryption-key permissions if your bucket policy requires
  them. Startup checks probe location/access and list jobs; they do not prove
  that every later write or transcription is authorized.
- In the Google Cloud project, [enable **YouTube Data API v3**](https://developers.google.com/youtube/data/getting-started), create an API
  key, restrict its API access to **YouTube Data API v3**, and set application
  restrictions to the server's outbound public IP addresses where stable.
  These are server requests, so browser referrer restrictions are inappropriate.
  Put the key in `YOUTUBE_API_KEY`; check project quota and alerts. Public
  source discovery needs only this API key. Follow Google's
  [key restriction guidance](https://docs.cloud.google.com/docs/authentication/api-keys-best-practices).
  Account sign-in, browser cookies,
  and private/age-gated/account-only videos are unsupported.

Keep `SESSION_COOKIE_SECURE=true` for the public HTTPS hostname, even though
the tunnel connects to the origin over HTTP. For a temporary local HTTP browser
test only, set it to `false` and recreate `web`; restore `true` before public
use. HTTP curl health checks need no login cookie.

## Platform Model Router

Commercial classification and guest extraction call the Bezalel Platform Model
Router (`POST /v1/model-tasks`) as the subject `intelligence:podcast`, on the
local routes `podcast.classification` (32,768-token context, 8,192 output
tokens) and `podcast.guests` (1,024 output tokens). Both are served by
`bonsai2:27b-pq2_0`, the `reasoner` role. Every call is local only, `internal`,
temperature 0, and carries a JSON schema in the router's subset. Calls cost
nothing, so there are no dollar budgets. Claude-era revisions, policies and
guest rows stay readable: their old error categories display as before, and
their `claude` decision source remains valid. New revisions record the decision
source `model`.

Only `classifier` holds router credentials. They are the client bundle issued
for `intelligence:podcast` (bezalel-deploy `bin/bezalel-router-credential`),
kept in `.runtime/router/` (directory mode 700, files mode 600, owned by UID
1000, ignored by Git and the image build) and mounted read-only at `/router`:

| File | Use |
| --- | --- |
| `client.crt`, `client.key` | mTLS client certificate required by Caddy on `:18443` |
| `router-ca.crt` | Verifies the router's server certificate |
| `token` | Bearer token, read on every call; never printed, logged or put on argv |
| `token.expires_at` | Token expiry, checked by the upgrade script |

```bash
install -d -m 700 .runtime/router
for file in client.crt client.key router-ca.crt token token.expires_at; do
  install -m 600 "/path/to/intelligence-podcast-bundle/$file" .runtime/router/
done
```

The container runs as UID/GID 1000:1000, so the directory and files must be
readable by that identity. If the SSH user has a different UID/GID, have the
host administrator set ownership to 1000:1000 while keeping the modes. For
another protected host directory set `ROUTER_CREDENTIALS_HOST_PATH` in the
Compose environment; Compose refuses to start `classifier` if the directory is
missing. Settings: `ROUTER_URL` (default `https://192.168.68.200:18443`),
`ROUTER_EXPECTED_MODEL` (default `bonsai2:27b-pq2_0`; any other
`selected_model` fails as `model_mismatch`), `CLASSIFIER_TIMEOUT_SECONDS` (540,
5 to 900: the router latency budget per block),
`CLASSIFIER_MAX_REVISION_SECONDS` (14400, counting generation time only),
`CLASSIFIER_MAX_OUTPUT_BYTES`, `GUEST_EXTRACTION_TIMEOUT_SECONDS` (120, 5 to
600) and `GUEST_EXTRACTION_MAX_CALLS_PER_DAY` (200, at most 2400). The container
paths `ROUTER_TOKEN_FILE`, `ROUTER_CA_FILE`, `ROUTER_CLIENT_CERT_FILE` and
`ROUTER_CLIENT_KEY_FILE` default to the mount. A retired setting (any former
`CLAUDE_*` setting, `GUEST_EXTRACTION_MAX_BUDGET_USD` or
`GUEST_EXTRACTION_DAILY_BUDGET_USD`) in the environment stops every service at
startup: remove it from `.env`.

The podcast Compose network is pinned to `192.168.144.0/20` because the
Platform firewall admits only that subnet to the router
(`platform_router_podcast_client_cidrs` in bezalel-deploy). Changing the subnet
recreates the network and needs the matching firewall change.

Router failures become closed categories:

| Router response | Category | Effect |
| --- | --- | --- |
| 429, 202 (still running), 500, 503, 504, other 502, timeout, refused or reset connection, Celery soft time limit | `router_busy` | Block back to pending, revision `retrying`; retried after 30 s doubling to 10 min; never counts as a failed attempt |
| A router 5xx `MODEL_PROVIDER_UNAVAILABLE` (client reason `provider_unavailable`) after the claim ran `INPUT_FAILURE_MIN_SECONDS` (60 s) or more, the third time on the same block (`MAX_BLOCK_PROVIDER_FAILURES`) | `model_unavailable` | Revision fails; `classification retry --item-id <id>` after the cause is fixed. Other coded 5xx (reason `router_failure`) and faster failures stay `router_busy` |
| 502 `MODEL_OUTPUT_INVALID`, or output failing the Python checks | `model_invalid_output` | Retried once under a new key (temperature 0 replays the same answer); the revision fails once its counted failures reach 2 (`MAX_INVALID_OUTPUT_ATTEMPTS`). Other failures still count toward `MAX_CLASSIFICATION_ATTEMPTS` (5) |
| 401, 403, certificate verification failure or TLS alert, unusable token | `model_auth` | Revision fails |
| Other 4xx, request too large | `model_config` | Revision fails |
| `selected_model` not `ROUTER_EXPECTED_MODEL` | `model_mismatch` | Revision fails |

The classifier prompt (`commercial-segment-classifier/v2`) asks the model to list
only the target segments that are not plain editorial content. Every target
segment it does not list is recorded as editorial (decision source `model`,
confidence 0.80 `IMPLIED_EDITORIAL_CONFIDENCE`, reason `substantive_content`);
an empty list means the whole block is editorial, except stretches inside a
classifier signal's range, which get 0.50. Ranges must be in order and must not
overlap. A target segment inside the range of a classifier signal that the
model leaves out gets confidence 0.50 (`IMPLIED_SIGNALLED_CONFIDENCE`),
so the threshold stores it as `uncertain` with `model_label` `editorial` and
review is required. Under prompt v2, 0.80 and 0.50 are how implied decisions
are recognised in stored artifacts; the model can also emit those values, so
this is a heuristic. Claude-era policies keep `commercial-segment-classifier/v1`
and stay valid.

Each block call uses the idempotency key
`podcast.classification:rev-<revision>:block-<ordinal>:g<generation>`. The
router stores every outcome it completes, failures included, and replays it for
the same key. The generation (`classification_blocks.request_generation`)
therefore advances after any outcome the router may have stored. It stays the
same after 429, 202, a timeout or a refused connection, so a lost response is
replayed instead of generated twice. A key still answered 202 long after it was
first sent (`CLASSIFIER_TIMEOUT_SECONDS` plus about 3.5 minutes, for example
after a router restart mid-generation) also advances.
`CLASSIFIER_MAX_REVISION_SECONDS` sums the durations of completed block calls,
so time spent waiting for a busy router does not count. The classifier runs one
task at a time, so the application never has more than one router request in
flight.

Busy retries end only for one case: a block on which the model itself fails
slowly three times (`classification_blocks.provider_failures`) fails as
`model_unavailable`, so one bad input cannot stall the queue or keep tripping the
shared Forge circuit breaker. Otherwise they never end by design: a revision
waiting on a busy router stays `retrying` with `router_busy` until the router answers. To spot them, run
`docker compose exec classifier flask --app app:create_app classification status`
and read `router_busy=<count>`, `oldest_busy_key_sent=` and
`oldest_busy_updated=`; the item page shows "Model router busy". A 503
`MODEL_NO_ELIGIBLE_CANDIDATE` caused by a missing or unloaded model is also
`router_busy`: the block retries at most every 10 minutes, and the router stores
a failed invocation for each try (each try uses a new key generation), so fix
the model on the router rather than waiting it out.

Classifier startup runs
`system check --services database,redis,s3,claude_absent,router`.
`claude_absent` fails on any `CLAUDE*` variable, a `claude` executable, or the
old package. `router` fails unless `GET /readyz` answers `ready` over mTLS and
the token file is usable, within the router probe's own 12-second bound (other
probes keep theirs). A router whose provider is down answers 503 there, so the
classifier restarts until the router is ready. Check and smoke-test the router
path:

```bash
docker compose exec classifier flask --app app:create_app system check --services database,redis,s3,claude_absent,router
docker compose exec classifier flask --app app:create_app classification router-smoke
docker compose exec classifier flask --app app:create_app classification router-smoke --wrong-client-cert
```

`router-smoke` sends one synthetic window and prints
`ready=1 model=<model> label=<label> seconds=<n>`. It touches no database or
storage data. "Router is not ready" means `/readyz` did not answer `ready`.
`Router smoke failed: <category> reason=<reason>` names the closed category.
With `--wrong-client-cert` it sends the same request with a throwaway client
certificate the router never issued and passes only when the router refuses it
as `model_auth` (it prints `wrong-client-cert=model_auth reason=tls`, or
`unauthorized`/`forbidden` when the proxy answers); any other result exits
nonzero. While the classifier is processing, its one inference slot is busy and
a smoke call can answer `router_busy` (429): stop `classifier` for the smoke
test, or repeat it.

After the router path is verified (a successful `router-smoke` and a completed
revision), archive the retired Claude configuration directory privately or
delete it; nothing reads it any more:

```bash
rm -rf -- .runtime/claude
```

### Comparing with Claude and reclassifying

`classification compare` takes a fixed-seed sample of Claude-classified episodes
spread across shows. It classifies at most `--max-blocks` windows of each with
the router model and reports, per episode and in total: agreement by Claude
label; content Claude called editorial that the router would exclude, and the
reverse; the share the router marks uncertain; and seconds per call. Claude's
side is the label stored in its policy, which was already thresholded (a Claude
near-miss is stored as `uncertain`). The `raw_*` figures compare that with the
router's raw label; the other figures compare both after the confidence
threshold, as a revision would store them, and `threshold_to_uncertain` counts
the router labels the threshold turned into `uncertain`
(`threshold_to_uncertain_by_label` splits them by the router's raw label). A busy router is
retried with the worker's back-off (at most about 15.5 minutes per window), then
the episode is reported as skipped (`busy_skipped`); after three skipped
episodes in a row the whole run stops with `Comparison stopped: router_busy`
and exit status 1. Invalid model output is counted and the window skipped. It
stores nothing and prints JSON with item and source IDs only. It needs the router
credentials, so run it in a `classifier` container, while the classifier
service is stopped so it is the only router caller (the reclassify script does
this). `--seed` (default 20261026) and `--max-blocks` (default 4, at most 50)
are optional; `--sample` is 1 to 100:

```bash
docker compose stop classifier
docker compose run --rm --no-deps -T --entrypoint flask classifier --app app:create_app classification compare --sample 20 > ~/Bezalel/knowledge-intake-work/evidence/podcast-router-compare.json
docker compose start classifier
```

The report is information only. `classification reclassify-all [--limit N]`
creates, newest first, a `policy_upgrade` revision for every completed episode
without a revision from the current classifier, including episodes already
exported. Old revisions stay for history. New revisions wait as `pending` with
error category `reclassify_queued`. The reconciler never publishes them. The
classifier releases the oldest one whenever no other revision is pending,
running or retrying. Rerunning queues nothing twice: an episode that already
has its reclassification revision, in any state, is not a candidate, so reruns
and `--limit` move on to episodes not yet queued. It prints
`candidates= queued= existing= busy= failed= released=`; `queued` counts only
revisions newly held by this run, and `existing` counts candidates that were
classified or queued by something else while the command waited for them.

```bash
docker compose exec classifier flask --app app:create_app classification reclassify-all
docker compose exec classifier flask --app app:create_app classification status
```

`status` shows `reclassify_queued=<n>` counting down, and `in_flight=<n>`
with `oldest_in_flight_updated=` for the revisions that are pending, running
or retrying and not held (normally the one being worked on). While an episode
waits, the exporter skips it (`projection_unavailable`). When its new revision
completes, the nightly export sends it as a replacement that supersedes the
earlier record.

A reclassification that fails (`failed` in `status`) is not queued again by
`reclassify-all`, and the classifier moves on to the next held episode. Until
it is retried, that episode cannot be exported (the exporter needs its latest
revision), while the record already exported for it stays live. Retry it with
`classification retry --item-id <id>`.

`classification reclassify-cancel` (stop `classifier` first) supersedes every
held revision (`reclassify_queued`) and prints `cancelled=<n>`; the episodes
keep their current revision and export as before. After a prompt or model
change, cancel the old queue, then run `reclassify-all` again: it queues under
the new classifier's key prefix. Notes:

- Under the same classifier provenance `reclassify-all` skips cancelled items
  (their key still matches) and `classification retry` does not apply, because
  the revision is superseded, not failed. A changed prompt, model or route
  gives a new key prefix, and `reclassify-all` queues them again (restoring
  prompt v1 is such a change).
- Before running `reclassify-all` under a new provenance, let any released or
  running revision finish, or accept that it will be superseded.
- Cancelled items export normally again on the next nightly run (they have no
  newer held revision) until `reclassify-all` holds them again.

### Upgrading from Claude classification

Upgrading an existing deployment applies migration `0004_router_classification`,
which only adds columns (`classification_blocks.request_generation`,
`request_generation_started_at` and `guest_extractions.router_failures`), and
`0005_block_provider_failures`, which adds
`classification_blocks.provider_failures`. Their `ALTER TABLE` statements need
an exclusive lock, so stop the writers (`web`, `worker`, `beat`, `classifier`,
`exporter`) before migrating. The operator script
`~/Bezalel/knowledge-intake-work/podcast-router-upgrade.sh` performs the
backup, build, migration, startup and smoke checks. A revision that failed as
`model_unavailable` is retried with `classification retry --item-id <id>` once
the model or the input problem is fixed.

Run `classification reclassify-all` (or keep the exporter stopped)
before the next nightly export after the upgrade. The episode record now carries
`attributes.uncertain_segments`, so every already-exported episode changes. A
nightly export before reclassification therefore exports the library twice:
once for that change, and again as each new revision completes. Queued episodes
are skipped until their new revision completes. An export that runs before
reclassification sends Claude-era revisions with their uncertain text, which
earlier exports withheld, marked as uncertain.

Rolling back the code is not a plain image rollback. The previous code does not
know the decision source `model`, so it rejects every policy that contains it:
router-classified episodes fail validation on the review page and in the
exporter. The previous code has no command that reclassifies an episode whose
active revision the router produced. The documented rollback is therefore to
restore the PostgreSQL backup taken before the upgrade (the upgrade script
writes it to `~/PodcastTranscriptionWeb-backups/postgres-<stamp>-*.dump`),
losing every change since. In this order:

1. Stop the writers.
2. Downgrade migrations `0005` and `0004` with the current image, which is the
   only one that has them. This drops the four columns.
3. Restore the pre-upgrade backup as described in
   [PostgreSQL backup and restore](#postgresql-backup-and-restore).
4. Check out the previous revision and restore the image. The upgrade script
   tagged the pre-upgrade image `podcast-transcription:pre-router`; if
   `docker image inspect podcast-transcription:pre-router` still finds it, run
   `docker tag podcast-transcription:pre-router podcast-transcription:local`
   instead of rebuilding. Otherwise rebuild the images.
5. Recreate the Compose network before starting. The current Compose file pins
   `podcasttranscriptionweb_default` to `192.168.144.0/20`; the previous
   revision's file does not, so run `docker compose down --remove-orphans`
   (never `-v`, which deletes the database volume) and let Compose create the
   network again. Then start the services.
6. Restore `.runtime/claude` from the archive the upgrade script suggested
   (`~/PodcastTranscriptionWeb-backups/claude-runtime-<stamp>.tar.gz`), or
   authenticate the Claude CLI again, because the previous classifier needs it.

```bash
docker compose stop web worker beat classifier exporter
docker compose run --rm --no-deps migrate flask --app app:create_app db downgrade 0003_knowledge_export
# Restore the pre-upgrade backup, then:
git checkout <previous revision>
docker image inspect podcast-transcription:pre-router &&
  docker tag podcast-transcription:pre-router podcast-transcription:local ||
  docker compose build
docker compose down --remove-orphans   # never -v
(umask 077 && tar -C .runtime -xzf ~/PodcastTranscriptionWeb-backups/claude-runtime-<stamp>.tar.gz)
```

### Rolling back Plan 7c

Plan 7c adds migration `0005_block_provider_failures` and writes policies with
`prompt_version` `commercial-segment-classifier/v2`, which the 7b code rejects
because it accepts only v1. Rolling back 7c to 7b therefore also needs a
restore, in this order: stop the writers; run
`db downgrade 0004_router_classification` with the 7c image; restore the
pre-7c backup (the 7c upgrade script writes
`~/PodcastTranscriptionWeb-backups/postgres-<stamp>-pre7c-*.dump`); check out
the 7b revision and rebuild, or tag the saved image with
`docker tag podcast-transcription:pre-7c podcast-transcription:local`; then
start the services.

Upgrading again recreates `request_generation` as 0 for every block, so keys
from generation `g0` repeat and the router may replay the outcome it stored for
them earlier; a replayed failure advances the key and counts as one attempt.

## Build, migrate, start, and provision the administrator

Build and migrate, then install the router credentials (see
[Platform Model Router](#platform-model-router)):

```bash
docker compose version
docker compose config --quiet
docker compose build
docker compose up -d --wait postgres redis
docker compose run --rm --no-deps migrate
```

Then start the services:

```bash
docker compose up -d
docker compose ps -a
docker compose logs --tail=100 migrate web worker beat classifier
```

`migrate` runs `flask --app app:create_app db upgrade`. Compose also gates
application startup on a successful migration. The web and worker entrypoints
check the database schema, Redis, S3, Transcribe, YouTube API, yt-dlp, and Deno
before starting; beat checks database, Redis, and its schedule. Classifier
startup pins `PYTHONPATH=/app` and checks database, Redis, S3, the absence of
any Claude remnant (`claude_absent`) and router readiness (`router`) before
executing Celery. A failure prevents the classification consumer from starting
and emits only the component names. If a provider
or credential fails, fix it and rerun `docker compose up -d`. Inspect logs and
readiness as described below; never bypass these checks to hide a failure.

Create the sole administrator through interactive prompts:

```bash
docker compose exec web flask --app app:create_app admin create
```

Enter its email and a password of at least 14 characters. Password input and
confirmation are hidden; no password flag exists. Creating another
administrator fails. Reset the existing administrator password with:

```bash
docker compose exec web flask --app app:create_app admin reset-password
```

If web cannot start, the same CLI is available without its startup entrypoint
after PostgreSQL is healthy and migrations succeed:

```bash
docker compose run --rm --no-deps --entrypoint flask web --app app:create_app admin reset-password
```

The fallback still requires valid application configuration and database
access. Use `admin create` instead of `admin reset-password` only for the first
administrator. Open `https://podcasts.ennovocon.com/login` after configuring
the external tunnel's public hostname and origin `http://localhost:8005`.

## Sources, backfills, and transcripts

Log in and use **Add Source** on `/dashboard`. Paste one public Apple Podcasts
page, RSS feed, or YouTube URL: video, Shorts, channel ID, `@handle`, `/user/`
channel, or playlist. Apple pages resolve to RSS. Use a video URL without a
playlist selector or a separate playlist URL; mixed video/playlist selectors,
account playlists, `/c/` vanity URLs, private media, and local-network URLs are
unsupported. Adding the same canonical source again reports a conflict.

The first import requests the newest 25 items. Scheduled discovery runs hourly.
Open the source page to see discovery health, imported items, and backfills.
Choose exactly one backfill selector: a count (1–5000), an inclusive date range,
or one episode/video URL belonging to that source. Leave other selector fields
empty. RSS history extends only as far as the publisher's current feed; the UI
reports limited history. YouTube tasks page in bounded batches and resume from
stored progress. Reload the source page for current counts. Cancel an active
backfill with its confirmation control; already imported items and artifacts
remain.

Open an item to see its processing status, transcript origin, language, and
available transcript. Creator captions are preferred, then automatic captions;
audio goes through Amazon Transcribe when usable captions are absent. The
application serves authenticated transcript views; S3 stays private.
Completed transcript and segment artifacts survive a worker restart.

## Status, health, and logs

```bash
docker compose ps -a
curl --fail --show-error http://localhost:8005/healthz
curl --show-error --max-time 110 --write-out '\nHTTP %{http_code}\n' http://localhost:8005/readyz
docker compose exec web flask --app app:create_app system check
docker compose logs --tail=200 web worker beat classifier
docker compose logs --follow --tail=100 worker
```

Stop log following with Ctrl-C. `/healthz` is process liveness and returns
`{"status":"ok"}`; it does not establish provider health. `/readyz` returns
component names mapped to `ok`/`failed`, with HTTP 200 or 503. Results are
cached for 15 seconds and each probe is bounded; a complete cold check can take
about 90 seconds. The CLI returns a nonzero exit code when any selected probe
fails. A targeted check is:

```bash
docker compose exec web flask --app app:create_app system check --services database,redis,s3,transcribe,youtube,yt_dlp,deno,worker,beat
```

The web `/readyz` deliberately does not probe the model router: web has no
router credentials, and web, migrations, beat and the media worker run without
them. The classifier's startup guard protects its own consumer; web readiness
alone does not establish classifier health. Verify the separate service and its
named worker:

```bash
docker compose ps -a classifier
docker compose logs --tail=100 classifier
docker compose exec classifier celery -A tasks.celery_app inspect --timeout 5 --destination "classifier@$(docker compose exec -T classifier hostname)" ping
docker compose exec classifier flask --app app:create_app system check --services database,redis,s3,claude_absent,router
```

The inspect destination must be the classifier hostname shown in its startup
logs (`classifier@<container-hostname>`); it prevents a media-worker reply from
being mistaken for classifier health. When the router credentials are rotated,
replace the files in `.runtime/router/` and run `docker compose restart
classifier`. The token is read on every call, so a token-only rotation needs no
restart.

The `beat` probe validates the configured hourly task; it is not a live
scheduler heartbeat. Confirm that the beat container is running and its logs
show scheduling. The worker probe pings a live worker. S3/Transcribe/YouTube
probes make bounded provider requests; the YouTube probe consumes API quota.
For failed components, check migrations/database, Redis, credential expiry and
IAM/bucket region, Google API enablement/key restrictions/quota, pinned tool
versions, or worker/beat logs respectively. Public health output omits provider
details; review local logs without sharing credentials or source data.

## Worker retry and recovery

Transient processing failures retry with exponential delay and jitter, then
become `deferred` after bounded attempts. Permanent failures become `failed`
or `unavailable`. After fixing the cause, open the item and use **Retry this
item** for those statuses. Retrying uses existing valid artifacts and the
recorded job identity. It requires an enabled source and refuses an item being
processed. Do not edit processing rows or delete S3 artifacts to force a retry.

For a stopped/crashed worker or scheduler:

```bash
docker compose up -d redis worker beat
docker compose logs --tail=100 worker beat
docker compose exec worker celery -A tasks.celery_app inspect --timeout 5 ping
```

Tasks acknowledge late; a worker lost during processing can be redelivered.
Database stage records and item locks protect active work and reuse valid
artifacts. Hourly discovery also replays committed pending dispatches and
pending/running backfills. To request that recovery scan immediately, once
dependencies are healthy:

```bash
docker compose exec worker celery -A tasks.celery_app call tasks.check_all_sources
```

This queues work and prints a task ID, not a completion result. Watch worker
logs and the UI. Redis has no named durable Compose volume. Its image declares
anonymous `/data` storage that can be retained during container recreation;
never assume an ordinary replacement starts with empty queues. Redis state is
not a durable recovery source: volume loss or renewal can lose broker messages.
Database state and S3 artifacts remain, but a lost message
that was already marked dispatched may need an explicit operator replay. For
an item stuck processing after its former worker is confirmed stopped, obtain
the numeric item ID from `/items/<id>` and replay the durable pipeline:

```bash
read -r -p 'Confirmed abandoned item ID: ' recovery_item_id
if [[ "$recovery_item_id" =~ ^[1-9][0-9]*$ ]]; then
  docker compose exec worker celery -A tasks.celery_app call tasks.process_item --args "[$recovery_item_id]"
else
  printf '%s\n' 'Enter a positive numeric item ID.' >&2
fi
unset recovery_item_id
```

Do not replay a still-running provider task merely because it is slow.
Amazon Transcribe jobs have deterministic names, so the pipeline checks and
reuses the existing job. Monitor the item after replay; escalating repeated
failures requires examining its safe error category and dependencies.

## Controlled live verification

Run the deterministic acceptance proof before using provider credentials:

```bash
.venv/bin/python -m pytest tests/test_phase1_smoke.py -q
.venv/bin/python -m pytest -q
```

The smoke proof uses the real administrator CLI, CSRF-protected login and API,
RSS and YouTube adapters, durable discovery/backfill tasks, caption normalization,
private storage wrapper, Amazon job logic, and authenticated transcript views.
Only HTTP, downloader execution, AWS SDK transport, and queue delivery are stubbed.
It proves 55 canonical items, 84 source memberships, three backfill selectors,
111 processing stages attempted once, origin/hash/citation lineage, and artifact
reuse after scheduled discovery and task replay. Scheduled discovery preserves
the initial observed-history baseline: older items omitted by newest-25 require
an explicit backfill. Count backfills accept new source memberships, excluding
already imported and unavailable items. The proof blocks socket access.

Live tests require both explicit selection of `tests/live/test_youtube_live.py`
and `RUN_LIVE_YOUTUBE=1`. An ordinary full-suite run skips them even if the opt-in
environment remains set. Each test skips clearly when its settings are missing.
Export credentials privately through your normal environment management; pytest
does not load `.env`. Never put credential values in command arguments or enable
verbose HTTP/debug logs. Use this bounded invocation without captured output or
local-variable tracebacks:

```bash
RUN_LIVE_YOUTUBE=1 .venv/bin/python -m pytest tests/live/test_youtube_live.py -q -rs --tb=short --show-capture=no
```

Set `YOUTUBE_API_KEY` and `AWS_REGION` to the configured bucket region. Select a
public, completed video at most 300 seconds long in `LIVE_YOUTUBE_VIDEO_URL` and
set `LIVE_YOUTUBE_EXPECTED_ORIGIN` to `creator_caption` or `automatic_caption`.
The check downloads the highest usable preferred-language captions, normalizes
them, and verifies timestamp citations. Run once for each caption origin using
deliberately chosen videos. No artifact is uploaded by this check.

Set `LIVE_YOUTUBE_CHANNEL_URL` and `LIVE_YOUTUBE_PLAYLIST_URL` to small public
sources with at most ten discovery pages; prefer 26–50 videos so historical
progress is observable. The checks use disposable SQLite state, import the
newest 25 (or all available when fewer), compare identities against a second
scan, request a 26-item count backfill, and replay it without duplicate rows.
They never dispatch to your running worker. Discovery and backfill consume quota;
avoid large channels and playlists. Set `LIVE_RSS_FEED_URL` to a small public
feed to resolve and discover at most 25 episodes without downloading audio.

Amazon fallback is a separate deliberate action: set
`RUN_LIVE_AWS_TRANSCRIBE=1`, valid AWS SDK credentials, and
`LIVE_YOUTUBE_FALLBACK_URL` to a caption-free public video at most 300 seconds
long. The check refuses videos with preferred-language captions, uploads private
audio to `podcast-transcriptions-web-2025`, runs Amazon Transcribe, reads its
private result, normalizes it, and checks deterministic job replay. This incurs
provider charges and leaves private audio, result, transcript, and segment
artifacts plus a Transcribe job for administrator review. It does not delete
provider evidence automatically. Record only status, origin, hashes, row counts,
and job success privately; never copy transcript bodies, keys, signed URLs, or
credentials into tickets, terminal output, or shared logs.

After live checks, verify the actual seven-service stack with the health/readiness
commands above. Add the selected video, RSS, channel, and playlist through the
authenticated UI, confirm origin labels and backfill progress, and replay
discovery. Inspect UI and local logs privately for any credential, private key,
URL, or transcript-body leak. Live adapter checks alone do not prove a running
worker, full readiness, browser behavior, or S3/Transcribe write permissions for
every deployed container. Without credentials, report those requirements as
unverified; passing the offline proof does not establish live provider success.

For a disposable Docker acceptance run, first inspect `docker compose ls`,
`docker ps`, and volumes read-only. Choose a fresh random project name and a
0600 temporary env file outside the checkout. Supply a temporary Compose override
that replaces each app service's `env_file` with that path and uses a unique
image tag; `--env-file` alone does not replace the service-level `.env` reference.
Use the same explicit project name, files, and env file for every command.
Use no existing application volume, router credential directory, or administrator.
Override the classifier's credential bind with a unique private disposable host
directory (owned by 1000:1000, mode 700), even when testing its failure without
credentials. Never reuse the deployed router bundle. Verify migration head
`0005_block_provider_failures`, provision a temporary sole admin, and record
all sixteen table counts: `admins`, `content_sources`, `content_items`,
`source_items`, `backfill_requests`, `discovery_runs`, `discovery_candidates`,
`processing_jobs`, `classification_revisions`, `classification_blocks`,
`segment_overrides`, `source_classification_rules`, `classification_sweeps`,
`guest_extractions`, `knowledge_export_batches`, and `knowledge_item_exports`
(plus Alembic's `alembic_version` metadata table). Perform ordinary project
`down`/`up` without volume deletion, then compare counts. Without router
credentials, confirm only the classifier fails closed and the media/web
services remain independent; router classification is a separately
authorized live check. Only web may publish loopback port 8005; if occupied, document an isolated
loopback port override. Full readiness requires real configured providers. Clean
up only resources carrying that exact disposable project label and its temporary
files. Never remove volumes from the deployed application to run this gate.

## Reviewed upgrades

Record the current Git revision and take a database backup before upgrading.
Review release notes and security changes before altering dependencies. Current
pins are `yt-dlp[default]==2026.8.19` in `requirements.txt` and Deno `2.9.7`;
yt-dlp reports `2026.08.19` at runtime. The image carries no Claude CLI or Node
(the build fails if `/usr/local/bin/claude` or `/usr/local/bin/node` exists).
The router model pin is `ROUTER_EXPECTED_MODEL`. Version expectations also live
in `config.py`, `.env.example`, `youtube_media.py`, `system_checks.py`, and
their tests. Update all applicable pins, runtime checks,
and the deployed `.env` together in a reviewed change. Do not enable runtime
self-updates, plugins, remote components, external configs, or browser cookies.

Run the offline suite using a development virtual environment:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
docker compose build --pull
```

On the deployed checkout, pause writers, migrate the new image, then restart:

```bash
docker compose stop web worker beat classifier exporter
docker compose run --rm --no-deps migrate
docker compose up -d
docker compose ps -a
docker compose exec web flask --app app:create_app system check
```

An image rollback cannot undo a schema migration. Review migration compatibility
before returning to a prior revision; for the router upgrade see
[Upgrading from Claude classification](#upgrading-from-claude-classification).
`docker compose up -d` does not start the optional exporter; start it again
with `COMPOSE_PROFILES=knowledge docker compose up -d exporter` if it was
running. After deployment, use one short public
video through the UI for a separately approved live smoke check; confirm
caption provenance or private S3/audio fallback and the transcript. Live
provider checks use quota and may incur charges; they are outside the offline
test suite.

## PostgreSQL backup and restore

The named `postgres_data` volume holds administrator credentials, sources,
memberships, discovery/backfill state, and processing records. Take regular
backups and keep encrypted off-host copies. A backup contains private data.
Keep the matching Git revision and a separately protected environment/credential
recovery record. Keep the same checkout/project identity when using the volume;
a different Compose project name creates a different volume.

The default backup directory is a sibling of the checkout, named with a
`-backups` suffix. Optionally set `PODCAST_BACKUP_DIR` to a dedicated directory
outside the checkout. The command resolves symlinks and rejects the checkout,
its descendants, and its ancestors before creating files or changing
permissions. Use a directory owned by this administrator; do not point it at a
shared directory. Backups use directory mode 700 and file mode 600. Git and
Docker also exclude `backups/` and `*.dump` as defense in depth if files are
accidentally copied into the checkout; do not override these exclusions.

For a consistent database checkpoint, pause writers (allow active work to stop)
and make a custom-format dump. `--entrypoint` is unnecessary for PostgreSQL
exec; its environment provides the internal database identity:

```bash
(
  set -eu
  umask 077
  checkout_dir="$(pwd -P)"
  backup_dir="$(realpath -m -- "${PODCAST_BACKUP_DIR:-${checkout_dir}-backups}")"
  if [ "$backup_dir" = / ]; then
    printf '%s\n' 'Use a dedicated backup directory, not the filesystem root.' >&2
    exit 1
  fi
  case "$backup_dir/" in
    "$checkout_dir/"*) printf '%s\n' 'Backup directory must be outside the checkout.' >&2; exit 1 ;;
  esac
  case "$checkout_dir/" in
    "$backup_dir/"*) printf '%s\n' 'Backup directory must not be an ancestor of the checkout.' >&2; exit 1 ;;
  esac
  mkdir -p -- "$backup_dir"
  chmod 700 -- "$backup_dir"
  docker compose stop web worker beat classifier
  backup_file="$(mktemp "$backup_dir/postgres-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.dump")"
  if docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$backup_file" && docker compose exec -T postgres pg_restore --list < "$backup_file" > /dev/null; then
    chmod 600 -- "$backup_file"
    printf 'Backup saved: %s\n' "$backup_file"
    docker compose up -d
  else
    printf 'Backup failed; writers remain stopped. Do not use partial file: %s\n' "$backup_file" >&2
    exit 1
  fi
)
```

Verify command exit codes, copy a successful dump to protected off-host storage,
and test restoration periodically. `pg_restore --list` checks archive structure,
not a full restore. Database backups do not include S3 objects. Protect the
external bucket separately with the administrator's versioning/retention and
backup policy; restoring a database without its matching artifacts leaves
broken transcript references.

Restore only to the intended deployment during a maintenance window. This
replaces the current database's contents; first back it up. Stop writers,
select a known successful dump, inspect its archive, and only then run the
restore command. No volume deletion is required:

```bash
docker compose stop web worker beat classifier
docker compose up -d --wait postgres redis
read -r -p 'Path to the reviewed PostgreSQL dump: ' restore_file
if [ -f "$restore_file" ]; then
  docker compose exec -T postgres pg_restore --list < "$restore_file"
else
  printf '%s\n' 'Dump not found. Stop here.' >&2
fi
```

After reviewing the file and confirming it is the intended replacement:

```bash
if [ -f "$restore_file" ]; then
  docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --single-transaction --exit-on-error' < "$restore_file"
fi
```

Proceed only if restore exits successfully. Redis may still contain messages
from the replaced database. With web, worker, beat, and classifier stopped, renew Redis's
anonymous volume, explicitly clear all Redis databases, and verify empty state
before migrations or writers restart. This deliberately discards stale broker,
result, and rate-limit state. Ordinary container recreation can retain the
image-declared anonymous `/data` volume and is insufficient. All commands are
in a subshell that stops on failure; do not resume writers if any step fails:

```bash
unset restore_file
(
  set -euo pipefail
  docker compose stop web worker beat classifier
  docker compose up -d --force-recreate --renew-anon-volumes --wait redis
  test "$(docker compose exec -T redis redis-cli FLUSHALL)" = OK
  test "$(docker compose exec -T redis redis-cli DBSIZE)" = 0
  redis_keyspace="$(docker compose exec -T redis redis-cli INFO keyspace | tr -d '\r')"
  [[ "$redis_keyspace" == *'# Keyspace'* ]]
  if printf '%s\n' "$redis_keyspace" | grep -q '^db[0-9]'; then
    printf '%s\n' 'Redis is not empty; keep writers stopped.' >&2
    exit 1
  fi
  docker compose run --rm --no-deps migrate
  docker compose up -d
  docker compose exec web flask --app app:create_app system check
)
```

Run the recovery scan above, inspect source/backfill state and sample
transcripts, and reconcile orphaned processing with explicit item replay.
Restore to the matching application revision before applying later migrations.

## Secret rotation, persistence, and shutdown

Change the administrator password through `admin reset-password`. To rotate
`SECRET_KEY`, generate a fresh value in a local editor workflow without printing
it into shared logs, update `.env`, and recreate application services. Existing
login sessions and CSRF tokens become invalid. To rotate AWS/YouTube credentials,
create replacements with the same restrictions, update `.env`, recreate
services, run `system check`, verify a sample operation, then revoke the old
credentials. `docker compose restart` alone does not load new environment values:

```bash
chmod 600 .env
docker compose up -d --force-recreate web worker beat classifier
docker compose exec web flask --app app:create_app system check
```

For PostgreSQL password rotation, stop writers and use the interactive psql
password command so the new password does not enter shell history or SQL text:

```bash
docker compose stop web worker beat classifier
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

At the psql prompt, run `\password podcasts`, enter/confirm the new password,
then `\q`. In the editor, update raw `POSTGRES_PASSWORD` and its percent-encoded
value in `DATABASE_URL` together. Merely changing `POSTGRES_PASSWORD` does not
change an initialized database role. Then run:

```bash
chmod 600 .env
docker compose up -d --force-recreate --wait postgres
docker compose run --rm --no-deps migrate
docker compose up -d --force-recreate web worker beat classifier
docker compose exec web flask --app app:create_app system check
```

For an ordinary shutdown:

```bash
docker compose down
```

Ordinary `docker compose down` preserves the PostgreSQL named volume.
**`docker compose down -v` deletes PostgreSQL data**, including the administrator,
all sources, and processing history. Volume pruning, host disk loss, or using
the wrong Compose project can also make the database unavailable. S3 is external
and private; Compose shutdown does not delete it, and surviving S3 objects alone
cannot reconstruct the application database. Redis has no named durable Compose
volume; its image-declared anonymous `/data` volume may survive recreation
unless renewed, and must not be treated as a reliable queue backup. Container
filesystems, beat's local schedule file, and worker temporary media are
ephemeral. Recover from tested PostgreSQL backups plus the matching private S3
artifacts, configuration, and application revision.
