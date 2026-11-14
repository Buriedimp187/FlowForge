"""Application-generated primary keys (docs/m2-persistence-design.md D1).

Every ``workflows``/``workflow_versions``/``runs`` row id is a UUIDv7 produced
here, in the application, by the standard library's ``uuid.uuid7()`` (new in
Python 3.14): no extra dependency, and no database-side default or PostgreSQL
UUIDv7 function (the baseline is PostgreSQL 17). The schema's ``id`` columns
therefore carry no ``DEFAULT`` and every INSERT supplies the value.

UUIDv7 is chosen only for B-tree insert locality. Nothing in the design depends
on id ordering for correctness (claim order uses ``visible_at``, fencing uses
``lease_epoch``, finalization precedence uses ``status``).
"""

from __future__ import annotations

import uuid


def new_id() -> uuid.UUID:
    """Return a new UUIDv7."""
    return uuid.uuid7()
