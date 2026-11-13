# FlowForge Milestones (M0–M7)

Status: **Normative plan**, as of architecture v3.4. See `docs/mvp-scope.md` for what
"done" means in scope terms and `docs/risks-and-release-gates.md` for what must be
demonstrated before v0.1 ships.

## M0 — Repository foundation (this milestone)

Deliverables: repository structure, `LICENSE`, `README.md`, `.gitignore`, editor
config, the canonical architecture documentation (`docs/architecture.md`), ADR-0001
through ADR-0018 (`docs/adr/`), verified toolchain version pins with sources
(`docs/toolchain-versions.md`), the MVP scope document, the milestone plan (this
file), the risk/gate register, the message catalog spec, and a minimal CI skeleton.

**No application code, dependencies, runtimes, or Docker files are created in M0.**
Exit criterion: the documentation set is internally consistent with v3.4 and CI
passes its structure/syntax checks. See the M0 completion report for the actual
outcome of this milestone.

## M1 — Domain and pure engine

Deliverables: `domain` models, the save-time validator, the absent-row node-state
model and readiness/skip logic (`docs/architecture.md` §3), Merge's wait-for-required
policy, the per-item execution mode, retry/backoff logic driven by a fake clock,
cancellation semantics, an in-memory `RunStore` port implementation for tests, and
deterministic per-item idempotency-key derivation (§6.4).

Depends on: M0. Exit criterion: unit tests cover branching, skip propagation, merge,
retries, and cancel — all against the in-memory store, with no database yet.

## M2 — Persistence and queue

Deliverables: the PostgreSQL schema and Alembic migrations, T0–T4 as specified in
`docs/architecture.md` §4–§5 (claim/reclaim, the fenced guard and heartbeat thread,
parking, graceful SIGTERM release, the poison guard via `recovery_count`, the
single-statement cancel/finalize race resolution), the `effect_class` immutability
trigger and CHECK, and the retention maintenance task.

Depends on: M1. Exit criterion: the **crash-injection and race-harness suite**
passes against a real PostgreSQL instance — every cell of the interruption matrix
(§6.2) and every race in the cancel/finalize/park/claim matrix, plus `effect_class`
immutability under a simulated classifier change. This is the project's flagship
test suite (release gate RB3).

## M3 — Nodes, expressions, and the outbound security spike

**Order matters here: the outbound security work comes first, before any node that
depends on it.**

1. **Spike S1** (`docs/architecture.md` §7.7): the dispatch-marker layer report and
   the fault-injection harness against the pinned httpcore/httpx versions, producing
   a recorded Tier (A, B, or C) with the mandatory zero-false-negative property.
2. **SSRF/pinning implementation and its full test suite**: the CS, AP6, AP7, AP4
   gates (§7.2–§7.6), the absolute-deny set (verified against current provider
   documentation, with the verification date recorded), the mode-eligibility table,
   the per-connection invariant, and the proxy-inertness tests.
3. Only then: the node registry, the restricted expression engine (§8, with the
   deterministic/no-wall-clock whitelist), and the built-in nodes — Set, IF, Switch,
   Merge, Split Out, and HTTP Request (with `retry_safety` per §6.1).

Depends on: M1 (parallel with M2 is acceptable once M1 is done). Exit criterion:
every release-blocking outbound test (RB1a, RB1b, RB1c, RB2) passes, or the
documented fail-closed/restricted fallback is in effect and recorded in an ADR;
sandbox-escape and redaction tests pass for the expression engine.

## M4 — API, security, and first Dockerfiles

Deliverables: authentication (setup token, sessions, rate limiting), workflow and
version CRUD, run endpoints, credential storage (encryption + startup canary),
webhook ingress, the `GET /node-types` schema endpoint, OpenAPI generation. **The
first Dockerfiles and Compose files are created here**, to dogfood the stack —
pinned, digest-referenced image versions per `docs/toolchain-versions.md`, no
floating tags.

Depends on: M2, M3. Exit criterion: a full workflow is drivable end-to-end with
`curl` against the local Compose stack.

## M5 — Frontend

Deliverables: the React Flow editor, schema-driven node forms, the view-model ⇄
definition adapter (with its own contract tests), the run inspector, credentials and
settings UI, the message-catalog indirection for i18n-readiness (U8).

Depends on: M4 (may start against the OpenAPI contract mid-M4). Exit criterion: a
user can build, run, inspect, and cancel a workflow entirely in the browser.

## M6 — LLM Prompt node and provider adapters

Deliverables: the AI provider port, 2–3 adapters (OpenAI-compatible/Ollama,
Anthropic, Gemini — as time allows), the `classify()` billing-contract
implementation per adapter with dated documentation citations
(`docs/architecture.md` §9.3), the message-catalog UI wording and its tests
(`docs/message-catalog.md`), and fake-provider tests (no real provider calls in CI).

Depends on: M3, M4. Exit criterion: a BYOK workflow runs end-to-end with a real key
(manual verification only); all RB8 tests pass, including the "processed-then-
dropped" scenario and the banned-term/catalog-allowlist tests.

## M7 — Hardening and v0.1 release

Deliverables: the backup/restore scripts and their CI-verified restore test,
operator documentation, Playwright end-to-end tests, a Compose smoke test, and the
v0.1 tag.

Depends on: all prior milestones. Exit criterion: a fresh clone reaches a running
workflow in under 10 minutes, a restore is verified, and **every release-blocking
gate in `docs/risks-and-release-gates.md` is either satisfied or has its documented
fallback in effect.**

## Parallelism notes

- M2 and M3 may proceed in parallel once M1 is complete.
- M5 may begin against the OpenAPI contract partway through M4.
- The engine, the queue, and the security-critical tests (M1–M3) are deliberately
  sequenced **before** the UI — a polished editor over an unreliable or insecure
  engine is the failure mode this plan avoids.

## Suggested post-v0.1 order (not scheduled, not scoped)

Cron and the Delay node, retry-from-failed-node, Server-Sent Events, the Code node's
sandbox, the AI Agent node.
