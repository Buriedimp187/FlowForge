# FlowForge

**FlowForge is an open-source, self-hosted visual workflow automation platform with
AI integration.** It lets you connect nodes visually to integrate APIs, transform
data, call language models (bring your own key), and run the result as a reliable,
auditable automated workflow — on infrastructure you own.

## Status

**Pre-implementation.** FlowForge is currently in **M0 — repository and
architecture foundation**. No application code, database schema, or Docker
configuration exists yet. This repository currently contains:

- the complete, consolidated architecture specification (`docs/architecture.md`),
- the Architecture Decision Records behind it (`docs/adr/`),
- the MVP scope, milestone plan, and risk/release-gate register
  (`docs/mvp-scope.md`, `docs/milestones.md`, `docs/risks-and-release-gates.md`),
- verified toolchain version pins (`docs/toolchain-versions.md`), and
- a minimal CI skeleton that validates the documentation structure itself.

See `docs/milestones.md` for what comes next (M1: the pure execution engine) and
`docs/risks-and-release-gates.md` for what must be demonstrated before a v0.1
release — including two currently **unproven** technical assumptions (secure
outbound IP pinning with TLS hostname verification, and a sound dispatch-boundary
marker for retry safety) that are scheduled to be resolved, with documented
fail-closed fallbacks, at the start of milestone M3.

## What FlowForge is

- A **modular monolith**: one Python backend (run as an `api` role and a `worker`
  role from the same package) plus a Next.js/TypeScript frontend using React Flow
  for the visual editor. **PostgreSQL is the only required infrastructure
  service** — there is no Redis, no message broker, and no separate scheduler
  service.
- A workflow engine where the visual graph is a view model, converted to a
  backend-owned, backend-validated execution definition. Execution state is fully
  reconstructable from PostgreSQL, so a crashed worker's work is recovered, not
  lost or silently duplicated.
- **BYOK**: you supply your own API keys for whichever LLM provider you use (or
  point it at a local model server). FlowForge never bills you and never computes a
  monetary cost; it only ever reports what a provider itself returns.
- Built for a reliable execution engine first, not for the largest possible list of
  integrations. See `docs/architecture.md` §0 for the full set of engineering
  principles this project holds itself to.

## Distribution

Self-hosted only, installed locally or on infrastructure you control, via Docker
Compose (once the first Dockerfiles exist — see `docs/milestones.md`, M4). There is
no managed/SaaS hosting offering, and none is planned.

## License

MIT — see [LICENSE](LICENSE).

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The canonical, consolidated architecture specification |
| [docs/adr/](docs/adr/README.md) | Architecture Decision Records, one per major decision |
| [docs/mvp-scope.md](docs/mvp-scope.md) | What v0.1 includes, and what is explicitly deferred |
| [docs/milestones.md](docs/milestones.md) | The M0–M7 implementation plan |
| [docs/risks-and-release-gates.md](docs/risks-and-release-gates.md) | What must be demonstrated before release, and known open risks |
| [docs/toolchain-versions.md](docs/toolchain-versions.md) | Verified, sourced, pinned runtime/tool versions |
| [docs/message-catalog.md](docs/message-catalog.md) | Exact required wording for LLM usage/billing UI states |

## Contributing

FlowForge is not yet accepting feature contributions — the engine, queue, and
security design (M1–M3) are being built and verified before the editor UI, per the
milestone plan. Issues and discussion on the architecture itself are welcome.
