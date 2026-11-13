# FlowForge MVP Scope (v0.1, as of architecture v3.4)

Status: **Normative.** This is the authoritative statement of what v0.1 does and does
not include. It must not be expanded without a documented architecture revision; see
`docs/architecture.md` for the full technical specification behind each item.

## In scope

- **Triggers:** Manual and Webhook only. Cron is deferred (see below).
- **Nodes (9):** Manual Trigger, Webhook Trigger, HTTP Request (SSRF-protected per
  `docs/architecture.md` §7), Set/Transform, IF, Switch, Merge (wait-for-required
  policy), Split Out, LLM Prompt. Iteration is the per-item execution mode on these,
  not a graph-level Loop node (decision **U1**).
- **Engine:** the full queue/lease/fencing/parking/recovery/cancellation/retry/
  timeout/idempotency behavior specified in `docs/architecture.md` §3–§8.
- **Editor:** React Flow canvas, schema-driven node forms (from `GET /node-types`), a
  credential picker, an expression field with a preview, save-as-version, activate,
  workflow import/export.
- **Runs:** manual run, Replay (a new run from the same pinned version and trigger
  payload — with a confirmation listing write-class nodes, since it re-executes
  everything), cancel, a history list, per-node output/error inspection, logs.
- **Security:** a single administrator account with a one-time setup token, encrypted
  credentials, central redaction, the SSRF/outbound policy, hashed single-use webhook
  tokens, and size/queue caps.
- **Operations:** retention pruning, documented `pg_dump`/`pg_restore` backup and
  restore with a CI-verified restore test, `/healthz` and `/readyz`, Docker Compose
  packaging (introduced starting M4).

## Deferred beyond v0.1 (explicitly out of scope — not silently assumed solved)

- **Cron and the scheduler service role.** When it returns, it runs inside the
  worker using a Postgres advisory lock — no new service.
- **The Delay node** (shares durable-timer machinery with cron; deferred together).
- **A dedicated Loop node** (U1) — per-item execution mode plus Split Out is the v0.1
  answer; a Loop node is added later only if real usage shows it's needed.
- **The AI Agent node**, streaming responses, and tool calls. The LLM node is a
  single-turn prompt/response only.
- **A Code node** (arbitrary user code execution) — requires a real sandboxing
  strategy (a separate, network-less, resource-limited container or similar), which
  is infrastructure of its own and a milestone in its own right. Never shipped
  without it (engineering principle #6).
- **Retry-from-failed-node** (as distinct from full Replay). Feasible later because
  outputs are persisted, but not built for v0.1.
- **Sub-workflows, parallel fan-in beyond Merge's wait-for-required policy, a
  human-in-the-loop approval step, binary data, streaming outputs.**
- **Synchronous webhook responses** ("respond with the workflow's result inline") —
  ties up HTTP connections and needs its own timeout design.
- **HMAC-signed webhooks.**
- **Per-workflow concurrency policies** beyond the engine's global
  `WORKER_CONCURRENCY`.
- **Server-Sent Events** for live updates — the MVP polls; SSE can be layered on
  later with no architectural change.
- **Per-item checkpointing** for non-idempotent per-item nodes (**U4**) — an
  interrupted per-item `unsafe_write` node is unknown as a whole, documented as a
  limitation, not silently assumed away.
- **A node plugin/marketplace system.** An internal, extensible node registry exists
  from M1; opening it to third parties is a later decision.
- **Multi-user, teams, RBAC** — a single administrator account only.
- **Any SaaS feature**: billing, subscriptions, multi-tenancy, managed hosting.
- **A forward/corporate outbound proxy mode** — operators needing one use
  network-level egress control instead (`docs/architecture.md` §7.1).
- **A cumulative LLM spend cap** (per-run or per-day) — token usage is recorded as a
  lower bound only; no monetary computation exists anywhere in the system.
- **Localization/translation (U8):** English only for code and documentation; the
  frontend routes all user-facing strings through a message-catalog indirection and
  the API returns stable machine-readable error codes, so a later translation effort
  needs no restructuring, but no translation is implemented in v0.1.
- **Redis or any additional infrastructure service** — PostgreSQL remains the only
  required service unless a documented, concrete technical justification and a new
  ADR supersede this (`docs/architecture.md` §1.1, ADR-0002).

## The U1–U8 decisions (confirmed, binding)

| ID | Decision |
|---|---|
| U1 | Per-item execution mode + Split Out; no dedicated Loop node in v0.1 |
| U2 | Explicit master-key provisioning by default (fail closed); an opt-in quick-start flow may generate one |
| U3 | Webhook tokens: hashed, shown once, regenerable |
| U4 | No per-item checkpointing in v0.1; the limitation is documented, not solved |
| U5 | On workflow failure, drain in-flight nodes rather than cancel them mid-flight (avoids manufacturing further outcome ambiguity) |
| U6 | Documented `pg_dump`/`pg_restore` scripts, with a tested restore procedure, rather than a bundled backup command |
| U7 | PostgreSQL major version 17 (exact minor pin: `docs/toolchain-versions.md`) |
| U8 | English for source and docs; frontend architecture kept translation-ready; no translations implemented |

## Deliberately excluded infrastructure (reaffirmed)

Microservices, a plugin marketplace, Celery, LangChain/LiteLLM, a bundled reverse
proxy, multi-user RBAC, Kubernetes, a separate scheduler service, an event-sourcing
framework, and a Code node — none of these appear anywhere in v0.1, and none should
be added to satisfy "completeness" rather than a demonstrated need.

## Narrow scope adjustments forced by the v3.1–v3.4 security corrections

These are not new features; they are the minimum additions required for the engine
to be honest about what it cannot guarantee, per `docs/architecture.md` §6–§7 and
§9.3:

- The HTTP node's `retry_safety` parameter (`auto` / `safe_to_retry` / `never_retry`)
  and optional `idempotency_key_header`, replacing any notion that PUT/DELETE are
  automatically safe to retry.
- Redirect-following limited to class `read`, and not followed at all in restricted
  outbound mode.
- No connection reuse, no HTTP/2, no streaming request bodies in the outbound HTTP
  client (needed for a provable dispatch boundary).
- A visible "outcome unknown" badge on affected runs/nodes.
- The three-state LLM usage/billing UI model (`docs/message-catalog.md`).
- Queue-timeout and run-timeout settings, surfaced in the UI and API.
- A `restricted`/`disabled` outbound mode as a **fail-closed state**, not a
  convenience feature operators choose — see `docs/architecture.md` §7.3.
