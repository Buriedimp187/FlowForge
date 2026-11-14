# ADR-0009: Cancellation and finalization protocol

**Status:** Accepted (revised)

## Context

Cancellation must behave correctly no matter which of several concurrent operations
(a cancel request, a finalize, a park, a reclaim) commits first, and must not
manufacture outcome ambiguity by forcibly interrupting an in-flight external write.

## Decision

A cancel request is one idempotent, atomic SQL statement: it finalizes a `queued`
run directly (which has no node rows and so is safe for the API to finalize itself),
makes a `waiting` run immediately claimable, and otherwise only sets a
`cancel_requested_at` flag observed by the owning worker's heartbeat. Finalization
(T4) is a single fenced statement whose terminal status is computed from the row **as
locked at statement time**, with precedence `failed`/`timed_out` (natural) >
`cancelled` > `succeeded` — so a cancel that commits first can never be overtaken by
a late `succeeded` finalize, and vice versa. A run deadline the database has
confirmed when T4 executes is decided inside that statement, from one clock sample
shared with `finished_at`, and yields `timed_out`. On workflow failure (including a
cancellation, or a database-confirmed run deadline, reaching in-flight nodes), the engine **drains** in-flight nodes to
their natural conclusion rather than forcibly cancelling them mid-flight (decision
**U5**), because forcing a stop on an external write would itself manufacture a new
outcome-unknown case.

## Consequences

Every cancel/finalize/park/claim race has one defined, tested result (see the race
matrix in `docs/architecture.md` §6.2a), and a cancelled run can honestly coexist with
node outputs that had already completed before the cancel was observed — the UI
states this explicitly rather than hiding it.

## Revision history

The original design risked a run finishing `succeeded` despite a cancel request that
had already committed; this was identified during review and closed by making
finalization a single statement that re-derives the terminal status from the locked
row rather than from a value computed before the lock was taken. The U5
drain-vs-force-cancel choice was confirmed explicitly rather than left as an
implementation detail. M2 design review: the drain was extended explicitly to a
confirmed run deadline; T4 now decides the deadline in-statement on one clock sample;
and suppressing a pending `retry_wait` preserves its recorded outcome rather than
resolving it as known.

## Related

`docs/architecture.md` §6.2 and §6.2a (exact SQL and the race matrix);
ADR-0003, ADR-0005.
