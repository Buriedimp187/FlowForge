"""T1 -- claim and reclaim (docs/m2-persistence-design.md sections 3.1 and 4.2).

Claim and reclaim are the same query: one transaction locks the oldest eligible
run with ``FOR UPDATE SKIP LOCKED`` and takes ownership of it, incrementing the
fencing epoch. There is no reaper; an expired lease is simply eligible again.

This module deliberately contains only T1. The post-claim checks (queue timeout,
run deadline, ``max_recoveries``, cancellation), the fenced guard, heartbeat,
park and T2-T4 are later stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Connection, text

from flowforge.worker.connection import require_clean_connection, require_positive_interval
from flowforge.worker.events import LeaseProof, append_event


@dataclass(frozen=True)
class ClaimedRun:
    """Record of this worker's belief that it owns a run, as of its last T1 claim.

    Grants NO write authorization by itself: every later operation must
    re-verify ``(lease_owner, lease_epoch)`` against the database. The epoch held
    here is only the parameter of that re-check.

    ``prev_status`` is the run's status as locked by T1 before the claim
    (``'queued'`` | ``'waiting'`` | ``'running'``).
    """

    run_id: str
    lease_owner: str
    lease_epoch: int
    deadline_at: datetime
    queue_deadline_at: datetime
    recovery_count: int
    prev_status: str


_SELECT_CANDIDATE = """
    SELECT id, status AS prev_status FROM runs
     WHERE status IN ('queued','waiting','running') AND visible_at <= now()
     ORDER BY visible_at LIMIT 1
     FOR UPDATE SKIP LOCKED
"""

_CLAIM = """
    UPDATE runs SET
       status = 'running', lease_owner = :worker,
       lease_epoch = lease_epoch + 1,
       recovery_count = recovery_count + CASE WHEN status = 'running' THEN 1 ELSE 0 END,
       visible_at  = now() + :ttl,
       started_at  = COALESCE(started_at, now()),
       deadline_at = COALESCE(deadline_at, now() + :run_timeout)
     WHERE id = :id
    RETURNING lease_epoch, recovery_count, cancel_requested_at,
              deadline_at, queue_deadline_at
"""


def claim_run(
    conn: Connection, worker: str, ttl: timedelta, run_timeout: timedelta
) -> Optional[ClaimedRun]:
    """Claim at most one eligible run; commit before returning.

    Returns ``None`` when nothing is eligible. Requires a clean connection and
    owns its transaction. All timestamps come from the database clock.
    """
    if not isinstance(worker, str) or not worker:
        raise ValueError(f"worker must be a non-empty str, got {worker!r}")
    require_positive_interval("ttl", ttl)
    require_positive_interval("run_timeout", run_timeout)
    require_clean_connection(conn)

    with conn.begin():
        row = conn.execute(text(_SELECT_CANDIDATE)).fetchone()
        if row is None:
            return None

        claimed = conn.execute(
            text(_CLAIM),
            {"id": row.id, "worker": worker, "ttl": ttl, "run_timeout": run_timeout},
        ).fetchone()
        if claimed is None:  # cannot happen: the row is locked by this transaction
            raise RuntimeError(f"claim UPDATE matched no row for locked run {row.id}")

        # `prev_status`, read from the row as locked BEFORE the UPDATE, is the
        # reclaim signal -- not `recovery_count > 0`, which stays positive on every
        # later ordinary claim (design section 3.1).
        if row.prev_status == "running":
            proof = LeaseProof(str(row.id), worker, claimed.lease_epoch, conn, conn.get_transaction())
            append_event(
                conn, proof, "info", None, f"run reclaimed (recovery_count={claimed.recovery_count})"
            )

    return ClaimedRun(
        run_id=str(row.id),
        lease_owner=worker,
        lease_epoch=claimed.lease_epoch,
        deadline_at=claimed.deadline_at,
        queue_deadline_at=claimed.queue_deadline_at,
        recovery_count=claimed.recovery_count,
        prev_status=row.prev_status,
    )
