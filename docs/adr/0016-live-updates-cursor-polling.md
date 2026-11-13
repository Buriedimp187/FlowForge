# ADR-0016: Live updates via cursor-based polling

**Status:** Accepted

## Context

The editor and run inspector need near-live visibility into run progress, but
without Redis or a broker (ADR-0002) there is no pub/sub layer to push updates, and
building one solely for this would reintroduce the infrastructure this project is
deliberately avoiding.

## Decision

Because exactly one fenced lease holder ever writes a given run's events at a time
(ADR-0003), that worker can assign a gap-free `run_events.seq` per run, re-initialized
from `MAX(seq)` on reclaim. The API exposes `GET /runs/{id}/events?after=seq`, and
the v0.1 client **polls** it (and the run/node state endpoints) at roughly 1-second
intervals.

## Consequences

Works with no broker and no additional infrastructure. Server-Sent Events can be
layered over the same cursor later with no change to the underlying data model,
should polling prove too coarse in practice — but that is not needed for v0.1.

## Related

`docs/architecture.md` §15; ADR-0002, ADR-0003.
