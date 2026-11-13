# ADR-0005: Effect classes, unknown outcomes, and idempotency keys

**Status:** Accepted (revised)

## Context

Exactly-once execution is impossible across a process boundary. Without an explicit
model, a crash, cancellation, timeout, or deadline during an external write risks
either silently duplicating a side effect or silently mislabeling an ambiguous one as
safe.

## Decision

Nodes declare one of four effect classes: `pure`, `read`, `idempotent_write`, or
`unsafe_write` (the default for any write the user has not explicitly asserted is
safe to repeat — HTTP method alone never implies safety; see ADR-0012's
companion ADR-0013 for the HTTP-specific `retry_safety` parameter). A single
cause-×-class **interruption matrix** governs crash/lease-loss, cancellation,
node-timeout, and run-deadline uniformly, producing a per-node `outcome ∈ {known,
unknown}` that refers strictly to **external-state mutation**. `unsafe_write` with an
unknown outcome is **never** retried automatically and **never** labeled safely
cancelled; it fails the run outright (ignoring `on_error`), because silently
continuing past an ambiguous write is exactly the hazard this ADR closes.

`effect_class` is **resolved once, at the first execution attempt, and is immutable
for that node's execution** thereafter — across retries, crash recovery, and parked
resumption — enforced by a database trigger and CHECK constraint independent of
application code, so a later code change cannot silently reclassify an in-flight
node.

Idempotency keys are deterministic: `UUIDv5(run_id, node_key[, item_index])`. They
are stable across retries and recovery because a node's inputs are derived from
persisted, never-rewritten upstream outputs in a fixed edge order — the same items in
the same order on every attempt. Keys deliberately exclude the attempt number (a
retry must present the same key) and a Replay run gets an entirely fresh `run_id`
(it is a new execution, not a resumption).

## Consequences

No silent duplicate or mislabeled external write, at the cost of a visible
`outcome_unknown` state that requires a human decision, and a documented limitation
that a key only protects as far as the remote API honors it.

## Revision history

The effect-class set and interruption matrix were refined across several review
passes: the HTTP-method-implies-safety assumption was removed (PUT/DELETE no longer
default to `idempotent_write`); the `effect_class`-immutability rule and its database
trigger were added after identifying that an application upgrade mid-run could
otherwise silently reclassify a node; per-item key derivation was added once the
single `run_id:node_key` key was found insufficient for per-item operations.

## Related

`docs/architecture.md` §5.2, §6; ADR-0004, ADR-0012, ADR-0013.
