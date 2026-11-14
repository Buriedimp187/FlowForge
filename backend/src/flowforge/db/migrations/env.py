"""Alembic environment for FlowForge.

Migrations are hand-written SQL (the schema's single source of truth, with no
parallel SQLAlchemy ``Table`` definitions), so there is no ``target_metadata``
and no autogenerate. Only online mode is supported.

The migration advisory lock is taken here so that every entry point -- the
programmatic :mod:`flowforge.db.migrate` helpers and the ``alembic`` CLI --
serializes concurrent starters (docs/architecture.md section 12:
``flowforge migrate`` "takes a ``pg_advisory_lock`` so concurrent starters don't
race"). PostgreSQL DDL is transactional, so one migration run is atomic.
"""

from __future__ import annotations

import contextlib

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from flowforge.db.engine import create_db_engine, database_url_from_env
from flowforge.db.migrate import MIGRATION_LOCK_KEY

config = context.config


def _run(connection: Connection) -> None:
    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
    connection.commit()  # the lock is session-level and survives the commit
    try:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        # Closing the connection releases the lock anyway, so a failed unlock
        # must not mask the migration's own exception.
        with contextlib.suppress(DBAPIError):
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
            connection.commit()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_db_engine(database_url_from_env())
    try:
        with engine.connect() as owned:
            _run(owned)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported; run against a database")

run_migrations_online()
