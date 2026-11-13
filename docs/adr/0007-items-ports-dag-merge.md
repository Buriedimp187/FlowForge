# ADR-0007: Items-and-ports data model, acyclic DAG, wait-for-required Merge

**Status:** Accepted

## Context

The engine needs a data-passing model between nodes and a scheduling model over the
workflow graph that supports branching and rejoining without the complexity of
general cyclic graph execution.

## Decision

Nodes pass lists of items (`{json: {...}}`) along named output ports to named input
ports. The execution graph must be acyclic; this is enforced at save time. Merge
declares its input ports as `required` or optional and uses a simple
**wait-for-required-inputs** policy: since readiness already waits for every
upstream node to resolve, an optional port is what lets `IF → (true|false branch) →
Merge` work, because only the taken branch supplies data while the other side
resolves `skipped`. Iteration is a node's own **per-item execution mode** plus a
**Split Out** node, not a graph-level cycle (decision U1 — no dedicated Loop node in
v0.1).

## Consequences

Scheduling stays simple (a DAG readiness computation, no cycle-breaking logic), and
skip semantics are well-defined. Some iterative patterns a cyclic graph could express
are not expressible in v0.1; this is accepted and may be revisited post-MVP based on
real usage.

## Related

`docs/architecture.md` §2–§3; `docs/mvp-scope.md` (U1).
