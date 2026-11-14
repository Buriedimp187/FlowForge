"""Programmatic migration helpers over Alembic.

Thin wrappers so application and test code run migrations against an explicit
:class:`sqlalchemy.Engine` without an ``alembic.ini``. The script location is
inside the package (docs/architecture.md section 12: "Alembic lives inside the
Python package, so it ships in the image"). The advisory lock and transaction
handling live in ``migrations/env.py``.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: Session-level advisory lock held while a migration runs ("FFmigrat" as a
#: big-endian 64-bit integer, which fits PostgreSQL's signed bigint). Taken in
#: migrations/env.py so the CLI and these helpers serialize identically.
MIGRATION_LOCK_KEY = int.from_bytes(b"FFmigrat", "big")


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    """The single head revision of the script directory (no database needed)."""
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {heads!r}")
    return heads[0]


def current_revision(engine: Engine) -> str | None:
    """The revision recorded in the database, or ``None`` if unmigrated."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.connect() as connection:
        command.upgrade(alembic_config(connection), revision)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.downgrade(alembic_config(connection), revision)
