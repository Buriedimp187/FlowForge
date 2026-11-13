"""Workflow definitions, nodes, edges, and immutable versions.

docs/architecture.md section 2 (representation and the view/execution-model
split), section 5 (the node contract), and ADR-0006 (immutable versions).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from flowforge.domain._immutable import deep_freeze


class OnErrorPolicy(str, Enum):
    """docs/architecture.md section 5 node contract field ``on_error``."""

    FAIL_RUN = "fail_run"
    CONTINUE = "continue"
    ROUTE = "route"


class ExecutionMode(str, Enum):
    """docs/architecture.md section 5: ``execution: batch | per_item(...)``."""

    BATCH = "batch"
    PER_ITEM = "per_item"


@dataclass(frozen=True)
class RetryPolicy:
    """The *budget and backoff shape* a node declares.

    Eligibility (whether a given failure may consume this budget at all)
    is a function of the effect class and the failure kind -- see
    flowforge.engine.retry -- and is deliberately NOT part of this policy
    object, to keep "can this retry at all" separate from "how many times,
    how fast".
    """

    max_attempts: int = 3
    backoff_base_s: float = 1.0
    backoff_multiplier: float = 2.0
    backoff_max_s: float = 60.0

    def __post_init__(self) -> None:
        # M1 final-correction finding 5: `isinstance(True, int)` is
        # `True` in Python, so `max_attempts < 1` alone would silently
        # accept `max_attempts=True` as if it were the integer 1. bool
        # is rejected explicitly, before any numeric comparison.
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise TypeError(f"max_attempts must be a plain int, got {self.max_attempts!r}")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        for name in ("backoff_base_s", "backoff_multiplier", "backoff_max_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a plain int or float, got {value!r}")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number, got {value!r}")
        if self.backoff_base_s < 0 or self.backoff_multiplier < 1 or self.backoff_max_s < 0:
            raise ValueError("backoff parameters must be non-negative (multiplier >= 1)")


@dataclass(frozen=True)
class NodeDefinition:
    """A single node within a :class:`WorkflowDefinition`.

    ``key`` is the immutable per-node slug referenced by edges, expressions,
    and idempotency keys (docs/architecture.md section 2.1: "`key` is an
    immutable per-node slug").

    This is a pure data record. It intentionally does NOT carry a node
    *implementation* (no ``execute`` callable) -- the node registry and
    built-in nodes are M3 scope (docs/milestones.md).
    """

    key: str
    type: str
    type_version: int
    name: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    credential_id: str | None = None
    retry: RetryPolicy | None = None
    timeout_s: float | None = None
    on_error: OnErrorPolicy = OnErrorPolicy.FAIL_RUN
    execution_mode: ExecutionMode = ExecutionMode.BATCH
    continue_on_item_error: bool = False

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("NodeDefinition.key must be non-empty")
        if not self.type:
            raise ValueError("NodeDefinition.type must be non-empty")
        if self.type_version < 1:
            raise ValueError("NodeDefinition.type_version must be >= 1")
        if self.timeout_s is not None:
            # M1 final-correction finding 5: `math.inf > 0` is True, so
            # the previous `not (self.timeout_s > 0)` check silently
            # accepted an infinite timeout. bool is also rejected
            # explicitly first (isinstance(True, int) is True).
            if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
                raise TypeError(f"NodeDefinition.timeout_s must be a plain int or float, got {self.timeout_s!r}")
            if not math.isfinite(self.timeout_s):
                raise ValueError(f"NodeDefinition.timeout_s must be finite, got {self.timeout_s!r}")
            if not self.timeout_s > 0:
                raise ValueError("NodeDefinition.timeout_s must be > 0 when given")
        # Deep-frozen: see flowforge.domain._immutable. Mutating the
        # caller's original `params` dict (including a nested dict/list
        # inside it) after construction must not affect this object, and
        # mutating through `self.params` must not be possible at all.
        object.__setattr__(self, "params", deep_freeze(dict(self.params)))


@dataclass(frozen=True)
class Edge:
    """A connection from one node's output port to another's input port."""

    id: str
    source_key: str
    source_port: str
    target_key: str
    target_port: str

    def __post_init__(self) -> None:
        for field_name in ("id", "source_key", "source_port", "target_key", "target_port"):
            if not getattr(self, field_name):
                raise ValueError(f"Edge.{field_name} must be non-empty")


@dataclass(frozen=True)
class WorkflowDefinition:
    """The backend-owned execution definition.

    docs/architecture.md section 2.1: "React Flow's state... is a UI
    concern only... The execution engine never reads layout data." This
    class therefore carries no UI/layout fields at all -- that conversion
    happens in a frontend adapter (M5), not here.

    ``nodes`` and ``edges`` are tuples, not sets or dicts keyed by anything
    that would reorder them: iteration order is exactly definition order,
    which docs/architecture.md section 6.4 relies on for idempotency-key
    and per-item-index stability.
    """

    schema_version: int
    nodes: tuple[NodeDefinition, ...]
    edges: tuple[Edge, ...]

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("WorkflowDefinition.schema_version must be >= 1")
        # Normalize: a caller may pass a list (or any other sequence) for
        # either field; tuple-ify defensively so (a) the stored value is
        # genuinely immutable, not just type-hinted as a tuple, and
        # (b) a caller mutating their original list afterwards cannot
        # affect this object (M1 audit correction -- a frozen dataclass
        # field typed `tuple[...]` was previously trusted to already BE a
        # tuple, which nothing enforced).
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))

        keys = [n.key for n in self.nodes]
        if len(keys) != len(set(keys)):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"duplicate node keys: {dupes}")
        edge_ids = [e.id for e in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate edge ids")

    def node(self, key: str) -> NodeDefinition | None:
        for n in self.nodes:
            if n.key == key:
                return n
        return None

    def edges_into(self, node_key: str) -> tuple[Edge, ...]:
        """Edges targeting ``node_key``, in definition order (unsorted)."""
        return tuple(e for e in self.edges if e.target_key == node_key)

    def edges_from(self, node_key: str) -> tuple[Edge, ...]:
        """Edges originating at ``node_key``, in definition order."""
        return tuple(e for e in self.edges if e.source_key == node_key)

    def upstream_keys(self, node_key: str) -> tuple[str, ...]:
        """Distinct upstream node keys feeding ``node_key``, in the order
        their edges first appear in the definition (not sorted, not
        de-duplicated via a set, so this stays deterministic)."""
        seen: list[str] = []
        for e in self.edges_into(node_key):
            if e.source_key not in seen:
                seen.append(e.source_key)
        return tuple(seen)


@dataclass(frozen=True)
class WorkflowVersion:
    """An immutable, saved version of a workflow.

    ADR-0006: "Saving a workflow creates a new immutable version row; a run
    pins a specific `workflow_version_id` at enqueue time." Persistence
    (the actual row, its id, timestamps) is M2 scope; this is the pure
    domain shape a persisted version wraps.
    """

    workflow_id: str
    version_number: int
    definition: WorkflowDefinition

    def __post_init__(self) -> None:
        if self.version_number < 1:
            raise ValueError("version_number must be >= 1")
