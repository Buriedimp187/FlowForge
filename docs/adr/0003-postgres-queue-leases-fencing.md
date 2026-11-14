# ADR-0003: Postgres-backed queue with leases, fencing, and parking

**Status:** Accepted (revised)

## Context

Without Redis or a broker (ADR-0002), the execution queue must live in PostgreSQL
itself, and must survive worker crashes, lost leases, and long retry waits without
leaking worker concurrency slots or corrupting state.

## Decision

`runs` is the queue, with statuses `queued | waiting | running | <terminal>`. A claim
uses `SELECT ... FOR UPDATE SKIP LOCKED` in a short transaction; reclaiming an
expired lease is **the same query** as an ordinary claim — there is no separate
reaper process. Every lease is `(lease_owner, lease_epoch, visible_at)`; every
state-changing write is **fenced** by re-checking `lease_owner`/`lease_epoch` before
it commits. A run with nothing currently executable (waiting only on a `retry_at` in
the future) is **parked**: moved to `status='waiting'`, `lease_owner` set to NULL,
`visible_at` set to the earliest of the pending retry time and the run deadline — and
the worker's concurrency slot is freed immediately. `recovery_count` increments only
on an expired-lease takeover, never on an ordinary claim or a wake from `waiting`
(so parking and retries don't trip the poison guard that bounds crash-looping
payloads). The heartbeat that renews a lease runs on a dedicated thread with its own
database connection, separate from the executor's event loop, so a CPU-heavy node
cannot starve lease renewal. On SIGTERM, the worker parks every run it owns for
immediate pickup by another worker, rather than waiting for the lease to expire; an
attempt still running at grace expiry is left in its `running` row, and the next
claimant resolves it as an orphan through the ordinary crash-recovery rules.

## Consequences

No reaper process, no worker resource held while waiting on a retry timer, and a
zombie worker can never write state after its epoch is superseded. Retry-timing
precision is bounded by the polling interval (~1 s plus jitter) — documented, not a
defect.

## Revision history

Originally specified with Redis assisting as a non-authoritative notifier; revised
during architecture review to remove Redis entirely (ADR-0002) and to add explicit
parking, a dedicated heartbeat thread, and SIGTERM-triggered graceful release, after
identifying that an unparked "waiting-on-retry" run would otherwise hold a worker
slot indefinitely.

## Related

`docs/architecture.md` §4; ADR-0002, ADR-0009, ADR-0017.
