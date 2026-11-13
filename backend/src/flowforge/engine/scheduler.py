"""Skip propagation and the "what should run next" scheduling pass.

docs/architecture.md section 3.2: "Skips are written idempotently by the
scheduling pass... so recovery never depends on T3 having done it."

Walking nodes in topological order and writing SKIPPED rows into the
RunStore as we go lets a multi-level skip cascade (A skipped -> B's only
required input was A -> B skipped -> C's only required input was B ->
C skipped) resolve correctly within a single call, because each
downstream node sees its upstream's just-written row before its own
readiness is evaluated.

This module does NOT claim runs, manage leases, or talk to PostgreSQL --
that is the worker's job (M2). It only computes, for one run, which nodes
are ready to execute right now and which newly became SKIPPED.
"""

from __future__ import annotations

from dataclasses import dataclass

from flowforge.domain.graph import WorkflowDefinition
from flowforge.domain.node_run import NodeRun
from flowforge.domain.states import NodeRunStatus
from flowforge.engine.clock import Clock
from flowforge.engine.graph_utils import topological_order
from flowforge.engine.ports import NodeTypeCatalog, RunStore
from flowforge.engine.readiness import NodeReadiness, evaluate_readiness


@dataclass(frozen=True)
class SchedulingPass:
    """The result of one scheduling pass: nodes ready to execute now, and
    nodes that were newly written as SKIPPED during this pass (already
    persisted to the RunStore by the time this is returned)."""

    ready: tuple[str, ...]
    newly_skipped: tuple[str, ...]


def plan_next_step(
    definition: WorkflowDefinition,
    run_id: str,
    run_store: RunStore,
    clock: Clock,
    node_types: NodeTypeCatalog,
) -> SchedulingPass:
    """Compute the next scheduling decision for every node that has not
    yet been resolved, propagating skips in topological order within this
    single call."""
    ready: list[str] = []
    newly_skipped: list[str] = []

    for key in topological_order(definition):
        if run_store.get(run_id, key) is not None:
            continue  # already resolved, running, or waiting on retry

        readiness = evaluate_readiness(key, definition, run_id, run_store, clock, node_types)

        if readiness is NodeReadiness.READY:
            ready.append(key)
        elif readiness is NodeReadiness.SKIPPED:
            run_store.put(NodeRun(run_id=run_id, node_key=key, status=NodeRunStatus.SKIPPED))
            newly_skipped.append(key)
        # NOT_REACHED / WAITING / TERMINAL: nothing to do this pass.

    return SchedulingPass(tuple(ready), tuple(newly_skipped))
