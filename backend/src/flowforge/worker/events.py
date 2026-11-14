"""Lease proof and ``run_events`` appends (docs/m2-persistence-design.md section 3).

``LeaseProof`` is bound to the connection and the currently open ROOT
transaction that issued it, and is valid only while that transaction stays open.
It is an in-process correctness guard against stale reuse, NOT a security
boundary: Python cannot seal the constructor (design section 3, disclosed
limitation). The database-enforced protection is the ``runs`` row lock plus the
``lease_owner``/``lease_epoch`` predicate on the SQL itself, which does not
consult this object.

Nested transactions (SAVEPOINTs) are rejected outright at issue and check sites:
a protected write executed while a savepoint is open could be undone by a later
rollback to it, and nothing in M2 needs a savepoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import Connection, text

from flowforge.worker.connection import as_uuid


@dataclass(frozen=True)
class LeaseProof:
    """Authorization bound to its issuing Connection and active root transaction.

    May be presented to any number of protected writes within that same root
    transaction; it is not single-use. See the module docstring for what it is
    and is not.
    """

    run_id: str
    lease_owner: str
    lease_epoch: int
    _conn: Connection = field(repr=False, compare=False)
    _txn: object = field(repr=False, compare=False)  # identity of conn.get_transaction() at issue


def _reject_if_nested(conn: Connection) -> None:
    if conn.get_nested_transaction() is not None:
        raise RuntimeError(
            "a SAVEPOINT is active on this connection -- protected "
            "operations (run_fenced_guard, claim_run, append_event) "
            "do not support nested transactions in this layer"
        )


def _check_proof(conn: Connection, proof: LeaseProof) -> None:
    _reject_if_nested(conn)
    if conn is not proof._conn:
        raise RuntimeError("LeaseProof presented on a different connection than issued it")
    if conn.get_transaction() is not proof._txn:
        raise RuntimeError(
            "LeaseProof is stale: its transaction has ended, or a new "
            "one has begun on this connection since"
        )


def append_event(
    conn: Connection, proof: LeaseProof, level: str, node_key: Optional[str], message: str
) -> None:
    """Append one ``run_events`` row inside the proof's transaction.

    ``seq`` is ``MAX(seq) + 1``, made race-free by the ``runs`` row lock the
    proof's transaction holds (design section 8, row 15). ``message`` must be
    pre-redacted by the caller (architecture section 10).
    """
    _check_proof(conn, proof)
    conn.execute(
        text(
            """
            INSERT INTO run_events (run_id, seq, level, node_key, message, ts)
            SELECT :run_id, COALESCE(MAX(seq), 0) + 1, :level, :node_key, :message, now()
            FROM run_events WHERE run_id = :run_id
            """
        ),
        {
            "run_id": as_uuid("proof.run_id", proof.run_id),
            "level": level,
            "node_key": node_key,
            "message": message,
        },
    )
