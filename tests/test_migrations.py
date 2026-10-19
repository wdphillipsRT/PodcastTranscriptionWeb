"""The actual Alembic history must match ORM metadata and reverse cleanly."""

import importlib

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import sqlalchemy as sa

from extensions import db


def test_initial_migration_upgrade_schema_and_downgrade(app_factory, tmp_path):
    importlib.import_module("models")
    database = tmp_path / "migration.sqlite"
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{database}")
    runner = app.test_cli_runner()
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    expected = {"admins", "content_sources", "content_items", "source_items", "discovery_runs",
                "discovery_candidates", "backfill_requests", "processing_jobs"}
    with app.app_context():
        inspector = sa.inspect(db.engine)
        assert set(inspector.get_table_names()) == expected | {"alembic_version"}
        with db.engine.connect() as connection:
            assert connection.scalar(sa.text("select version_num from alembic_version")) == "0001_media_foundation"
            context = MigrationContext.configure(connection, opts={"compare_type": True})
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
    result = runner.invoke(args=["db", "downgrade", "base"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    with app.app_context():
        assert set(sa.inspect(db.engine).get_table_names()) == {"alembic_version"}
        with db.engine.connect() as connection:
            assert connection.scalar(sa.text("select count(*) from alembic_version")) == 0
    # A fresh upgrade after downgrade catches stale indexes and broken reversibility.
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output + repr(result.exception)
