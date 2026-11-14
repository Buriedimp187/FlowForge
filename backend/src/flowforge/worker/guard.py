"""The fenced guard (docs/m2-persistence-design.md section 3.1; architecture section 4.4).

``run_fenced_guard`` is both the heartbeat statement and the prelude of every
state-changing worker write. It authorizes by ``status``, ``lease_owner`` and
``lease_epoch`` ONLY -- ``visible_at`` is never in its predicate, because an
expired ``visible_at`` does not revoke ownership until a new claimant actually
increments ``lease_epoch``. Zero rows means the lease is lost: the caller must
write nothing further.

The renewal is stamped from ``clock_timestamp()`` (not the transaction-start
``now()``), and the returned ``db_now`` is that same single sample
(``visible_at - :ttl``), so a guard that waited on the ``runs`` row lock does not
stamp a stale expiry. All timestamps are database timestamps; a worker's local
clock is never compared with them.

The guard does not own a transaction: the caller opens one explicitly, runs the
guard, does its protected writes under the returned proof, and commits. The row
lock the guard takes lives until that transaction ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Connection, text

from flowforge.worker.claim import ClaimedRun
from flowforge.worker.connection import as_uuid, require_positive_interval
from flowforge.worker.events import LeaseProof, _reject_if_nested


@dataclass(frozen=True)
class FencedGuardResult:
    """Pairs a ``LeaseProof`` with the business columns the same ``RETURNING`` yields.

    ``db_now`` is the database-clock sample that also stamped the lease renewal;
    callers compare ``deadline_at`` against THIS value, never against their own
    clock.
    """

    proof: LeaseProof
    cancel_requested_at: Optional[datetime]
    deadline_at: datetime
    db_now: datetime


_GUARD = """
    UPDATE runs SET visible_at = clock_timestamp() + :ttl
     WHERE id = :run_id AND status = 'running'
       AND lease_owner = :worker AND lease_epoch = :epoch
    RETURNING lease_owner, lease_epoch, cancel_requested_at, deadline_at,
              visible_at - :ttl AS db_now
"""


def run_fenced_guard(
    conn: Connection, claimed: ClaimedRun, ttl: timedelta
) -> Optional[FencedGuardResult]:
    """Verify ownership and renew the lease inside the caller's open transaction.

    Returns ``None`` when the lease is lost (zero rows): the caller must not
    proceed to any write. Requires an already-open caller-owned transaction and
    rejects a nested one (a protected write under a later-rolled-back savepoint
    could be undone).
    """
    _reject_if_nested(conn)
    if not conn.in_transaction():
        raise RuntimeError(
            "run_fenced_guard requires an explicitly open, caller-owned transaction "
            "(begin one with conn.begin() before calling it)"
        )
    require_positive_interval("ttl", ttl)

    row = conn.execute(
        text(_GUARD),
        {
            "run_id": as_uuid("claimed.run_id", claimed.run_id),
            "worker": claimed.lease_owner,
            "epoch": claimed.lease_epoch,
            "ttl": ttl,
        },
    ).fetchone()
    if row is None:
        return None  # lease lost -- caller must not proceed to any write
    proof = LeaseProof(claimed.run_id, row.lease_owner, row.lease_epoch, conn, conn.get_transaction())
    return FencedGuardResult(proof, row.cancel_requested_at, row.deadline_at, row.db_now)
