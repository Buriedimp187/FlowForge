# ADR-0015: Toolchain baselines and version pinning

**Status:** Accepted

## Context

Reproducible builds require exact, verified version pins rather than floating tags,
but the proposed baseline (Python 3.13, Node.js 24 LTS, PostgreSQL 17) was explicitly
a candidate set to verify, not a final commitment, and this machine's own installed
interpreters (Python 3.15.2, Node v26.13.1) are not themselves the project pin.

## Decision

Verified at M0 (2026-11-13) against official/near-official sources:

- **Python: 3.14** (a deviation from the proposed 3.13 — see
  `docs/toolchain-versions.md` for the full reasoning: 3.13 exited regular bugfix
  maintenance in autumn 2026 and is now security-fix-only, so a new project starting
  today gains nothing from pinning to it over the still-actively-maintained 3.14
  series). Exact patch is recorded as **unresolved-precision**, to be re-confirmed
  against `python.org` immediately before `.python-version`/the Dockerfile are
  written.
- **Node.js: 24 LTS ("Krypton")** — accepted as proposed, no deviation. Exact patch
  is unresolved-precision pending re-confirmation against `nodejs.org`.
- **PostgreSQL: 17** (major version already settled by decision U7) — accepted,
  exact minor unresolved-precision pending re-confirmation against
  `postgresql.org`.

No floating `latest`/bare-major tags; lockfiles (`uv.lock`, `package-lock.json`) are
the actual pin for dependencies once they exist; Docker image references add a
digest alongside the version tag once Dockerfiles exist (M4); CI third-party actions
are pinned to a commit SHA, never a tag.

## Consequences

A documented, sourced, and dated version decision rather than an invented one, with
an explicit obligation to re-verify exact patch numbers at the moment each is first
consumed in configuration, rather than treating this document's numbers as
permanently binding.

## Related

`docs/toolchain-versions.md` (full verification detail, sources, and dates).
