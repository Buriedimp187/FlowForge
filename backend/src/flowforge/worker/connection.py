"""Connection plumbing and argument conventions for the worker layer.

Transaction ownership (design section 3.1, D2): ``enqueue_run`` and
``claim_run`` each own one transaction. They begin it themselves, so the
connection they receive must be *clean*: SQLAlchemy 2 autobegins on the first
statement, and ``Connection.begin()`` fails once that has happened. A clean
connection is a freshly obtained ``engine.connect()`` that has executed nothing.

Conventions (M2.3 decisions):

* Intervals (``ttl``, ``run_timeout``, ``queue_timeout``) are
  :class:`datetime.timedelta` and are sent as PostgreSQL ``interval``
  parameters; ``now() + <integer>`` does not type-check in PostgreSQL. There are
  no defaults: callers state them explicitly.
* Run and workflow-version identifiers cross the public API as canonical
  strings and are accepted as ``str`` or :class:`uuid.UUID`; SQL parameters are
  always sent as ``uuid.UUID`` objects.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from sqlalchemy import Connection, Engine


def require_clean_connection(conn: Connection) -> None:
    """Refuse a connection that already has a transaction (or savepoint) open."""
    if conn.in_transaction():
        raise RuntimeError(
            "a clean connection is required: a transaction or SAVEPOINT is already "
            "open on this connection, and this operation owns its own transaction"
        )


@contextmanager
def worker_connection(engine: Engine) -> Iterator[Connection]:
    """A clean connection for one worker operation, closed on exit."""
    connection = engine.connect()
    try:
        yield connection
    finally:
        connection.close()


def require_positive_interval(name: str, value: object) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError(f"{name} must be a datetime.timedelta, got {value!r}")
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive, got {value!r}")
    return value


def as_uuid(name: str, value: object) -> uuid.UUID:
    """Accept a ``uuid.UUID`` or its string form; reject everything else."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            raise ValueError(f"{name} is not a valid UUID: {value!r}") from None
    raise TypeError(f"{name} must be a str or uuid.UUID, got {value!r}")
