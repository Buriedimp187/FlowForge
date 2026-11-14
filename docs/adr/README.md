# Architecture Decision Records

Status legend used below:

- **Accepted** — the decision stands as the current normative design.
- **Accepted (revised)** — the decision stands, but has been revised at least once
  since it was first recorded; the "Revision history" section says what changed and
  why. The ADR body reflects only the **current** (final, v3.4) decision — it is not
  a diff log.

All ADRs here are consistent with `docs/architecture.md`, which is the canonical,
consolidated specification. Where an ADR and `docs/architecture.md` ever appear to
disagree, `docs/architecture.md` governs, and the ADR should be corrected to match —
file an issue rather than relying on the mismatch.

No new ADR was created solely to pad this directory. Every ADR here corresponds to
an actual decision point raised and resolved during architecture review.

| ID | Title | Status |
| --- | --- | --- |
| [0001](0001-modular-monolith.md) | Modular monolith with three roles | Accepted |
| [0002](0002-postgresql-only-required-service.md) | PostgreSQL as the only required service | Accepted |
| [0003](0003-postgres-queue-leases-fencing.md) | Postgres-backed queue with leases, fencing, and parking | Accepted (revised) |
| [0004](0004-durable-node-state-absent-row.md) | Durable, node-granular execution state; absent-row representation | Accepted (revised) |
| [0005](0005-effect-classes-idempotency.md) | Effect classes, unknown outcomes, and idempotency keys | Accepted (revised) |
| [0006](0006-immutable-workflow-versions.md) | Immutable workflow versions; definition separate from UI layout | Accepted |
| [0007](0007-items-ports-dag-merge.md) | Items-and-ports data model, acyclic DAG, wait-for-required Merge | Accepted |
| [0008](0008-restricted-expression-language.md) | Restricted expression language (no `eval`) | Accepted |
| [0009](0009-cancellation-finalization.md) | Cancellation and finalization protocol | Accepted (revised) |
| [0010](0010-credential-encryption-redaction.md) | Credential encryption and central redaction | Accepted |
| [0011](0011-single-admin-cookie-sessions.md) | Single-administrator cookie sessions | Accepted |
| [0012](0012-ssrf-outbound-policy.md) | SSRF and outbound HTTP policy | Accepted (revised) |
| [0013](0013-ai-provider-port-byok.md) | AI provider port and BYOK | Accepted (revised) |
| [0014](0014-backup-restore-migrations.md) | Backup, restore, and migration compatibility | Accepted |
| [0015](0015-toolchain-baselines-and-pinning.md) | Toolchain baselines and version pinning | Accepted |
| [0016](0016-live-updates-cursor-polling.md) | Live updates via cursor-based polling | Accepted |
| [0017](0017-run-and-queue-time-limits.md) | Run and queue time limits | Accepted |
| [0018](0018-outbound-http-transport.md) | Outbound HTTP transport (pinning + dispatch marker) | Accepted (revised) |
