"""Relational invariants must hold through ORM and direct database writes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import importlib
from threading import Barrier

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, StatementError

from extensions import db


NAMES = ("Admin", "ContentSource", "ContentItem", "SourceItem", "DiscoveryRun",
         "DiscoveryCandidate", "BackfillRequest", "ProcessingJob")


@pytest.fixture
def schema(app):
    models = importlib.import_module("models")
    for name in NAMES:
        assert hasattr(models, name), f"models must expose {name}"
    assert models.db is db, "models must share the factory's unbound db extension"
    with app.app_context():
        with db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.create_all()
        yield models
        db.session.remove()
        db.drop_all()


def admin(m):
    value = m.Admin(email="owner@example.com", password_hash="test-hash")
    db.session.add(value)
    db.session.flush()
    return value


def source(m, owner, **changes):
    values = dict(admin_id=owner.id, source_type="youtube_channel", external_id="UC123",
                  canonical_url="https://www.youtube.com/channel/UC123", name="Channel")
    values.update(changes)
    value = m.ContentSource(**values)
    db.session.add(value)
    db.session.flush()
    return value


def item(m, owner, **changes):
    values = dict(admin_id=owner.id, platform="youtube", item_type="youtube_video",
                  external_id="abcdefghijk", canonical_url="https://www.youtube.com/watch?v=abcdefghijk",
                  title="Video")
    values.update(changes)
    value = m.ContentItem(**values)
    db.session.add(value)
    db.session.flush()
    return value


def graph(m):
    owner = admin(m)
    origin = source(m, owner)
    content = item(m, owner)
    run = m.DiscoveryRun(source_id=origin.id, run_type="initial")
    db.session.add(run)
    db.session.flush()
    return owner, origin, content, run


def test_schema_replaces_legacy_models(schema):
    assert set(db.metadata.tables) == {
        "admins", "content_sources", "content_items", "source_items", "discovery_runs",
        "discovery_candidates", "backfill_requests", "processing_jobs",
    }
    assert not hasattr(schema, "User")


def test_database_allows_only_one_admin_and_rejects_false_singleton(schema):
    owner = admin(schema)
    assert owner.singleton_key is True
    with pytest.raises(IntegrityError):
        db.session.execute(schema.Admin.__table__.insert().values(
            email="second@example.com", password_hash="hash"))
    db.session.rollback()
    with pytest.raises(IntegrityError):
        db.session.execute(schema.Admin.__table__.insert().values(
            email="other@example.com", password_hash="hash", singleton_key=False))


def test_one_admin_survives_competing_independent_transactions(app_factory, tmp_path):
    m = importlib.import_module("models")
    assert hasattr(m, "Admin"), "models must expose Admin"
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'race.sqlite'}")
    with app.app_context():
        db.create_all()
        engine = db.engine

    ready = Barrier(2)

    def provision(number):
        try:
            with engine.begin() as connection:
                ready.wait(timeout=5)
                connection.execute(m.Admin.__table__.insert().values(
                    email=f"admin{number}@example.com", password_hash="hash"))
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(provision, (1, 2))) == [False, True]
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(m.Admin.__table__)) == 1
    engine.dispose()


@pytest.mark.parametrize("kind", ["source", "item", "membership", "job", "candidate"])
def test_identity_uniqueness_is_database_enforced(schema, kind):
    owner, origin, content, run = graph(schema)
    factories = {
        "source": lambda: schema.ContentSource(admin_id=owner.id, source_type=origin.source_type,
            external_id=origin.external_id, canonical_url=origin.canonical_url, name="Duplicate"),
        "item": lambda: schema.ContentItem(admin_id=owner.id, platform=content.platform,
            item_type=content.item_type, external_id=content.external_id,
            canonical_url=content.canonical_url, title="Duplicate"),
        "membership": lambda: schema.SourceItem(source_id=origin.id, item_id=content.id),
        "job": lambda: schema.ProcessingJob(item_id=content.id, stage="youtube_captions"),
        "candidate": lambda: schema.DiscoveryCandidate(run_id=run.id, platform=content.platform,
            external_id=content.external_id, descriptor={"title": "Normalized"}),
    }
    if kind in {"membership", "job", "candidate"}:
        db.session.add(factories[kind]())
        db.session.flush()
    db.session.add(factories[kind]())
    with pytest.raises(IntegrityError):
        db.session.flush()


def test_shared_item_and_different_stages_are_allowed(schema):
    owner, origin, content, _ = graph(schema)
    playlist = source(schema, owner, source_type="youtube_playlist", external_id="PL123")
    db.session.add_all([
        schema.SourceItem(source_id=origin.id, item_id=content.id),
        schema.SourceItem(source_id=playlist.id, item_id=content.id),
        schema.ProcessingJob(item_id=content.id, stage="youtube_captions"),
        schema.ProcessingJob(item_id=content.id, stage="normalize"),
    ])
    db.session.commit()
    assert len(content.sources) == 2
    assert len(content.processing_jobs) == 2


VALID_SELECTORS = [
    dict(selector_type="date_range", start_date=date(2020, 1, 1), end_date=date(2020, 12, 31)),
    dict(selector_type="count", requested_count=25),
    dict(selector_type="explicit_url", explicit_url="https://www.youtube.com/watch?v=abcdefghijk"),
]
INVALID_SELECTORS = [
    {}, dict(selector_type="bogus"), dict(selector_type="count"),
    dict(selector_type="count", requested_count=0),
    dict(selector_type="count", requested_count=-1),
    dict(selector_type="count", requested_count=2, explicit_url="https://example.com/video"),
    dict(selector_type="date_range", start_date=date(2020, 1, 1)),
    dict(selector_type="date_range", start_date=date(2021, 1, 1), end_date=date(2020, 1, 1)),
    dict(selector_type="explicit_url", explicit_url=""),
    dict(selector_type="explicit_url", explicit_url="   "),
    dict(selector_type="explicit_url", explicit_url="https://example.com", requested_count=2),
]


@pytest.mark.parametrize("selector", VALID_SELECTORS)
def test_valid_backfill_selectors(schema, selector):
    origin = source(schema, admin(schema))
    request = schema.BackfillRequest(source_id=origin.id, **selector)
    db.session.add(request)
    db.session.commit()
    assert request.state == "pending"


@pytest.mark.parametrize("selector", INVALID_SELECTORS)
def test_invalid_backfill_selector_rejected_by_orm(schema, selector):
    origin = source(schema, admin(schema))
    with pytest.raises(ValueError, match="selector"):
        db.session.add(schema.BackfillRequest(source_id=origin.id, **selector))
        db.session.flush()


@pytest.mark.parametrize("selector", INVALID_SELECTORS)
def test_invalid_backfill_selector_rejected_by_database(schema, selector):
    origin = source(schema, admin(schema))
    with pytest.raises(IntegrityError):
        db.session.execute(schema.BackfillRequest.__table__.insert().values(source_id=origin.id, **selector))


def test_backfill_updates_cannot_bypass_model_validation(schema):
    origin = source(schema, admin(schema))
    request = schema.BackfillRequest(source_id=origin.id, selector_type="count", requested_count=2)
    db.session.add(request)
    db.session.commit()
    request.explicit_url = "https://example.com"
    with pytest.raises(ValueError, match="selector"):
        db.session.flush()


CHECK_FIELDS = [
    ("ContentSource", "source_type"), ("ContentItem", "item_type"),
    ("ContentItem", "platform"), ("ContentItem", "status"),
    ("ContentItem", "transcript_origin"), ("ContentItem", "last_successful_stage"),
    ("ContentItem", "processing_stage"), ("DiscoveryRun", "run_type"),
    ("DiscoveryRun", "state"), ("DiscoveryCandidate", "platform"),
    ("BackfillRequest", "state"), ("ProcessingJob", "stage"), ("ProcessingJob", "state"),
]


@pytest.mark.parametrize("model_name,field", CHECK_FIELDS)
def test_controlled_values_have_database_checks(schema, model_name, field):
    _, origin, content, run = graph(schema)
    objects = {"ContentSource": origin, "ContentItem": content, "DiscoveryRun": run,
               "DiscoveryCandidate": schema.DiscoveryCandidate(run_id=run.id, platform="youtube",
                   external_id="other-video", descriptor={}),
               "BackfillRequest": schema.BackfillRequest(source_id=origin.id, selector_type="count", requested_count=2),
               "ProcessingJob": schema.ProcessingJob(item_id=content.id, stage="normalize")}
    instance = objects[model_name]
    db.session.add(instance)
    db.session.flush()
    with pytest.raises(IntegrityError):
        db.session.execute(sa.update(type(instance).__table__).where(
            type(instance).__table__.c.id == instance.id).values({field: "invalid-value"}))


def test_item_type_and_platform_must_agree(schema):
    owner = admin(schema)
    with pytest.raises(IntegrityError):
        item(schema, owner, platform="podcast", item_type="youtube_video")


def test_defaults_and_utc_timestamps_survive_database_round_trip(schema):
    owner, origin, content, run = graph(schema)
    aware = datetime(2020, 1, 2, 12, tzinfo=timezone(timedelta(hours=-5)))
    content.published_at = aware
    content.transcript_origin = "creator_caption"
    content.media_s3_key = "private/media.mp3"
    content.transcript_s3_key = "private/transcript.txt"
    db.session.commit()
    db.session.expire_all()
    assert content.status == "discovered"
    assert origin.enabled is True
    assert run.state == "pending"
    for instance in (owner, origin, content, run):
        assert instance.created_at.utcoffset() == timedelta(0)
        assert instance.updated_at.utcoffset() == timedelta(0)
    assert content.published_at == aware.astimezone(timezone.utc)
    assert content.published_at.utcoffset() == timedelta(0)
    before = content.updated_at
    content.title = "Updated"
    db.session.commit()
    assert content.updated_at > before
    assert not hasattr(content, "transcript_url")


def test_naive_timestamps_are_rejected(schema):
    owner = admin(schema)
    with pytest.raises((ValueError, StatementError), match="timezone"):
        item(schema, owner, published_at=datetime(2020, 1, 1))


@pytest.mark.parametrize("unsafe_url", [
    "https://user:secret@example.com/audio.mp3", "https://example.com/audio.mp3?token=secret",
    "file:///etc/passwd", "not-a-url",
])
def test_item_remote_media_url_must_be_safe(schema, unsafe_url):
    owner = admin(schema)
    with pytest.raises((ValueError, StatementError)):
        item(schema, owner, remote_media_url=unsafe_url)


def test_item_allows_safe_podcast_media_url(schema):
    owner = admin(schema)
    episode = item(schema, owner, platform="podcast", item_type="podcast_episode",
                   remote_media_url="https://cdn.example.com/episode.mp3")
    assert episode.remote_media_url == "https://cdn.example.com/episode.mp3"


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("unsafe_url", [
    "https://user:secret@example.com/audio.mp3",
    "https://example.com/audio.mp3?token=secret",
    "https://user:secret@example.com/audio.mp3?token=secret",
])
def test_core_writes_reject_unsafe_remote_media_urls(schema, operation, unsafe_url):
    owner = admin(schema)
    if operation == "insert":
        statement = sa.insert(schema.ContentItem).values(
            admin_id=owner.id, platform="podcast", item_type="podcast_episode",
            external_id="core-episode", canonical_url="https://example.com/episode",
            title="Core episode", remote_media_url=unsafe_url,
        )
    else:
        episode = item(schema, owner, platform="podcast", item_type="podcast_episode",
                       remote_media_url="https://cdn.example.com/episode.mp3")
        statement = sa.update(schema.ContentItem).where(schema.ContentItem.id == episode.id).values(
            remote_media_url=unsafe_url)
    with pytest.raises(StatementError, match="credentials"):
        db.session.execute(statement)


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("safe_url", [None, "https://cdn.example.com/episode.mp3", "https://cdn.example.com/audio?episode=1",
    "https://cdn.example.com/audio?episode=2&episode=1", "https://cdn.example.com/audio?episode=1&episode=2"])
def test_core_writes_preserve_safe_remote_media_urls_and_null(schema, operation, safe_url):
    owner = admin(schema)
    if operation == "insert":
        db.session.execute(sa.insert(schema.ContentItem).values(
            admin_id=owner.id, platform="podcast", item_type="podcast_episode",
            external_id="core-episode", canonical_url="https://example.com/episode",
            title="Core episode", remote_media_url=safe_url,
        ))
        episode = db.session.scalar(sa.select(schema.ContentItem).where(
            schema.ContentItem.external_id == "core-episode"))
    else:
        episode = item(schema, owner, platform="podcast", item_type="podcast_episode",
                       remote_media_url="https://cdn.example.com/original.mp3")
        db.session.execute(sa.update(schema.ContentItem).where(schema.ContentItem.id == episode.id).values(
            remote_media_url=safe_url))
    db.session.commit()
    db.session.expire_all()
    assert episode.remote_media_url == safe_url


@pytest.mark.parametrize("direct", [False, True])
def test_admin_delete_cascades_all_eight_tables(schema, direct):
    owner, origin, content, run = graph(schema)
    backfill = schema.BackfillRequest(source_id=origin.id, selector_type="count", requested_count=2)
    db.session.add(backfill)
    db.session.flush()
    run.backfill_id = backfill.id
    db.session.add_all([
        schema.SourceItem(source_id=origin.id, item_id=content.id),
        schema.ProcessingJob(item_id=content.id, stage="normalize"),
        schema.DiscoveryCandidate(run_id=run.id, platform="youtube", external_id=content.external_id, descriptor={}),
    ])
    db.session.commit()
    if direct:
        db.session.execute(sa.delete(schema.Admin.__table__).where(schema.Admin.id == owner.id))
    else:
        db.session.delete(owner)
    db.session.commit()
    for table in db.metadata.sorted_tables:
        assert db.session.scalar(sa.select(sa.func.count()).select_from(table)) == 0, table.name


def test_source_delete_retains_shared_item_and_other_memberships(schema):
    owner, origin, content, run = graph(schema)
    other = source(schema, owner, source_type="youtube_playlist", external_id="PL123")
    db.session.add_all([schema.SourceItem(source_id=origin.id, item_id=content.id),
                        schema.SourceItem(source_id=other.id, item_id=content.id)])
    db.session.commit()
    db.session.delete(origin)
    db.session.commit()
    assert db.session.get(schema.ContentItem, content.id) is content
    assert db.session.query(schema.SourceItem).count() == 1
    assert db.session.query(schema.DiscoveryRun).count() == 0


@pytest.mark.parametrize("unsafe", [
    {"raw_response": {"api_key": "secret"}}, {"headers": {"Authorization": "secret"}},
    {"canonical_url": "https://user:secret@example.com/video"},
    {"remote_media_url": "https://example.com/audio?token=secret"},
    {"title": {"unexpected": "nested data"}}, {"duration_seconds": -1},
    {"published_at": "not-a-date"}, {"platform": "private-provider"},
])
def test_candidate_rejects_raw_or_unsafe_descriptor_data(schema, unsafe):
    _, _, _, run = graph(schema)
    with pytest.raises((ValueError, StatementError)):
        db.session.add(schema.DiscoveryCandidate(run_id=run.id, platform="youtube", external_id="abcdefghijk", descriptor=unsafe))
        db.session.flush()


def test_candidate_normalized_descriptor_round_trip_and_finalization_cleanup(schema):
    _, _, _, run = graph(schema)
    descriptor = {"platform": "youtube", "item_type": "youtube_video", "external_id": "abcdefghijk",
                  "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                  "title": "Normalized", "published_at": "2020-01-01T00:00:00+00:00",
                  "duration_seconds": 42, "channel_id": "UC123", "creator_name": "Creator"}
    candidate = schema.DiscoveryCandidate(run_id=run.id, platform="youtube", external_id="abcdefghijk", descriptor=descriptor)
    db.session.add(candidate)
    db.session.commit()
    db.session.expire_all()
    assert candidate.descriptor == descriptor
    candidate.descriptor = None
    db.session.commit()
    assert candidate.descriptor is None


def test_candidate_mutation_cannot_bypass_descriptor_validation(schema):
    _, _, _, run = graph(schema)
    candidate = schema.DiscoveryCandidate(run_id=run.id, platform="youtube", external_id="abcdefghijk", descriptor={"title": "Safe"})
    db.session.add(candidate)
    db.session.commit()
    candidate.descriptor["raw_response"] = {"token": "secret"}
    with pytest.raises((ValueError, StatementError)):
        db.session.flush()


def test_public_query_locators_and_reacquisition_marker_survive_candidate_bind(schema):
    _, _, _, run = graph(schema)
    descriptor = {"platform": "podcast", "canonical_url": "https://example.org/episode?id=2",
        "remote_media_url": "https://cdn.example.org/audio?episode=2&format=mp3",
        "requires_reacquisition": False}
    candidate = schema.DiscoveryCandidate(run_id=run.id, platform="podcast", external_id="episode-2", descriptor=descriptor)
    db.session.add(candidate)
    db.session.commit()
    db.session.expire_all()
    assert candidate.descriptor == descriptor


@pytest.mark.parametrize("key", ["ToKeN", "api_key", "X-Amz-Signature", "X-Goog-Credential", "Sig", "%74oken"])
def test_case_insensitive_signed_queries_rejected_at_bind_time(schema, key):
    owner = admin(schema)
    with pytest.raises(StatementError, match="credentials"):
        db.session.execute(sa.insert(schema.ContentItem).values(admin_id=owner.id,
            platform="podcast", item_type="podcast_episode", external_id="unsafe",
            canonical_url="https://example.org/episode", title="Episode",
            remote_media_url=f"https://cdn.example.org/audio?{key}=secret"))


@pytest.mark.parametrize("query", ["episode=2&episode=1", "episode=1&episode=2"])
def test_candidate_core_bind_preserves_duplicate_public_query_order(schema, query):
    _, _, _, run = graph(schema)
    descriptor = {"canonical_url": "https://example.org/episode?" + query,
        "remote_media_url": "https://cdn.example.org/audio?" + query}
    db.session.execute(sa.insert(schema.DiscoveryCandidate).values(run_id=run.id, platform="podcast",
        external_id="query-order", descriptor=descriptor))
    db.session.commit()
    value = db.session.scalar(sa.select(schema.DiscoveryCandidate).where(
        schema.DiscoveryCandidate.external_id == "query-order"))
    assert value.descriptor == descriptor


def test_remote_media_core_bind_rejects_sensitive_duplicates(schema):
    owner = admin(schema)
    with pytest.raises(StatementError, match="credentials"):
        db.session.execute(sa.insert(schema.ContentItem).values(admin_id=owner.id,
            platform="podcast", item_type="podcast_episode", external_id="unsafe-duplicate",
            canonical_url="https://example.org/episode", title="Episode",
            remote_media_url="https://cdn.example.org/audio?episode=2&TOKEN=&token=secret"))
