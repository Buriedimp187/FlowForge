# ADR-0018: Outbound HTTP transport (pinning + dispatch marker)

**Status:** Accepted (revised — final, v3.4 revision; see history below)

## Context

Two distinct safety properties both need to live in the same outbound HTTP
transport: IP pinning with correct TLS hostname verification (ADR-0012), and a
dispatch marker that can tell the interruption-matrix logic (ADR-0005) whether an
external write's bytes may have actually left the process. Neither property may be
assumed; both must be demonstrated against the actual pinned library versions.

## Decision

A custom `httpcore` network backend (wrapped in a small custom `httpx` transport)
provides both properties from one place: `connect_tcp` resolves once, validates
every resolved address, and connects only to the validated address; `start_tls`
verifies against the *original* hostname for SNI and certificate checking; the
returned stream sets the dispatch marker **immediately before its first write, with
no `await` between**, so that a write that raises or is cancelled mid-flight still
counts as dispatched (the conservative default). Connection reuse, HTTP/2, and
streaming request bodies are all disabled, and TCP Fast Open / TLS 0-RTT are never
enabled — each of these could otherwise blur exactly where the dispatch boundary is.
Redirects are followed only for class `read` (ADR-0005); a write-class request that
receives a 3xx returns that response to the workflow instead of silently re-sending
the write elsewhere.

**Dispatch-marker tiers, defined at the application-data layer, not the TCP layer:**

| Tier | Marker set when |
|---|---|
| **A** (preferred) | the first write on the **TLS-wrapped stream, after the handshake** (https) or the stream (http) — not a write to the underlying TCP socket, which also carries handshake bytes a TCP-level hook cannot distinguish from request data |
| **B** | TLS handshake completion / TCP connect completion, with no write-level distinction |
| **C** | our own `connect_tcp` returning (TCP established) |

**The zero-false-negative requirement applies to every tier**, and a lower tier
never weakens the `unknown`-outcome policy — it only loses precision. Spike S1 (the
first task of M3) determines which tier the pinned httpcore/httpx stack actually
supports, via a TLS-terminating fault-injection harness that records both raw and
decrypted-application bytes received, with Tier A additionally requiring the marker
to be deterministically `False` in handshake-only failure scenarios. **If the
implementation cannot distinguish application-data writes from handshake writes,
Tier B or C is used — the soundness bar is never relaxed to claim Tier A.** Because
Tier C's hook lives in the same backend that performs IP pinning, a backend that
cannot be installed at all fails CS itself (ADR-0012), so there is no tier below C.

## Consequences

One audited transport path backs both the SSRF guarantee and the idempotency/outcome
guarantee, at the cost of coupling to specific `httpcore`/`httpx` internal behavior
that must be re-verified on every dependency bump (gate AP8) — a minimal self-written
HTTP/1.1 client over asyncio streams remains a documented last resort if the chosen
libraries cannot support this.

## Revision history

The dispatch marker was originally specified as a call (`ctx.mark_dispatched()`) made
by individual node authors — this placed a correctness-critical boundary on author
discipline rather than on the transport itself, and was corrected to the
transport-set design above. The tier definitions were further sharpened to
distinguish the TLS-wrapped application-data layer from the raw TCP layer, after
identifying that a TCP-level hook would be sound but imprecise (it would
misclassify handshake interruptions as dispatched) and could not honestly be called
Tier A.

## Related

`docs/architecture.md` §7.7; ADR-0005, ADR-0012; `docs/risks-and-release-gates.md`
(RB2, R5).
