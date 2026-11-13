"""Deterministic idempotency keys.

docs/architecture.md section 6.4:

    key(run, node)             = UUIDv5(FLOWFORGE_NS, f"{run_id}/{node_key}")
    key(run, node, item_index) = UUIDv5(FLOWFORGE_NS, f"{run_id}/{node_key}/{item_index}")

No fixed project namespace existed anywhere else in the repository prior
to this module (checked: no other file defines ``FLOWFORGE_NAMESPACE`` or
an equivalent constant), so one is selected and documented here, once.

The namespace is derived deterministically from a stable, public string
using the standard ``uuid.NAMESPACE_DNS`` root, rather than a hand-typed
random UUID4 with no provenance -- so anyone can recompute and verify it
independently:

    uuid.uuid5(uuid.NAMESPACE_DNS, "flowforge.dev")
    == 37263b35-e483-5cfc-a59c-8119406691d2   (computed and verified by test)

THIS VALUE MUST NEVER CHANGE once any run has used it to compute a key
sent to an external system (docs/architecture.md section 6.4: "a key only
protects as far as the remote API honors it" -- changing the namespace
would silently change every future key for no benefit and break any
operator-side deduplication already relying on it).
"""

from __future__ import annotations

import uuid

#: The fixed FlowForge project namespace. Derived once, deterministically,
#: from ``uuid.uuid5(uuid.NAMESPACE_DNS, "flowforge.dev")`` -- see the
#: module docstring. Do not change this value.
FLOWFORGE_NAMESPACE: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "flowforge.dev")


def node_key(run_id: str, node_key_: str) -> uuid.UUID:
    """``key(run, node)`` from docs/architecture.md section 6.4.

    Deliberately excludes the attempt number: a retried operation must
    present the *same* key on every attempt so the remote side can
    deduplicate it.
    """
    return uuid.uuid5(FLOWFORGE_NAMESPACE, f"{run_id}/{node_key_}")


def item_key(run_id: str, node_key_: str, item_index: int) -> uuid.UUID:
    """``key(run, node, item_index)`` from docs/architecture.md section 6.4.

    ``item_index`` must be the item's stable position in the node's
    persisted, ordered input (see flowforge.engine.per_item), never a
    value re-derived from a filtered or re-sorted list on retry.
    """
    if item_index < 0:
        raise ValueError("item_index must be >= 0")
    return uuid.uuid5(FLOWFORGE_NAMESPACE, f"{run_id}/{node_key_}/{item_index}")
