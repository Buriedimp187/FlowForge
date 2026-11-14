"""Shared support for the PostgreSQL integration tests (M2.1).

Where the server comes from (docs/m2-persistence-design.md D3 -- no Dockerfile
or Compose file exists before M4):

* **CI:** set ``FLOWFORGE_TEST_DATABASE_URL`` to a PostgreSQL server the job
  provides through a GitHub Actions ``services:`` block (digest-pinned, see
  ``.github/workflows/ci.yml``). The role needs ``CREATEDB``.
* **Local:** otherwise a throw-away PostgreSQL container is started once per
  test process with ``testcontainers`` from the digest-pinned
  :data:`POSTGRES_IMAGE`, and stopped at interpreter exit.

If neither is possible (no URL, no Docker) the integration tests are SKIPPED
with an explicit reason -- unless ``FLOWFORGE_REQUIRE_DB_TESTS=1``, in which
case that is an error, so CI can never silently skip them.

Isolation: every test class gets its own freshly created database (dropped
afterwards), so classes never see each other's schema or rows.

The testcontainers "Ryuk" reaper is disabled: it is an additional image pulled
by an unpinned tag, which the digest-pinning discipline (ADR-0015) forbids. The
container is stopped explicitly instead.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import unittest
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from flowforge.db import migrate
from flowforge.db.engine import create_db_engine, normalize_database_url
from flowforge.db.ids import new_id

#: The ONE place the local-test PostgreSQL image is named. Must equal the image
#: of the CI ``services:`` block (checked by tests/unit/test_toolchain_pins.py).
#: Tag for humans, digest for reproducibility (the digest is the OCI index of
#: the multi-arch ``postgres:17.12``; server version 17.12 observed).
POSTGRES_IMAGE = "postgres:17.12@sha256:dbb9623d5121b5d2c2c9e2fba56e40193cbb3510d4af7f035b1ab1823aff3205"

#: The complete M2.1 schema scope (docs/m2-persistence-design.md section 2).
FIVE_TABLES = frozenset({"workflows", "workflow_versions", "runs", "node_runs", "run_events"})

TEST_DATABASE_URL_ENV = "FLOWFORGE_TEST_DATABASE_URL"
REQUIRE_DB_TESTS_ENV = "FLOWFORGE_REQUIRE_DB_TESTS"

_PG_USER = "flowforge_test"
_PG_PASSWORD = "flowforge_test"

_lock = threading.Lock()
_admin_url: Optional[str] = None
_unavailable_reason: Optional[str] = None


def _start_container() -> str:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username=_PG_USER, password=_PG_PASSWORD, dbname="postgres", driver="psycopg"
    )
    container.start()
    atexit.register(container.stop)
    return container.get_connection_url()


def admin_database_url() -> str:
    """URL of a PostgreSQL server (maintenance database) for these tests.

    Raises :class:`unittest.SkipTest` when no server is available, or
    ``RuntimeError`` if ``FLOWFORGE_REQUIRE_DB_TESTS=1`` forbids skipping.
    """
    global _admin_url, _unavailable_reason
    with _lock:
        if _admin_url is not None:
            return _admin_url
        if _unavailable_reason is None:
            configured = os.environ.get(TEST_DATABASE_URL_ENV)
            try:
                _admin_url = configured or _start_container()
            except Exception as exc:  # noqa: BLE001 - any startup failure means "unavailable"
                _unavailable_reason = (
                    f"no PostgreSQL available: {TEST_DATABASE_URL_ENV} unset and the "
                    f"testcontainers start failed ({type(exc).__name__}: {exc})"
                )
        if _admin_url is not None:
            return _admin_url
    if os.environ.get(REQUIRE_DB_TESTS_ENV) == "1":
        raise RuntimeError(f"{REQUIRE_DB_TESTS_ENV}=1 but {_unavailable_reason}")
    raise unittest.SkipTest(_unavailable_reason)


class TempDatabase:
    """A uniquely named database on the test server, dropped on request."""

    def __init__(self, admin_url: str) -> None:
        self._admin_url = normalize_database_url(admin_url)
        self.name = "ff_test_" + uuid.uuid4().hex[:16]  # hex only: safe to interpolate
        self.url: URL = self._admin_url.set(database=self.name)

    def _admin_engine(self) -> Engine:
        return create_engine(self._admin_url, isolation_level="AUTOCOMMIT")

    def create(self) -> "TempDatabase":
        engine = self._admin_engine()
        try:
            with engine.connect() as connection:
                connection.execute(text(f"CREATE DATABASE {self.name}"))
        finally:
            engine.dispose()
        return self

    def drop(self) -> None:
        engine = self._admin_engine()
        try:
            with engine.connect() as connection:
                connection.execute(text(f"DROP DATABASE IF EXISTS {self.name} WITH (FORCE)"))
        finally:
            engine.dispose()


class DatabaseTestCase(unittest.TestCase):
    """Base class: a freshly migrated database per test class, emptied per test."""

    engine: Engine
    _temp_db: TempDatabase

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._temp_db = TempDatabase(admin_database_url()).create()
        cls.addClassCleanup(cls._temp_db.drop)
        cls.engine = create_db_engine(cls._temp_db.url)
        cls.addClassCleanup(cls.engine.dispose)
        migrate.upgrade(cls.engine)

    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE TABLE run_events, node_runs, runs, workflow_versions, workflows CASCADE"))


class Sql(str):
    """Marks a value as a trusted SQL expression rather than a bind parameter."""


def insert(connection: Connection, table: str, **values: Any) -> None:
    """INSERT one row; ``dict``/``list`` values are sent as jsonb. Test-only."""
    columns, placeholders, params = [], [], {}
    for name, value in values.items():
        columns.append(name)
        if isinstance(value, Sql):
            placeholders.append(str(value))
        elif isinstance(value, (dict, list)):
            placeholders.append(f"CAST(:{name} AS jsonb)")
            params[name] = json.dumps(value)
        else:
            placeholders.append(f":{name}")
            params[name] = value
    connection.execute(
        text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"), params
    )


def make_workflow(connection: Connection, **overrides: Any) -> uuid.UUID:
    row = {"id": new_id(), "name": "wf"} | overrides
    insert(connection, "workflows", **row)
    return row["id"]


def make_version(connection: Connection, workflow_id: uuid.UUID, **overrides: Any) -> uuid.UUID:
    row = {
        "id": new_id(),
        "workflow_id": workflow_id,
        "version_number": 1,
        "schema_version": 1,
        "definition": {},
    } | overrides
    insert(connection, "workflow_versions", **row)
    return row["id"]


def make_run(connection: Connection, version_id: uuid.UUID, **overrides: Any) -> uuid.UUID:
    row = {
        "id": new_id(),
        "workflow_version_id": version_id,
        "status": "queued",
        "visible_at": Sql("now()"),
        "queue_deadline_at": Sql("now() + interval '1 hour'"),
        "trigger_payload": {},
    } | overrides
    insert(connection, "runs", **row)
    return row["id"]


def make_node_run(connection: Connection, run_id: uuid.UUID, node_key: str = "n1", **overrides: Any) -> None:
    row = {"run_id": run_id, "node_key": node_key, "status": "running", "effect_class": "pure"} | overrides
    insert(connection, "node_runs", **row)


def make_event(connection: Connection, run_id: uuid.UUID, seq: int = 1, **overrides: Any) -> None:
    row = {"run_id": run_id, "seq": seq, "level": "info", "message": "m"} | overrides
    insert(connection, "run_events", **row)


def make_run_tree(connection: Connection, **run_overrides: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """workflow -> version -> run; returns ``(workflow_id, version_id, run_id)``."""
    workflow_id = make_workflow(connection)
    version_id = make_version(connection, workflow_id)
    return workflow_id, version_id, make_run(connection, version_id, **run_overrides)


# PostgreSQL SQLSTATE codes the constraint tests assert on.
NOT_NULL_VIOLATION = "23502"
FOREIGN_KEY_VIOLATION = "23503"
UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"
RAISE_EXCEPTION = "P0001"


def assert_db_error(
    test: unittest.TestCase,
    sqlstate: str,
    action: Callable[[], Any],
    constraint: Optional[str] = None,
) -> None:
    """Assert that ``action`` fails with the given SQLSTATE (and constraint name)."""
    with test.assertRaises(DBAPIError) as caught:
        action()
    original = caught.exception.orig
    test.assertEqual(getattr(original, "sqlstate", None), sqlstate, msg=str(original))
    if constraint is not None:
        test.assertEqual(original.diag.constraint_name, constraint)


def public_tables(connection: Connection) -> set[str]:
    rows = connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    return {row[0] for row in rows}
