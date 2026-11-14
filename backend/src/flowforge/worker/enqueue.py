"""T0 -- enqueue a run (docs/m2-persistence-design.md section 4.1).

One INSERT of a ``queued`` run, visible immediately, in its own transaction.
Timestamps come from the database clock. De-duplication by idempotency key is
left to the caller: a duplicate ``(workflow_version_id, idempotency_key)``
surfaces as the schema's unique violation (the API milestone decides what to
return for it).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import Connection, text

from flowforge.db.ids import new_id
from flowforge.worker.connection import as_uuid, require_clean_connection, require_positive_interval


def enqueue_run(
    conn: Connection,
    workflow_version_id: str | uuid.UUID,
    trigger_payload: Any,
    queue_timeout: timedelta,
    idempotency_key: Optional[str] = None,
) -> str:
    """Insert a ``queued`` run and commit; return its id (canonical string).

    ``trigger_payload`` must be strict JSON (no NaN/Infinity). Requires a clean
    connection and owns its transaction.
    """
    version_id = as_uuid("workflow_version_id", workflow_version_id)
    require_positive_interval("queue_timeout", queue_timeout)
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise TypeError(f"idempotency_key must be a str or None, got {idempotency_key!r}")
    payload = json.dumps(trigger_payload, allow_nan=False)  # ValueError on NaN/Infinity, TypeError otherwise
    require_clean_connection(conn)

    run_id = new_id()
    with conn.begin():
        conn.execute(
            text(
                """
                INSERT INTO runs (id, workflow_version_id, status, visible_at,
                                  queue_deadline_at, trigger_payload, idempotency_key)
                VALUES (:id, :wv_id, 'queued', now(), now() + :queue_timeout,
                        CAST(:payload AS jsonb), :idem_key)
                """
            ),
            {
                "id": run_id,
                "wv_id": version_id,
                "queue_timeout": queue_timeout,
                "payload": payload,
                "idem_key": idempotency_key,
            },
        )
    return str(run_id)
