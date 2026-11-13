# ADR-0006: Immutable workflow versions; definition separate from UI layout

**Status:** Accepted

## Context

A workflow editor's view state (React Flow positions, viewport, handle geometry) is
not the same thing as what the execution engine needs, and reproducible run history
requires that editing a workflow never retroactively change what a past or in-flight
run executes.

## Decision

React Flow's state is converted, through one frontend adapter module, to a
backend-owned, backend-validated **definition** schema; the engine never reads
layout data. Saving a workflow creates a new **immutable version row**; a run pins a
specific `workflow_version_id` at enqueue time. Versions referenced by any run are
never deleted.

## Consequences

Reproducible execution history and safe concurrent editing (an in-progress run is
unaffected by a save that happens during it), at the cost of version rows
accumulating over time — pruned only once unreferenced by any run, per the
retention policy.

## Related

`docs/architecture.md` §2.
