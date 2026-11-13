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

**`idempotent_write`/Node-timeout clarification (M1 final correction, user-approved):**
an earlier M1 audit pass identified a textual conflict between this ADR's general
"an outcome_unknown from crash or timeout fails the run" rule and the matrix's own
`idempotent_write`/Node-timeout cell ("unknown; retryable with the same key"), and
reported it rather than silently resolving it. The user has since approved an
unambiguous rule, now normative in `docs/architecture.md` §6.2: an `idempotent_write`
node whose outcome is unknown after a timeout goes to `retry_wait` (same key, never a
new one, never reported as `succeeded`) while its retry budget remains; the run fails
unconditionally, regardless of `on_error`, only once that budget is exhausted with the
outcome still unknown. The general "fails the run" rule is therefore understood as
applying to `unsafe_write` always, and to `idempotent_write` only once its own
retries are exhausted. The pure engine implementation of this rule is
`flowforge.engine.interruption.resolve_idempotent_write_timeout`.

**Consistency fix (M1 final consistency fix, second audit pass):** the initial
implementation of the rule above left `resolve_interruption()`'s own, simpler
cause x effect-class classification still able to return a terminal `FAILED`
resolution for `idempotent_write`/`NODE_TIMEOUT` without knowing the retry
budget — a live contradiction with `resolve_idempotent_write_timeout`'s
`retry_wait`-while-budget-remains behavior if a caller used the generic
function instead of the dedicated one. `resolve_interruption()` no longer
classifies that combination itself: it requires the caller to supply retry
context and delegates to `resolve_idempotent_write_timeout`, so there is one
authoritative decision path, not two that could disagree.

## Related

`docs/architecture.md` §5.2, §6; ADR-0004, ADR-0012, ADR-0013.
