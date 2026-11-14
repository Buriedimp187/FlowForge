# FlowForge Architecture (v3.4 — canonical)

**Status: Authoritative.** This document is the single consolidated architecture
specification for FlowForge, folding in every decision and correction through
revision v3.4 of the architecture review. It supersedes every earlier draft (v1, v2,
v3, v3.1, v3.2, v3.3) in full; those drafts are not preserved elsewhere in this
repository and should not be treated as normative if found anywhere else (chat logs,
issue history, etc.).

This document is meant to let implementation proceed **without relying on
undocumented conversation history**. Anything an implementer needs to know about why
the system is shaped this way should be here, in the companion ADRs under
`docs/adr/`, or in `docs/mvp-scope.md`, `docs/milestones.md`,
`docs/risks-and-release-gates.md`, and `docs/message-catalog.md`.

Nothing in this document has been implemented. M0 (this milestone) is documentation
and repository foundation only — see `docs/milestones.md`.

## 0. Product vision and principles

FlowForge is an open-source, self-hosted, visually-driven workflow automation
platform with AI integration (BYOK — Bring Your Own Key). It targets a solo or small
team of developers, is distributed under the MIT license, and is installed via Docker
Compose onto infrastructure the user owns and operates. There is no SaaS hosting, no
multi-tenancy, and no managed-cloud component in the roadmap.

Engineering principles (unchanged since the first architecture review and binding on
every decision below):

1. A reliable execution engine takes priority over feature breadth.
2. Modular monolith over microservices.
3. Frontend, API and worker responsibilities are clearly separated.
4. Designed for local deployment and persistent, durable data.
5. Credentials and user-supplied API keys are handled securely.
6. No execution of arbitrary user code without a real isolation strategy (which the
   MVP does not yet provide — see `docs/mvp-scope.md`, "A Code node").
7. No unnecessary dependencies or infrastructure.
8. Suitable for a professional public GitHub portfolio.
9. No SaaS billing, multi-tenancy, subscriptions, or managed cloud infrastructure.
10. No Kubernetes or other infrastructure the architecture has not approved.

## 1. System architecture

### 1.1 Runtime topology

```text
Browser ──> web (Next.js) ──HTTP──> api (FastAPI) ──┐
                                                    ├──> PostgreSQL  (the only stateful service)
                              worker (Python) ──────┘
```

FlowForge is a **modular monolith** run as three processes from (at most) two
container images: a Next.js frontend, and a single Python backend package run in two
roles (`api` and `worker`). **PostgreSQL is the only required infrastructure
service.** There is no Redis, no message broker, no separate scheduler service, and
no pub/sub layer (ADR-0002).

| Role | Process | Owns | Must not |
| --- | --- | --- | --- |
| **web** | Next.js, TypeScript | Editor, run inspector, settings UI | Execute workflows, hold secrets, talk to PostgreSQL directly |
| **api** | `flowforge serve` | Auth, CRUD, validation, enqueue, webhook ingress, cancel requests, read APIs | Execute nodes; write node-level state or terminal run status, except the single `queued → cancelled` transition |
| **worker** | `flowforge worker` (same package as `api`) | Claim runs, execute nodes, persist node state, finalize runs, retention maintenance | Serve user-facing HTTP (a health endpoint only) |
| **PostgreSQL** | pinned major version (17; see `docs/toolchain-versions.md`) | All durable state **and** the queue | |

**Queue ownership rule:** the `runs` table is the queue. A run is owned by exactly one
worker while its lease is valid, identified by `(lease_owner, lease_epoch)`. Only the
owner may write that run's `node_runs` and `run_events` rows and its terminal status.
The API writes only `queued` rows, the `cancel_requested_at` flag, and the single
`queued → cancelled` transition. See §4 for the exact SQL semantics.

Cron scheduling is **deferred past v0.1** (see `docs/mvp-scope.md`); when it returns,
it runs inside the worker role using a Postgres advisory lock, not as a new service.

### 1.2 Layering and module boundaries (backend)

The backend package is organized so the execution engine has no dependency on
transport or persistence frameworks, enforced by an import-linter rule in CI once code
exists:

- `domain` — pure models (definitions, items, enums, effect classes). Imports
  nothing internal.
- `engine` — graph, readiness, executor, retry/timeout logic, recovery. Imports
  `domain` only; depends on abstract ports (a run store, a clock, a node registry),
  never on FastAPI or a live SQLAlchemy session.
- `expressions` — the restricted template/JMESPath evaluator (§8).
- `nodes` — node registry and built-in node implementations. Depends on `domain`,
  `security`, and `ai`; never on `persistence` directly.
- `ai` — the provider port and adapters (§9).
- `security` — crypto, redaction, the outbound HTTP policy (§7), passwords, sessions.
- `persistence` — SQLAlchemy models, repositories, the queue SQL (§4), Alembic
  migrations.
- `services` — the use-case layer (workflows, runs, credentials, auth).
- `api` / `worker` — thin leaves calling `services`.

See `docs/adr/0001-modular-monolith.md`.

## 2. Workflow representation and data model

### 2.1 View model vs. execution model

React Flow's state (node positions, viewport, handle geometry) is a UI concern only.
The frontend converts it, through one adapter module, to a backend-owned **definition**
schema; the backend validates and is authoritative over that schema. The execution
engine never reads layout data.

```text
Definition (versioned by schema_version)
  nodes[]: { key, type, type_version, name, params, credential_id?,
             retry?, timeout_s?, on_error }
  edges[]: { source_key, source_port, target_key, target_port }
  ui:      { positions, viewport }      # stored, ignored by the engine
```

`key` is an immutable per-node slug; expressions and idempotency keys (§6.4) reference
it, so renaming a node's display name never breaks a workflow or its in-flight runs.

### 2.2 Save-time validation (backend-authoritative)

- Exactly one **source node** (the trigger: Manual or Webhook — see §3.3 and
  `docs/mvp-scope.md`).
- The graph is **acyclic**, and every node is reachable from the trigger.
- Ports and node keys are valid and unique.
- Node parameters validate against that node type's JSON Schema.
- Referenced credentials exist and are of the correct type.
- Expressions parse under the restricted grammar (§8).

A save creates an **immutable version row**; there is no autosave into versions. A
version referenced by any run is never deleted.

### 2.3 Tables (PostgreSQL, UTC `timestamptz`, UUIDv7 or bigint ids)

| Table | Notes |
| --- | --- |
| `admin_user` | Single row enforced by constraint; argon2id password hash |
| `sessions` | Hashed session tokens, expiry, revocable |
| `workflows` | name, `active_version_id`, `webhook_enabled` |
| `workflow_versions` | Immutable `definition` JSONB, `schema_version`, version number |
| `webhook_endpoints` | `token_hash` (unique, 256-bit token, shown once — U3), workflow, allowed method(s) |
| `credentials` | name, type, `ciphertext`, `nonce`, `key_id` (AES-256-GCM, AAD-bound; §10) |
| `runs` | the queue and run record — see §4 for the full column set and semantics |
| `node_runs` | PK `(run_id, node_key)` — see §5 and §6 for the full column set |
| `run_events` | PK `(run_id, seq)`: level, node_key, message, ts (redacted) |
| `app_settings` | schema/app version, encrypted key-check canary (§10) |

Node **inputs are not stored**; they are derived from `edges` plus the persisted
outputs of upstream `node_runs`, in a fixed edge order. This derivability is exactly
what makes crash recovery possible (§5, §6). Resolved (post-expression, redacted)
params are stored on `node_runs` for debugging.

**Size caps (configurable, enforced):** webhook body default 1 MiB, node output
default 1 MiB (the node fails `output_too_large` beyond it), plus a per-run total cap.
**Retention:** a worker maintenance task, guarded by `pg_try_advisory_lock`, prunes
*terminal* runs past a configurable age/count. It never touches `queued`, `waiting`,
or `running` rows.

## 3. Node state model

### 3.1 Representation rule (absent rows mean unstarted)

A `node_runs` row **exists if and only if** the node has been started (its first T2
committed, §6.3) or resolved as `skipped`. An absent row means the node has never
started. There is no persisted `pending` state.

Rationale: no N-row insert at enqueue time, no pending→running write, and recovery
needs exactly this one fact ("did this node ever start") — which an absent row gives
for free. The UI renders an absent row as "not reached", computed against the pinned
definition.

```text
(absent) ─[T2]→ running ─[T3]→ succeeded | failed | cancelled | retry_wait
retry_wait ─[T2]→ running                 (when retry_at ≤ db_now)
(absent) ─[scheduling pass or T3]→ skipped
```

`node_runs.status ∈ {running, retry_wait, succeeded, failed, skipped, cancelled}`.
**Terminal** = `succeeded | failed | skipped | cancelled`. `running` and `retry_wait`
are non-terminal. An **orphan** is a `running` row whose `started_epoch` is older than
the run's current `lease_epoch` — its owner is gone (crash or lost lease). Orphans are
the only input recovery needs; there is no separate recovery table or pass.

### 3.2 Readiness

| Node condition | Ready? |
| --- | --- |
| **Source node** (no incoming edges — the trigger), no row | Ready at run start. Exempt from the "received data" rule below; "all upstream terminal" holds vacuously |
| Other node, no row | Ready iff all upstream nodes of its *connected* edges are terminal |
| `retry_wait` row | Ready iff `retry_at ≤ db_now` |
| Orphan row | Not scheduled for ordinary readiness — resolved per the interruption matrix (§6.2) |
| `running` row of the current epoch | Not ready (in flight in this worker) |

**Skip rule** (applies to non-source nodes once all connected upstream nodes are
terminal): the node runs iff **at least one** connected input port received data
**and** every port marked `required` received data. Otherwise it is written `skipped`.
Skips are written idempotently by the scheduling pass (`ON CONFLICT DO NOTHING`) and
also by T3 for immediate visibility, so recovery never depends on T3 having run.

**Merge** uses a simple **wait-for-required-inputs** policy: it declares ports, each
`required` or optional. Because readiness already waits for all upstream nodes to
resolve, an optional port is what makes `IF → (true|false branch) → Merge` work (only
the taken branch supplies data; the other side resolves `skipped`, and an optional
port tolerates that). A Merge whose only required input was skipped is itself skipped.

**Iteration** is the node's own **per-item execution mode** plus a **Split Out** node
(explode an array field into items); it does not introduce a graph cycle. There is no
dedicated Loop node in v0.1 (decision U1 — see `docs/mvp-scope.md`).

### 3.3 Trigger (source node) execution

The trigger node goes through the **normal** T2/T3 path, effect class `pure`. Its
`execute` emits the run's `trigger_payload` as a single item on its output port
(`{method, headers, query, body}` for Webhook; `{json: manual_input ?? {}}` for
Manual). Its output is persisted exactly like any other node's, so every downstream
input is uniformly derivable from persisted rows — there is no special-cased "virtual"
trigger output. The validator (§2.2) requires exactly one source node, and it must be
the trigger; running a webhook workflow manually injects a user-supplied sample
payload as if it were the webhook body.

## 4. Queue, leases, fencing, and parking

All of §4 is **specification**, not implementation; no queue code exists yet (that is
M2). All timestamps are the **database clock** (`now()`), never a worker's local
clock — worker clock skew is irrelevant to correctness. **READ COMMITTED isolation on
a single PostgreSQL primary** is assumed throughout (see Risk R7).

### 4.1 Run statuses and the `visible_at` column

`runs.status ∈ {queued, waiting, running, succeeded, failed, cancelled, timed_out}`.

| Status | Owned? | `visible_at` means | Claimable when |
| --- | --- | --- | --- |
| `queued` | no | earliest start (normally enqueue time) | `visible_at ≤ now()` |
| `waiting` | no (parked — §4.5) | next wake time | `visible_at ≤ now()` |
| `running` | yes (lease) | lease expiry | `visible_at ≤ now()` (an expired lease is reclaimed) |

One partial index serves all three: `ON runs (visible_at) WHERE status IN
('queued','waiting','running')`. Terminal runs fall out of it automatically.

`queued` **implies no `node_runs` rows exist** for that run (nothing has ever
started). `waiting` means the run has started before and is currently parked and
unowned.

Relevant columns on `runs` (non-exhaustive — see §2.3 and the data-model notes above):
`lease_owner` (nullable — NULL while queued, waiting, or terminal), `lease_epoch`,
`recovery_count` (increments only on an expired-lease takeover, never on an ordinary
claim or a wake from `waiting`), `cancel_requested_at`, `queue_deadline_at` (set at
enqueue), `deadline_at` (set once, at first claim — §4.6), `has_unknown_outcome`
(derived at finalization).

### 4.2 T0 — Enqueue (api, one transaction)

Insert with `status='queued'`, `visible_at=now()`, `queue_deadline_at = now() +
:queue_timeout`, the pinned `workflow_version_id`, and the trigger payload. A webhook
request returns `202` only after this commits. There is no second system, so there is
no dual-write problem. An optional `Idempotency-Key` header de-duplicates via a unique
`(workflow_id, idempotency_key)` constraint; a per-workflow cap on queued runs returns
`429`.

### 4.3 T1 — Claim (worker, short transaction; claim and reclaim are the same query)

```sql
BEGIN;
SELECT id, status AS prev_status FROM runs
 WHERE status IN ('queued','waiting','running') AND visible_at <= now()
 ORDER BY visible_at LIMIT 1
 FOR UPDATE SKIP LOCKED;                       -- no row ⇒ ROLLBACK, poll again

UPDATE runs SET
   status = 'running',
   lease_owner = :worker,
   lease_epoch = lease_epoch + 1,
   recovery_count = recovery_count + (status = 'running')::int,  -- only expired-lease takeovers
   visible_at  = now() + :ttl,
   started_at  = COALESCE(started_at, now()),
   deadline_at = COALESCE(deadline_at, now() + :run_timeout)      -- set once, at first claim
 WHERE id = :id
RETURNING lease_epoch, recovery_count, cancel_requested_at,
          deadline_at, queue_deadline_at;
COMMIT;
```

**Post-claim checks, evaluated in order**, each finalizing via the fenced T4 (§6.2)
when it applies. `db_now` below is the database-clock reading (`clock_timestamp()`)
returned by the first fenced guard after the claim (§4.4), never the worker's clock:

1. `prev_status = 'queued'` and `db_now > queue_deadline_at` → finalize `timed_out`,
   `error.code = 'queue_timeout'` (no node has ever run).
2. `deadline_at ≤ db_now` → resolve any orphans (§6.2), then finalize `timed_out`.
3. `recovery_count > max_recoveries` (default 3) → a **resolution-only pass**: apply
   the interruption matrix to orphans (never executing new work), then finalize
   `failed`, `error.code = 'max_recoveries_exceeded'`. This bounds the damage from a
   payload that reliably crashes the worker (e.g. an OOM).
4. `cancel_requested_at` is set → resolution-only pass, then finalize per §6.2.
5. Otherwise, run the scheduler loop (§4.7).

There is **no reaper process**; reclaiming an expired lease is literally the same
query as an ordinary claim.

### 4.4 The fenced guard (heartbeat, and the prelude of every state-changing write)

```sql
UPDATE runs SET visible_at = clock_timestamp() + :ttl
 WHERE id = :id AND status = 'running'
   AND lease_owner = :worker AND lease_epoch = :epoch
RETURNING cancel_requested_at, deadline_at,
          visible_at - :ttl AS db_now;                          -- 0 rows ⇒ lease lost
```

- Default TTL 30 s, heartbeat period `ttl/3` (10 s).
- **`clock_timestamp()`, not `now()`.** `now()` is the *transaction-start* time, so a
  guard that waited on the row lock would stamp a stale expiry; the renewal and
  `db_now` are one `clock_timestamp()` sample (`db_now` is `visible_at − :ttl`). The
  guard authorizes by `lease_owner`/`lease_epoch` only — `visible_at` is never in its
  predicate, because an expired `visible_at` does not revoke ownership until a new
  claimant increments the epoch. If the lock's previous holder released it without
  modifying the row, the sample may predate the wait by at most the wait: an earlier
  expiry, which is conservative and never affects fencing.
- **The heartbeat runs on a dedicated thread with its own connection**, not as an
  asyncio task sharing the executor's event loop — this is deliberate, so that a
  CPU-heavy or accidentally blocking node cannot starve lease renewal and trigger a
  spurious lease loss (Risk R3). It publishes `cancel_requested_at` to the execution
  loop via a thread-safe flag.
- T2 and T3 (§5) each begin with this guard.
- **0 rows returned ⇒ the lease is lost.** The worker aborts in-flight tasks and stops
  immediately; it writes nothing further, because it structurally cannot (every
  further write is fenced the same way). Any external side effects already in flight
  are now of **unknown** outcome — see §6.2.

### 4.5 Parking (releasing ownership while waiting on `retry_at`)

A worker **parks** a run when, with **no in-flight node tasks**, there is no node
ready now and at least one node is `retry_wait` with `retry_at > db_now`. (If other
nodes are still executing, the worker keeps ownership and handles the due retry
itself when the time comes.)

```sql
UPDATE runs SET
   status = 'waiting',
   lease_owner = NULL,
   visible_at = LEAST(
       (SELECT min(retry_at) FROM node_runs WHERE run_id = :id AND status = 'retry_wait'),
       deadline_at)
 WHERE id = :id AND status = 'running'
   AND lease_owner = :worker AND lease_epoch = :epoch
   AND cancel_requested_at IS NULL
RETURNING status;
```

- **The worker frees its concurrency slot the moment this commits** — a parked run
  holds no worker resources at all.
- `lease_epoch` is **unchanged** by parking; the *next* claim increments it, so a
  zombie from the old epoch can never write.
- Including `deadline_at` in the `LEAST(...)` means a parked run wakes up to be timed
  out rather than lingering silently past its deadline.
- 0 rows returned ⇒ either the lease was already lost (stop), or a cancel arrived
  concurrently (read the flag and take the cancel path, §6.2).
- Any worker — including the same one — may claim the run once `visible_at` passes,
  via the ordinary T1 path with `prev_status='waiting'` (which does **not** increment
  `recovery_count`). Readiness is recomputed, and the due `retry_wait` node proceeds
  through T2 as `attempt + 1`.
- Retry timing precision is bounded by the poll interval (~1 s plus jitter); this is
  documented, not a bug.
- **Deadlock assertion:** if nothing is in flight, ready, `retry_wait`, or terminal
  while unstarted nodes remain, that is an engine bug — the run is finalized `failed`
  with `error.code = 'engine_inconsistency'`, and the invariant is covered by tests.

**Graceful release (SIGTERM):** the worker stops claiming, lets in-flight **nodes**
finish within a grace period, then parks every remaining owned run with
`status='waiting', lease_owner=NULL, visible_at=now()` — through a dedicated
statement with the same lease-owner and epoch predicates as the park above but
without its `cancel_requested_at IS NULL` predicate (the run is claimable at once,
so a pending cancel cannot sleep, and the next claimant observes it under its own
lease) — so another worker picks it up immediately with no lease-expiry delay. An
attempt still running when the grace period expires is **not** resolved by the
exiting worker and no interruption cause is invented: its `node_runs` row stays
`running` with its original `started_epoch`, and nothing further is written for it.
The next claimant's higher `lease_epoch` makes that row an orphan, which it
resolves exactly as for a crash, through the §6.2 *Crash / lease lost* column.
Fencing stops the exiting process's stale database writes, but it cannot undo or
disprove an external side effect that process already performed, and its attempt
may briefly overlap the new owner's recovery; the idempotency key and the
`unknown`-outcome rules bound that risk. The Compose `stop_grace_period` must exceed
the longest expected node timeout (an operational requirement to document at M4).

### 4.6 Time limits

| Limit | Starts | Ends | Enforced by | On expiry |
| --- | --- | --- | --- | --- |
| **Queue timeout** (`queue_deadline_at`) | Enqueue | First claim | The claiming worker (post-claim check 1) | `timed_out`, `queue_timeout`; nothing ever executes |
| **Run timeout** (`deadline_at`) | First claim | Terminal | The live worker's timer; any later claimer (check 2); a parked run wakes at `deadline_at` | `timed_out` once live attempts have drained; any node left started-without-result is resolved per the §6.2 *Run deadline* column |
| **Node timeout** | T2 of that attempt | That attempt's end | The executor | Per the §6.2 matrix; retry backoff time is excluded |

**Run-deadline expiry.** Once the database confirms `deadline_at` has passed (a
worker timer only prompts the check; the fresh `db_now` of §4.4 decides): no new node
or retry starts; attempts already running drain and persist through normal T3 — no
task cancellation or interruption event is issued because the deadline expired; T4
finalizes `timed_out` only after the current live attempts have resolved, deciding the
deadline atomically (§6.2a); a node left started-without-result by an earlier lease
epoch (an orphan) is resolved by the §6.2 *Run deadline* column and is never
re-executed. The per-node timeout is independent and unchanged.

The run timeout is wall-clock from first claim and **deliberately includes retry
waits and parked downtime** — nothing resets it, and `COALESCE` in T1 guarantees it
is set exactly once. A `queued` run cannot "go back to queued" once started, since
`queued` implies no node rows (§3.1). A run whose queue timeout has silently expired
is only detected the next time a worker claims it; until then the UI shows it as
stalled, which is accurate.

Proposed defaults (configurable): queue timeout 1 h, run timeout 1 h, node timeout
5 min.

### 4.7 Execution loop (summary)

```text
claim → post-claim checks → [loop]
   refresh state (rows + definition) → resolve skips → compute ready set
   ready empty & in-flight empty:
        all terminal      → T4 (finalize)
        retry_wait present → PARK
        else               → engine_inconsistency (finalize failed)
   T2 → execute (≤ concurrency, with node timeout & the cancel event) → T3 / interruption resolution
   on heartbeat flag (cancel requested) → raise the cancel event
   on lease loss (0 rows from the guard) → abort silently, write nothing further
```

Everything the loop needs is recomputed from `(definition, node_runs rows, db_now)`;
in-memory state is a cache only, never the source of truth.

### 4.8 Capacity and wake-up

- The worker runs at most `WORKER_CONCURRENCY` runs at a time, claiming only when a
  slot is free. Scale by running more worker containers; the SQL above needs no
  further coordination.
- Idle workers poll with jitter (~1 s). `LISTEN/NOTIFY` is an optional latency
  optimization that may be added later; correctness never depends on it, since
  `NOTIFY` is not durable.
- At this system's expected scale (tens of runs/second at most), Postgres is not
  expected to be a bottleneck; if heartbeat/claim churn on `runs` is ever measured as
  a problem, leases can be extracted into a small dedicated `run_leases` table without
  changing the semantics above (Risk R10).

## 5. Node contract

```text
NodeType:
  type, version
  params_schema            # a JSON Schema (Draft 2020-12) document; drives the UI form
  credential_types
  inputs[]:  {name, required}      outputs[]: {name}
  execution: batch | per_item(concurrency, continue_on_item_error)
  effect(params) -> pure | read | idempotent_write | unsafe_write   # see §6.1
  async execute(ctx, params, inputs) -> { port: [items] }
```

Items are `{ json: {...} }`; binary data is explicitly deferred (not in v0.1). The
engine renders node forms from `GET /node-types`'s JSON Schema, so the frontend never
duplicates node knowledge — this is central to keeping the system extensible without
a plugin marketplace (ADR-0007; not in v0.1 scope, see `docs/mvp-scope.md`).

**`params_schema` is JSON Schema Draft 2020-12, explicitly** — never a bare
`validate()` call that would let a library guess the draft from a schema's own
`$schema` keyword (or silently default to an older draft when that keyword is
absent). The backend validates both the schema itself (well-formedness) and every
node's parameters against it at save time (§2.2); the backend remains the sole
authoritative validation boundary — no parallel, custom parameter-validation system
exists alongside it. A node type that declares no `params_schema` at all accepts any
parameters; a node-type author who wants to reject unknown parameters declares
`"additionalProperties": false` in their own schema, exactly as any JSON-Schema-based
system requires. See `docs/toolchain-versions.md` for the exact validator library and
version, and for the Python-3.14 compatibility-evidence discussion.

`ctx` provides: the decrypted credential for *that node's own declared credential
only*, the audited outbound HTTP client (§7), the redacting logger, a stable
per-item/per-node idempotency key (§6.4), and the cancellation event.

### 5.1 Node start and finish (T2 / T3)

**T2 (node start, committed *before* execution).** Run the fenced guard, then
**upsert** the `node_runs` row:

```sql
INSERT INTO node_runs (run_id, node_key, status, attempt, failures, outcome,
                       started_epoch, effect_class, started_at)
VALUES (:run, :node, 'running', 1, 0, 'known', :epoch, :resolved_class, now())
ON CONFLICT (run_id, node_key) DO UPDATE SET
       status        = 'running',
       attempt       = node_runs.attempt + 1,
       started_epoch = :epoch,
       started_at    = now(),
       retry_at      = NULL
       -- effect_class is deliberately absent from this SET list — see §5.2
RETURNING effect_class, attempt;
```

Append a `run_events` row and commit. This write-ahead record is exactly what lets
recovery distinguish "never started" from "started, outcome unknown".

**T3 (node finish, one transaction).** Run the fenced guard, then set the row to
`succeeded` (with `output`), `failed` (with `error`, and `output` on the `error` port
for `on_error = route`/`continue` — **only when the outcome is `known`**: a node with an
`unknown` outcome is written with no `output` at all, so no route can run past it, see
§6.2), `cancelled`, or `retry_wait` (`retry_at = now() +
backoff`, computed **by the database**, `failures = failures + 1`). In the same
transaction, write `skipped` rows for any downstream node now unreachable (§3.2), and
append events. Commit.

`attempt` counts every execution start, including crash re-executions; `failures`
counts only attempts that ended in an ordinary failure, so a retry budget is not
consumed by crash recovery, and `recovery_count` on `runs` is not consumed by ordinary
retries.

### 5.2 `effect_class` is immutable per node execution

**Rule:** the class is resolved and persisted on the **first** T2 for
`(run_id, node_key)`, from that point immutable for the whole execution of that node —
across retries, `retry_wait` resumes, crash recovery, reclaim, and parking. Nothing
ever recomputes it.

- The worker takes `effect_class` **from the `RETURNING` clause**, never from a
  freshly computed classification after the first attempt. If the two differ (e.g. an
  application upgrade changed a node's classifier mid-run), the worker appends an
  `effect_class_drift` diagnostic event and **behavior is unchanged** — the persisted
  class still governs.
- Every later decision (retry eligibility, the interruption matrix in §6.2) reads the
  class from the row, never from the node registry.
- **Database-level enforcement**, independent of application-code discipline:
  - a `BEFORE UPDATE` trigger on `node_runs` raises if
    `NEW.effect_class IS DISTINCT FROM OLD.effect_class`;
  - a `CHECK` ensures `status = 'skipped' OR effect_class IS NOT NULL`;
  - skipped rows carry a NULL class and never transition to `running`.
- A **Replay** run (§4 of the lifecycle discussion, and `docs/mvp-scope.md`) is a new
  execution with a new `run_id`; its first T2 resolves the class under whatever code
  is current at that time. That is intentional — Replay already carries its own
  confirmation dialog warning about re-executing writes (Risk R1).
- **Upgrade guidance:** a node whose behavior changes should bump `type_version`.
  Already-persisted rows keep their persisted class regardless; the *code* that
  executes a retry is whatever is deployed at that moment, which is why graceful
  shutdown (§4.5) — draining before upgrading, rather than upgrading mid-run — is the
  recommended operational practice.

## 6. Effect classes, idempotency, and unknown outcomes

### 6.1 Effect classes

| Class | Meaning | Examples |
| --- | --- | --- |
| `pure` | No external effects; deterministic | Trigger, Set, IF, Switch, Merge, Split Out |
| `read` | External reads; no intended mutation of external state | HTTP GET/HEAD/OPTIONS by default; LLM Prompt (see §6.3 for the billing caveat specific to LLM) |
| `idempotent_write` | A write the **user has explicitly asserted** is safe to repeat | An HTTP request with `retry_safety = safe_to_retry` |
| `unsafe_write` | A write not asserted safe (the conservative default) | HTTP PUT, DELETE, POST, PATCH by default; any HTTP request with `retry_safety = never_retry` |

**HTTP classification is never derived from the method alone.** PUT and DELETE are
*not* assumed safe to retry just because the RFC describes them as idempotent — a
real server may implement them with side effects FlowForge cannot see. The HTTP
node's `retry_safety` parameter is the only thing that can move a write out of
`unsafe_write`:

| `retry_safety` | GET/HEAD/OPTIONS | PUT/DELETE/POST/PATCH/other |
| --- | --- | --- |
| `auto` (default) | `read` | `unsafe_write` |
| `safe_to_retry` (user asserts repeating is harmless, e.g. the API dedupes by key) | `read` | `idempotent_write` |
| `never_retry` | `unsafe_write` | `unsafe_write` |

An optional `idempotency_key_header` sends the node's stable key (§6.4) as a request
header. Setting it without `safe_to_retry` raises a non-blocking validator warning —
sending a header is not, by itself, a guarantee the remote side honors it.

### 6.2 Unknown outcomes — the resolution matrix

An **interruption** is a node attempt that was started (T2 committed) without a
recorded result, caused by: **crash or lease loss**, **cancellation**, **node
timeout**, or **run deadline**. The row's `outcome` field is `known` (no external
mutation is in doubt) or `unknown` (an external write may have been applied).
**`outcome` refers only to mutation of external state** — see §6.3 for why LLM
billing is tracked separately and is *not* represented by `outcome`.

A run deadline does not itself interrupt a *live* attempt: once it is
database-confirmed the worker starts no new nodes or retries, lets live attempts
finish through normal T3 (no cancellation call or interruption event is issued for
the deadline), and finalizes `timed_out` after they resolve. The **Run deadline**
column below applies to an attempt left started-without-result that no live worker
is draining — an orphan — and to nothing else. The per-node timeout is independent.

| Class | Crash / lease lost (orphan at claim) | Cancel | Node timeout | Run deadline |
| --- | --- | --- | --- | --- |
| `pure` | Re-execute | `cancelled`, known | `failed` (timeout), known; retryable per policy | `cancelled`, known |
| `read` | Re-execute | `cancelled`, known | `failed` (timeout), known; retryable | `cancelled`, known |
| `idempotent_write` | Re-execute with the same key | `cancelled`, **unknown** | `retry_wait` (same key) while the retry budget remains; once exhausted, `failed`, **unknown**, and the run fails regardless of `on_error` — see the dedicated rule below | `cancelled`, **unknown** |
| `unsafe_write`, not dispatched (live worker only; a crash can never prove this) | n/a | `cancelled`, known | `failed` (timeout), known; retry allowed | `cancelled`, known |
| `unsafe_write`, dispatched or unprovable | `failed`, `outcome_unknown`, **unknown** | `failed`, `outcome_unknown`, **unknown** | `failed`, `outcome_unknown`, **unknown** | `failed`, `outcome_unknown`, **unknown** |

Rules:

- **`unsafe_write` with `outcome_unknown` is never retried automatically and never
  labeled `cancelled`.** `error.cause ∈ {crash, cancel, timeout, deadline,
  lease_lost, post_dispatch_ambiguous}` records why.
- If a reclaimer finds orphans **and** `cancel_requested_at` is set: `pure`/`read`
  resolve `cancelled` with no re-execute; `idempotent_write` resolves `cancelled`
  with unknown; `unsafe_write` follows the crash column (unknown).
- **`idempotent_write` Node-timeout, resolved (this is the normative rule; it
  replaces an earlier ambiguity between this paragraph and the general
  "outcome_unknown from timeout fails the run" rule below, which is scoped to
  `unsafe_write` and to `idempotent_write` only once its own retries are
  exhausted — see ADR-0005's revision history for that clarification):
  1. The idempotency key is **never regenerated** across retries of the same
     logical operation (§6.4) — the retry is the same operation, retried.
  2. An unknown outcome is never reported as `succeeded` merely because the write
     is idempotent.
  3. While the node's retry budget is not exhausted, an unknown-outcome timeout
     writes `retry_wait` (non-terminal), **not** `failed` — downstream nodes
     cannot execute, because readiness (§3.2) only proceeds once every upstream
     node is terminal, and `retry_wait` is not terminal.
  4. If a later attempt returns a **confirmed success**, the node completes
     normally (`succeeded`, known) through the ordinary T3 path, and downstream
     execution proceeds.
  5. If a later attempt returns a **definitive failure** (no longer ambiguous),
     it is handled by the ordinary retry/`on_error` rules for an ordinary
     failure — this paragraph no longer applies once the outcome is no longer
     unknown.
  6. Once the retry budget is exhausted **with the outcome still unknown**, the
     node becomes `failed`/`unknown` and the run fails **unconditionally**,
     regardless of the node's `on_error` policy.
- An `outcome_unknown` from **crash or timeout fails the run** — it drains in-flight
  nodes (U5: drain, don't cancel mid-flight, to avoid manufacturing further
  ambiguity), schedules nothing new, and ends `failed`, **ignoring `on_error`**. A
  silently-continued unsafe write is exactly the hazard this closes. For
  `idempotent_write` specifically, this rule applies only once its own retry
  budget is exhausted (see the dedicated rule immediately above) — not on the
  first unknown-outcome timeout, which retries instead. Ordinary crash recovery
  restarts an orphaned `idempotent_write` attempt with the same key and never writes
  a `failed`/`unknown` row for it; this excludes a resolution-only pass (the run is
  being cancelled, has hit its deadline, or is already failing by this rule), where
  the orphan is not restarted but resolved `cancelled`/`unknown`.
  The rule also covers an **ordinary** post-dispatch-ambiguous failure of an
  `unsafe_write` (below), and it is **reconstructible from the persisted rows
  alone**, so a worker that takes over after a crash reaches the same decision: a
  node forces the run to fail iff its row is `failed` with `outcome = unknown` and
  `error.cause` is neither `cancel` nor `deadline` (those runs end `cancelled` /
  `timed_out`); a missing or unrecognized cause also forces (fail closed). A node
  with an unknown outcome carries no `output`, so `on_error = route`/`continue`
  can never schedule a node from it.
- An `outcome_unknown` from **cancel or deadline** ends the run `cancelled` /
  `timed_out`, with `has_unknown_outcome = true` and the affected nodes listed; the UI
  shows a prominent "outcome unknown" badge.
- **Ordinary-failure retry eligibility:** `pure`/`read` retry per policy;
  `idempotent_write` retries per policy with the same key; `unsafe_write` retries
  **only** for failures provably pre-dispatch (DNS failure, connection refused, TLS
  failure, an SSRF block, or validation) — a post-dispatch failure (read timeout,
  reset, any received HTTP response including 5xx) is **never** auto-retried for
  `unsafe_write`. A post-dispatch failure the node classifies as ambiguous (a response
  may have reached the other side) is recorded `failed`, `outcome = unknown`,
  `error.cause = post_dispatch_ambiguous`, and fails the run ignoring `on_error` like
  any other unknown `unsafe_write`; which concrete HTTP conditions count as ambiguous
  is the HTTP node's classification (M3), not decided here.
- **Per-item nodes:** without per-item checkpointing (U4, explicitly out of scope —
  see `docs/mvp-scope.md`), an interrupted per-item `unsafe_write` node is unknown as
  a whole. For cancel/timeout the error detail best-effort lists
  `items_dispatched`/`items_not_dispatched` from in-memory state; a crash erases that
  information, and the detail says so.
- **Replay re-executes everything**, including previously succeeded writes — the
  Replay action shows a confirmation listing write-class nodes before proceeding
  (Risk R1).

### 6.2a Cancellation and finalization: exact statements and race semantics

Cancellation is a single idempotent statement issued by the API, and finalization
(T4) is a single fenced statement issued by the worker. Together they guarantee that
a cancel which commits first can never be overtaken by a later `succeeded` finalize,
and that a finalize which commits first is never retroactively reopened.

**API cancel (one statement, idempotent):**

```sql
UPDATE runs SET
   cancel_requested_at = COALESCE(cancel_requested_at, now()),
   status      = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
   finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
   visible_at  = CASE WHEN status = 'waiting' THEN now() ELSE visible_at END
 WHERE id = :id AND status IN ('queued', 'waiting', 'running')
RETURNING status;                          -- 0 rows ⇒ already terminal ⇒ HTTP 409
```

- `queued` has no `node_runs` rows at all (§3.1), so the API may finalize it
  directly without violating the single-writer rule on node state.
- `waiting` is made immediately claimable; the worker that claims it observes the
  flag, resolves any `retry_wait` rows as `cancelled` — keeping each row's recorded
  outcome (an unknown outcome stays unknown), last failure, and attempt count, since
  the retry was suppressed, never executed — and finalizes.
- `running`: only the flag is set. The owning worker observes it through its next
  heartbeat, raises the cancellation event, lets in-flight nodes drain (U5), and
  resolves them per the interruption matrix above.

**Worker finalize, T4 (one statement, fenced, atomic against cancel):**

```sql
WITH s AS MATERIALIZED (SELECT clock_timestamp() AS t)   -- the one database-clock sample
UPDATE runs r SET
   status = CASE
      WHEN r.deadline_at <= s.t                 THEN 'timed_out'  -- a confirmed deadline outranks a natural failure
      WHEN :natural IN ('failed', 'timed_out')  THEN :natural     -- real failures are never masked
      WHEN r.cancel_requested_at IS NOT NULL    THEN 'cancelled'  -- a cancel that committed first wins
      ELSE :natural END,                                          -- 'succeeded' | 'cancelled'
   finished_at = s.t, error = :err, lease_owner = NULL,
   has_unknown_outcome = EXISTS (
       SELECT 1 FROM node_runs WHERE run_id = :id AND outcome = 'unknown')
  FROM s
 WHERE r.id = :id AND r.status = 'running'
   AND r.lease_owner = :worker AND r.lease_epoch = :epoch
   AND NOT EXISTS (SELECT 1 FROM node_runs
                    WHERE run_id = :id AND status IN ('running', 'retry_wait'))
RETURNING r.status;
```

- **One clock sample, after the lock.** `s.t` is the only clock reading: it decides
  the deadline term and sets `finished_at`, so the two cannot straddle the deadline
  (`clock_timestamp()` advances within a statement). T4 executes only inside the
  transaction whose fenced guard (§4.4, the same transaction's `LeaseProof` in the M2
  design) has already locked the `runs` row, and row locks are held to transaction end,
  so the sample is taken with the lock held — by statement order. PostgreSQL does not
  guarantee evaluation order between a CTE and a row lock, and this design does not
  rely on one.
- **No unresolved node.** T4 refuses while any node row is `running` or `retry_wait`.
  The finalizing transaction therefore first resolves pending `retry_wait` rows —
  including a failed run's siblings — as `cancelled`, keeping outcome, last failure,
  and attempt count (an unknown outcome never becomes known by this), and, when the
  deadline is confirmed, resolves earlier-epoch orphans by the §6.2 *Run deadline*
  column; a node with a live attempt of the current epoch is never touched and must
  drain first.
- The status is computed from the **row as locked at statement time** — there is no
  read-then-write window in which a concurrent cancel could be missed.
- **Terminal-status precedence:** `failed`/`timed_out` (natural) > `cancelled` >
  `succeeded`. `:natural` is computed by the worker *before* applying any
  cancel-caused interruption, so a node interrupted by cancel never counts as a
  natural failure. A run deadline the database has confirmed by the time T4
  executes outranks a natural `failed` and yields `timed_out`; node rows keep their
  failures and outcomes.
- Completed node rows are **never rewritten by finalization**. If a cancel wins after every
  node had already completed, the nodes still show `succeeded` with their real
  outputs, and the run shows `cancelled` with an event explaining that a cancel was
  requested before finalization and that N nodes had already completed — the side
  effects of those nodes genuinely happened, and the UI says so rather than hiding
  it.

**Race matrix** (each row is a required concurrency test against a real PostgreSQL
instance, per gate RB3 — never a mock):

| Race | Outcome |
| --- | --- |
| Cancel commits **before** T4(`succeeded`) | T4's `CASE` observes the flag → run ends `cancelled` |
| T4 commits **before** cancel | The cancel's `WHERE status IN (...)` predicate fails (already terminal) → `409`, the already-final status is returned |
| Cancel vs. park | Park's `UPDATE` predicate requires `cancel_requested_at IS NULL`. If cancel wins, park returns 0 rows and the worker takes the cancel path instead. If park wins, the run becomes `waiting` and the cancel statement sets `visible_at = now()`, so it is claimed and finalized promptly |
| Cancel of a `queued` run vs. a concurrent claim | `SKIP LOCKED` plus the re-checked predicate after the row lock: either the claim wins (cancel then only sets the flag on a now-`running` row) or the cancel wins (the claim never sees a `queued` row to lock) |
| A zombie's T4 vs. a reclaimer | `lease_epoch` mismatch on the zombie's write ⇒ 0 rows ⇒ the zombie's finalize is refused |
| Reclaim vs. a delayed heartbeat | The row lock taken by the reclaiming claim and the row lock taken by the heartbeat's `UPDATE` serialize against each other; the epoch guard decides the outcome |
| T4 vs. a deadline that has expired | T4 takes one clock sample inside its own statement: deadline confirmed ⇒ `timed_out` (node rows untouched); otherwise the normal result |

### 6.3 LLM requests: billable but not exactly-once

`LLM Prompt` stays classified `read` — meaning *"external read, no intended mutation
of external state we know of"*. This classification is about `outcome` (state
mutation), and **it is not a billing guarantee**. A provider may process and charge
for a request even when the response never arrives, and **FlowForge cannot know
this**, so:

- **FlowForge never guarantees exactly-once execution or exactly-once billing for LLM
  calls.** No UI string or documentation sentence may imply otherwise.
- Billing certainty is **provider-specific**, decided by the adapter, never inferred
  from an HTTP status code alone (see §9.3 for the adapter contract).
- A **successful (2xx) response with reported token usage does not, by itself,
  establish that a financial charge occurred.** FlowForge surfaces only
  provider-reported usage, never a computed monetary amount, and never claims a
  charge from usage alone (see `docs/message-catalog.md`).
- **Retry policy for interrupted LLM requests:**

  | Situation | Default |
  | --- | --- |
  | Failure established as `pre_dispatch` | Retryable |
  | Retryable per a documented provider rule, and not billing-uncertain | Retried by default |
  | Retryable but billing-uncertain (including an undocumented 429) | **Not retried by default**; opt-in via `retry_on`, labeled "may cause duplicate charges" |
  | Permanent failure | Not retried; billing certainty per the adapter's documented rule table |
  | Post-dispatch ambiguity (timeout, reset, truncated body) | Not retried by default; billing uncertain |
  | Crash / lease-loss orphan | **Re-executed automatically**, per the `read` row of §6.2 — the one automatic duplicate-billing path, bounded by `max_recoveries`, per-attempt `max_tokens`, and a visible attempt count in the UI |

- No v0.1 adapter sends a provider-side idempotency key unless that provider's
  documentation is verified, at the time the adapter is built (M6), to guarantee
  deduplication.
- There is **no cumulative per-run or per-day spend cap** in v0.1 (an accepted
  post-MVP risk — R11).
- A required test scenario ("processed-then-dropped": a fake provider processes a
  request and then drops the connection before responding) and the exact UI wording
  are specified in `docs/message-catalog.md` and are release-blocking (gate RB8, see
  `docs/risks-and-release-gates.md`).

### 6.4 Idempotency keys

```text
key(run, node)             = UUIDv5(FLOWFORGE_NS, f"{run_id}/{node_key}")
key(run, node, item_index) = UUIDv5(FLOWFORGE_NS, f"{run_id}/{node_key}/{item_index}")
```

`FLOWFORGE_NS` is a fixed project constant. UUIDv5 satisfies APIs expecting
UUID-shaped keys and reveals nothing structured about the run.

**Why `item_index` is stable** across retries, crash recovery, and parked resumes: a
node's input items are the concatenation, in the *definition's fixed edge order*
(versions are immutable), of upstream outputs persisted once in T3 and never
rewritten. The same items in the same order therefore reappear on every attempt, so
index `i` always names the same logical item. Distinct items never collide (the index
is part of the key); distinct nodes never collide (`node_key` is part of the key). A
**Replay** run gets a fresh `run_id`, so its keys are intentionally fresh — it is a
new execution, not a resumption.

Keys **deliberately exclude the attempt number**: a retried operation must present
the *same* key so the remote side can deduplicate it.

**Determinism requirement:** the request body built for an item must be the same
function of persisted data on every attempt, or the same key would carry different
payloads to the remote side. Expressions therefore have **no wall-clock or random
functions** (§8) — `now` resolves to the run's persisted `created_at`, and there is
no `uuid()`/`random()` in the whitelist. LLM outputs are persisted before downstream
nodes consume them, so they are covered by the same guarantee.

A key only protects as far as the remote API honors it; FlowForge sends it (via a
configurable header) and never claims a server-side guarantee it cannot verify
(Risk R6).

## 7. Outbound HTTP: SSRF policy, dispatch marker, and fail-closed modes

**This is the most security-critical section of the architecture** and the one with
the most unresolved engineering risk (Risks R2, R5; gates RB1a/RB1b/RB1c, RB2). Every
rule below applies uniformly to the HTTP Request node **and** every AI provider
adapter (§9) — there is exactly one audited outbound path in the system.

### 7.1 One client path

A single `OutboundHttpClientFactory` is the only way any workflow-originated request
leaves the process. A ruff banned-API lint rule forbids importing `httpx`,
`requests`, `urllib3`, `aiohttp`, `http.client`, or opening a raw socket outside
`security/outbound/`; CI fails on violation. **Environment HTTP proxies are never
honored** (`trust_env=False`, no proxy mounts, no `.netrc`): proxy resolution happens
outside the pinning/validation boundary, so a proxy could itself sit on a private
address and would defeat IP pinning entirely. This matters concretely because Docker
Desktop / the Docker CLI can inject proxy environment variables into containers
automatically. Operators who need a forward proxy use network-level egress control
(a container or host firewall); a first-class proxy mode is out of scope for v0.1.

### 7.2 Gate sets

| Set | Gates | Protects |
| --- | --- | --- |
| **CS — core connection security** | AP1 (resolve once, connect to the validated address), AP2 (TLS SNI + certificate hostname verification against the *original* hostname), AP3 (DNS-rebinding immunity), AP5 (environment proxies inert), the startup self-test, the no-unpinned-code-path rule, and the per-connection invariant (§7.5) | Every outbound connection is pinned and TLS-verified, **regardless of destination** |
| **AP6 — address validation** | The absolute-deny set (§7.6) and the private/overridable tier, applied to every resolved address and every redirect hop | Destinations can be screened safely at all |
| **AP7 — allowlist semantics** | Exact `{host, port, scheme}` entries, no wildcards, entry-scoped `allow_private` | Operator-approved destinations are unambiguous |
| **AP4 — redirect re-validation** | Every redirect hop re-runs the full validation procedure | Needed only for `full` mode (redirects are not followed at all outside `full`) |

### 7.3 Mode eligibility (normative)

| CS | AP6 | AP7 | AP4 | Effective mode |
| --- | --- | --- | --- | --- |
| fail | any | any | any | **disabled** |
| pass | **fail** | any | any | **disabled** |
| pass | pass | **fail** | any | **disabled** |
| pass | pass | pass | **fail** | **restricted** |
| pass | pass | pass | pass | **full** |

**If CS fails, every outbound-capable node is unavailable — the HTTP Request node and
every LLM adapter, with no exception.** A hard-coded vendor hostname for an LLM
provider is **never** a substitute for pinning and TLS verification; it is only a
convenience catalog entry subject to exactly the same validation as any other
destination. **If AP6 or AP7 fails, restricted mode is not available either** —
restricted mode's safety depends on address validation and allowlist matching being
correct, so without them an "operator-approved" destination could resolve to a
metadata or internal address, or match the wrong host. In that case the mode is
`disabled`, not restricted. There is **no** restricted-mode design in this document
that bypasses AP6/AP7; one would require its own ADR, specification, and independent
verification before it could replace `disabled` in that case.

`AP4` alone failing is the **only** case where `restricted` is reached as a graceful
degradation from `full` — because restricted mode never follows redirects anyway, a
redirect-validation shortfall does not compromise it.

**Effective mode = min(build ceiling, operator setting, startup probes).**

- *Build ceiling:* computed at release time from CI gate results (§7.3 table) and
  recorded in the release manifest.
- *Operator setting:* can only lower the effective mode, never raise it above the
  ceiling.
- *Startup probes* (every outbound-capable process, on every start):
  1. the **CS self-test** against a loopback fixture with its own throwaway test CA
     (never added to the production trust store): resolves once and connects to the
     validated address; SNI/hostname verification succeeds for a matching name and
     fails for a mismatch; a rebinding-resolver stub never causes a connection to its
     second answer; the client has no proxy configuration;
  2. a **policy probe table**: pure-function tests of the address validator and
     allowlist matcher (the absolute-deny set, private ranges, encoded/embedded IP
     forms, exact-match and wildcard-rejection) — failure sets `disabled`;
  3. a **loopback redirect probe** — failure only lowers `full` to `restricted`;
  4. the **dispatch-marker hook check** (§7.7) — failure degrades the marker to
     Tier C and logs it; it does **not** disable outbound, because Tier C is safe,
     merely less precise.

### 7.4 Restricted mode, precisely

Valid **only** when CS, AP6, and AP7 have all independently passed:

- Destinations are exact `{host, port, scheme}` allowlist entries — no wildcards, no
  subdomain matching. Nothing is approved by default.
- **No redirects are followed in restricted mode, for any effect class.**
- The built-in vendor hostname catalog (for LLM providers) is only a convenience: an
  operator must explicitly enable a provider
  (`FLOWFORGE_OUTBOUND_ALLOW_VENDORS=...`). A vendor hostname receives **exactly the
  same** pinned, TLS-verified, address-validated treatment as any other entry —
  nothing about being "a known vendor" relaxes a single check.
- There is **no TLS-verification override** anywhere in the system — no "ignore
  certificate errors" option exists on the HTTP node in v0.1.

### 7.5 Startup behavior and the per-connection invariant

1. Read the build ceiling and the operator setting.
2. Run the startup probes (§7.3).
3. Compute the effective mode; log it; show a persistent UI banner; expose it on a
   status endpoint.
4. In `disabled` mode, the node registry marks HTTP Request and every LLM adapter
   node `unavailable` with the reason. Save-time validation warns, and activation is
   blocked for a workflow using them. A run that nonetheless reaches such a node (an
   already-active workflow, mode lowered after activation) fails it with
   `error.code = 'outbound_disabled'`, classed **pre-dispatch / known** — nothing was
   ever sent, so there is no ambiguity.

**Per-connection invariant — no silent unpinned request, ever, even after a passing
startup.** Every outbound connection must originate from the pinning backend (its
stream carries a `pinned` marker attribute that only that backend sets, and TLS
verification must be active for `https`). The request path asserts both **before**
the first write. A violation raises `OutboundSecurityError`, sends nothing, logs
`outbound_security_violation`, and **latches the process into `disabled` until
restart** — this is the defense against, e.g., a dependency upgrade silently changing
transport internals between releases.

The weaker "validate the peer IP after connecting" check may exist only as
**additional defense-in-depth alongside** pinning — it is never an acceptable
substitute, and it is never, on its own, grounds to claim CS has passed.

### 7.6 Address validation: absolute deny vs. private/overridable

**Two tiers**, applied in this order to **every resolved address** (not only the one
connected to) and to **every redirect hop in `full` mode**:

1. **Absolute deny — never overridable**, by `allow_private`, by any allowlist entry,
   by vendor-catalog enablement, by mode, or by any operator configuration:
   - Link-local: `169.254.0.0/16` (covers `169.254.169.254` and `169.254.170.2`) and
     `fe80::/10`.
   - Named cloud/platform metadata and infrastructure endpoints (verified against
     current provider documentation at M3, with the verification date recorded):
     `fd00:ec2::254/128`, `100.100.100.200/32`, `168.63.129.16/32`,
     `192.0.0.192/32`.
   - Non-routable/special: `0.0.0.0/8`, `::/128`, multicast (`224.0.0.0/4`,
     `ff00::/8`), broadcast `255.255.255.255/32`, reserved `240.0.0.0/4`.
   - Operators may **add** entries via `FLOWFORGE_OUTBOUND_DENY`; **nothing can
     remove a built-in entry.**
2. **Private, overridable per exact allowlist entry with `allow_private: true`**:
   loopback, RFC1918, the CGNAT range (minus anything already in tier 1), ULA
   `fc00::/7` (minus anything already in tier 1), host-gateway addresses. Without
   `allow_private`, these are denied too.

**Validation procedure, every request, every mode, every caller, including every
redirect hop:**

1. Resolve once; normalize every address, **including IPv4 forms embedded in IPv6**
   (both the IPv6 form and the embedded IPv4 are checked).
2. If **any** resolved address is in the absolute-deny set, **reject the entire
   request** — regardless of allowlist entry or `allow_private`.
3. Otherwise, if any resolved address is in a private/overridable range, allow only
   if the destination matches an exact allowlist entry with `allow_private: true`;
   otherwise reject.
4. Connect only to a validated address (the CS pinning rule).
5. **Redirects (`full` mode only):** each hop re-runs this entire procedure,
   including step 2, against its own host. Restricted mode follows no redirects at
   all (§7.4).

Allowlist entries covering, or whose hostnames resolve to, an absolute-deny address
are **rejected at startup** (configuration error, logged, the entry ignored) and
**checked again at request time** as a second line of defense. Hostnames are never
trusted on their own (no hostname denylist is relied on) — the IP-level check is
authoritative, since a metadata hostname is only a name for a denied address.
`allow_private` is **scoped to the exact entry it is set on**, never to a neighboring
address, subdomain, or port.

### 7.7 Dispatch marker: HTTPS layer and tiers

**Dispatched** means: application-layer HTTP request bytes may have left the
process. The preferred (Tier A) boundary is **the first HTTP application-data write
on the TLS-wrapped stream, after the TLS handshake has completed** — not a write to
the underlying TCP socket, which also carries handshake records (e.g. the
ClientHello) that a TCP-level hook cannot distinguish from request data. For a
plaintext `http://` destination (permitted only for an approved/allowlisted entry)
the stream has no TLS layer, so the first stream write *is* application data.

The marker is set **by the transport layer**, not by node authors — this removes the
author-discipline risk the earlier draft carried (superseded; see Risk R5's current
wording below).

| Tier | Marker set when | Pre-dispatch (`known`) is claimable only for failures before… |
| --- | --- | --- |
| **A** | the first write issued by the HTTP layer on the TLS-wrapped stream (https) or the stream (http); set before the write, with no `await` between | that first application-data write (including handshake failures) |
| **B** | TLS handshake completion (https) / TCP connect completion (http), no write-level distinction | handshake/connect completion |
| **C** | our own `connect_tcp` returning (TCP established) | a TCP connection existing at all (DNS, validation, connect failures only) |

**The zero-false-negative requirement applies to every tier.** A lower tier is always
safe, only less precise; it never weakens the `unknown`-outcome policy of §6.2.
Because Tier C's hook lives in the same backend that performs IP pinning (the CS
gate), if that backend cannot be installed at all, CS itself fails and outbound is
`disabled` — there is no tier below C.

**Spike S1** (the first task of M3, release-blocking per gate RB2) must, against the
pinned httpcore/httpx versions, determine and document in a "layer report":

1. whether the handshake completes *inside* `start_tls` before it returns;
2. whether the HTTP layer's writes go through the TLS-wrapped stream's `write()`, and
   whether handshake writes ever appear there too;
3. whether any other write path exists (e.g. a direct writer on the underlying
   socket).

The spike's harness uses a **TLS-terminating fault-injection test server** that
records both raw TCP bytes and **decrypted application bytes received**, across at
least: connection refused; TCP accepted then closed before read; TLS failure (bad
cert, hostname mismatch); TLS completed then reset before any application byte; full
request received then no response; partial headers then reset; slow response
(client-side timeout); write-blocked by a non-reading server; task cancellation at
each phase; DNS failure/SSRF block; two sequential requests never reusing a
connection. **Soundness** (zero false negatives, over a large randomized-timing run)
is mandatory for every tier. **Tier A precision** additionally requires the marker to
be deterministically `False` in handshake-only scenarios where the server decrypted
zero application bytes — if the marker is `True` there, the hook is at the TCP layer
and Tier A cannot be claimed; the result is then Tier B or C. **If the implementation
cannot distinguish application-data writes from TLS handshake writes, Tier B or C is
used — the zero-false-negative bar is never relaxed to claim a higher tier.**

Additional transport constraints the spike must verify hold on the pinned versions:
no connection reuse/keep-alive pooling, no HTTP/2 (its connection preface precedes
any request), only fully buffered request bodies (no streaming uploads), and TCP Fast
Open / TLS 0-RTT never enabled — all of these can otherwise blur the dispatch
boundary. **Redirects are followed only for class `read`**; a write-class request
that receives a 3xx returns that response to the workflow rather than silently
re-sending the write to another host (this is also what makes the AP4 gate
non-essential to restricted mode, §7.3).

## 8. Expressions

`"Hello {{ trigger.body.name }}"` templates containing **JMESPath** queries over a
fixed context: `trigger`, `input` (items), `item` (per-item mode),
`nodes.<key>.output.<port>`, and `run` (id and timestamps). A string that is exactly
one `{{ }}` preserves the result's native type.

- Implementation: a pure-Python JMESPath library plus a small **whitelisted**
  function set (string/number/date helpers). **No `eval`/`exec`, no attribute access
  beyond the fixed context, no I/O, no imports.**
- **Credentials are never in the expression context**, so an expression cannot read
  or print a secret.
- **No wall-clock or random functions** — required for idempotency-key determinism
  (§6.4); `now` resolves to the run's persisted `created_at`.
- Limits on expression length, evaluation depth, and output size bound CPU/memory use
  (JMESPath over a huge input can be slow).
- Expressions are resolved by the engine **before** `execute` is called, so nodes
  receive concrete, already-resolved parameters and never touch the expression system
  themselves.
- Known expressiveness ceiling (deliberate, documented): JMESPath has no arithmetic
  or string concatenation beyond what the whitelisted function set provides.

## 9. AI provider abstraction and BYOK

### 9.1 Port and adapters

A narrow internal port: `complete(request) -> {text, usage, finish_reason, raw}`. No
streaming and no tool calls in v0.1. Adapters: an OpenAI-compatible adapter (which
also covers Ollama and many gateways via a configurable `base_url`), Anthropic, and
Google Gemini — built as thin wrappers over `httpx`/the outbound client (§7), **no
LangChain or LiteLLM dependency**.

Keys live only in the credential store (§10), typed per provider; the LLM node holds
a credential *reference*, never an embedded key. The only network egress is to the
provider the operator configured, through the same outbound policy as every other
request (§7) — a local Ollama endpoint therefore requires the operator allowlist
under `restricted` mode, or falls under `full` mode's normal private-range rules.

### 9.2 Cost control (what exists, and what explicitly does not)

A default `max_tokens`, and `usage` (provider-reported) recorded on `node_runs` as a
lower bound. **There is no per-run or per-day spend cap in v0.1** (Risk R11, accepted
post-MVP). **FlowForge never computes a monetary amount anywhere in the system** — no
price tables, no currency fields, no estimated cost display (see
`docs/message-catalog.md`).

### 9.3 Adapter billing-classification contract

```text
classify(outcome) -> Disposition {
   retryable: bool,
   usage_reported: { input_tokens?, output_tokens?, total_tokens? } | null,
   billing_uncertain: bool,
   billing_basis: "pre_dispatch" | "provider_documented:<rule_id>" | "none",
}
```

- `usage_reported` holds **only** what the provider's response actually returned,
  parsed by the adapter; `null` when absent or unparsable. It is never treated as
  proof of a charge.
- `billing_uncertain = false` is permitted **only** alongside a non-empty
  `billing_basis`: either `pre_dispatch` (the dispatch marker, §7.7, shows no
  application bytes left the process — a transport fact, sound only to the degree
  the chosen tier is sound, and never a claim about provider semantics), or
  `provider_documented:<rule_id>` (a rule in that adapter's response table, keyed by
  `(status, provider error code/type)`, carrying a **dated documentation reference**
  stating that such a response means no billable processing occurred).
- **Everything else defaults to `billing_uncertain = true`**: any unlisted status,
  any unlisted provider error code, any 4xx/5xx without a documented rule, truncated
  or unparsable 2xx bodies, timeouts, and resets after dispatch.
- **4xx responses are generally treated as permanent request failures** (not
  retried), but billing certainty is **never inferred from the status code alone** —
  it comes only from that adapter's documented, cited rule table.
- **HTTP 429 may be retried** only if the adapter's documented rate-limit semantics
  for that specific provider say so; retryability and billing certainty are tracked
  as independent attributes.
- Each adapter's rule table is populated at M6 from that provider's then-current
  documentation; every "not charged" rule carries a dated citation and is re-reviewed
  whenever its adapter changes (Risk R13).
- **No provider semantics are assumed by this architecture** — every claim of "not
  charged" traces to either a transport fact or a specific, cited, dated provider
  document.

The UI wording this contract drives, including the exact three-state message catalog
(`llm.usage_reported`, `llm.not_charged`, `llm.billing_uncertain`), is specified in
full in `docs/message-catalog.md` and is normative.

## 10. Credentials, secrets, and redaction

- **Encryption:** AES-256-GCM (the `cryptography` library), a random 96-bit nonce per
  write, AAD = `credential_id || key_id` (so ciphertexts cannot be swapped between
  rows). `key_id` enables future rotation.
- **Master key:** `FLOWFORGE_SECRET_KEY`, readable from an env var or a file
  (`..._FILE`, Docker-secrets style). `flowforge keygen` generates it. **Explicit
  provisioning by default** (U2): the app refuses to start without a key. An opt-in
  quick-start flow may generate one into the data volume with a loud, explicit
  warning, but this is never the default path. An encrypted **canary** in
  `app_settings`, verified at startup, makes a wrong or missing key after a restore
  fail fast with a clear message instead of silently corrupting data.
- **Handling:** secret fields are write-only (never returned after creation) and
  masked in the UI. Decrypted material exists only in the worker's memory for the
  duration of the one node execution that declared that credential.
- **Redaction** is central, in the engine, never per-node: every decrypted secret
  value is registered with a per-run redactor that masks it in event logs, resolved
  params, node outputs, and errors **before** anything is persisted or logged.
  Documented limitation: this is exact-substring matching, so a transformed secret
  (base64-encoded, partially sliced) is not caught — another reason credentials are
  kept entirely out of the expression context (§8).
- **Workflow export** contains credential references (name, type) only, never
  values.
- **Webhook tokens** (U3): 256-bit, stored hashed, shown once, and regenerable.

## 11. Authentication and web security

- **Single administrator account** (no multi-user, no RBAC — explicitly out of
  scope, see `docs/mvp-scope.md`), argon2id password hashing.
- **Server-side sessions** in PostgreSQL: random 256-bit tokens stored hashed,
  revocable, idle and absolute expiry. Not JWT.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` whenever served over HTTPS (localhost
  HTTP is a documented exception).
- **CSRF**: `SameSite` plus strict `Origin`/`Host` verification on state-changing
  requests, JSON-only request bodies. Single-origin deployment (the web container
  proxies `/api`) avoids CORS entirely.
- **First-run admin creation** requires a **one-time setup token** printed to the
  `api` log (or the `flowforge create-admin` CLI) — never an open signup form, since
  an exposed fresh instance could otherwise be claimed by whoever reaches it first.
- Login rate limiting / lockout is persisted in PostgreSQL (no Redis needed).
- **Webhooks** are intentionally unauthenticated at the cookie/session level
  (`POST /hooks/{token}`), relying on the 256-bit hashed token instead, subject to
  the body-size cap and per-workflow queue cap. No cookies are involved, so CSRF does
  not apply. HMAC signing is explicitly post-MVP.

## 12. Schema migrations and version compatibility

Alembic lives inside the Python package, so it ships in the image. `flowforge
migrate` takes a `pg_advisory_lock` so concurrent starters don't race. **Both `api`
and `worker` refuse to run (retrying) unless the database's revision equals the
code's head**, so the two roles can never run against a skewed schema. Migrations
are forward-only; restore targets the same or a newer app version.

## 13. Backup and restore

- **What must be backed up:** (a) the PostgreSQL database, and (b) the **master
  key**, kept **separately** — a database dump without the key cannot decrypt
  credentials, and storing them together defeats the encryption. This is stated
  prominently in operator documentation.
- **Mechanism (U6):** documented, tested `pg_dump -Fc` / `pg_restore` scripts run
  through the Postgres container — no Postgres client is bundled into the app image.
- **Restore procedure:** stop `api` and `worker`, restore into an empty database,
  supply the same key, run `migrate`, start. The startup canary (§10) validates the
  key.
- **Workflow import/export** as JSON is a separate, DB-independent portability layer;
  it excludes credentials.
- **Required CI test (M7, release gate RB4):** backup, wipe, restore, then assert
  workflows are intact, history is readable, and a credential decrypts.
- A run that was `running` at backup time simply has no live owner after a restore;
  it is reclaimed and resolved by the ordinary §6 rules — a restore introduces no new
  recovery case.

## 14. Observability

Structured JSON logs to stdout from both roles, with run/node correlation ids;
`/healthz` (liveness) and `/readyz` (database reachable, schema at head) on each
role; run history in the UI. A run whose `visible_at` has passed with no live owner
is shown as **stalled**. No metrics stack in v0.1.

## 15. Live updates

A single-writer-per-run invariant (the fenced lease holder) lets the worker assign a
gap-free `run_events.seq` per run, re-initialized from `MAX(seq)` on reclaim. The API
exposes `GET /runs/{id}/events?after=seq`; the MVP client **polls** this endpoint
(~1 s) and the run/node state endpoints. Server-Sent Events can later be layered over
the same cursor with no architectural change — nothing here requires a broker.

## 16. Toolchain, repository structure, MVP scope, milestones, risks, and gates

This document intentionally does not restate material that has its own authoritative
file, to avoid the drift that comes from keeping the same fact in two places:

- **Exact version pins, verification dates, and sources:** `docs/toolchain-versions.md`.
- **MVP boundary, in/out-of-scope features, and the U1–U8 decisions:**
  `docs/mvp-scope.md`.
- **Milestone plan (M0–M7) and their dependencies:** `docs/milestones.md`.
- **Full risk register and release-blocking gates (RB1a–RB8):**
  `docs/risks-and-release-gates.md`.
- **Exact LLM usage/billing UI wording and its tests:** `docs/message-catalog.md`.
- **Individual architectural decisions, their context, and alternatives considered:**
  `docs/adr/` (index at `docs/adr/README.md`).

**Target repository structure** (not yet created beyond what M0 needs — see
`docs/milestones.md` for when each directory is introduced):

```text
flowforge/
├── backend/                      # introduced starting M1
│   ├── pyproject.toml  uv.lock  .python-version  alembic.ini
│   └── src/flowforge/
│       ├── core/  domain/  engine/  expressions/  nodes/  ai/
│       ├── security/  persistence/  services/  api/  worker/  cli.py
│   └── tests/{unit,integration,recovery,security,contract}/
├── frontend/                     # introduced starting M5
├── deploy/                       # Dockerfiles, compose, .env.example — introduced M4
├── docs/                         # introduced M0 (this milestone)
│   └── adr/
├── examples/                     # introduced when the first sample workflow exists
├── .github/workflows/            # introduced M0 (minimal), expanded each milestone
└── LICENSE  README.md  .gitignore  (M0)
```

## 17. Consistency summary

| Check | Result |
| --- | --- |
| Queue ownership | `runs` is the sole queue. `queued` ⇒ no node rows; `waiting` ⇒ unowned; `running` ⇒ owned by exactly one `(lease_owner, lease_epoch)` |
| Writers | `node_runs`/`run_events`: the fenced lease holder only. `runs`: the API writes only enqueue, the idempotent cancel, and `queued → cancelled`; the worker writes everything else |
| Execution persistence | State is a pure function of `(definition, node_runs rows, db_now)`; retry timers, deadlines, and parked wake times are columns, never memory |
| `effect_class` immutability | Resolved once at first T2, enforced by a DB trigger and CHECK independent of application code; read from the row everywhere it matters |
| Cancel vs. finalize vs. park vs. claim | Each pair resolved by a row lock plus a predicate in one statement; no read-then-write window |
| Unknown outcomes | One cause × effect-class matrix governs crash, cancel, timeout, and deadline uniformly; `unsafe_write` is never silently retried or labeled safely cancelled |
| LLM billing | `read` class is about state mutation only; billing is provider-specific, defaults to uncertain, and is never inferred from a status code or from usage alone |
| Outbound modes | CS failure ⇒ disabled for every outbound node, LLM included; AP6/AP7 failure ⇒ disabled (not restricted); AP4-only failure ⇒ restricted; all pass ⇒ full |
| Absolute-deny addresses | Never overridable by `allow_private`, allowlist, vendor catalog, mode, or operator config; validated on every resolved address and every redirect hop |
| Dispatch marker | Transport-set, zero-false-negatives mandatory at every tier, Tier A defined at the application-data layer and provable only by spike S1 |
| Deferred infrastructure | No Redis, broker, or scheduler service anywhere in this document; cron/Delay/Loop/Agent/Code-node are explicitly deferred (`docs/mvp-scope.md`) |

Nothing in this document has been contradicted by a later revision without that
contradiction being resolved here; where v3.1–v3.4 corrected earlier wording, only
the corrected version appears above.
