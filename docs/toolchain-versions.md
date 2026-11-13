# Toolchain and Version Pins

Status: **Normative for documentation purposes. Re-verification required before any
Dockerfile, lockfile, or CI runtime-install step is created (M3 for the outbound-transport
spike runtime, M4 for the first Dockerfiles).**

This document exists because Architecture v3.4 (ADR-0015) requires exact, pinned,
non-floating versions, with the verification date and source recorded, and requires any
deviation from the originally proposed baseline (Python 3.13, Node.js 24 LTS,
PostgreSQL 17) to be explained rather than silently accepted or silently changed.

No runtime, package, or dependency has been installed as part of M0. The versions below
are **selected pins for future milestones**, not evidence of anything installed in this
repository.

## Verification date

**2026-11-13**, via web search against official or near-official sources. Where a search
result could not be cross-checked against the live `python.org` / `nodejs.org` /
`postgresql.org` pages directly (the fetch tool was not used to load those pages in this
pass), this is noted explicitly below as **unresolved-precision**, per the instruction to
document what cannot be reliably verified rather than invent a number.

## Python

| | |
|---|---|
| Proposed baseline | 3.13 |
| **Selected pin** | **3.14** (exact patch unresolved-precision, see below) |
| Status of 3.13 as of verification date | 3.13 left regular bugfix maintenance in autumn 2026; sources describe 3.13.16 (2026-09-30) as "the last full maintenance release" and 3.13.17 / 3.13.18 (Oct 2026) as **security-only, source-only** releases, with no further installers. 3.13 remains supported for security fixes only, through its documented end-of-life (~2029). |
| Status of 3.14 as of verification date | 3.14 is the actively bugfix-maintained series (released Oct 2025). Latest patch found: **3.14.8** (2026-09-30, described as "the eighth maintenance release... an expedited security release"). PEP 745 lists the regular bugfix period running to approximately October 2027, then security-only to approximately October 2030. |
| Status of 3.15 | 3.15 is the newest feature series (this development machine has 3.15.2 installed locally). It is explicitly **not** selected: a brand-new feature series has the least ecosystem/wheel compatibility coverage, which conflicts with the architecture's "avoid unnecessary dependencies and infrastructure" / stability principles. |

**Deviation explained:** the architecture's proposed baseline (3.13) is still a
candidate, not a final pin (per instruction). At the verification date it has just
exited regular bugfix maintenance and receives security fixes only. Starting a new
project on a security-only branch forgoes future bugfix releases for no compatibility
benefit, since 3.14 is already the actively maintained prior-feature series with broad
dependency-wheel availability by now. **Recommendation: pin to 3.14, exact patch to be
re-confirmed at the moment `pyproject.toml` / `.python-version` / the backend Dockerfile
are created (M1 / M4), against `https://www.python.org/downloads/` directly.** 3.14.8 is
recorded here as the best currently-evidenced patch and is **not binding** until that
re-confirmation.

Sources consulted: `https://www.python.org/downloads/release/python-31318/`,
`https://www.python.org/downloads/release/python-31317/`,
`https://www.python.org/downloads/release/python-31316/`,
`https://www.python.org/downloads/release/python-3148/`, PEP 745
(`https://peps.python.org/pep-745/`).

## Node.js

| | |
|---|---|
| Proposed baseline | 24 LTS |
| **Selected pin** | **24 LTS ("Krypton")** — accepted as proposed, no deviation |
| Status as of verification date | Node.js 24 ("Krypton") is Active LTS, supported through approximately April 2028. Latest patch found via nodejs.org release notes: **24.18.1** (2026-07-29, a security release). A later patch (the search also surfaced a `v24.11.0` blog post, which is an *older* version by semver than 24.18.1) likely exists by the verification date; the live `nodejs.org/en/download` / `nodejs.org/dist/` index was not fetched directly in this pass. |
| Node.js 26 | Promoted toward Current/LTS around Oct 2026; **not selected**, since the architecture approved 24 LTS specifically and Active LTS (not Current) is the appropriate stability tier for a self-hosted, solo-maintained project. |

**No deviation** from the proposed baseline. Exact patch is
**unresolved-precision**: 24.18.1 is recorded as the best-evidenced patch at the
verification date, but must be re-confirmed directly against
`https://nodejs.org/en/download` (or the `dist/` index) before being written into
`.nvmrc`, `package.json#engines`, or the frontend Dockerfile (M4/M5).

Sources consulted: `https://nodejs.org/about/previous-releases`,
`https://nodejs.org/blog/release/v24.11.0`, localized mirrors of the Node.js release
notes for 24.18.1.

## PostgreSQL

| | |
|---|---|
| Proposed baseline | 17 |
| **Selected pin** | **17** — accepted as proposed per U7 (already decided during architecture review); this document only pins the **minor/patch**, not the major version |
| Status as of verification date | PostgreSQL 17.x receives quarterly minor releases; dump/restore is not required between 17.x minors. Latest minor found: **17.11** (release notes page dated 2026-08-13). A later minor (17.12 or newer) may exist by the verification date and was not ruled out. |

**No deviation.** Major version 17 was already a settled architectural decision (U7);
this is a routine minor-version pin. Exact minor is **unresolved-precision**: 17.11 is
recorded as the best-evidenced minor at the verification date and must be re-confirmed
against `https://www.postgresql.org/docs/release/` before being written into the
Postgres service's Docker image tag (M4), which must additionally be pinned by digest,
not tag alone.

Sources consulted: `https://www.postgresql.org/docs/release/17.11/`,
`https://www.postgresql.org/docs/release/17.10/`,
`https://www.postgresql.org/docs/release/17.9/`.

## Pinning rules that apply once any of the above is used in configuration

1. **No floating tags.** `latest`, `lts`, `17`, `24`, bare major/minor tags, and any
   unpinned `FROM`/`engines`/`requires-python` range are prohibited once Dockerfiles or
   lockfiles exist (M1+).
2. **Image references add a digest** (`@sha256:...`) in addition to the version tag,
   once Dockerfiles are written (M4).
3. **Lockfiles are the actual pin** for application dependencies: `uv.lock` for the
   backend, `package-lock.json` for the frontend. Neither exists yet, since no
   dependency has been installed.
4. **CI third-party actions are pinned to a commit SHA**, not a tag (see
   `.github/workflows/ci.yml` and `docs/adr/0015-toolchain-baselines-and-pinning.md`).
5. **Re-verification is required, not optional**, at each milestone that first
   consumes one of these versions (M1 for Python, M4 for Docker images, M5 for Node in
   the frontend Dockerfile), against the official source directly — not against a
   cached or summarized value such as the one in this document.

## Other tools observed on this development machine (not pins, informational only)

Recorded only to document what M0's environment inspection found; **none of this was
installed by this task, and none of it is a project requirement**:

| Tool | Observed version |
|---|---|
| Git | 2.56.1.windows.1 |
| Docker | 29.9.3 |
| Docker Compose | v5.7.0 |
| System Python (not the project pin) | 3.15.2 |
| System Node.js (not the project pin) | v26.13.1 |
| npm (bundled with system Node) | 11.20.0 |

No `uv`, `pnpm`, or `psql` client was found on this machine. Their absence is not a
blocker for M0 (documentation only) and does not trigger an install in this milestone.
