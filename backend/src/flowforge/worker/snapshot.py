"""A consistent, read-only snapshot of one run for the execution loop.

Stage 1 of M2.5 (docs/m2-persistence-design.md sections 4.7, 3.2 of the architecture:
"refresh state (rows + definition), then resolve skips, then compute the ready set").

The workflow definition, the node rows and ``db_now`` are read inside ONE
``REPEATABLE READ, READ ONLY`` transaction, so they describe a single database
snapshot (under the engine's READ COMMITTED default each statement would otherwise see
its own, and a node row committed between two reads could contradict the definition or
the run row). The isolation level is set with ``SET TRANSACTION`` as the transaction's
first statement, so it never leaks to later work on the connection.

``SET TRANSACTION`` is only effective inside a transaction block. On an AUTOCOMMIT
connection PostgreSQL merely emits a warning and every later statement would run in its
own snapshot, with no read-only protection. The first statement therefore also reads back
``transaction_isolation`` and ``transaction_read_only`` and the load is REFUSED
(``RuntimeError``, before any data is read) unless they are ``repeatable read`` and
``on``. That statement is the first query of the transaction, so it is the one that fixes
the snapshot, and it samples ``db_now`` from the same instant.

The snapshot is advisory: it informs scheduling, but authorizes nothing. It performs no
write and takes no row lock, so it never renews or extends a lease; every later write
re-checks ownership through the fenced guard and its own SQL predicates. ``db_now`` is a
single ``clock_timestamp()`` sample from the same transaction; readiness is evaluated
against it (via :class:`SnapshotClock`), never against the worker's clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import Connection, text

from flowforge.domain.graph import WorkflowDefinition
from flowforge.domain.node_run import NodeRun
from flowforge.engine.inmemory import InMemoryRunStore
from flowforge.worker.codec import (
    NODE_RUN_COLUMNS,
    datetime_to_epoch,
    definition_from_json,
    node_run_from_row,
)
from flowforge.worker.connection import as_uuid, require_clean_connection


class SnapshotClock:
    """The engine's ``Clock`` port, fixed at the snapshot's database time."""

    def __init__(self, db_now: datetime) -> None:
        self._now = datetime_to_epoch(db_now)

    def now(self) -> float:
        return self._now


@dataclass(frozen=True)
class RunSnapshot:
    """Run row facts, definition and node rows as of one database snapshot."""

    run_id: str
    workflow_version_id: str
    status: str
    lease_owner: Optional[str]
    lease_epoch: int
    recovery_count: int
    cancel_requested_at: Optional[datetime]
    deadline_at: Optional[datetime]
    queue_deadline_at: datetime
    db_now: datetime
    definition: WorkflowDefinition
    node_runs: tuple[NodeRun, ...]

    def node_run(self, node_key: str) -> Optional[NodeRun]:
        for node_run in self.node_runs:
            if node_run.node_key == node_key:
                return node_run
        return None

    def clock(self) -> SnapshotClock:
        return SnapshotClock(self.db_now)

    def run_store(self) -> InMemoryRunStore:
        """A FRESH in-memory store holding the node rows.

        The scheduling pass writes ``SKIPPED`` rows into the store it is given; handing
        out a new store each time keeps this snapshot itself immutable.
        """
        store = InMemoryRunStore()
        for node_run in self.node_runs:
            store.put(node_run)
        return store


_PROBE = """
    SELECT clock_timestamp() AS db_now,
           current_setting('transaction_isolation') AS isolation,
           current_setting('transaction_read_only') AS read_only
"""

_RUN = """
    SELECT r.id, r.workflow_version_id, r.status, r.lease_owner, r.lease_epoch,
           r.recovery_count, r.cancel_requested_at, r.deadline_at, r.queue_deadline_at,
           wv.schema_version, wv.definition
      FROM runs r
      JOIN workflow_versions wv ON wv.id = r.workflow_version_id
     WHERE r.id = :id
"""

_NODE_RUNS = f"SELECT {', '.join(NODE_RUN_COLUMNS)} FROM node_runs WHERE run_id = :id ORDER BY node_key"


def load_run_snapshot(conn: Connection, run_id: str) -> RunSnapshot:
    """Read a run's snapshot. Requires a clean connection; owns a read-only transaction.

    Raises ``LookupError`` for an unknown run, :class:`~flowforge.worker.codec.CodecError`
    for stored data the domain rejects, and ``RuntimeError`` if the connection cannot give a
    ``REPEATABLE READ``, ``READ ONLY`` transaction (for example, AUTOCOMMIT).
    """
    params = {"id": as_uuid("run_id", run_id)}
    require_clean_connection(conn)
    with conn.begin():
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        probe = conn.execute(text(_PROBE)).mappings().one()  # the first query: fixes the snapshot
        if probe["isolation"] != "repeatable read" or probe["read_only"] != "on":
            raise RuntimeError(
                "run snapshot refused: the transaction is not REPEATABLE READ and READ ONLY "
                f"(isolation={probe['isolation']!r}, read_only={probe['read_only']!r}); "
                "is the connection in AUTOCOMMIT?"
            )
        run = conn.execute(text(_RUN), params).mappings().one_or_none()
        if run is None:
            raise LookupError(f"run {run_id} does not exist")
        rows = conn.execute(text(_NODE_RUNS), params).mappings().all()

    return RunSnapshot(
        run_id=str(run["id"]),
        workflow_version_id=str(run["workflow_version_id"]),
        status=run["status"],
        lease_owner=run["lease_owner"],
        lease_epoch=run["lease_epoch"],
        recovery_count=run["recovery_count"],
        cancel_requested_at=run["cancel_requested_at"],
        deadline_at=run["deadline_at"],
        queue_deadline_at=run["queue_deadline_at"],
        db_now=probe["db_now"],
        definition=definition_from_json(run["definition"], run["schema_version"]),
        node_runs=tuple(node_run_from_row(row) for row in rows),
    )
