# Docker Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run PodcastTranscriptionWeb on this machine as a durable, admin-only Docker Compose application that can add a feed and complete an S3-backed Amazon Transcribe episode workflow.

**Architecture:** A Flask application factory serves the private UI and CLI, PostgreSQL stores durable operational state, Redis carries Celery messages, and separate worker and beat containers process and discover episodes. Audio and transcript artifacts are private S3 objects in `podcast-transcriptions-web-2025`; the database stores object keys and hashes rather than public URLs. A one-shot migration service owns schema upgrades, while loopback-only Gunicorn is the sole host-published port for the administrator-managed Cloudflare Tunnel.

**Tech Stack:** Python 3.11, Flask, SQLAlchemy, Alembic/Flask-Migrate, Flask-Login, Flask-WTF, Flask-Limiter, PostgreSQL 15, Redis 7, Celery 5, boto3, Amazon Transcribe, Gunicorn, Docker Compose, pytest.

## Global Constraints

- This plan implements only Phase 1 of the approved design in `docs/superpowers/specs/2026-10-19-docker-podcast-learning-platform-design.md`.
- Phase 1 accepts Apple Podcasts pages and direct RSS/Atom podcast feeds. YouTube video, channel, and playlist ingestion is not implemented by this plan and requires its own source-adapter design.
- Do not add Claude extraction, learning bundles, Lamad delivery, Ebenezer delivery, Agent Factory delivery, or Ask & Learn in this phase.
- Do not add Stripe, Mailgun, public registration, OpenAI, OpenRouter, Heroku, or public S3 objects.
- Keep `web` bound to `127.0.0.1:8005`; do not publish PostgreSQL or Redis ports.
- Store only S3 bucket/key identifiers and content hashes in PostgreSQL. Never store presigned URLs because they expire and must not appear in logs.
- Keep every state-changing browser request under CSRF protection, including login, logout, add-feed, retry, and reprocess actions.
- Keep all network operations bounded by connect/read timeouts and content-size limits.
- Make each Celery stage idempotent: a retry must reuse a valid S3 artifact and must not create a duplicate episode.
- Use `podcast-transcriptions-web-2025` as the configured bucket. The application must fail its startup check if it cannot read and write that exact bucket or call Amazon Transcribe in the configured region.
- Preserve unrelated user changes. Before each commit, inspect `git status --short` and stage only files named by the current task.

---

## File and Responsibility Map

| Path | Responsibility after Phase 1 |
| --- | --- |
| `config.py` | Environment parsing, safe defaults, required-setting validation, cookie and request limits |
| `extensions.py` | Unbound Flask extension instances: SQLAlchemy, Migrate, LoginManager, CSRF, Limiter |
| `app.py` | Application factory, authenticated routes, CSRF error handling, and health routes |
| `wsgi.py` | Production WSGI object created from the application factory |
| `admin_cli.py` | Interactive single-admin create and password-reset commands |
| `system_checks.py` | Database, Redis, S3, Transcribe, worker, and beat readiness checks |
| `models.py` | `Admin`, `Podcast`, `Episode`, and `ProcessingJob` operational models and constraints |
| `feed_resolver.py` | Bounded Apple URL and generic RSS resolution |
| `episode_fetcher.py` | Bounded RSS download/parsing, stable identity, and bounded audio download |
| `s3_utils.py` | Private S3 key/object operations and bucket validation |
| `transcribe.py` | Idempotent Amazon Transcribe job execution and deterministic transcript normalization |
| `tasks.py` | Celery discovery, download, transcription, retry, and hourly discovery tasks |
| `celeryconfig.py` | Redis broker/backend, queues, reliability controls, and beat schedule |
| `migrations/` | Versioned Alembic history for the Phase 1 schema |
| `tests/` | Unit, security, task, adapter, and Compose-contract tests |
| `scripts/start-web.sh` | Required external-service checks followed by Gunicorn |
| `scripts/start-worker.sh` | Required external-service checks followed by Celery worker |
| `docker-compose.yml` | Loopback web, internal PostgreSQL/Redis, migrate, worker, and beat topology |
| `Dockerfile` | Reproducible non-root Python application image |
| `.env.example` | Non-secret deployment configuration contract |
| `README.md` | Docker-first installation and daily-use guide |
| `docs/operations.md` | Cloudflare target, backup/restore, upgrade, reset, retry, and diagnostics |

---

### Task 1: Establish the runtime configuration and test harness

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Modify: `.gitignore`
- Replace: `.env.example`

**Consumes:** Environment variables from Docker Compose and pytest overrides.

**Produces:** A validated configuration object with no Heroku, payment, mail, OpenAI, or OpenRouter settings; a repeatable pytest entry point.

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_config.py` with tests that assert:

```python
import pytest

from config import ConfigError, load_settings


def valid_environment():
    return {
        "SECRET_KEY": "s" * 64,
        "DATABASE_URL": "postgresql+psycopg2://podcasts:test@postgres:5432/podcasts",
        "REDIS_URL": "redis://redis:6379/0",
        "RATELIMIT_STORAGE_URI": "redis://redis:6379/1",
        "AWS_REGION": "us-east-1",
        "AWS_S3_BUCKET_NAME": "podcast-transcriptions-web-2025",
    }


def test_load_settings_accepts_the_designated_bucket():
    settings = load_settings(valid_environment())
    assert settings.AWS_S3_BUCKET_NAME == "podcast-transcriptions-web-2025"
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.MAX_AUDIO_BYTES == 536_870_912


@pytest.mark.parametrize(
    "missing",
    ["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "AWS_REGION", "AWS_S3_BUCKET_NAME"],
)
def test_load_settings_rejects_missing_required_values(missing):
    environment = valid_environment()
    environment.pop(missing)
    with pytest.raises(ConfigError, match=missing):
        load_settings(environment)


def test_load_settings_rejects_a_different_bucket():
    environment = valid_environment()
    environment["AWS_S3_BUCKET_NAME"] = "wrong-bucket"
    with pytest.raises(ConfigError, match="podcast-transcriptions-web-2025"):
        load_settings(environment)
```

- [ ] **Step 2: Prove the tests fail for the missing module**

Run:

```bash
python -m pytest tests/test_config.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'config'`.

- [ ] **Step 3: Replace dependency manifests with the Phase 1 dependency set**

Keep runtime dependencies limited to packages imported by the Phase 1 application. `requirements.txt` must include compatible, bounded releases for:

```text
beautifulsoup4>=4.12,<5
boto3>=1.34,<2
celery[redis]>=5.3,<6
email-validator>=2.1,<3
feedparser>=6.0,<7
Flask>=3.0,<4
Flask-Limiter[redis]>=3.8,<5
Flask-Login>=0.6,<1
Flask-Migrate>=4.0,<5
Flask-SQLAlchemy>=3.1,<4
Flask-WTF>=1.2,<2
gunicorn>=21,<24
psycopg2-binary>=2.9,<3
python-dateutil>=2.8,<3
python-dotenv>=1.0,<2
requests>=2.31,<3
tenacity>=8.2,<10
```

Create `requirements-dev.txt`:

```text
-r requirements.txt
pytest>=8,<10
pytest-mock>=3.14,<4
responses>=0.25,<1
```

- [ ] **Step 4: Implement typed environment loading in `config.py`**

Implement `ConfigError`, strict integer/boolean parsers, and `load_settings(environment=None)`. The returned object must expose Flask-compatible uppercase attributes and these defaults:

```python
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
REMEMBER_COOKIE_SECURE = True
REMEMBER_COOKIE_HTTPONLY = True
REMEMBER_COOKIE_SAMESITE = "Lax"
MAX_CONTENT_LENGTH = 1_048_576
MAX_AUDIO_BYTES = 536_870_912
RSS_MAX_BYTES = 10_485_760
HTTP_CONNECT_TIMEOUT_SECONDS = 10
HTTP_READ_TIMEOUT_SECONDS = 60
DISCOVERY_EPISODE_LIMIT = 10
DISCOVERY_INTERVAL_SECONDS = 3600
TRANSCRIBE_POLL_SECONDS = 10
TRANSCRIBE_TIMEOUT_SECONDS = 14_400
TRANSCRIBE_LANGUAGE_CODE = "en-US"
RATELIMIT_DEFAULT = ""
RATELIMIT_LOGIN = "5 per minute"
SQLALCHEMY_TRACK_MODIFICATIONS = False
```

Reject a default/dev `SECRET_KEY`, empty required values, a non-positive numeric limit, and any bucket name other than `podcast-transcriptions-web-2025`. Allow test overrides through a mapping without mutating `os.environ`.

Map `DATABASE_URL` to Flask-SQLAlchemy's `SQLALCHEMY_DATABASE_URI`, preserve `REDIS_URL` for Celery and readiness, and default `RATELIMIT_STORAGE_URI` to Redis database 1 only when it is not explicitly supplied.

- [ ] **Step 5: Replace `.env.example` and harden ignored paths**

The example must document only:

```dotenv
SECRET_KEY=
POSTGRES_DB=podcasts
POSTGRES_USER=podcasts
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+psycopg2://podcasts:replace-with-url-safe-password@postgres:5432/podcasts
REDIS_URL=redis://redis:6379/0
RATELIMIT_STORAGE_URI=redis://redis:6379/1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
AWS_REGION=us-east-1
AWS_S3_BUCKET_NAME=podcast-transcriptions-web-2025
AWS_PROFILE=
TRANSCRIBE_LANGUAGE_CODE=en-US
DISCOVERY_EPISODE_LIMIT=10
DISCOVERY_INTERVAL_SECONDS=3600
LOG_LEVEL=INFO
```

Add `.env`, `.aws/`, `.claude/`, `.pytest_cache/`, `__pycache__/`, `*.pyc`, and local coverage files to `.gitignore`. Do not put a credential value in the example.

- [ ] **Step 6: Install test dependencies and run the tests**

Run:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

- [ ] **Step 7: Commit the configuration foundation**

```bash
git add requirements.txt requirements-dev.txt config.py tests/__init__.py tests/test_config.py .gitignore .env.example
git commit -m "test: establish docker runtime configuration"
```

---

### Task 2: Introduce the Flask application factory and extension boundary

**Files:**
- Create: `extensions.py`
- Create: `wsgi.py`
- Modify: `models.py:1-6`
- Replace: `app.py`
- Create: `tests/conftest.py`
- Create: `tests/test_app_factory.py`

**Consumes:** `load_settings()` from Task 1.

**Produces:** `create_app(config_override=None)`, a production `wsgi:app` boundary, initialized Flask extensions, and isolated test applications.

- [ ] **Step 1: Write failing factory tests**

Create `tests/conftest.py` with an in-memory SQLite application, temporary limiter storage, secure-cookie override, and database lifecycle fixtures. The base `app` fixture must not create tables; a separate `db_session` fixture creates and drops tables for tests that need persistence. Create `tests/test_app_factory.py` asserting:

```python
def test_healthz_is_a_dependency_free_liveness_probe(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_factory_does_not_create_tables_implicitly(app):
    from sqlalchemy import inspect
    from extensions import db

    with app.app_context():
        assert inspect(db.engine).get_table_names() == []


def test_security_cookie_defaults(app):
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
```

The test fixture passes `TESTING=True`, `SQLALCHEMY_DATABASE_URI="sqlite+pysqlite:///:memory:"`, `SESSION_COOKIE_SECURE=False`, `WTF_CSRF_ENABLED=False`, and `RATELIMIT_ENABLED=False`.

- [ ] **Step 2: Run the factory tests and verify failure**

Run:

```bash
python -m pytest tests/test_app_factory.py -q
```

Expected: import or fixture setup fails because `create_app` and `extensions.py` do not exist.

- [ ] **Step 3: Create unbound extension instances**

Create `extensions.py` exporting exactly:

```python
csrf = CSRFProtect()
db = SQLAlchemy()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
login_manager = LoginManager()
migrate = Migrate()
```

Move the `db` definition out of `models.py`; `models.py` must import it from `extensions`.

- [ ] **Step 4: Replace `app.py` with an application factory**

Implement `create_app(config_override=None)` with this sequence:

1. Load strict production settings unless a complete test override is supplied.
2. Apply the override.
3. Wrap `app.wsgi_app` in `ProxyFix(x_for=1, x_proto=1, x_host=1)`.
4. Initialize `db`, `migrate`, `login_manager`, `csrf`, and `limiter`.
5. Configure `login_manager.login_view = "login"` and a generic unauthorized response.
6. Register routes and CLI commands without calling `db.create_all()`.
7. Expose `GET /healthz` without authentication or dependency access.
8. Keep `app.py` import-safe: it must not construct a production application at module import time.

Create `wsgi.py` with the production construction boundary:

```python
from app import create_app

app = create_app()
```

Gunicorn imports `wsgi:app`. Flask CLI commands use `--app app:create_app`. Celery constructs one Flask application inside `tasks.py` after container configuration is present.

Remove unused `session`, raw `SQLAlchemy`, `UserMixin`, and `requests` imports. Retain route behavior temporarily so subsequent tasks can refactor it under tests. Remove the `__main__` block that calls `db.create_all()`.

- [ ] **Step 5: Run focused and existing tests**

```bash
python -m pytest tests/test_config.py tests/test_app_factory.py -q
```

Expected: all tests pass; no table exists until a fixture or migration explicitly creates it.

- [ ] **Step 6: Commit the factory boundary**

```bash
git add extensions.py models.py app.py wsgi.py tests/conftest.py tests/test_app_factory.py
git commit -m "refactor: add flask application factory"
```

---

### Task 3: Define the Phase 1 schema and versioned migration

**Files:**
- Replace: `models.py`
- Modify: `app.py`
- Delete: `migrations.py`
- Create: `migrations/alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_docker_foundation.py`
- Create: `tests/test_models.py`
- Create: `tests/test_migrations.py`

**Consumes:** SQLAlchemy/Migrate instances from Task 2.

**Produces:** A one-admin relational schema, idempotent feed/episode identity, explicit episode stages, and one mutable processing record per episode stage.

- [ ] **Step 1: Write failing model tests**

Cover these invariants in `tests/test_models.py`:

- `Admin.singleton_key` is always true and a second row raises `IntegrityError`.
- `Podcast.rss_url` is unique because only one admin exists.
- `(podcast_id, guid)` is unique for episodes.
- `(episode_id, job_type)` is unique for processing jobs.
- An episode defaults to `discovered` and has no public URL field for an S3 artifact.
- Deleting the admin cascades to podcasts, episodes, and processing jobs.

Run:

```bash
python -m pytest tests/test_models.py -q
```

Expected: failures reference missing `Admin`, `singleton_key`, and Phase 1 fields.

- [ ] **Step 2: Replace the model layer**

Define these tables and constraints:

```text
admins
  id integer primary key
  singleton_key boolean not null default true unique
  email varchar(254) not null unique
  password_hash varchar(255) not null
  created_at timestamptz not null
  updated_at timestamptz not null
  check singleton_key = true

podcasts
  id integer primary key
  admin_id integer not null references admins(id) on delete cascade
  name varchar(255) not null
  source_url varchar(2048) not null
  rss_url varchar(2048) not null unique
  description text
  image_url varchar(2048)
  enabled boolean not null default true
  last_checked timestamptz
  last_error text
  created_at timestamptz not null
  updated_at timestamptz not null

episodes
  id integer primary key
  podcast_id integer not null references podcasts(id) on delete cascade
  guid varchar(1024) not null
  title varchar(500) not null
  description text
  audio_url varchar(4096) not null
  published_at timestamptz
  duration_seconds integer
  status varchar(32) not null default discovered
  last_successful_stage varchar(32)
  error_message text
  audio_s3_key varchar(2048)
  audio_sha256 char(64)
  transcript_s3_key varchar(2048)
  transcript_sha256 char(64)
  segments_s3_key varchar(2048)
  transcription_provider varchar(64)
  transcription_job_name varchar(200)
  language varchar(16)
  created_at timestamptz not null
  updated_at timestamptz not null
  unique podcast_id, guid
  check status in discovered, downloading, transcribing, extracting, bundling, completed, failed

processing_jobs
  id integer primary key
  episode_id integer not null references episodes(id) on delete cascade
  job_type varchar(32) not null
  status varchar(32) not null default pending
  attempts integer not null default 0
  celery_task_id varchar(255)
  started_at timestamptz
  completed_at timestamptz
  error_message text
  created_at timestamptz not null
  updated_at timestamptz not null
  unique episode_id, job_type
  check job_type in download, transcribe
  check status in pending, running, retrying, completed, failed
```

Use timezone-aware UTC factories. Use ORM cascades plus foreign-key `ondelete="CASCADE"`. Add helpers on `Episode` for `clear_error()`, `mark_failed(redacted_message)`, and `artifact_ready(stage)`; do not put network operations in models.

Update `app.py` in the same task so it imports `Admin` rather than the removed `User`, uses `Podcast.admin_id`, and no longer imports `EmailNotification`. This keeps the application importable between schema and authentication commits; Task 4 removes the temporary registration-era behavior.

- [ ] **Step 3: Create the initial Alembic history**

Delete the broken top-level `migrations.py`, initialize Flask-Migrate’s standard `migrations/` directory, and create revision ID `0001_docker_foundation`. The migration `upgrade()` must create the four tables above in foreign-key order plus indexes on:

```text
podcasts.enabled
episodes.podcast_id
episodes.status
episodes.published_at
processing_jobs.status
```

The migration `downgrade()` must drop indexes and tables in reverse order. Do not call `db.create_all()` from the migration.

- [ ] **Step 4: Add migration tests**

`tests/test_migrations.py` must create a temporary file-backed SQLite database, run `flask db upgrade`, inspect the four table names and the Alembic version, run `flask db downgrade base`, and assert the application tables are gone. Keep the production PostgreSQL migration smoke test for Task 10.

- [ ] **Step 5: Run model and migration tests**

```bash
python -m pytest tests/test_models.py tests/test_migrations.py -q
```

Expected: all tests pass and the recorded version is `0001_docker_foundation`.

- [ ] **Step 6: Commit the schema**

```bash
git add models.py migrations tests/test_models.py tests/test_migrations.py
git rm migrations.py
git commit -m "feat: add versioned phase one schema"
```

---

### Task 4: Enforce single-admin provisioning and secure authentication

**Files:**
- Create: `admin_cli.py`
- Modify: `app.py`
- Create: `tests/test_admin_cli.py`
- Create: `tests/test_auth.py`

**Consumes:** `Admin` and the database singleton constraint from Task 3.

**Produces:** Interactive `flask admin create`, `flask admin reset-password`, throttled login, POST logout, and no registration route.

- [ ] **Step 1: Write failing CLI and authentication tests**

Tests must prove:

- `flask admin create` prompts for email and a hidden confirmed password, normalizes the email, and creates one admin.
- A password shorter than 14 characters is rejected.
- A second create attempt exits nonzero and leaves one admin row.
- `flask admin reset-password` changes the only admin’s password.
- `GET /register` and `POST /register` return 404.
- Valid login redirects to `/dashboard`.
- Invalid email and invalid password return the same message and status.
- Six failed login attempts from one address produce a 429 when the configured limit is `5 per minute`.
- `GET /logout` returns 405 and CSRF-protected `POST /logout` ends the session.

Run:

```bash
python -m pytest tests/test_admin_cli.py tests/test_auth.py -q
```

Expected: failures identify the missing CLI and still-public registration route.

- [ ] **Step 2: Implement `admin_cli.py`**

Register a Click group named `admin` with commands:

```text
flask admin create
flask admin reset-password
```

Both commands must run inside the application context. `create` validates the email with `email-validator`, prompts twice for a hidden password, enforces 14 characters, checks `Admin.query.count()`, hashes with Werkzeug, catches `IntegrityError`, rolls back, and never echoes the password. `reset-password` refuses to operate unless exactly one admin exists and uses the same password validation.

- [ ] **Step 3: Replace registration-era authentication routes**

In `app.py`:

- Load the sole `Admin` in `login_manager.user_loader`.
- Delete the `/register` route.
- Apply `@limiter.limit(lambda: current_app.config["RATELIMIT_LOGIN"])` to `/login`.
- Return the same `Invalid email or password` response for every authentication failure.
- Reject an unsafe `next` redirect; allow only a relative same-host path.
- Change logout to `POST` only.
- Keep unauthenticated API responses JSON and browser responses redirected to login.

- [ ] **Step 4: Run authentication tests**

```bash
python -m pytest tests/test_admin_cli.py tests/test_auth.py -q
```

Expected: all tests pass, including database and application refusal of a second admin.

- [ ] **Step 5: Commit admin-only authentication**

```bash
git add admin_cli.py app.py tests/test_admin_cli.py tests/test_auth.py
git commit -m "feat: enforce single administrator access"
```

---

### Task 5: Remove public-product UI and protect every state-changing request

**Files:**
- Modify: `templates/base.html`
- Replace: `templates/index.html`
- Modify: `templates/login.html`
- Replace: `templates/dashboard.html`
- Replace: `templates/episodes.html`
- Modify: `static/js/app.js`
- Delete: `templates/register.html`
- Delete: `templates/subscription.html`
- Delete: `templates/emails/summary_ready.html`
- Delete: `templates/emails/transcript_ready.html`
- Delete: `templates/emails/welcome.html`
- Modify: `app.py`
- Create: `tests/test_web_security.py`

**Consumes:** Admin authentication and global CSRF from Task 4.

**Produces:** A private podcast dashboard with CSRF-safe JSON actions, authorized transcript viewing, and no registration/payment/email promises.

- [ ] **Step 1: Write failing browser-security tests**

Cover:

- The landing page contains `Private Podcast Learning Workspace` and a login link, with no register, pricing, subscription, email-delivery, or payment call to action.
- An unauthenticated add-feed, episode detail, transcript, and retry request cannot access data.
- A logged-in POST without a CSRF token returns 400.
- A valid token permits the request to reach the route handler.
- Episode JSON returns S3 artifact availability booleans, never a bucket URL, S3 key, or presigned URL.
- `GET /api/episodes/<id>/transcript` reads through the S3 adapter only after authorization and returns plain text with `Cache-Control: no-store`.
- Error pages and JSON errors do not contain exception text.

- [ ] **Step 2: Run the tests and verify the current UI fails**

```bash
python -m pytest tests/test_web_security.py -q
```

Expected: failures find registration links, missing CSRF, and direct artifact path exposure.

- [ ] **Step 3: Add a single CSRF transport for forms and JSON**

Render this meta field in authenticated pages:

```html
<meta name="csrf-token" content="{{ csrf_token() }}">
```

Update the shared `apiRequest` helper in `static/js/app.js` to set `X-CSRFToken` on non-GET requests, set `Accept: application/json`, and preserve `Content-Type: application/json` only when a JSON body is present. Route all dashboard and episode POSTs through this helper; remove duplicate inline `fetch()` calls.

- [ ] **Step 4: Simplify the private UI**

The landing page must offer login only. The dashboard must show the feed-add form, canonical feed URL, enabled state, last check, last error, episode counts, and recent episode stages. The episode page must show metadata, stage, retry action, and transcript-view action. Remove summary buttons and all payment/email/registration language.

Use a CSRF-protected POST form for logout. Do not add a public transcript link.

- [ ] **Step 5: Make API responses artifact-safe**

Change episode detail JSON to include:

```json
{
  "id": 1,
  "title": "Episode title",
  "status": "completed",
  "has_audio": true,
  "has_transcript": true,
  "has_segments": true,
  "published_at": "2026-10-19T12:00:00+00:00",
  "updated_at": "2026-10-19T12:30:00+00:00",
  "error_message": null
}
```

Never include `audio_url`, `audio_s3_key`, `transcript_s3_key`, `segments_s3_key`, or exception strings. Add the authorized transcript endpoint and return 404 if no transcript artifact exists.

- [ ] **Step 6: Run security tests**

```bash
python -m pytest tests/test_web_security.py tests/test_auth.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the private UI**

```bash
git add app.py templates static/js/app.js tests/test_web_security.py
git rm templates/register.html templates/subscription.html templates/emails/summary_ready.html templates/emails/transcript_ready.html templates/emails/welcome.html
git commit -m "feat: make the web interface admin only"
```

---

### Task 6: Make feed resolution, discovery, and media downloads bounded and deterministic

**Files:**
- Modify: `feed_resolver.py`
- Replace: `episode_fetcher.py`
- Create: `tests/fixtures/feed.xml`
- Create: `tests/test_feed_resolver.py`
- Create: `tests/test_episode_fetcher.py`

**Consumes:** URL, timeout, RSS-size, episode-limit, and audio-size settings.

**Produces:** Canonical RSS URLs, deterministic feed entries, and bounded temporary audio files.

- [ ] **Step 1: Write failing resolver and fetcher tests**

Tests must cover:

- Apple URL `https://podcasts.apple.com/us/podcast/the-garyvee-audio-experience/id928159684` resolves the lookup result’s `feedUrl`.
- A direct RSS URL is accepted after a bounded GET even when HEAD is unsupported.
- Relative HTML `<link rel="alternate">` feeds are resolved against the final response URL.
- HTTP timeouts, more than `RSS_MAX_BYTES`, and non-feed documents raise a typed `FeedResolutionError` with a safe message.
- RSS parsing returns one record per first audio enclosure with timezone-aware publication time.
- Entries without a GUID use a SHA-256 identity derived from the enclosure URL, not MD5.
- Audio download rejects an oversized `Content-Length`, aborts when streamed bytes cross `MAX_AUDIO_BYTES`, removes the partial file, and succeeds for a bounded fixture.

- [ ] **Step 2: Prove current network helpers fail the contract**

```bash
python -m pytest tests/test_feed_resolver.py tests/test_episode_fetcher.py -q
```

Expected: failures show unbounded feed parsing, MD5 identity, and unbounded audio streaming.

- [ ] **Step 3: Implement a shared bounded HTTP reader**

Implement a private response-stream helper that:

1. Uses `(HTTP_CONNECT_TIMEOUT_SECONDS, HTTP_READ_TIMEOUT_SECONDS)`.
2. Sends a fixed application user agent.
3. Follows redirects.
4. Checks `Content-Length` before reading.
5. Streams in 64 KiB chunks and aborts above the supplied byte limit.
6. Closes the response and removes a partial destination on every failure.

Do not log query strings or response bodies.

- [ ] **Step 4: Refactor feed resolution and episode parsing**

Pass downloaded bytes to `feedparser.parse()` instead of passing a remote URL. Normalize every GUID to a string; if absent, use `sha256(audio_url.encode("utf-8")).hexdigest()`. Preserve title, description, audio URL, content type, byte length when present, and timezone-aware published time.

Remove aria2/subprocess probing so the byte limit applies uniformly.

- [ ] **Step 5: Run feed tests**

```bash
python -m pytest tests/test_feed_resolver.py tests/test_episode_fetcher.py -q
```

Expected: all tests pass with no real network access.

- [ ] **Step 6: Commit bounded ingestion**

```bash
git add feed_resolver.py episode_fetcher.py tests/fixtures/feed.xml tests/test_feed_resolver.py tests/test_episode_fetcher.py
git commit -m "fix: bound podcast feed and media ingestion"
```

---

### Task 7: Make S3 artifacts private and validate AWS access

**Files:**
- Replace: `s3_utils.py`
- Create: `system_checks.py`
- Modify: `app.py`
- Create: `tests/test_s3_utils.py`
- Create: `tests/test_system_checks.py`

**Consumes:** AWS default credential chain, `AWS_REGION`, and exact S3 bucket name.

**Produces:** Key-based private artifact storage, a startup validation command, and readiness checks without signed-URL leakage.

- [ ] **Step 1: Write failing S3 and readiness tests**

Test with mocked boto3 clients that:

- The manager uses boto3’s default credential chain and explicit region, without passing null key values.
- `put_file`, `put_text`, and `put_json` return the S3 key, not an HTTP URL.
- All puts set private server-side encryption (`ServerSideEncryption="AES256"`) and a content type.
- `get_text` enforces a maximum response size.
- `validate_access` performs bucket head, an encrypted probe put/get/delete under `_health/`, and verifies `get_bucket_location` matches `AWS_REGION` with the `us-east-1` null-location rule.
- The Transcribe check makes an authenticated `list_transcription_jobs(MaxResults=1)` call.
- `flask system check --services database,redis,s3,transcribe` exits zero only when all selected checks pass.
- `/readyz` returns only component names and `ok`/`failed`, never credentials, S3 keys, exception messages, or transcripts.

- [ ] **Step 2: Run tests and verify URL-returning behavior fails**

```bash
python -m pytest tests/test_s3_utils.py tests/test_system_checks.py -q
```

Expected: failures show HTTP URL return values and the missing checks.

- [ ] **Step 3: Replace `s3_utils.py`**

Implement `S3Manager(bucket_name, region, s3_client=None)` and these methods:

```python
artifact_key(podcast_id, episode_id, kind, filename) -> str
put_file(local_path, key, content_type) -> str
put_text(content, key, content_type="text/plain; charset=utf-8") -> str
put_json(payload, key) -> str
get_bytes(key, max_bytes) -> bytes
get_text(key, max_bytes) -> str
get_json(key, max_bytes) -> dict
exists(key) -> bool
head(key) -> dict
delete(key) -> None
validate_access() -> None
```

Raise typed exceptions; do not convert AWS failures to falsey success values. Construct clients through `boto3.session.Session(region_name=region)` so environment variables, an optional profile, or the standard credentials file work naturally.

- [ ] **Step 4: Add system checks and CLI**

Implement named checks for `database`, `redis`, `s3`, `transcribe`, `worker`, and `beat`. The worker check uses Celery `inspect().ping()` with a bounded timeout. The beat check verifies the configured discovery task and interval. Register:

```text
flask system check --services database,redis,s3,transcribe
```

Add `/readyz` with a 15-second in-process cache. Return 200 only when required Phase 1 checks pass; otherwise return 503. Keep `/healthz` dependency-free.

- [ ] **Step 5: Run adapter and check tests**

```bash
python -m pytest tests/test_s3_utils.py tests/test_system_checks.py tests/test_app_factory.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit private artifact storage**

```bash
git add s3_utils.py system_checks.py app.py tests/test_s3_utils.py tests/test_system_checks.py
git commit -m "feat: validate private s3 artifact storage"
```

---

### Task 8: Normalize Amazon Transcribe output into immutable artifacts

**Files:**
- Replace: `transcribe.py`
- Create: `tests/fixtures/transcribe-result.json`
- Create: `tests/test_transcribe.py`

**Consumes:** An existing private audio S3 key, deterministic episode identity, configured language and timeout, and mocked or live Amazon Transcribe/S3 clients.

**Produces:** Stable timestamped `transcript.txt` and `segments.json` S3 keys with SHA-256 hashes and provider lineage.

- [ ] **Step 1: Write failing transcription contract tests**

Test that:

- Job name is deterministic: `podcast-{podcast_id}-episode-{episode_id}-v1`.
- A pre-existing in-progress AWS job is resumed after `ConflictException`.
- A completed job is reused and not started again.
- Failed jobs raise a redacted `TranscriptionError`.
- Polling stops at `TRANSCRIBE_TIMEOUT_SECONDS`.
- Provider JSON is fetched from the private output bucket/key with a maximum-size limit.
- Punctuation attaches to the preceding word.
- Segments have stable IDs derived from transcript hash, start milliseconds, end milliseconds, and normalized text.
- Transcript and segment JSON are written to deterministic episode keys.
- Returned data contains keys, SHA-256 values, language, duration, provider, and job name; it contains no signed URL.

- [ ] **Step 2: Run the contract tests and verify failure**

```bash
python -m pytest tests/test_transcribe.py -q
```

Expected: current random job names, signed-result HTTP fetch, and missing hashes fail.

- [ ] **Step 3: Implement deterministic normalization**

Expose:

```python
class TranscriptionError(RuntimeError):
    """Safe, redacted failure raised by the transcription adapter."""
```

Implement this exact callable interface with the complete tested workflow:

```text
transcribe_audio_from_s3(*, audio_s3_key: str, podcast_id: int, episode_id: int, language_code: str, poll_seconds: int, timeout_seconds: int, s3_manager: S3Manager, transcribe_client) -> dict
```

Use this output shape:

```json
{
  "transcript_s3_key": "transcripts/podcast_1/episode_2/transcript.txt",
  "segments_s3_key": "transcripts/podcast_1/episode_2/segments.json",
  "transcript_sha256": "64-lowercase-hex-characters",
  "segments_sha256": "64-lowercase-hex-characters",
  "language": "en-US",
  "duration_seconds": 123,
  "provider": "amazon-transcribe",
  "job_name": "podcast-1-episode-2-v1"
}
```

The canonical segment JSON must include `schema_version: "podcast-transcript-segments/v1"`, episode identity, transcript hash, normalized timestamped segments, provider job name, and generated timestamp. Hash serialized JSON with sorted keys and compact separators.

- [ ] **Step 4: Run transcription tests**

```bash
python -m pytest tests/test_transcribe.py -q
```

Expected: all deterministic and timeout tests pass.

- [ ] **Step 5: Commit the transcription adapter**

```bash
git add transcribe.py tests/fixtures/transcribe-result.json tests/test_transcribe.py
git commit -m "fix: make amazon transcription deterministic"
```

---

### Task 9: Rebuild Celery discovery and processing as idempotent stages

**Files:**
- Replace: `tasks.py`
- Replace: `celeryconfig.py`
- Modify: `app.py`
- Create: `tests/test_tasks_discovery.py`
- Create: `tests/test_tasks_processing.py`

**Consumes:** Phase 1 models, bounded feed/audio helpers, private S3 manager, and deterministic transcription adapter.

**Produces:** Durable discovery, download, transcribe, retry, and hourly discovery tasks with no summarization or email queue.

- [ ] **Step 1: Write failing discovery task tests**

Tests must prove:

- A feed refresh inserts each `(podcast_id, guid)` once and queues only newly inserted episodes.
- Replaying the same refresh creates no row and no task duplicate.
- Discovery commits episode rows before task dispatch.
- The configured episode limit is honored.
- A feed error updates `Podcast.last_error` with a redacted category while leaving the feed enabled.
- Disabled feeds are skipped by the periodic task.

- [ ] **Step 2: Write failing processing task tests**

Tests must prove:

- `process_episode` routes `discovered` to download, an episode with valid audio to transcription, and a valid transcript to completed.
- Download stores a bounded temporary file, computes SHA-256, uploads once to a deterministic key, deletes the temporary file, commits state, then queues transcription.
- A repeated download with a matching S3 object and hash skips the network download.
- Transcription writes every returned key/hash/lineage field, sets `last_successful_stage="transcribing"`, clears the error, and sets `completed`.
- A retry increments `ProcessingJob.attempts`; retryable network/AWS failures use exponential Celery retry, while invalid feed/media input fails without an infinite retry.
- Errors stored in PostgreSQL contain a category and correlation ID but no URL query string, AWS token, transcript body, or signed URL.
- The manual retry route resets only the failed stage and queues `process_episode` after commit.

- [ ] **Step 3: Run task tests and verify current pipeline fails**

```bash
python -m pytest tests/test_tasks_discovery.py tests/test_tasks_processing.py -q
```

Expected: failures include undefined `whisper_model`, mismatched summary signature, mail tasks, and non-idempotent scheduling.

- [ ] **Step 4: Replace task orchestration**

Keep only these Celery tasks:

```text
tasks.refresh_podcast
tasks.process_episode
tasks.download_episode
tasks.transcribe_episode
tasks.check_all_podcasts_for_new_episodes
```

Use queue names `default`, `downloads`, and `transcription`. Remove `summarization`, `emails`, and every Mailgun helper. Set `task_acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`, `broker_connection_retry_on_startup=True`, JSON serializers, UTC, explicit soft/hard time limits, and result expiry.

Set the beat interval from `DISCOVERY_INTERVAL_SECONDS` at process startup; route the periodic discovery task to `default`.

Use a `ProcessingJob` upsert helper per episode/stage. Commit each durable stage before calling the next task’s `.delay()`. Never pass ORM objects through Celery; pass integer IDs only.

- [ ] **Step 5: Update web actions**

The add-feed route must resolve the canonical feed, insert `Podcast`, commit, then dispatch `refresh_podcast`. The manual retry endpoint must be `POST /api/episodes/<id>/retry`, authorized and CSRF-protected. Remove `trigger-summary` and the old trigger-transcription endpoint.

- [ ] **Step 6: Run all application tests**

```bash
python -m pytest -q
```

Expected: all tests pass with no outbound network access.

- [ ] **Step 7: Commit the repaired pipeline**

```bash
git add tasks.py celeryconfig.py app.py tests/test_tasks_discovery.py tests/test_tasks_processing.py
git commit -m "fix: rebuild the durable episode pipeline"
```

---

### Task 10: Harden the image and Compose topology

**Files:**
- Replace: `Dockerfile`
- Replace: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `scripts/start-web.sh`
- Create: `scripts/start-worker.sh`
- Create: `tests/test_compose_contract.py`

**Consumes:** Environment contract, migrations, system-check CLI, WSGI app, and Celery app.

**Produces:** `postgres`, `redis`, `migrate`, `web`, `worker`, and `beat` services with loopback-only ingress and durable database state.

- [ ] **Step 1: Write failing Compose contract tests**

Parse `docker compose config --format json` and assert:

- The exact service set is `postgres`, `redis`, `migrate`, `web`, `worker`, and `beat`.
- Only `web` publishes a port, and it is `127.0.0.1:8005:8005`.
- PostgreSQL uses a named volume and neither database nor Redis has a host port.
- `migrate` runs `flask db upgrade` and exits.
- Web, worker, and beat depend on successful migration plus healthy dependencies.
- Worker consumes all three Phase 1 queues.
- Beat runs as its own service.
- No Compose key or value references Heroku, Stripe, Mailgun, OpenAI, OpenRouter, registration, or a summary/email queue.
- The application image uses a non-root user.

- [ ] **Step 2: Run the contract test and verify failure**

```bash
python -m pytest tests/test_compose_contract.py -q
```

Expected: failures identify published ports 5432/6379, absent beat/migrate services, obsolete variables, and root image execution.

- [ ] **Step 3: Replace the Dockerfile**

Build from `python:3.11-slim`, set `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, and `FLASK_APP=app:create_app`, install only `ca-certificates` and `ffmpeg`, install `requirements.txt`, create an unprivileged `app` user/group with UID/GID build args defaulting to 1000, copy source with `--chown`, and run as that user. Do not copy `.env`, `.aws`, `.claude`, Git metadata, tests caches, or local data into the image.

- [ ] **Step 4: Add startup scripts**

`scripts/start-web.sh` must use `set -eu`, run:

```bash
flask system check --services database,redis,s3,transcribe
exec gunicorn --config gunicorn.conf.py --bind 0.0.0.0:8005 wsgi:app
```

`scripts/start-worker.sh` must use `set -eu`, run the same checks, then exec:

```bash
celery -A tasks.celery_app worker --loglevel=INFO --queues=default,downloads,transcription
```

Mark both scripts executable.

- [ ] **Step 5: Replace Compose**

Use YAML anchors for shared app environment. Required topology:

```text
postgres: postgres:15-alpine, internal only, named postgres_data, pg_isready
redis: redis:7-alpine, internal only, redis-cli ping, appendonly no
migrate: app image, restart no, flask db upgrade
web: app image, 127.0.0.1:8005:8005, start-web.sh, healthz healthcheck
worker: app image, start-worker.sh, no host ports
beat: app image, celery -A tasks.celery_app beat --loglevel=INFO, no host ports
```

Add `init: true`, bounded stop grace periods, and `restart: unless-stopped` for long-running services. Do not mount the source tree or a local data directory in production Compose. Pass AWS credentials through the ignored `.env` variables and rely on boto3’s standard environment/default chain.

- [ ] **Step 6: Validate rendered Compose and build the image**

Create a temporary local `.env` with syntactically valid non-secret test values if the real file is not yet configured, then run:

```bash
docker compose config --quiet
python -m pytest tests/test_compose_contract.py -q
docker compose build
docker compose run --rm --no-deps web python -m compileall -q /app
```

Expected: configuration, contract test, build, and compilation all succeed. Remove the temporary `.env` if it was created solely for this step.

- [ ] **Step 7: Run a real PostgreSQL migration smoke test**

```bash
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose exec -T postgres psql -U podcasts -d podcasts -tAc "select version_num from alembic_version"
```

Expected: output is `0001_docker_foundation`.

- [ ] **Step 8: Commit the container topology**

```bash
git add Dockerfile docker-compose.yml .dockerignore scripts/start-web.sh scripts/start-worker.sh tests/test_compose_contract.py
git commit -m "feat: add hardened docker compose deployment"
```

---

### Task 11: Remove obsolete deployment and provider code

**Files:**
- Delete: `Procfile`
- Delete: `app.json`
- Delete: `runtime.txt`
- Delete: `deploy.sh`
- Delete: `heroku-setup.sh`
- Delete: `summarize.py`
- Delete: `prompts.py`
- Delete: `storage.py`
- Delete: `podcast_pipeline.py`
- Delete: `check_redis_queue.py`
- Delete: `debug_episodes.py`
- Delete: `trigger_discovery.py`
- Delete: `DEPLOYMENT.md`
- Delete: `DEPLOY_NOW.md`
- Delete: `LAUNCH.md`
- Delete: `NETWORK_ACCESS.md`
- Delete: `TROUBLESHOOTING.md`
- Create: `tests/test_repository_hygiene.py`

**Consumes:** The replacement Docker workflow from Tasks 1–10.

**Produces:** One supported deployment path and no executable legacy integration surface.

- [ ] **Step 1: Write a failing repository-hygiene test**

The test must scan runtime Python, templates, the Dockerfile, Compose, `.env.example`, and root deployment scripts. It must fail on case-insensitive references to:

```text
heroku
stripe
mailgun
openai
openrouter
register route
subscription route
send_welcome_email
send_transcript_email
send_summary_email
```

Exclude the approved design/spec and plan documents because they explain removals and later phases.

- [ ] **Step 2: Run the hygiene test and verify failure**

```bash
python -m pytest tests/test_repository_hygiene.py -q
```

Expected: it reports the legacy files and terms listed above.

- [ ] **Step 3: Delete obsolete code and deployment assets**

Remove exactly the files listed in this task. Before deletion, run `rg` for imports and route references; if any replacement runtime code still imports a deleted module, fix that reference within the owning Task 1–10 file and rerun its focused tests.

- [ ] **Step 4: Run hygiene and full unit tests**

```bash
python -m pytest tests/test_repository_hygiene.py -q
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit legacy removal**

```bash
git add tests/test_repository_hygiene.py
git add -u
git commit -m "chore: remove obsolete hosted service code"
```

---

### Task 12: Write operating documentation and prove the Phase 1 exit condition

**Files:**
- Replace: `README.md`
- Create: `docs/operations.md`
- Create: `tests/test_phase1_smoke.py`

**Consumes:** The complete Phase 1 image, Compose topology, CLI, health checks, and mocked adapter contracts.

**Produces:** Reproducible machine setup instructions, a Cloudflare handoff, backup/restore procedures, and evidence for the Phase 1 exit condition.

- [ ] **Step 1: Add an automated Phase 1 smoke test**

With feed, audio, S3, and Transcribe calls stubbed, exercise this sequence against the Flask app and eager Celery tasks:

```text
create one admin
log in
add one RSS feed
discover one episode
download and store one audio artifact
normalize and store one transcript artifact
mark the episode completed
retrieve the transcript through the authorized endpoint
repeat discovery and processing without duplicates
```

Assert one admin, one podcast, one episode, one download job, one transcribe job, deterministic S3 keys, and completed state.

- [ ] **Step 2: Run the smoke test and fix only Phase 1 integration defects**

```bash
python -m pytest tests/test_phase1_smoke.py -q
```

Expected: pass with no real network or AWS calls.

- [ ] **Step 3: Replace the README with Docker-first installation**

Document exact commands for:

1. Copying `.env.example` to `.env`.
2. Generating `SECRET_KEY` and a URL-safe PostgreSQL password.
3. Supplying AWS credentials with least-privilege access to `podcast-transcriptions-web-2025` and Amazon Transcribe.
4. Building and starting Compose.
5. Running `docker compose run --rm web flask admin create`.
6. Verifying `curl --fail http://127.0.0.1:8005/healthz` and `/readyz`.
7. Configuring the administrator-owned Cloudflare Tunnel hostname `podcasts.ennovocon.com` to origin `http://localhost:8005`.
8. Logging in and adding Apple or RSS podcast URLs.

State explicitly that Stripe, Mailgun, public registration, and Heroku are unsupported and absent.

- [ ] **Step 4: Add the operating runbook**

`docs/operations.md` must include:

- Start, stop, status, and log commands.
- Migration-first upgrade procedure.
- PostgreSQL backup with `pg_dump` and restore with `psql`.
- Admin password reset.
- AWS credential and Flask secret rotation.
- Safe episode retry behavior.
- Health-check diagnosis by component.
- Cloudflare Tunnel target and the fact that application login remains required.
- Persistence check across `docker compose down` followed by `docker compose up -d`; warn that `docker compose down -v` intentionally deletes PostgreSQL data.

- [ ] **Step 5: Run the complete automated suite**

```bash
python -m pytest -q
docker compose config --quiet
docker compose build
```

Expected: all tests pass and the image builds.

- [ ] **Step 6: Run the live local deployment check**

After the administrator has supplied real `.env` AWS credentials:

```bash
docker compose up -d
docker compose ps --all
curl --fail http://127.0.0.1:8005/healthz
curl --fail http://127.0.0.1:8005/readyz
docker compose run --rm web flask admin create
```

Expected: six services are present, long-running services are healthy/running, both HTTP checks return 200, and the CLI creates exactly one administrator. If the administrator already exists, the final command must refuse a second account with a clear nonzero exit.

- [ ] **Step 7: Prove database persistence without deleting the volume**

Record counts, recreate application containers, and compare:

```bash
docker compose exec -T postgres psql -U podcasts -d podcasts -tAc "select count(*) from admins; select count(*) from podcasts; select count(*) from episodes;"
docker compose down
docker compose up -d
docker compose exec -T postgres psql -U podcasts -d podcasts -tAc "select count(*) from admins; select count(*) from podcasts; select count(*) from episodes;"
```

Expected: the before and after counts match.

- [ ] **Step 8: Perform one real controlled episode check**

Set `DISCOVERY_EPISODE_LIMIT=1` for this controlled gate, restart the app services, then add an RSS or Apple Podcasts feed with a short recent episode. Verify:

```text
episode reaches completed
audio object exists under audio/podcast_<id>/episode_<id>/
transcript.txt and segments.json exist under transcripts/podcast_<id>/episode_<id>/
the private transcript renders only after login
no public object URL appears in page source or logs
```

Use a short episode for this gate to control Amazon Transcribe time and cost.

- [ ] **Step 9: Commit documentation and Phase 1 proof**

```bash
git add README.md docs/operations.md tests/test_phase1_smoke.py
git commit -m "docs: add docker operations and phase one smoke test"
```

---

## Phase 1 Completion Gate

Do not call Phase 1 complete until every item is true:

- [ ] `python -m pytest -q` passes.
- [ ] `docker compose config --quiet` passes.
- [ ] `docker compose build` passes.
- [ ] Fresh PostgreSQL upgrades to `0001_docker_foundation` through the `migrate` service.
- [ ] `web` is the only published service and binds `127.0.0.1:8005`.
- [ ] `/healthz` and `/readyz` return 200 with the stack running.
- [ ] The admin CLI creates one admin and refuses a second.
- [ ] Registration, payment, email-delivery, OpenAI/OpenRouter, and Heroku runtime paths are absent.
- [ ] One feed produces one idempotent episode and a completed private S3-backed transcript.
- [ ] Recreating application containers preserves the admin, feed, episode, and job records.
- [ ] The Cloudflare Tunnel can target `http://localhost:8005`; DNS/tunnel configuration remains administrator-owned.
- [ ] The working tree contains no `.env`, AWS credentials, transcript bodies, signed URLs, or other secrets.

## Deferred Plans

After this gate passes, write and approve separate implementation plans in this order:

1. Phase 2: restricted `claude -p` extraction, schema-validated learning bundles, learning UI, and publication outbox.
2. Phase 3: Lamad, Ebenezer, and Agent Factory adapters with independent idempotent delivery and the approved ASP routing rules.
3. Phase 4: cited Ask & Learn retrieval, lesson generation, and learning-outcome feedback to Lamad and Ebenezer.
