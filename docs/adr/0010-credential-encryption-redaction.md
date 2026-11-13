# ADR-0010: Credential encryption and central redaction

**Status:** Accepted

## Context

Self-hosted workflow credentials (API keys, tokens) must be stored safely and never
leaked through logs, event history, or workflow export, while remaining honest about
what encryption at rest can and cannot protect against.

## Decision

Credentials are encrypted with AES-256-GCM, a random 96-bit nonce per write, and an
AAD binding of `credential_id || key_id` so ciphertexts cannot be swapped between
rows. The master key (`FLOWFORGE_SECRET_KEY`) must be **explicitly provisioned by
default** (decision **U2**) — the application refuses to start without one; an
opt-in quick-start flow may generate one into the data volume with an explicit
warning, but this is never the default path. An encrypted canary in `app_settings`,
checked at startup, makes a wrong or missing key fail fast with a clear message
rather than silently corrupting data. Redaction of decrypted secret values is
**central, in the engine, never per-node** — every secret is registered with a
per-run redactor that masks it in logs, resolved parameters, outputs, and errors
before persistence.

**Honest limitation, documented rather than implied away:** encryption at rest
protects against a leaked database dump or backup; it cannot protect against an
attacker who has both database access and the master key on a running,
compromised host. Redaction is exact-substring matching, so a transformed secret
(base64-encoded, partially sliced) is not caught — which is also why credentials are
never placed in the expression context (ADR-0008).

## Consequences

Credentials and run history are safe against the realistic self-hosted threat (a
leaked backup, a compromised unrelated service reading the database), with clearly
documented limits against a fully compromised host.

## Related

`docs/architecture.md` §10; ADR-0008, ADR-0014.
