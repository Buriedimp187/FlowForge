# ADR-0001: Modular monolith with three roles

**Status:** Accepted

## Context

FlowForge is built and maintained by a solo developer or small team, with a zero
initial infrastructure budget and a self-hosted, single-instance deployment target.
Microservices add operational surface (service discovery, inter-service network
policy, independent deployment pipelines) that this project cannot justify.

## Decision

One backend Python package, run as two roles (`api` and `worker`) from the same
image, plus a separate Next.js frontend. Layer boundaries inside the backend
(`domain`, `engine`, `expressions`, `nodes`, `ai`, `security`, `persistence`,
`services`, `api`, `worker`) are enforced by an import-linter contract in CI, not
merely by convention — once code exists, `engine` may depend only on `domain` and on
abstract ports, never on FastAPI or a live SQLAlchemy session.

## Consequences

One deployable unit, a single mental model, and a straightforward Docker Compose
topology. This requires discipline to keep the module boundaries honest without the
hard isolation microservices would impose for free; the import-linter check is how
that discipline is made structural rather than aspirational.

## Related

`docs/architecture.md` §1.1–§1.2.
