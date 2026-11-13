# ADR-0008: Restricted expression language (no `eval`)

**Status:** Accepted

## Context

Workflows need a way to reference upstream data in node parameters (e.g.
`{{ nodes.http_1.output.items[0].json.id }}`). An unrestricted expression language
(Python/JavaScript `eval`) in a self-hosted worker process is a remote-code-execution
vector, since the worker holds decrypted credentials and database access.

## Decision

`{{ }}` templates containing **JMESPath** queries over a fixed context (`trigger`,
`input`, `item`, `nodes.<key>.output.<port>`, `run`), plus a small whitelisted
function set. No `eval`/`exec`, no attribute access beyond the fixed context, no I/O,
no imports. Credentials are never placed in the expression context. Expressions have
**no wall-clock or random functions** — `now` resolves to the run's persisted
creation time — which is required for deterministic idempotency keys (ADR-0005).
Expression length, evaluation depth, and output size are bounded. Resolution happens
in the engine before a node's `execute` is called.

## Consequences

Safe by construction, with a deliberate expressiveness ceiling (JMESPath has no
native arithmetic or string concatenation beyond the whitelisted helpers) and a
documented surprise (no "current wall-clock time" expression) that trades user
convenience for execution determinism.

## Related

`docs/architecture.md` §8; ADR-0005 (idempotency-key determinism);
`docs/risks-and-release-gates.md` (RB7, R9).
