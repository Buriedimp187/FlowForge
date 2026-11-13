# ADR-0004: Durable, node-granular execution state; absent-row representation

**Status:** Accepted (revised)

## Context

Crash recovery and resumption require the engine's state to be fully reconstructable
from the database, with no reliance on in-memory process state, and the
representation of "a node that hasn't run yet" needed to be made explicit and
consistent across readiness computation, recovery, and scheduling.

## Decision

A `node_runs` row exists **if and only if** the node has started (its first "node
start" write committed) or been resolved `skipped`. There is no persisted `pending`
row. Node start is write-ahead — committed before the node's `execute` runs — so an
**orphan** (a `running` row whose `started_epoch` predates the run's current
`lease_epoch`) is the only fact recovery needs; there is no separate recovery table
or pass. Readiness is a pure function of `(definition, node_runs rows, db_now)`. A
**source node** (the trigger, with no incoming edges) is ready at run start and is
exempt from the "received data" skip rule, but still executes through the normal
start/finish path so its output is persisted like any other node's.

## Consequences

No N-row insert at enqueue time, no pending→running write, and the UI derives
"not reached" from the definition rather than from a stored state. Recovery logic is
simple because it has exactly one case to detect (an orphaned `running` row) rather
than several.

## Revision history

The representation question (absent row vs. persisted `pending` row) was raised
explicitly during review as a consistency risk and resolved in favor of absent rows,
with the rule stated as binding across readiness, recovery, and scheduling rather
than left implicit.

## Related

`docs/architecture.md` §3; ADR-0005.
