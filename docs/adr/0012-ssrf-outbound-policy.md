# ADR-0012: SSRF and outbound HTTP policy

**Status:** Accepted (revised — this is the final, v3.4 revision; see history below)

## Context

The HTTP Request node and the LLM provider adapters both make outbound requests on
behalf of workflow definitions the operator controls but that may reference
arbitrary hosts. Without protection, this is a server-side request forgery vector
against the host's own network, cloud metadata services, and the application's own
internal services (Postgres, the API itself).

## Decision

Exactly one audited outbound path (`OutboundHttpClientFactory`) is used by every
workflow-originated request, HTTP node and LLM adapter alike; a banned-import lint
rule and tests enforce this structurally. **Environment HTTP proxies are never
honored** (`trust_env=False`, no proxy mounts), because a proxy would resolve and
connect on the application's behalf, defeating IP pinning entirely — and Docker's
own tooling can inject proxy environment variables into containers automatically,
making this a realistic bypass rather than a theoretical one.

**Gate sets and mode eligibility** (the final, corrected rule — see history):

| CS | AP6 | AP7 | AP4 | Effective mode |
|---|---|---|---|---|
| fail | any | any | any | **disabled** |
| pass | **fail** | any | any | **disabled** |
| pass | pass | **fail** | any | **disabled** |
| pass | pass | pass | **fail** | **restricted** |
| pass | pass | pass | pass | **full** |

- **CS** (core connection security: resolve-once + connect-to-validated-address, TLS
  SNI/hostname verification against the original hostname, DNS-rebinding immunity,
  proxy inertness, the startup self-test, the no-unpinned-path rule, and the
  per-connection invariant) gates **every** outbound request. A CS failure disables
  **all** outbound-capable nodes — the HTTP node and **every** LLM adapter, with no
  exception. A hard-coded vendor hostname is never treated as a substitute for
  pinning and TLS verification.
- **AP6** (absolute-deny address set, non-overridable by `allow_private`, the
  allowlist, vendor-catalog enablement, mode, or operator configuration — plus the
  private/overridable tier) and **AP7** (exact-match allowlist semantics,
  entry-scoped `allow_private`) are both required for **restricted or full** mode.
  If either fails, the mode is `disabled`, **not restricted** — restricted mode's
  safety depends on these two being correct, so without them an
  "operator-approved" destination could resolve to a forbidden address.
- **AP4** (per-hop redirect re-validation) is required only for `full` mode, since
  restricted mode never follows redirects; AP4 failing alone degrades `full` to
  `restricted`.
- Effective mode = `min(build ceiling, operator setting, startup probes)`; the
  operator can only lower the mode, never raise it above the CI-determined ceiling.
- A **per-connection invariant** asserts, before every first write, that the
  connection actually came from the pinning backend and that TLS verification is
  active for `https`; a violation latches the process into `disabled` until restart.
  The weaker "validate the peer IP after connecting" check may exist only as
  additional defense-in-depth **alongside** pinning — never as a substitute, and
  never grounds on its own to claim CS has passed.

## Consequences

A fail-closed ladder (`full > restricted > disabled`) with no state in which an
unvalidated destination is reachable. In the worst case — CS, AP6, or AP7 cannot be
demonstrated — FlowForge ships with **no** HTTP Request node and **no** LLM adapters
available, which is not a functionally complete MVP (Risk R14); this is the accepted
cost of refusing to weaken the guarantee rather than shipping a weaker substitute.

## Revision history

This ADR went through five review passes. Earlier drafts: (1) proposed a weaker
"validate the peer IP post-connect" fallback as an acceptable substitute for pinning
— removed, since it cannot establish TLS-hostname-bound trust; (2) allowed LLM
adapters to reach "built-in vendor endpoints" even in a mode where core connection
security had failed — corrected, since a hostname is not a substitute for pinning;
(3) treated a single AP-gate set as sufficient for restricted mode — split into CS
(blocks everything on failure) vs. AP6/AP7 (also blocks restricted, since restricted
depends on them) vs. AP4 (only gates `full`, since restricted never follows
redirects); (4) allowed `allow_private` to implicitly cover metadata-style addresses
— corrected by introducing the non-overridable absolute-deny tier, checked ahead of
and independent of `allow_private`, on every resolved address and every redirect
hop. The table and rules above are the final, v3.4 state.

## Related

`docs/architecture.md` §7; ADR-0018 (the dispatch marker and transport);
`docs/risks-and-release-gates.md` (RB1a, RB1b, RB1c; R2, R14, R15).
