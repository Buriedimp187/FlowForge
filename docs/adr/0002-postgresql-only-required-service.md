# ADR-0002: PostgreSQL as the only required service

**Status:** Accepted

## Context

A zero-budget, solo-maintained, self-hosted project. Redis (or any broker) adds a
second stateful service to deploy, back up, and reason about for consistency with
the primary store, with no capability the project currently needs that PostgreSQL
cannot provide.

## Decision

All durable state and the execution queue live in PostgreSQL. Redis, a message
broker, and a separate scheduler service are not used in v0.1.

**Revisit criterion** (the door is open, but guarded): Redis or an equivalent may be
reconsidered only if measurement shows PostgreSQL polling or claim contention is a
genuine bottleneck that tuning and `LISTEN/NOTIFY` cannot fix, or a concrete
cross-process fan-out need arises that polling cannot satisfy. Neither is expected at
this project's target scale (tens of runs/second at most), and neither justifies
adding the dependency speculatively.

## Consequences

One volume to back up, no cross-store consistency problem between a queue and a
database, and live updates via polling instead of push (accepted — see ADR-0016).

## Related

`docs/architecture.md` §1.1, §4, §15; ADR-0003.
