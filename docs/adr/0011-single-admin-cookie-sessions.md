# ADR-0011: Single-administrator cookie sessions

**Status:** Accepted

## Context

v0.1 targets a single self-hosted instance for one operator; multi-user, teams, and
RBAC are explicitly out of scope (see `docs/mvp-scope.md`), so the authentication
model should not carry complexity for a feature that doesn't exist yet.

## Decision

A single administrator account, argon2id password hashing, and server-side sessions
in PostgreSQL (random 256-bit tokens, stored hashed, revocable, with idle and
absolute expiry) — not JWT. Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure`
whenever served over HTTPS. CSRF protection relies on `SameSite` plus strict
`Origin`/`Host` verification and JSON-only request bodies; the single-origin
deployment (the frontend container proxies `/api`) avoids CORS entirely.
**First-run admin creation requires a one-time setup token** printed to the API log
(or created via a CLI command), never an open signup form — an exposed fresh
instance could otherwise be claimed by whoever reaches it first. Login rate
limiting/lockout state is persisted in PostgreSQL, needing no additional service.

## Consequences

A simple, revocable session model appropriate to a single-operator deployment, with
no path to multi-user support without a future redesign — accepted, since that
redesign is out of scope for the foreseeable roadmap.

## Related

`docs/architecture.md` §11.
