# Toolchain and Version Pins

Status: **Normative for documentation purposes. No exact patch/minor version in this
document is a finalized project pin.** Re-verification is required, directly against
the official source, before any Dockerfile, lockfile, or CI runtime-install step is
created (M3 for the outbound-transport spike runtime, M4 for the first Dockerfiles).

This document exists because Architecture v3.4 (ADR-0015) requires exact, pinned,
non-floating versions, with the verification date and source recorded, and requires
any deviation from the originally proposed baseline (Python 3.13, Node.js 24 LTS,
PostgreSQL 17) to be explained rather than silently accepted or silently changed.

No runtime, package, or dependency has been installed as part of M0.

## Three distinct things this document tracks (do not conflate them)

1. **Selected baseline series** — the major (and, for Node, LTS line) version this
   project targets. This *is* a decision, made during architecture review and
   recorded in ADR-0015: **Python 3.14, Node.js 24 LTS, PostgreSQL 17.**
2. **Latest observed release evidence** — the newest patch/minor release of that
   series that has been found via search and attributed to a specific official
   source and date, as of the verification date below. This is **evidence**, not a
   pin — it exists so a later re-verification has a documented starting point and
   so no number in this document is ever invented.
3. **Final exact configuration pin** — the specific patch/minor version actually
   written into `.python-version`, `.nvmrc`, a Dockerfile `FROM` line, or a lockfile.
   **None of these exist yet.** Each will be set only after a direct re-check against
   the official source at the milestone that first needs it (M1 for Python, M4 for
   Docker images, M5 for the frontend's Node pin) — not copied from this document's
   evidence column without that re-check, since time will have passed and newer
   patches may exist by then.

## Verification dates

- **2026-11-13** (M0, initial pass): web search against official or near-official
  sources, for the initial baseline-selection decision.
- **2026-11-13** (M0 audit correction, same day): refreshed with release evidence
  supplied directly from official release-notes URLs, listed per tool below. This
  refresh **only updates "latest observed release evidence"** — it does not change
  the selected baseline series, and it does not create a final pin.

## Python

| | |
| --- | --- |
| Selected baseline series | **3.14** (deviates from the proposed 3.13 baseline — see below) |
| Latest observed release evidence | **3.14.10**, released 2026-10-25. Source: `https://www.python.org/downloads/release/python-31410/` |
| Final exact configuration pin | **Not yet set.** To be fixed in `.python-version` / `pyproject.toml#requires-python` / the backend Dockerfile, with a fresh direct check against `https://www.python.org/downloads/` at that time (M1 for the version constraint, M4 for the Docker image tag+digest) |
| Status of 3.13 at verification date | Left regular bugfix maintenance in autumn 2026 (3.13.16, 2026-09-30, described as "the last full maintenance release"); 3.13.17/3.13.18 (Oct 2026) are security-only, source-only releases. Still supported for security fixes through its documented end-of-life (~2029), but receives no further bugfix releases |
| Status of 3.15 | The newest feature series (this development machine has 3.15.2 installed locally, which is **not** the project pin). Not selected: a brand-new feature series has the least ecosystem/wheel compatibility coverage of the three, which runs against the architecture's "avoid unnecessary dependencies" / stability principles |

**Deviation explained:** the proposed baseline (3.13) was a candidate to verify, not
a final commitment. At the verification date it had just exited regular bugfix
maintenance and moved to security-fix-only status. Starting a new project on a
security-only branch forgoes future bugfix releases for no compatibility benefit,
since 3.14 is the actively-maintained prior-feature series with materially broader
dependency-wheel availability by now than the brand-new 3.15 series. **This
reasoning is unchanged by the 2026-11-13 evidence refresh.**

**Compatibility with the rest of the eventual backend dependency set is unverified
and remains an open item**, not silently assumed solved: FastAPI, Pydantic v2,
argon2-cffi, cryptography, httpx, httpcore, and jmespath have not been checked
against Python 3.14 wheel availability, because none of them has been installed or
even selected with exact versions yet. (SQLAlchemy, Alembic and psycopg 3 were
selected and installed at M2.1; see "M2.1 persistence dependencies" below.) This is
narrower than it once was: `jsonschema` **has** since been declared and installed
(the first backend dependency — see "Backend dependencies" below), with its own
Python 3.14 evidence tracked separately there, which is metadata/wheel-availability
evidence, not an executed-on-3.14 verification. **The check for this remaining,
not-yet-installed list must happen before the M1 dependency lockfile (`uv.lock`) is
finalized** — if a required dependency lacks a 3.14 wheel or has an unresolved
incompatibility at that time, the baseline series decision itself (3.14 vs. 3.13 vs.
another series) must be revisited and documented as a new deviation, not silently
patched around.

Sources: `https://www.python.org/downloads/release/python-31410/` (3.14.10, primary
evidence for this refresh); also consulted:
`https://www.python.org/downloads/release/python-31318/`,
`https://www.python.org/downloads/release/python-31317/`,
`https://www.python.org/downloads/release/python-31316/`, PEP 745
(`https://peps.python.org/pep-745/`).

## Node.js

| | |
| --- | --- |
| Selected baseline series | **24 LTS ("Krypton")** — accepted as proposed, no deviation |
| Latest observed release evidence | **24.22.3**, listed in the official Node.js 24 download archive, last updated 2026-11-10. Source: `https://nodejs.org/en/download/archive/v24` |
| Final exact configuration pin | **Not yet set.** To be fixed in `.nvmrc` / `package.json#engines` / the frontend Dockerfile, with a fresh direct check against `https://nodejs.org/en/download/archive/v24` at that time (M5) |
| Status at verification date | Active LTS, supported through approximately April 2028. Node.js 26 is Current/promoting toward LTS around Oct 2026 and is **not** selected, since the architecture approved 24 LTS specifically and Active LTS (not Current) is the appropriate stability tier here |

**No deviation** from the proposed baseline. The 2026-11-13 refresh replaces the
earlier, less-precise evidence (24.18.1, found via a secondary/localized source)
with the official archive page's listing (24.22.3) — this is a correction to the
evidence, not a pin.

Sources: `https://nodejs.org/en/download/archive/v24` (primary evidence for this
refresh); also consulted: `https://nodejs.org/about/previous-releases`.

## PostgreSQL

| | |
| --- | --- |
| Selected baseline series | **17** — accepted as proposed per decision U7 (already settled during architecture review); this document only ever pins the **minor/patch**, never the major version |
| Latest observed release evidence | **17.12**, released 2026-11-12. Source: `https://www.postgresql.org/docs/release/17.12/` |
| Final exact configuration pin | **Not yet set.** To be fixed in the Postgres service's Docker image tag (plus a `@sha256:` digest, per the pinning rules below) at M4, with a fresh direct check against `https://www.postgresql.org/docs/release/` at that time |
| Status at verification date | PostgreSQL 17.x receives quarterly minor releases; dump/restore is not required between 17.x minors |

**No deviation.** Major version 17 was already a settled architectural decision
(U7); this document only tracks routine minor-version evidence. The 2026-11-13
refresh replaces the earlier evidence (17.11) with the newer 17.12 release that had
been published in the interim — again, a correction to the evidence, not a pin.

Sources: `https://www.postgresql.org/docs/release/17.12/` (primary evidence for
this refresh); also consulted: `https://www.postgresql.org/docs/release/17.11/`,
`https://www.postgresql.org/docs/release/17.10/`.

## Backend dependencies

Unlike the runtime baselines above, this section tracks an actual **installed and
exercised** third-party Python package — the first one the backend has introduced.

### JSON Schema dependency (`jsonschema`)

M1's final correction pass replaced a small custom parameter validator with real
**JSON Schema Draft 2020-12** validation (docs/architecture.md §5, §2.2; the
user-approved decision behind ADR-0005's latest revision). The library:

| | |
| --- | --- |
| Package | `jsonschema` |
| Exact version | **4.26.0**, pinned in `backend/pyproject.toml` and `backend/requirements-lock.txt` |
| Transitive dependencies (exact, from the same lock file) | `attrs==26.1.0`, `jsonschema-specifications==2025.9.1`, `referencing==0.37.0`, `rpds-py==2026.9.1` |
| Validator class used | `jsonschema.Draft202012Validator`, explicitly — never a bare `jsonschema.validate()` call that would let the library infer the draft |
| Installed into | An isolated project virtual environment, `backend/.venv` (created with `python -m venv .venv`; **never** the global/system Python installation, and nothing on `PATH` or in global `site-packages` was touched) |

**Python 3.14 compatibility — what was, and was not, verified.**

- **Not executed on Python 3.14.** No 3.14 interpreter is installed anywhere on the
  machine this work was done on (confirmed via `py -0p` and a `winget list` query,
  which show only Python 3.15.2). Installing a new Python interpreter was judged out
  of scope for a dependency-verification task and was not done. The isolated venv
  above was therefore created with, and every test below was actually run on,
  **Python 3.15.2** (CPython, win_amd64) — the only interpreter available.
- **What was verified instead, as genuine (not fabricated) evidence:** the PyPI JSON
  API (`https://pypi.org/pypi/rpds-py/2026.9.1/json`) was queried directly and shows
  that `rpds-py` 2026.9.1 — the one dependency in this chain written partly in Rust
  and therefore the one actually at risk of lacking 3.14 support — **publishes a
  prebuilt binary wheel for `cp314-cp314-win_amd64`**, alongside `cp311` through
  `cp315` and every other supported platform. `rpds-py`'s own declared
  `Requires-Python` is `>=3.11`; `jsonschema`'s is `>=3.10`. Both ranges, and the
  published wheel, are consistent with Python 3.14 support.
- **This is metadata/wheel-availability evidence, not an executed-on-3.14
  verification**, and it is reported as exactly that distinction — not blurred into
  a claim that compatibility was "verified" in the stronger sense. Re-running
  `backend/requirements-lock.txt`'s install and the full backend test suite on an
  actual Python 3.14 interpreter, once one is available in this environment, remains
  an **open item** and should happen before this dependency is treated as fully
  verified for production use.
- `jsonschema.Draft202012Validator` was functionally exercised (not merely imported)
  in this same Python-3.15.2 venv: schema well-formedness checks
  (`check_schema`), instance validation with nested objects/arrays, `required`,
  `additionalProperties`, `enum`, numeric/string constraints, `oneOf`/`anyOf`/`allOf`,
  `if`/`then`/`else`, and a `$ref`/`$defs` reference — see
  `backend/tests/unit/test_param_schema.py`.

### M2.1 persistence dependencies

Added for the PostgreSQL persistence foundation (`docs/m2-persistence-design.md`).
All are exact pins in `backend/pyproject.toml`; installed versions were resolved in
the isolated `backend/.venv` on **Python 3.15.2** only.

| Package | Pin | Role |
| --- | --- | --- |
| SQLAlchemy | 2.1.8 | Core only (no ORM) |
| Alembic | 1.20.1 | hand-written SQL migrations |
| psycopg (`[binary]`) | 3.3.9 | the only supported driver (`postgresql+psycopg`) |
| testcontainers (`[postgres]`, test only) | 4.15.0 | local PostgreSQL for integration tests |

- **Locks:** `backend/requirements-lock.txt` (runtime) and
  `backend/requirements-dev-lock.txt` (test-only, disjoint from runtime), both
  exact `==` pins including transitive packages. `backend/tests/unit/test_toolchain_pins.py`
  checks `pyproject.toml` against both.
- **Python floor:** `requires-python = ">=3.14,<3.16"` because ids use the
  standard-library `uuid.uuid7()` (added in 3.14). **No code has been executed on
  Python 3.14**; that runtime verification is still outstanding.
- **PostgreSQL test/CI image:** `postgres:17.12` pinned with its OCI index digest
  `sha256:dbb9623d5121b5d2c2c9e2fba56e40193cbb3510d4af7f035b1ab1823aff3205`
  (observed 2026-11-13). It is named once in `backend/tests/integration/support.py`
  and must equal the `services:` image in `.github/workflows/ci.yml` (enforced by a
  unit test). Re-verify tag and digest when bumping the PostgreSQL minor.
- The Testcontainers reaper (Ryuk) is disabled so that no unpinned helper image is
  pulled; the test support module removes its container at process exit.

### No lockfile tool (`uv`) yet

As before (M0), `uv` is not installed, so `backend/requirements-lock.txt` (plus the test-only
`backend/requirements-dev-lock.txt`) — plain pinned output from isolated venvs — is
the project's dependency lock
until `uv.lock` is introduced at a later milestone. It is regenerated the same way
(`pip install` into a fresh venv, then `pip freeze`) whenever a dependency changes.

## Pinning rules that apply once any of the above is used in configuration

1. **No floating tags.** `latest`, `lts`, `17`, `24`, bare major/minor tags, and any
   unpinned `FROM`/`engines`/`requires-python` range are prohibited once Dockerfiles
   or lockfiles exist (M1+).
2. **Image references add a digest** (`@sha256:...`) in addition to the version
   tag: already applied to the PostgreSQL test/CI image (M2.1); Dockerfiles follow
   at M4.
3. **Lockfiles are the actual pin** for application dependencies. For the frontend,
   that is still `package-lock.json`, which does not exist yet (no frontend
   dependency has been installed). For the backend, `uv` is not installed (see
   "No lockfile tool (`uv`) yet" below), so `uv.lock` does not exist either — but
   the backend is no longer dependency-free. Three distinct things, not one:
   `backend/pyproject.toml` is the authoritative **declaration** of what the
   backend directly depends on (currently `jsonschema`, SQLAlchemy, Alembic and psycopg, each an
   exact version, not a range, plus the test-only testcontainers); `backend/requirements-lock.txt` (a plain `pip freeze` from the
   isolated `backend/.venv`) is the actual **exact-version installation lock**
   used by the current install-and-test workflow, covering `jsonschema` and its
   transitive dependencies together, and is what is really installed, until
   `uv.lock` replaces it; and Python 3.14 **execution verification** of that
   installed set is a third, still-open thing neither file establishes on its
   own (see "Backend dependencies" above) — installed-and-pinned is not the same
   claim as executed-and-verified-on-3.14.
4. **CI third-party actions are pinned to a commit SHA**, not a tag (see
   `.github/workflows/ci.yml` and `docs/adr/0015-toolchain-baselines-and-pinning.md`).
5. **Re-verification is required, not optional**, at each milestone that first
   consumes one of these versions (M1 for Python, M4 for Docker images, M5 for Node
   in the frontend Dockerfile), against the official source directly — never against
   a cached or summarized value in this document, however recently it was refreshed.

## Other tools observed on this development machine (not pins, informational only)

Recorded only to document what M0's environment inspection found; **none of this
was installed by this task, and none of it is a project requirement**:

| Tool | Observed version |
| --- | --- |
| Git | 2.56.1.windows.1 |
| Docker | 29.9.3 |
| Docker Compose | v5.7.0 |
| System Python (not the project pin) | 3.15.2 |
| System Node.js (not the project pin) | v26.13.1 |
| npm (bundled with system Node) | 11.20.0 |

No `uv`, `pnpm`, or `psql` client was found on this machine. Their absence is not a
blocker for M0 (documentation only) and does not trigger an install in this
milestone.
