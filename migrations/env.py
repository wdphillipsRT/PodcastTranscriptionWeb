"""Alembic uses the factory's configured engine without constructing another app."""

from alembic import context
from flask import current_app

import models  # Register all tables even for a fresh flask db invocation.


database = current_app.extensions["migrate"].db
target_metadata = database.metadata


def run_migrations_offline():
    context.configure(
        url=database.engine.url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    with database.engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
