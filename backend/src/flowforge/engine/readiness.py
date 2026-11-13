"""Readiness evaluation.

docs/architecture.md section 3.2, verbatim table:

    - Source node, no row: ready at run start.
    - Other node, no row: ready iff all upstream nodes of its connected
      edges are terminal.
    - retry_wait row: ready iff retry_at <= db_now.
    - running row of the current epoch: not ready.

And the skip rule: "the node runs iff at least one connected input port
received data and every port marked required received data. Otherwise it
is skipped."

"Orphans" (a running row whose owning worker is gone) require lease/epoch
tracking, which is explicitly M2 scope (leases, fencing, crash recovery).
This module therefore has no orphan handling at all -- a `running` row is
always treated as in-flight-and-not-ready, which is the correct and only
interpretation available without a worker/lease concept.

Determining which ports are "required" is a property of the node's
*type* (docs/architecture.md section 5: ``inputs[]: {name, required}``),
so this module takes a :class:`~flowforge.engine.ports.NodeTypeCatalog` as
an explicit dependency -- never a hidden global, and never guessed from
the definition alone.
"""

from __future__ import annotations

from enum import Enum

from flowforge.domain.graph import WorkflowDefinition
from flowforge.domain.states import NodeRunStatus, is_terminal
from flowforge.engine.clock import Clock
from flowforge.engine.ports import NodeTypeCatalog, RunStore


class NodeReadiness(str, Enum):
    """The five states the M1 task instructions ask the engine to
    distinguish, mapped directly onto this enum's members."""

    NOT_REACHED = "not_reached"  # a node that has not been reached
    READY = "ready"  # a node that is ready
    SKIPPED = "skipped"  # a node that must be skipped (not yet written)
    WAITING = "waiting"  # already running, or retry_wait not yet due
    TERMINAL = "terminal"  # a terminal node


def evaluate_readiness(
    node_key: str,
    definition: WorkflowDefinition,
    run_id: str,
    run_store: RunStore,
    clock: Clock,
    node_types: NodeTypeCatalog,
) -> NodeReadiness:
    """Evaluate the current readiness of ``node_key`` within ``run_id``.

    Pure given its inputs: calling this twice without any intervening
    ``run_store.put`` or clock advance returns the same answer.
    """
    existing = run_store.get(run_id, node_key)

    if existing is not None:
        if is_terminal(existing.status):
            return NodeReadiness.TERMINAL
        if existing.status is NodeRunStatus.RUNNING:
            return NodeReadiness.WAITING
        if existing.status is NodeRunStatus.RETRY_WAIT:
            if existing.retry_at is not None and clock.now() >= existing.retry_at:
                return NodeReadiness.READY
            return NodeReadiness.WAITING
        # Unreachable with the current NodeRunStatus members, but keep the
        # function total rather than silently falling through.
        raise AssertionError(f"unhandled NodeRunStatus: {existing.status}")

    upstream = definition.upstream_keys(node_key)

    if not upstream:
        # A node with no incoming edges is a source node
        # (docs/architecture.md section 3.3): ready at run start,
        # unconditionally.
        return NodeReadiness.READY

    upstream_rows = {key: run_store.get(run_id, key) for key in upstream}
    if any(row is None or not is_terminal(row.status) for row in upstream_rows.values()):
        return NodeReadiness.NOT_REACHED

    # All upstream nodes are terminal: apply the skip rule over this
    # node's *connected* input ports only.
    node = definition.node(node_key)
    if node is None:
        raise ValueError(f"no such node: {node_key!r}")
    spec = node_types.get(node.type, node.type_version)
    if spec is None:
        raise ValueError(
            f"no node type '{node.type}' version {node.type_version} registered for "
            f"node '{node_key}' -- validate the workflow before scheduling it"
        )

    # Every required port the node TYPE declares, regardless of whether an
    # edge happens to be connected to it. An unconnected required port can
    # never receive data, so it must never be silently dropped from this
    # set -- that was the M1 audit defect: a prior version of this
    # function only tracked required ports that had at least one incoming
    # edge, which let a node become READY/SKIPPED based solely on its
    # optional ports whenever a required port had no edge at all.
    required_port_names = {p.name for p in spec.inputs if p.required}

    connected_edges = definition.edges_into(node_key)
    ports_with_data: set[str] = set()
    any_port_received_data = False

    for edge in connected_edges:
        upstream_row = upstream_rows[edge.source_key]
        # emitted_on() distinguishes "the upstream node produced an
        # (even empty) item list on this port" from "this port's key is
        # simply absent from the upstream's output" -- see
        # NodeRun.emitted_on and docs/architecture.md section 3.2.
        received = upstream_row is not None and upstream_row.emitted_on(edge.source_port)
        if received:
            any_port_received_data = True
            ports_with_data.add(edge.target_port)

    # Every declared required port -- connected or not -- must be among
    # the ports that actually received data.
    all_required_satisfied = required_port_names.issubset(ports_with_data)

    if any_port_received_data and all_required_satisfied:
        return NodeReadiness.READY
    return NodeReadiness.SKIPPED
