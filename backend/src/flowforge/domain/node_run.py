"""The execution record for one node within one run.

docs/architecture.md section 3.1 ("absent row means unstarted") and
section 5.2 (``effect_class`` immutability). This module models the
*record*; the rules about when/how it changes live in
flowforge.engine (readiness, retry, interruption), which depend only on
this module and flowforge.domain, never the reverse.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from flowforge.domain.effects import EffectClass
from flowforge.domain.items import ItemList
from flowforge.domain.states import NodeRunStatus, Outcome, is_terminal


def _check_epoch(name: str, value: object) -> None:
    """A lease epoch is a plain non-negative ``int`` (``bool`` is rejected).

    Mirrors ``runs.lease_epoch bigint CHECK (lease_epoch >= 0)``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a plain int, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


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
    #: The ``lease_epoch`` that was active at this row's most recent start
    #: (docs/m2-persistence-design.md section 6; the ``node_runs.started_epoch``
    #: column). ``None`` for records that were never started under a lease
    #: (every M1 record). A ``running`` row whose ``started_epoch`` is older
    #: than the current lease epoch is an *orphan* of a lost lease.
    started_epoch: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.node_key:
            raise ValueError("NodeRun.run_id and node_key must be non-empty")
        if self.attempt < 0 or self.failures < 0:
            raise ValueError("attempt and failures must be >= 0")
        if self.started_epoch is not None:
            _check_epoch("NodeRun.started_epoch", self.started_epoch)
        if self.retry_at is not None:
            if isinstance(self.retry_at, bool) or not isinstance(self.retry_at, (int, float)):
                raise TypeError(f"NodeRun.retry_at must be a plain int or float, got {self.retry_at!r}")
            if not math.isfinite(self.retry_at):
                raise ValueError(f"NodeRun.retry_at must be finite, got {self.retry_at!r}")
            if self.retry_at < 0:
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
#: 3.1's state diagram, ``(absent) -> running`` (no prior record at all;
#: handled separately below) and ``retry_wait -> running`` (a due retry)
#: are the ordinary edges into `running`. A third, conditional edge is the
#: M2 orphan reclaim (docs/m2-persistence-design.md section 6): a
#: ``running`` row whose ``started_epoch`` is strictly older than the
#: caller's ``current_lease_epoch``. Every other existing status is
#: illegal to restart from:
#:
#: - the four terminal statuses (succeeded/failed/skipped/cancelled) are
#:   immutable within a run once reached -- a Replay is a *new* run_id,
#:   never a restart of an existing terminal row (section 5.2, Risk R1);
#: - a `running` row is restartable ONLY through the orphan edge, never
#:   by a caller merely asserting that it is one.
_RESUMABLE_STATUSES: frozenset[NodeRunStatus] = frozenset({NodeRunStatus.RETRY_WAIT})


def start_attempt(
    previous: Optional[NodeRun],
    *,
    run_id: str,
    node_key: str,
    resolved_effect_class: EffectClass,
    current_lease_epoch: Optional[int] = None,
) -> tuple[NodeRun, bool]:
    """Begin (or retry, or reclaim an orphan of) a node's execution.

    Implements docs/architecture.md section 5.2 and ADR-0005:
    ``effect_class`` is resolved once, on the very first attempt, and is
    immutable thereafter. If ``resolved_effect_class`` differs from an
    already-persisted class on a later attempt, the original class is
    kept and ``drift`` is reported True (mirroring the
    ``effect_class_drift`` diagnostic event; behavior is otherwise
    unchanged, per the architecture).

    ``current_lease_epoch`` (docs/m2-persistence-design.md section 6) is the
    caller's own just-claimed lease epoch. When supplied it is recorded as
    the new record's ``started_epoch`` on every edge, mirroring the T2 SQL.
    When ``None`` (every M1 caller) the absent edge leaves ``started_epoch``
    ``None`` and the ``retry_wait`` edge preserves the previous value.

    **Orphan reclaim.** A ``running`` ``previous`` may be restarted only if
    ``current_lease_epoch`` is given and ``previous.started_epoch`` is
    strictly older than it: the epoch is the sole input, so a caller cannot
    restart a live attempt by asserting it is an orphan. The restart gives
    ``attempt + 1`` and the new ``started_epoch``; ``failures``, ``outcome``,
    ``error``, ``output`` and ``item_results`` are carried over unchanged
    (an ``unknown`` outcome stays ``unknown``).

    **What this function does NOT decide.** It checks the epoch only. It does
    not look at ``effect_class`` or ``outcome``: whether an orphan may be
    re-executed at all (never for ``unsafe_write``, never silently turning an
    unknown outcome known) is decided by the M2.5 orphan resolution that runs
    after every claim and before the scheduler (design sections 4.2 and 7).
    This primitive must not be read as that gate.

    Raises ``ValueError`` if:

    - ``previous`` is given but its ``run_id``/``node_key`` do not match
      the arguments (a caller integrity error -- this function must never
      silently operate on the wrong record), or
    - ``previous.status`` is terminal, or is ``running`` without the orphan
      condition above holding (no ``current_lease_epoch``, no recorded
      ``started_epoch``, or a ``started_epoch`` not older than the current
      epoch), or
    - ``current_lease_epoch`` is negative.

    Raises ``TypeError`` if ``current_lease_epoch`` is not a plain ``int``
    (``bool`` is rejected).

    Returns ``(new_node_run, drift_detected)``.
    """
    if current_lease_epoch is not None:
        _check_epoch("current_lease_epoch", current_lease_epoch)

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
                started_epoch=current_lease_epoch,
            ),
            False,
        )

    if previous.run_id != run_id or previous.node_key != node_key:
        raise ValueError(
            f"start_attempt() called with run_id={run_id!r}/node_key={node_key!r} "
            f"but previous record is for run_id={previous.run_id!r}/"
            f"node_key={previous.node_key!r}"
        )

    if previous.status is NodeRunStatus.RUNNING:
        if current_lease_epoch is None:
            raise ValueError(
                f"cannot start a new attempt for node '{node_key}' in run '{run_id}': "
                "it is already 'running', and restarting a running record is only "
                "legal as an orphan reclaim, which requires current_lease_epoch so "
                "this function can verify the orphan condition itself"
            )
        if previous.started_epoch is None or previous.started_epoch >= current_lease_epoch:
            raise ValueError(
                f"cannot start a new attempt for node '{node_key}' in run '{run_id}': "
                f"it is not an orphan (started_epoch={previous.started_epoch!r} is not "
                f"older than current_lease_epoch={current_lease_epoch!r}); refusing to "
                "restart a live attempt"
            )
    elif previous.status not in _RESUMABLE_STATUSES:
        raise ValueError(
            f"cannot start a new attempt for node '{node_key}' in run '{run_id}': "
            f"its current status is '{previous.status.value}', but it is terminal "
            "(succeeded/failed/skipped/cancelled are immutable within a run -- "
            "Replay a workflow with a new run_id instead)"
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
        # Recorded whenever the caller supplies its epoch (the T2 SQL sets it
        # on every start); otherwise the previous value is preserved.
        started_epoch=(
            current_lease_epoch if current_lease_epoch is not None else previous.started_epoch
        ),
    )
    return updated, drift
