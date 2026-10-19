# Docker operations

## Scope and prerequisites

Run commands from the repository root in a Bash SSH session. The only supported
installation is `docker-compose.yml`: `postgres`, `redis`, `migrate`, `web`,
`worker`, and `beat`. The `migrate` service exits successfully after applying
schema migrations; the other five services stay running. Keep one worker
service and one beat service unless a reviewed scaling change is made.

Use a Linux host with Docker Engine, the Docker Compose v2 plugin, Git,
Python 3 for secret generation, curl, and an editor. Allow enough disk for the
database/images and RAM for PostgreSQL, two worker processes, and the worker's
768 MiB temporary media filesystem. The image supplies Python 3.11, FFmpeg,
yt-dlp, and Deno; no host media tools are needed. The host needs outbound HTTPS
to AWS, Google/YouTube, Apple, public feed/media hosts, and image/package
registries. Amazon Transcribe usage and provider quotas can incur costs.

The administrator owns the Cloudflare Tunnel and DNS configuration for
`podcasts.ennovocon.com`. Run the tunnel connector on this host so its origin
`http://localhost:8005` reaches the published loopback port. Tunnel containers
have a different localhost; they are outside this stack. No tunnel credentials
or DNS automation are included here. Do not publish the application port on
all host interfaces: the app trusts one forwarding proxy.

Phase 1 supports ingestion and transcription. Claude learning bundles and
delivery to Lamad, Ebenezer, and Agent Factory are deferred to later phases.

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

## Build, migrate, start, and provision the administrator

```bash
docker compose version
docker compose config --quiet
docker compose build
docker compose up -d --wait postgres redis
docker compose run --rm --no-deps migrate
docker compose up -d
docker compose ps -a
docker compose logs --tail=100 migrate web worker beat
```

`migrate` runs `flask --app app:create_app db upgrade`. Compose also gates
application startup on a successful migration. The web and worker entrypoints
check the database schema, Redis, S3, Transcribe, YouTube API, yt-dlp, and Deno
before starting; beat checks database, Redis, and its schedule. If a provider
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
docker compose logs --tail=200 web worker beat
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
logs and the UI. Redis has no persistent volume, so container replacement can
lose broker messages. Database state and S3 artifacts remain, but a lost message
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

## Reviewed upgrades

Record the current Git revision and take a database backup before upgrading.
Review release notes and security changes before altering dependencies. Current
pins are `yt-dlp[default]==2026.8.19` in `requirements.txt` and Deno `2.9.7`
in `Dockerfile`; yt-dlp reports `2026.08.19` at runtime. Version expectations
also live in `config.py`, `.env.example`, `youtube_media.py`,
`system_checks.py`, and their tests. Update all applicable pins, runtime checks,
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
docker compose stop web worker beat
docker compose run --rm --no-deps migrate
docker compose up -d
docker compose ps -a
docker compose exec web flask --app app:create_app system check
```

An image rollback cannot undo a schema migration. Review migration compatibility
before returning to a prior revision. After deployment, use one short public
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

For a consistent database checkpoint, pause writers (allow active work to stop)
and make a custom-format dump. `--entrypoint` is unnecessary for PostgreSQL
exec; its environment provides the internal database identity:

```bash
umask 077
mkdir -p backups
chmod 700 backups
docker compose stop web worker beat
backup_file="backups/postgres-$(date -u +%Y%m%dT%H%M%SZ).dump"
if docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$backup_file" && docker compose exec -T postgres pg_restore --list < "$backup_file" > /dev/null; then
  chmod 600 "$backup_file"
  printf 'Backup saved: %s\n' "$backup_file"
else
  printf 'Backup failed; do not use partial file: %s\n' "$backup_file" >&2
fi
unset backup_file
docker compose up -d
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
docker compose stop web worker beat
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
from the replaced database. With all writers stopped, remove that stale broker
state by recreating only Redis (it has no persistent volume):

```bash
unset restore_file
docker compose up -d --force-recreate --wait redis
docker compose run --rm --no-deps migrate
docker compose up -d
docker compose exec web flask --app app:create_app system check
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
docker compose up -d --force-recreate web worker beat
docker compose exec web flask --app app:create_app system check
```

For PostgreSQL password rotation, stop writers and use the interactive psql
password command so the new password does not enter shell history or SQL text:

```bash
docker compose stop web worker beat
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
docker compose up -d --force-recreate web worker beat
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
cannot reconstruct the application database. Redis queue state, container
filesystems, beat's local schedule file, and worker temporary media are
ephemeral. Recover from tested PostgreSQL backups plus the matching private S3
artifacts, configuration, and application revision.
