"""Node execution states and terminal-state classification.

docs/architecture.md section 3.1:

    "A `node_runs` row exists if and only if the node has started... or
    resolved as `skipped`. There is no persisted `pending` state."

This module deliberately has NO "pending"/"not started" enum member. The
absence of a NodeRun record *is* "not started" (see
flowforge.domain.node_run and flowforge.engine.readiness). Do not add one.
"""

from __future__ import annotations

from enum import Enum


class NodeRunStatus(str, Enum):
    """The status of an existing ``NodeRun`` record.

    There is deliberately no ``PENDING`` member -- see the module
    docstring. A node that has never started simply has no ``NodeRun``.
    """

    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


#: Terminal statuses, per docs/architecture.md section 3.1:
#: "Terminal = succeeded | failed | skipped | cancelled. `running` and
#: `retry_wait` are non-terminal."
TERMINAL_STATUSES: frozenset[NodeRunStatus] = frozenset(
    {
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.FAILED,
        NodeRunStatus.SKIPPED,
        NodeRunStatus.CANCELLED,
    }
)


def is_terminal(status: NodeRunStatus) -> bool:
    """Whether ``status`` is one of the four terminal statuses."""
    return status in TERMINAL_STATUSES


class Outcome(str, Enum):
    """Whether a node's external-state mutation is known or unknown.

    docs/architecture.md section 6.2: "`outcome` refers only to mutation of
    external state." This is deliberately narrower than "did anything
    happen" -- see section 6.3 for why LLM billing is tracked separately.
    """

    KNOWN = "known"
    UNKNOWN = "unknown"
