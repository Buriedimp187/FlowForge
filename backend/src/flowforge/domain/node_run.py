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
        object.__setattr__(self, "output", dict(self.output))

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

    existing_class = previous.effect_class
    drift = existing_class is not None and existing_class != resolved_effect_class
    effective_class = existing_class if existing_class is not None else resolved_effect_class

    updated = dataclasses.replace(
        previous,
        status=NodeRunStatus.RUNNING,
        attempt=previous.attempt + 1,
        effect_class=effective_class,
        retry_at=None,
    )
    return updated, drift
