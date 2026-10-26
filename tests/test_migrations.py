"""The actual Alembic history must match ORM metadata and reverse cleanly."""

import importlib
import importlib.util
from io import StringIO
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
import pytest

from extensions import db


def test_initial_migration_upgrade_schema_and_downgrade(app_factory, tmp_path):
    importlib.import_module("models")
    database = tmp_path / "migration.sqlite"
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{database}")
    runner = app.test_cli_runner()
    result = runner.invoke(args=["db", "upgrade", "0001_media_foundation"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO admins (id, email, password_hash) VALUES (1, 'owner@example.com', 'hash')"))
        connection.execute(sa.text("""INSERT INTO content_sources
            (id, admin_id, source_type, external_id, canonical_url, name, provider_metadata)
            VALUES (1, 1, 'podcast_rss', 'feed', 'https://example.com/feed', 'Feed', '{}')"""))
        connection.execute(sa.text("""INSERT INTO content_items
            (id, admin_id, item_type, platform, external_id, canonical_url, title, status,
             transcript_s3_key, transcript_sha256, segments_s3_key, segments_sha256)
            VALUES (1, 1, 'youtube_video', 'youtube', 'old-item', 'https://example.com/item',
                    'Existing item', 'completed', 'transcript.json', :digest, 'segments.json', :digest)"""),
                           {"digest": "a" * 64})
        connection.execute(sa.text("INSERT INTO source_items (source_id, item_id) VALUES (1, 1)"))
        connection.execute(sa.text("INSERT INTO processing_jobs (item_id, stage) VALUES (1, 'normalize')"))
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    expected = {"admins", "content_sources", "content_items", "source_items", "discovery_runs",
                "discovery_candidates", "backfill_requests", "processing_jobs",
                "classification_revisions", "classification_blocks", "segment_overrides",
                "source_classification_rules", "classification_sweeps", "guest_extractions",
                "knowledge_export_batches", "knowledge_item_exports"}
    with app.app_context():
        inspector = sa.inspect(db.engine)
        assert set(inspector.get_table_names()) == expected | {"alembic_version"}
        media_column = next(column for column in inspector.get_columns("content_items")
                            if column["name"] == "remote_media_url")
        assert isinstance(media_column["type"], sa.String)
        assert media_column["type"].length == 4096
        assert media_column["nullable"] is True
        # Bind-time validation must retain the migration's bounded SQL type.
        for dialect in (sqlite.dialect(), postgresql.dialect()):
            assert db.metadata.tables["content_items"].c.remote_media_url.type.compile(
                dialect=dialect) == "VARCHAR(4096)"
        with db.engine.connect() as connection:
            assert connection.scalar(sa.text("select version_num from alembic_version")) == "0004_router_classification"
            retained = connection.execute(sa.text("SELECT status, transcript_s3_key, transcript_sha256, segments_s3_key, segments_sha256, active_classification_revision_id FROM content_items WHERE id = 1")).one()
            assert tuple(retained) == ("completed", "transcript.json", "a" * 64, "segments.json", "a" * 64, None)
            assert connection.scalar(sa.text("SELECT count(*) FROM source_items WHERE item_id = 1")) == 1
            assert connection.scalar(sa.text("SELECT count(*) FROM processing_jobs WHERE item_id = 1")) == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
            assert compare_metadata(context, db.metadata) == []
        for table in expected:
            reflected = inspector.get_check_constraints(table)
            declared = [c for c in db.metadata.tables[table].constraints if isinstance(c, sa.CheckConstraint)]
            assert {c["name"] for c in reflected} == {c.name for c in declared}
            actual = {c["name"]: " ".join(c["sqltext"].split()) for c in reflected}
            desired = {c.name: " ".join(str(c.sqltext).split()) for c in declared}
            assert actual == desired
            for foreign_key in inspector.get_foreign_keys(table):
                assert foreign_key["options"]["ondelete"] in {"CASCADE", "SET NULL"}
        index_columns = {
            "content_sources": {("enabled",)}, "content_items": {("status",), ("published_at",)},
            "discovery_runs": {("state",)}, "backfill_requests": {("state",)},
            "processing_jobs": {("state",)},
        }
        for table, required in index_columns.items():
            assert required <= {tuple(index["column_names"]) for index in inspector.get_indexes(table)}
    result = runner.invoke(args=["db", "downgrade", "0001_media_foundation"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT transcript_s3_key FROM content_items WHERE id = 1")) == "transcript.json"
        assert connection.scalar(sa.text("SELECT count(*) FROM source_items WHERE item_id = 1")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM processing_jobs WHERE item_id = 1")) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert "active_classification_revision_id" not in {c["name"] for c in sa.inspect(connection).get_columns("content_items")}
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)


    with app.app_context(), db.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), db.metadata) == []
    result = runner.invoke(args=["db", "downgrade", "base"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context():
        assert set(sa.inspect(db.engine).get_table_names()) == {"alembic_version"}
        with db.engine.connect() as connection:
            assert connection.scalar(sa.text("select count(*) from alembic_version")) == 0
    # A fresh upgrade after downgrade catches stale indexes and broken reversibility.
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)


def test_classification_migration_renders_postgresql_in_dependency_order():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0002_commercial_classification.py"
    specification = importlib.util.spec_from_file_location("classification_migration", path)
    assert specification is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    assert migration.revision == "0002_commercial_classification"
    assert migration.down_revision == "0001_media_foundation"
    sql = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    statements = sql.getvalue()
    assert "BIGINT" in statements and "VARCHAR(27)" in statements
    assert statements.index("CREATE TABLE classification_sweeps") < statements.index("ALTER TABLE content_items ADD COLUMN active_classification_revision_id")
    assert "ADD CONSTRAINT fk_content_items_active_classification_revision" in statements
    assert statements.index("DROP CONSTRAINT fk_content_items_active_classification_revision") < statements.index("DROP TABLE classification_revisions")


def test_migrated_storage_accepts_public_selectors_and_rejects_signed_bindings(app_factory, tmp_path):
    m = importlib.import_module("models")
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'locator-migration.sqlite'}")
    result = app.test_cli_runner().invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        owner = m.Admin(email="owner@example.org", password_hash="test")
        db.session.add(owner)
        db.session.flush()
        origin = m.ContentSource(admin_id=owner.id, source_type="podcast_rss", external_id="feed",
            canonical_url="https://example.org/feed?show=2", name="Selectors")
        db.session.add(origin)
        episode = m.ContentItem(admin_id=owner.id, platform="podcast", item_type="podcast_episode",
            external_id="episode", canonical_url="https://example.org/episode?id=2", title="Episode",
            remote_media_url="https://cdn.example.org/audio?episode=2")
        db.session.add(episode)
        db.session.commit()
        db.session.expire_all()
        assert origin.canonical_url.endswith("?show=2")
        assert episode.remote_media_url.endswith("?episode=2")
        with pytest.raises(sa.exc.StatementError):
            db.session.execute(sa.update(m.ContentItem).where(m.ContentItem.id == episode.id).values(
                remote_media_url="https://cdn.example.org/audio?X-Amz-Signature=secret"))


def test_knowledge_export_migration_keeps_rows_and_reverses(app_factory, tmp_path):
    importlib.import_module("models")
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'knowledge.sqlite'}")
    runner = app.test_cli_runner()
    result = runner.invoke(args=["db", "upgrade", "0002_commercial_classification"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO admins (id, email, password_hash) VALUES (1, 'owner@example.com', 'hash')"))
        connection.execute(sa.text("""INSERT INTO content_sources
            (id, admin_id, source_type, external_id, canonical_url, name, provider_metadata)
            VALUES (1, 1, 'podcast_rss', 'feed', 'https://example.com/feed', 'Feed', '{}')"""))
        connection.execute(sa.text("""INSERT INTO content_items
            (id, admin_id, item_type, platform, external_id, canonical_url, title, status)
            VALUES (1, 1, 'podcast_episode', 'podcast', 'episode', 'https://example.com/e', 'Episode', 'completed')"""))
        connection.execute(sa.text("INSERT INTO source_items (source_id, item_id) VALUES (1, 1)"))
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.connect() as connection:
        assert connection.scalar(sa.text("select version_num from alembic_version")) == "0004_router_classification"
        assert tuple(connection.execute(sa.text(
            "SELECT feed_hosts, admin_hosts, hosts_updated_at FROM content_sources WHERE id = 1")).one()) == (None, None, None)
        assert tuple(connection.execute(sa.text(
            "SELECT title, feed_guests, admin_guests FROM content_items WHERE id = 1")).one()) == ("Episode", None, None)
        assert connection.scalar(sa.text("SELECT count(*) FROM source_items WHERE item_id = 1")) == 1
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, db.metadata) == []
    result = runner.invoke(args=["db", "downgrade", "0002_commercial_classification"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.connect() as connection:
        inspector = sa.inspect(connection)
        assert not {"guest_extractions", "knowledge_export_batches", "knowledge_item_exports"} & set(inspector.get_table_names())
        assert "feed_hosts" not in {column["name"] for column in inspector.get_columns("content_sources")}
        assert "feed_guests" not in {column["name"] for column in inspector.get_columns("content_items")}
        assert connection.scalar(sa.text("SELECT count(*) FROM source_items WHERE item_id = 1")) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_knowledge_export_migration_renders_postgresql():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0003_knowledge_export.py"
    specification = importlib.util.spec_from_file_location("knowledge_migration", path)
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("0003_knowledge_export", "0002_commercial_classification")
    sql = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    statements = sql.getvalue()
    assert "ALTER TABLE content_sources ADD COLUMN feed_hosts JSON" in statements
    assert "ALTER TABLE content_items ADD COLUMN admin_guests JSON" in statements
    assert statements.index("CREATE TABLE knowledge_export_batches") < statements.index("CREATE TABLE knowledge_item_exports")
    assert statements.index("DROP TABLE knowledge_item_exports") < statements.index("DROP TABLE knowledge_export_batches")


def test_router_classification_migration_adds_and_removes_the_block_key_generation(app_factory, tmp_path):
    importlib.import_module("models")
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'router.sqlite'}")
    runner = app.test_cli_runner()
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.connect() as connection:
        columns = {column["name"]: column for column in sa.inspect(connection).get_columns("classification_blocks")}
        assert columns["request_generation"]["nullable"] is False
        assert str(columns["request_generation"]["default"]).strip("'\"") == "0"
        assert columns["request_generation_started_at"]["nullable"] is True
        assert connection.scalar(sa.text("select version_num from alembic_version")) == "0004_router_classification"
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, db.metadata) == []
    result = runner.invoke(args=["db", "downgrade", "0003_knowledge_export"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context(), db.engine.connect() as connection:
        names = {c["name"] for c in sa.inspect(connection).get_columns("classification_blocks")}
        assert not {"request_generation", "request_generation_started_at"} & names


def test_router_classification_migration_renders_postgresql():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0004_router_classification.py"
    specification = importlib.util.spec_from_file_location("router_migration", path)
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("0004_router_classification", "0003_knowledge_export")
    sql = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    statements = sql.getvalue()
    assert "ALTER TABLE classification_blocks ADD COLUMN request_generation INTEGER DEFAULT '0' NOT NULL" in statements
    assert "ALTER TABLE classification_blocks ADD COLUMN request_generation_started_at TIMESTAMP WITH TIME ZONE" in statements
    assert "ALTER TABLE classification_blocks DROP COLUMN request_generation" in statements
    assert "ALTER TABLE classification_blocks DROP COLUMN request_generation_started_at" in statements
