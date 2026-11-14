"""Park operations (docs/m2-persistence-design.md sections 4.5 and 4.11).

Two DISTINCT operations, never one with a flag:

* :func:`park_run` -- the ordinary park (architecture section 4.5). It keeps its
  ``cancel_requested_at IS NULL`` predicate and its ``LEAST(min(retry_at),
  deadline_at)`` wake time. "No in-flight node tasks" is a caller-side
  precondition, not part of the SQL.
* :func:`park_for_shutdown` -- the graceful-release park (D-H, SIGTERM). Same
  owner/epoch fencing and epoch transition, ``visible_at = now()`` so another
  worker continues at once, and NO cancel predicate (a pending cancel cannot
  sleep because the run is claimable immediately; the next claimant observes the
  sticky flag under its own lease).

Neither writes a ``node_runs`` row, and neither touches ``lease_epoch``: the NEXT
claim increments it, which is what turns a still-``running`` node row into an
orphan. Wake is not a separate operation: a parked run is claimed by T1 when its
``visible_at`` passes (``prev_status = 'waiting'``, which does not count as a
recovery).
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum, auto

from sqlalchemy import Connection, text

from flowforge.worker.claim import ClaimedRun
from flowforge.worker.connection import as_uuid, require_clean_connection, require_positive_interval
from flowforge.worker.events import append_event
from flowforge.worker.guard import run_fenced_guard


class ParkResult(Enum):
    PARKED = auto()
    #: Ordinary park only: zero rows. Either the lease was lost or a cancel arrived
    #: concurrently; the caller distinguishes them with a fresh fenced guard.
    NOT_PARKED = auto()
    #: Shutdown park only: the fenced guard found the lease lost; nothing was written.
    LEASE_LOST = auto()


_PARK = """
    UPDATE runs SET
       status = 'waiting', lease_owner = NULL,
       visible_at = LEAST(
           (SELECT min(retry_at) FROM node_runs WHERE run_id = :id AND status = 'retry_wait'),
           deadline_at)
     WHERE id = :id AND status = 'running'
       AND lease_owner = :worker AND lease_epoch = :epoch
       AND cancel_requested_at IS NULL
    RETURNING status
"""

_PARK_FOR_SHUTDOWN = """
    UPDATE runs SET status = 'waiting', lease_owner = NULL, visible_at = now()
     WHERE id = :id AND status = 'running'
       AND lease_owner = :worker AND lease_epoch = :epoch
    RETURNING status
"""

LIVE_ATTEMPTS_MESSAGE = (
    "graceful shutdown: attempt(s) still running at grace expiry; run parked; "
    "node row(s) left for orphan recovery"
)
CLEAN_RELEASE_MESSAGE = "graceful shutdown: run released"


def _params(claimed: ClaimedRun) -> dict:
    return {
        "id": as_uuid("claimed.run_id", claimed.run_id),
        "worker": claimed.lease_owner,
        "epoch": claimed.lease_epoch,
    }


def park_run(conn: Connection, claimed: ClaimedRun) -> ParkResult:
    """Ordinary park: release ownership until ``LEAST(min retry_at, deadline_at)``.

    Requires a clean connection and owns its transaction. Returns ``PARKED`` or
    ``NOT_PARKED`` (zero rows: lease lost, or a cancel is pending).
    """
    require_clean_connection(conn)
    with conn.begin():
        row = conn.execute(text(_PARK), _params(claimed)).fetchone()
    return ParkResult.PARKED if row is not None else ParkResult.NOT_PARKED


def park_for_shutdown(
    conn: Connection, claimed: ClaimedRun, ttl: timedelta, *, live_attempts: bool
) -> ParkResult:
    """Graceful-release park, in one transaction: guard, event, park.

    The event is a ``warning`` when attempts are still running at grace expiry
    (their node rows are deliberately left for orphan recovery) and an ``info``
    for a clean release. A lease lost at the guard writes nothing and returns
    ``LEASE_LOST``. Requires a clean connection and owns its transaction.
    """
    require_positive_interval("ttl", ttl)
    require_clean_connection(conn)
    with conn.begin():
        guard = run_fenced_guard(conn, claimed, ttl)
        if guard is None:
            return ParkResult.LEASE_LOST
        if live_attempts:
            append_event(conn, guard.proof, "warning", None, LIVE_ATTEMPTS_MESSAGE)
        else:
            append_event(conn, guard.proof, "info", None, CLEAN_RELEASE_MESSAGE)
        row = conn.execute(text(_PARK_FOR_SHUTDOWN), _params(claimed)).fetchone()
        if row is None:  # impossible: the guard holds the row lock and the predicate is identical
            raise RuntimeError(f"shutdown park matched no row for guarded run {claimed.run_id}")
    return ParkResult.PARKED
