# ADR-0017: Run and queue time limits

**Status:** Accepted

## Context

Without a bounded lifetime, a run stuck waiting indefinitely for a worker (none
alive, all at capacity) and a run that keeps retrying or getting reclaimed
indefinitely are two different failure modes that need two different limits, and it
needed to be explicit whether a timeout clock starts at enqueue or at execution
start.

## Decision

Two separate, explicitly-scoped limits, plus a per-attempt node timeout:

| Limit | Starts | Ends | On expiry |
| --- | --- | --- | --- |
| **Queue timeout** | Enqueue | First claim | `timed_out`, `queue_timeout` — detected only when a worker finally claims the run; nothing has ever executed |
| **Run timeout** | First claim | Terminal | `timed_out`, with any interrupted nodes resolved per the interruption matrix (ADR-0005). Deliberately includes retry waits and parked downtime — nothing resets it once set |
| **Node timeout** | Each attempt's start | That attempt's end | Per the interruption matrix; retry backoff time is excluded |

`deadline_at` (the run timeout) is set exactly once, at first claim, via
`COALESCE` in the claim statement, so it can never be silently extended by a later
reclaim. A parked run (ADR-0003) wakes at the earlier of its next retry time or its
deadline, so a timeout fires on schedule even with no live owner in between.

## Consequences

Every run has a bounded lifetime by construction, at the cost of queue-timeout
expiry only being detected the next time some worker looks at the row — until then,
the UI correctly shows the run as "stalled" rather than claiming a status it cannot
yet know.

## Related

`docs/architecture.md` §4.6; ADR-0003, ADR-0005.
