"""JSON Schema Draft 2020-12 validation of node parameters.

docs/architecture.md section 5 ("params_schema... JSON Schema") and
section 2.2 ("Node parameters validate against that node type's JSON
Schema"). This is the user-approved replacement for the earlier,
explicitly-non-JSON-Schema ``ParamSpec`` mini-validator.

This module is the ONE place in the codebase that imports the
third-party `jsonschema` library and the ONE place that knows the exact
validator class used. The selected draft is **Draft 2020-12**,
explicit, via ``jsonschema.Draft202012Validator`` -- never a bare
``jsonschema.validate()`` call, which would let the library guess the
draft from a schema's own ``$schema`` keyword (or silently default to
an older draft when that keyword is absent). See
docs/toolchain-versions.md for the exact dependency version and the
Python-3.14 compatibility-evidence discussion, and ADR-0005 for why
this module, not ``flowforge.domain``, owns the dependency.

Error strings returned by this module are plain, stable text built from
``ValidationError.path`` and ``ValidationError.message`` -- never a
repr() of the exception object itself, which is the library's own
unstable internal representation and must not leak to callers.
"""

from __future__ import annotations

from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from flowforge.domain._immutable import thaw


def check_node_type_schema(schema: Mapping[str, Any]) -> None:
    """Validate that ``schema`` is itself a well-formed Draft 2020-12
    JSON Schema document.

    ``schema`` may be a frozen (``MappingProxyType``-based) structure --
    it is converted to plain, mutable ``dict``/``list`` values via
    :func:`flowforge.domain._immutable.thaw` first, since `jsonschema`'s
    default type checker does not recognize ``MappingProxyType`` as a
    mapping (``isinstance(MappingProxyType(...), dict)`` is ``False``).
    ``thaw`` allocates an entirely new structure, so the original frozen
    schema is never mutated and nothing internal to it is exposed by
    reference.

    Raises ``ValueError`` (never a raw `jsonschema` exception type) if
    the schema itself is invalid.
    """
    try:
        Draft202012Validator.check_schema(thaw(schema))
    except SchemaError as exc:
        raise ValueError(f"invalid JSON Schema (Draft 2020-12): {exc.message}") from exc


def validate_params(schema: Mapping[str, Any] | None, params: Mapping[str, Any]) -> list[str]:
    """Validate ``params`` against ``schema`` (assumed already checked
    well-formed via :func:`check_node_type_schema` -- this function does
    not re-check the schema itself, only the instance).

    Returns a deterministic list of human-readable error strings (empty
    if valid, or if ``schema`` is ``None`` -- a node type that declares
    no schema at all accepts any parameters; see
    ``NodeTypeSpec.params_schema``'s docstring for why that is the
    chosen default rather than an implicit "reject everything").

    Both ``schema`` and ``params`` are thawed to plain structures before
    being handed to the validator, for the same reason as
    :func:`check_node_type_schema`.
    """
    if schema is None:
        return []

    validator = Draft202012Validator(thaw(schema))
    plain_params = thaw(params)

    errors = sorted(
        validator.iter_errors(plain_params),
        key=lambda e: ([str(p) for p in e.path], str(e.validator), e.message),
    )
    return [_format_error(e) for e in errors]


def _format_error(error: Any) -> str:
    """Render one ``jsonschema.exceptions.ValidationError`` as a plain,
    stable string: ``"<json-pointer-like path>: <message>"``. Never the
    exception's own ``repr()`` or ``str()`` (the task's "avoid exposing
    unstable internal exception representations" requirement) -- only
    the two documented-stable public attributes, ``path`` and
    ``message``.
    """
    location = "/".join(str(p) for p in error.path) or "<root>"
    return f"{location}: {error.message}"
