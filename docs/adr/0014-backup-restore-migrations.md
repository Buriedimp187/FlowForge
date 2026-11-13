# ADR-0014: Backup, restore, and migration compatibility

**Status:** Accepted

## Context

A self-hosted application with encrypted credentials needs a backup strategy that
doesn't accidentally defeat its own encryption (by backing up the key alongside the
data it protects) and a migration strategy that prevents `api` and `worker` from
ever running against mismatched schemas.

## Decision

Backup and restore use documented, tested `pg_dump -Fc` / `pg_restore` scripts run
through the Postgres container (decision **U6**) — no Postgres client is bundled
into the application image. **The master key is backed up separately from the
database dump**; a dump without the key cannot decrypt credentials, and storing them
together would defeat the encryption, so this is stated prominently in operator
documentation. Restore: stop `api` and `worker`, restore into an empty database,
supply the same key, run migrations, start — the startup canary (ADR-0010) validates
the key. Alembic migrations live inside the Python package and are forward-only;
`flowforge migrate` takes a `pg_advisory_lock`, and **both `api` and `worker` refuse
to run unless the database revision equals the code's head**, so the two roles can
never run against a skewed schema.

## Consequences

A credible, CI-testable disaster-recovery story (backup → wipe → restore, release
gate RB4) using only tools already present, at the cost of a manual two-part backup
(dump + key) that operators must be told about clearly rather than assume is
automatic.

## Related

`docs/architecture.md` §12–§13; ADR-0010; `docs/risks-and-release-gates.md` (RB4).
