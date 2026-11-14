"""SQLAlchemy Core engine configuration for PostgreSQL via psycopg 3.

docs/m2-persistence-design.md D2: SQLAlchemy Core for connection and
transaction management, explicit SQL, psycopg 3 as the only driver, no ORM.

Transaction boundaries stay explicit: connections are never autocommit, so
every unit of work is opened with ``engine.begin()`` (commit on success,
rollback on error) or ``engine.connect()`` followed by an explicit
``commit()``/``rollback()``. Nothing here opens a session or an implicit unit
of work.

``READ COMMITTED`` is requested explicitly because the queue/lease/fencing
design assumes it (docs/architecture.md section 4, Risk R7); a deployment that
silently changed the default isolation level must not change correctness.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url

DATABASE_URL_ENV = "FLOWFORGE_DATABASE_URL"

#: The isolation level the persistence design assumes throughout.
ISOLATION_LEVEL = "READ COMMITTED"

#: psycopg 3's SQLAlchemy dialect name.
_DIALECT = "postgresql+psycopg"

#: Timestamps are UTC ``timestamptz`` (docs/architecture.md section 2.3);
#: pinning the session time zone makes any textual rendering UTC as well. Stored
#: values are absolute instants either way.
_SESSION_OPTIONS = "-c timezone=UTC"


def normalize_database_url(url: str | URL) -> URL:
    """Return ``url`` as a ``postgresql+psycopg`` URL.

    Accepts ``postgresql://``, ``postgres://`` and ``postgresql+psycopg://``.
    Anything else -- another database, or another PostgreSQL driver such as
    psycopg2 or asyncpg -- is rejected, because this layer supports exactly one
    driver.
    """
    parsed = make_url(url)
    backend, _, driver = parsed.drivername.partition("+")
    if backend not in ("postgresql", "postgres"):
        raise ValueError(f"unsupported database backend {backend!r}: only PostgreSQL is supported")
    if driver not in ("", "psycopg"):
        raise ValueError(f"unsupported PostgreSQL driver {driver!r}: only psycopg 3 ('psycopg') is supported")
    return parsed.set(drivername=_DIALECT)


def database_url_from_env(variable: str = DATABASE_URL_ENV) -> str:
    """Read the database URL from the environment, failing loudly if absent."""
    value = os.environ.get(variable)
    if not value:
        raise RuntimeError(f"environment variable {variable} is not set")
    return value


def create_db_engine(url: str | URL, *, echo: bool = False) -> Engine:
    """Create the Core engine for ``url`` (see :func:`normalize_database_url`)."""
    return create_engine(
        normalize_database_url(url),
        isolation_level=ISOLATION_LEVEL,
        pool_pre_ping=True,
        connect_args={"options": _SESSION_OPTIONS},
        echo=echo,
    )
