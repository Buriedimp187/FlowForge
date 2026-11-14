# FlowForge Risks and Release Gates (v3.4)

Status: **Normative.** This is the consolidated, final risk register and
release-gate list. Earlier numbering from prior architecture drafts (v3, v3.1, v3.2,
v3.3) is not preserved separately; this file is the only authoritative version.

## Release-blocking gates

Each gate is a requirement to demonstrate at a specific milestone (see
`docs/milestones.md`); none is currently satisfied, since no code exists yet. None
should be treated as passed until its test suite actually runs green.

| Gate | Requirement | Milestone | If not met |
| --- | --- | --- | --- |
| **RB1a** | Core connection-security (CS) gates pass: AP1 (resolve once, connect to validated address), AP2 (TLS SNI + hostname verification against the original hostname), AP3 (DNS-rebinding immunity), AP5 (environment proxies inert), the startup self-test, the no-unpinned-code-path rule, the per-connection invariant | M3 | Outbound is `disabled` for every outbound-capable node, HTTP and every LLM adapter included. Release is delayed; an ADR records the decision. There is no weaker fallback |
| **RB1b** | AP6 (absolute-deny set + private/overridable tier, validated on every resolved address and every redirect hop) and AP7 (exact-match allowlist semantics, entry-scoped `allow_private`) pass, including tests with `allow_private` both false and true against every absolute-deny address | M3 | Outbound is `disabled`. Restricted mode is **not** a fallback for an AP6/AP7 shortfall — see `docs/architecture.md` §7.3 |
| **RB1c** | AP4 (per-hop redirect re-validation) passes | M3 | Mode ceiling is `restricted` (redirects are not followed there anyway); documented, with an ADR |
| **RB2** | Spike S1: zero dispatch-marker false negatives across randomized-timing fault injection, a completed layer report, and a recorded Tier (A, B, or C) | M3 | Tier C is adopted (valid and safe). The HTTP node is not considered production-ready without a recorded tier |
| **RB3** | The crash-injection and race-harness suite passes against a real PostgreSQL: every cell of the interruption matrix, every cell of the cancel/finalize/park/claim race matrix, and `effect_class` immutability under a simulated classifier change | M2 | v0.1 does not ship |
| **RB4** | Backup → wipe → restore test passes in CI, including credential decryption with the key and the startup canary check | M7 | v0.1 does not ship |
| **RB5** | Outbound proxy-inertness tests pass (environment proxy variables have zero effect on outbound requests, in either direction) | M3 | v0.1 does not ship |
| **RB6** | Lease-stability stress test: under a CPU-heavy node workload at the documented payload/expression caps, no spurious lease loss across N runs (N fixed at M2) | M2 | Lower the payload/expression caps until it passes |
| **RB7** | Expression-sandbox escape tests pass (no `eval`, no attribute access beyond the fixed context, no I/O, no wall-clock/random access) | M3 | v0.1 does not ship |
| **RB8** | LLM billing: adapter conformance tests, cited rule tables manually reviewed against each provider's then-current documentation, the "processed-then-dropped" scenario, and the message-catalog wording/allowlist/banned-term tests (`docs/message-catalog.md`) | M6 | v0.1 does not ship |
| **AP8** (process gate) | The full outbound test suite re-runs on every dependency bump (Dependabot PRs touching httpx/httpcore or TLS-adjacent packages) | Ongoing from M3 | CI blocks the bump until green |

## Risk register

| ID | Risk | Status / mitigation |
| --- | --- | --- |
| R1 | Replay re-executes previously succeeded writes, so a user can duplicate real-world side effects | Mitigated only by a confirmation dialog listing write-class nodes before Replay proceeds. A "skip succeeded nodes" replay is the post-MVP retry-from-failed-node feature, not v0.1 |
| **R2** | Secure IP pinning with correct TLS hostname verification may not be achievable cleanly on the chosen transport | Unproven until M3. A CS failure disables **all** outbound-capable nodes (HTTP and LLM), with no weaker fallback — see RB1a. An AP6/AP7 shortfall also disables outbound rather than falling back to restricted |
| R3 | Lease loss from event-loop or GIL starvation (e.g. CPU-heavy expression evaluation on a large payload) could risk duplicate execution after a reclaim | Mitigated by the dedicated heartbeat thread (separate connection from the executor loop) and by payload/expression size limits. Not eliminated — validated by the M2 stress test (RB6). A lease loss is safe for *state* (fenced writes) but not necessarily for an in-flight external effect |
| R4 | `outcome_unknown` will be seen by real users (any worker kill during an unsafe write is, correctly, surfaced this way) — a real usability cost even though the behavior is correct | Mitigated by graceful SIGTERM release (reduces how often it happens) and by documentation. A dedicated UX for resolving unknown outcomes ("mark succeeded", "mark failed") is not designed and is deferred past v0.1 |
| **R5** | Dispatch-marker correctness depends on which layer the pinned httpcore/httpx stack actually exposes | Unproven until spike S1 produces the layer report (RB2). Tiers B and C are safe fallbacks if Tier A cannot be demonstrated; the zero-false-negative requirement never moves to claim a higher tier. Re-verified on every dependency bump (AP8) |
| R6 | Remote APIs may not honor the idempotency key FlowForge sends, even when the operator asserts `safe_to_retry` | Documented; the HTTP node's validator warns when `idempotency_key_header` is set without `safe_to_retry`. FlowForge cannot verify third-party dedupe behavior |
| R7 | The entire queue/lease design assumes READ COMMITTED isolation and a single PostgreSQL primary. pgbouncer in transaction-pooling mode, altered isolation settings, or read replicas could break the claim/fencing semantics | Documented as unsupported configurations. `/readyz` can check `default_transaction_isolation` once implemented (M4) |
| R8 | Database clock steps (NTP correction, VM clock jumps) shift lease and deadline decisions, since all timing is DB-clock-based | All decisions use the single DB clock, so workers can't disagree with each other, but a clock step can still cause early or late expiry. Lease TTL is deliberately generous; documented, not solved |
| R9 | Expressions have no wall-clock access (`now` resolves to the run's creation time), which is required for idempotency-key determinism but may surprise users expecting "the current time" | Deliberate (§6.4, §8 of `docs/architecture.md`). Documented. A possible later "current time" node whose output is persisted (and thus deterministic on replay) is a candidate post-MVP feature, not committed |
| R10 | PostgreSQL growth and bloat from JSONB node outputs and heartbeat/claim churn on `runs` | Size caps and retention pruning exist from the first release. If heartbeat churn is ever measured as a problem, leases can be extracted to a dedicated `run_leases` table without changing the semantics. Not yet measured, since no code exists |
| R11 | LLM calls can be billed more than once (automatic crash re-execution of a `read`-class node, or a user opting into retrying a billing-uncertain failure), and recorded usage is only ever a lower bound, never a monetary figure | Documented; defaults are conservative (no auto-retry when billing-uncertain). Bounded by `max_recoveries` and per-attempt `max_tokens`, with the attempt count visible in the UI. No cumulative spend cap in v0.1 — accepted post-MVP |
| R12 | Disabling connection reuse, HTTP/2, and streaming request bodies in the outbound HTTP client (required for a provable dispatch boundary) costs performance and rules out some large-upload use cases | Accepted for correctness. Revisit only once a proven, tested dispatch boundary exists that tolerates these features — not before |
| **R13** | Provider documentation about billing semantics may be ambiguous, may change, or may be misread when an adapter's rule table is built | Mitigated by requiring every "not charged" rule to carry a dated citation, defaulting to `billing_uncertain = true` otherwise, and re-reviewing citations whenever an adapter changes. Residual risk accepted after the RB8 review |
| **R14** | A CS, AP6, or AP7 failure leaves FlowForge with no HTTP Request node and no LLM adapters at all, which is not a functionally complete MVP | By design (fail closed, per `docs/architecture.md` §7.3) — this is the intended consequence of refusing to weaken the security guarantee, not an oversight. In practice this makes RB1a/RB1b release-blocking in the fullest sense: a failure here means delaying the release, not shipping a weaker substitute |
| **R15** | The absolute-deny address list may miss a cloud or platform metadata/infrastructure address, whether a newly introduced one or one this document's authors simply didn't know about | The built-in list is verified against current provider documentation at M3 with a recorded verification date, and re-checked at each subsequent release. Operators may add entries via `FLOWFORGE_OUTBOUND_DENY`. Residual risk accepted; broad `169.254.0.0/16` and `fe80::/10` coverage mitigates the most common case (link-local metadata services) |

## Acceptable post-MVP risks (explicitly not blocking v0.1)

R1 (Replay confirmation dialog only, no selective skip), R4 (no dedicated
unknown-outcome resolution UX), R6, R7, R8, R9, R10, R11 (LLM duplicate billing
possible, no spend cap), R12 (no keep-alive/HTTP/2), R13 (documentation-drift
residue, after the RB8 review), R15 (absolute-deny list is best-effort, not
exhaustive by construction), no per-item checkpointing (U4), no forward-proxy
support, and every deferred feature listed in `docs/mvp-scope.md`.

## Explicitly not-yet-resolved at M0

The following are correctly left open by design — M0 does not attempt to resolve
them, and nothing in M0's output should be read as having resolved them:

- Whether the pinned httpcore/httpx stack can reach Tier A for the dispatch marker
  (R5/RB2) — requires spike S1, scheduled first in M3.
- Whether secure IP pinning with TLS hostname verification can be demonstrated on
  that same stack (R2/RB1a) — also M3.
- The exact patch/minor versions for Python, Node.js, and PostgreSQL
  (`docs/toolchain-versions.md` marks these **unresolved-precision**, to be
  re-confirmed against official sources immediately before each is first consumed in
  configuration).
- The exact absolute-deny address list's completeness against current cloud-provider
  documentation (R15) — verification is scheduled for M3, not performed in M0.
