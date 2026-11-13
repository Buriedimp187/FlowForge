"""Abstract ports the engine depends on, per ADR-0001.

None of these protocols is implemented against a real external system in
M1: PostgreSQL persistence is M2, the node registry and real credential
store are M3/M4. ``flowforge.engine.inmemory`` provides minimal,
test-oriented implementations of each -- "the smallest useful contract",
not the production subsystem.
"""

from __future__ import annotations

from typing import Protocol

from flowforge.domain.node_run import NodeRun
from flowforge.domain.node_types import NodeTypeSpec


class RunStore(Protocol):
    """Storage for :class:`~flowforge.domain.node_run.NodeRun` records.

    This is the smallest contract M1's pure engine needs: look up a
    node's record (or learn it is absent/"not started"), and write an
    updated record. It deliberately has NO run-level queue/lease/status
    operations -- those belong to the ``runs`` table and the worker
    (docs/architecture.md section 4), which is explicitly M2 scope.
    """

    def get(self, run_id: str, node_key: str) -> NodeRun | None: ...

    def put(self, node_run: NodeRun) -> None: ...

    def list_for_run(self, run_id: str) -> tuple[NodeRun, ...]: ...


class NodeTypeCatalog(Protocol):
    """Looks up a node type's declared contract.

    NOT the M3 node registry: no ``execute`` dispatch, no built-in nodes,
    just the data shape validation and readiness need.
    """

    def get(self, type_name: str, type_version: int) -> NodeTypeSpec | None: ...


class CredentialDirectory(Protocol):
    """Checks whether a referenced credential exists and has an expected
    type, without ever exposing the credential's value.

    NOT the M4 credential store (no encryption, no storage) -- a
    reference-existence check only, which is all save-time validation
    needs (docs/architecture.md section 2.2).
    """

    def exists(self, credential_id: str, expected_type: str) -> bool: ...


class ExpressionSyntaxChecker(Protocol):
    """Checks expression *syntax* only.

    NOT the M3 restricted expression engine (ADR-0008): no JMESPath
    evaluation, no whitelisted-function execution, no context resolution.
    Returns a list of human-readable syntax error strings (empty if the
    string contains no syntactically invalid `{{ }}` template).
    """

    def check(self, text: str) -> list[str]: ...
