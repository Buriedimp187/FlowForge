"""Shared fixtures for the M2.3 worker-layer PostgreSQL tests."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from tests.integration.support import DatabaseTestCase, Sql, make_run, make_version, make_workflow

TTL = timedelta(seconds=45)
RUN_TIMEOUT = timedelta(minutes=7)
QUEUE_TIMEOUT = timedelta(minutes=30)

PAST = Sql("now() - interval '1 minute'")
FUTURE = Sql("now() + interval '1 hour'")


def as_id(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


class WorkerDatabaseTestCase(DatabaseTestCase):
    """A migrated database with one workflow version, emptied before each test."""

    workflow_id: uuid.UUID
    version_id: uuid.UUID

    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as connection:
            self.workflow_id = make_workflow(connection)
            self.version_id = make_version(connection, self.workflow_id)

    def add_run(self, **overrides: Any) -> uuid.UUID:
        with self.engine.begin() as connection:
            return make_run(connection, self.version_id, **overrides)

    def add_expired_running_run(self, **overrides: Any) -> uuid.UUID:
        """A run whose owner crashed: ``running``, lease epoch 1, lease already expired."""
        values: dict[str, Any] = dict(
            status="running",
            lease_owner="crashed-worker",
            lease_epoch=1,
            visible_at=PAST,
            started_at=Sql("now() - interval '1 hour'"),
            deadline_at=Sql("now() + interval '30 minutes'"),
        )
        values.update(overrides)
        return self.add_run(**values)

    def expire_lease(self, run_id: str | uuid.UUID) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE runs SET visible_at = now() - interval '1 second' WHERE id = :id"),
                {"id": as_id(run_id)},
            )

    def run_row(self, run_id: str | uuid.UUID):
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": as_id(run_id)}).one()

    def events(self, run_id: str | uuid.UUID) -> list[tuple]:
        """``(seq, level, node_key, message)`` rows in ``seq`` order."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT seq, level, node_key, message FROM run_events WHERE run_id = :id ORDER BY seq"),
                {"id": as_id(run_id)},
            )
            return [tuple(row) for row in rows]

    def count_runs(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM runs")).scalar_one()
