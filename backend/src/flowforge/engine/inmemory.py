"""Minimal in-memory implementations of the engine's ports.

These exist for M1's own tests and as a reference for later milestones;
they are explicitly NOT the production subsystems docs/milestones.md
schedules for M2 (persistence), M3 (the node registry), or M4 (the
credential store). No PostgreSQL, Redis, SQLAlchemy, or Alembic is used or
implied anywhere in this module.
"""

from __future__ import annotations

import re

from flowforge.domain.node_run import NodeRun
from flowforge.domain.node_types import NodeTypeSpec
from flowforge.engine.param_schema import check_node_type_schema


class InMemoryRunStore:
    """A deterministic, process-local :class:`~flowforge.engine.ports.RunStore`.

    Keyed by ``(run_id, node_key)``. Not thread-safe, not persistent --
    exactly as much as M1's pure-engine tests need, and no more.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], NodeRun] = {}

    def get(self, run_id: str, node_key: str) -> NodeRun | None:
        return self._rows.get((run_id, node_key))

    def put(self, node_run: NodeRun) -> None:
        self._rows[(node_run.run_id, node_run.node_key)] = node_run

    def list_for_run(self, run_id: str) -> tuple[NodeRun, ...]:
        # Order is insertion order filtered by run_id: deterministic given
        # a deterministic caller, and never re-sorted by this store.
        return tuple(row for (rid, _key), row in self._rows.items() if rid == run_id)


class InMemoryNodeTypeCatalog:
    """A static, dict-backed :class:`~flowforge.engine.ports.NodeTypeCatalog`.

    Intended for tests to populate with a handful of fake node-type specs.
    This is not the M3 node registry: it has no plugin loading and no
    built-in HTTP/LLM/Set/IF implementations, only the data shape a
    validator checks against.
    """

    def __init__(self, specs: tuple[NodeTypeSpec, ...] = ()) -> None:
        self._specs: dict[tuple[str, int], NodeTypeSpec] = {}
        for s in specs:
            self.register(s)

    def register(self, spec: NodeTypeSpec) -> None:
        # Validate the node type's own declared parameter schema here,
        # at the point a type enters the catalog -- per the user-approved
        # decision to use real JSON Schema (Draft 2020-12), a node type's
        # author must be told immediately if their own schema is
        # malformed, not only when some future workflow happens to
        # reference it.
        if spec.params_schema is not None:
            check_node_type_schema(spec.params_schema)
        self._specs[(spec.type, spec.version)] = spec

    def get(self, type_name: str, type_version: int) -> NodeTypeSpec | None:
        return self._specs.get((type_name, type_version))


class InMemoryCredentialDirectory:
    """A static :class:`~flowforge.engine.ports.CredentialDirectory`.

    Tests register ``(credential_id -> type)`` pairs; no encryption, no
    storage of secret values -- this never holds anything but an id and a
    type string, which is all save-time validation needs.
    """

    def __init__(self, known: dict[str, str] | None = None) -> None:
        self._known: dict[str, str] = dict(known or {})

    def register(self, credential_id: str, credential_type: str) -> None:
        self._known[credential_id] = credential_type

    def exists(self, credential_id: str, expected_type: str) -> bool:
        return self._known.get(credential_id) == expected_type


class BalancedTemplateSyntaxChecker:
    """A minimal placeholder for the real expression engine (ADR-0008,
    M3). It checks ONLY that ``{{`` / ``}}`` delimiters are balanced and
    that no template body is empty or contains a nested delimiter -- it
    performs NO JMESPath parsing, NO function-whitelist checking, and NO
    context resolution. It exists solely to satisfy the M1 requirement
    that "expression syntax must be checked through an appropriate
    interface, without implementing the expression engine itself."
    """

    _TOKEN = re.compile(r"\{\{|\}\}")

    def check(self, text: str) -> list[str]:
        errors: list[str] = []
        depth = 0
        start_positions: list[int] = []
        for match in self._TOKEN.finditer(text):
            if match.group() == "{{":
                if depth > 0:
                    errors.append("nested '{{' before a matching '}}'")
                depth += 1
                start_positions.append(match.start())
            else:  # "}}"
                if depth == 0:
                    errors.append("unmatched '}}' with no preceding '{{'")
                else:
                    depth -= 1
                    opened_at = start_positions.pop()
                    body = text[opened_at + 2 : match.start()].strip()
                    if not body:
                        errors.append("empty expression body '{{ }}'")
        if depth > 0:
            errors.append("unmatched '{{' with no closing '}}'")
        return errors
