"""The execution record for one node within one run.

docs/architecture.md section 3.1 ("absent row means unstarted") and
section 5.2 (``effect_class`` immutability). This module models the
*record*; the rules about when/how it changes live in
flowforge.engine (readiness, retry, interruption), which depend only on
this module and flowforge.domain, never the reverse.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from flowforge.domain.effects import EffectClass
from flowforge.domain.items import ItemList
from flowforge.domain.states import NodeRunStatus, Outcome, is_terminal


@dataclass(frozen=True)
class NodeError:
    """A node-level or item-level failure detail."""

    code: str
    message: str = ""
    cause: Optional[str] = None


@dataclass(frozen=True)
class ItemResult:
    """The result of processing one item in per-item execution mode.

    ``index`` ties this result back to its input item
    (docs/architecture.md section 6.4 relies on index-based identity being
    stable; see flowforge.engine.per_item and flowforge.engine.idempotency).
    """

    index: int
    status: str  # "succeeded" | "failed"
    output: Optional[ItemList] = None
    error: Optional[NodeError] = None

    _ALLOWED = frozenset({"succeeded", "failed"})

    def __post_init__(self) -> None:
        if self.status not in self._ALLOWED:
            raise ValueError(f"ItemResult.status must be one of {sorted(self._ALLOWED)}")
        if self.index < 0:
            raise ValueError("ItemResult.index must be >= 0")


@dataclass(frozen=True)
class NodeRun:
    """A node's execution record for one run.

    There is no constructor for an "unstarted" NodeRun: per
    docs/architecture.md section 3.1, "unstarted" is represented by the
    *absence* of a NodeRun in the store, never by a NodeRun instance with
    some pending-like status. Use :func:`start_attempt` to create the
    first record.
    """

    run_id: str
    node_key: str
    status: NodeRunStatus
    attempt: int = 0
    failures: int = 0
    effect_class: Optional[EffectClass] = None
    outcome: Outcome = Outcome.KNOWN
    retry_at: Optional[float] = None
    output: Mapping[str, ItemList] = field(default_factory=dict)
    error: Optional[NodeError] = None
    item_results: Optional[tuple[ItemResult, ...]] = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.node_key:
            raise ValueError("NodeRun.run_id and node_key must be non-empty")
        if self.attempt < 0 or self.failures < 0:
            raise ValueError("attempt and failures must be >= 0")
        if self.retry_at is not None and self.retry_at < 0:
            raise ValueError("NodeRun.retry_at must be >= 0 when given")
        # M1 audit correction: a plain `dict(self.output)` here was still
        # a mutable mapping reachable as `node_run.output` -- a frozen
        # dataclass only prevents *reassigning* the attribute, not
        # mutating the object it points to. Wrap in a MappingProxyType
        # over tuple-normalized values, so neither the mapping nor any
        # per-port item list can be mutated through this object, and a
        # caller's original dict/list cannot affect it after construction.
        object.__setattr__(
            self,
            "output",
            MappingProxyType({port: tuple(items) for port, items in self.output.items()}),
        )
        if self.item_results is not None:
            object.__setattr__(self, "item_results", tuple(self.item_results))

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    def emitted_on(self, port: str) -> bool:
        """Whether this (terminal) node run produced data on ``port``.

        docs/architecture.md section 3.2: a downstream port "received
        data" only if the upstream node "emitted on the connected port,
        even if with zero items" -- so a present-but-empty tuple still
        counts; a missing key does not.
        """
        return port in self.output


#: The only predecessor statuses :func:`start_attempt` may advance from
#: when a prior record already exists. Per docs/architecture.md section
#: 3.1's state diagram, exactly two edges lead into `running`:
#: ``(absent) -> running`` (no prior record at all; handled separately
#: below) and ``retry_wait -> running`` (a due retry). Every other
#: existing status is illegal to restart from in M1:
#:
#: - the four terminal statuses (succeeded/failed/skipped/cancelled) are
#:   immutable within a run once reached -- a Replay is a *new* run_id,
#:   never a restart of an existing terminal row (section 5.2, Risk R1);
#: - `running` itself has no legal re-entry here: resuming a node whose
#:   lease was lost to a crash is the M2 orphan-takeover mechanism
#:   (leases, fencing, `recovery_count`), which does not exist in M1 and
#:   must not be silently approximated by just letting this function
#:   restart it.
_RESUMABLE_STATUSES: frozenset[NodeRunStatus] = frozenset({NodeRunStatus.RETRY_WAIT})


def start_attempt(
    previous: Optional[NodeRun],
    *,
    run_id: str,
    node_key: str,
    resolved_effect_class: EffectClass,
) -> tuple[NodeRun, bool]:
    """Begin (or retry) a node's execution.

    Implements docs/architecture.md section 5.2 and ADR-0005:
    ``effect_class`` is resolved once, on the very first attempt, and is
    immutable thereafter. If ``resolved_effect_class`` differs from an
    already-persisted class on a later attempt, the original class is
    kept and ``drift`` is reported True (mirroring the
    ``effect_class_drift`` diagnostic event; behavior is otherwise
    unchanged, per the architecture).

    Raises ``ValueError`` if:

    - ``previous`` is given but its ``run_id``/``node_key`` do not match
      the arguments (a caller integrity error -- this function must never
      silently operate on the wrong record), or
    - ``previous.status`` is not one of the statuses :data:`_RESUMABLE_STATUSES`
      allows resuming from (see that constant's docstring): restarting a
      terminal or already-``running`` record is an M1 state-machine
      violation, not a supported recovery path.

    Returns ``(new_node_run, drift_detected)``.
    """
    if previous is None:
        return (
            NodeRun(
                run_id=run_id,
                node_key=node_key,
                status=NodeRunStatus.RUNNING,
                attempt=1,
                failures=0,
                effect_class=resolved_effect_class,
                outcome=Outcome.KNOWN,
            ),
            False,
        )

    if previous.run_id != run_id or previous.node_key != node_key:
        raise ValueError(
            f"start_attempt() called with run_id={run_id!r}/node_key={node_key!r} "
            f"but previous record is for run_id={previous.run_id!r}/"
            f"node_key={previous.node_key!r}"
        )

    if previous.status not in _RESUMABLE_STATUSES:
        reason = (
            "it is terminal (succeeded/failed/skipped/cancelled are immutable "
            "within a run -- Replay a workflow with a new run_id instead)"
            if is_terminal(previous.status)
            else (
                "it is already 'running' -- restarting an in-flight node requires "
                "M2's lease/orphan-takeover mechanism, which does not exist in M1"
            )
        )
        raise ValueError(
            f"cannot start a new attempt for node '{node_key}' in run '{run_id}': "
            f"its current status is '{previous.status.value}', but {reason}"
        )

    existing_class = previous.effect_class
    drift = existing_class is not None and existing_class != resolved_effect_class
    effective_class = existing_class if existing_class is not None else resolved_effect_class

    updated = dataclasses.replace(
        previous,
        status=NodeRunStatus.RUNNING,
        attempt=previous.attempt + 1,
        # `failures` is deliberately NOT in this replace() call, so it is
        # carried over unchanged -- see docs/architecture.md section 5.1:
        # "failures counts only attempts that ended in an ordinary
        # failure", and this call site never represents such an ending.
        effect_class=effective_class,
        retry_at=None,
    )
    return updated, drift
