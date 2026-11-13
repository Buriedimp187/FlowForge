"""The data shape of a node *type* declaration.

This is deliberately pure data (ports, a minimal parameter schema,
credential types, effect class) with NO ``execute`` implementation. The
node registry that loads these for real built-in nodes (HTTP, LLM, Set,
IF, ...) is explicitly M3 scope (docs/milestones.md); M1 only needs the
*shape* a validator and the readiness engine can check definitions against.

See flowforge.engine.ports.NodeTypeCatalog for the lookup interface, and
flowforge.engine.inmemory.InMemoryNodeTypeCatalog for a minimal in-memory
implementation used by tests -- not a production registry.

M1 audit note on ``ParamSpec``/``validate_params`` (read before changing
either): docs/architecture.md section 5 describes a node type's
``params_schema`` as "JSON Schema". ``ParamSpec`` is explicitly NOT a
JSON Schema implementation and must never be described as one -- it
checks only required-key presence and a primitive type tag (`string` /
`number` / `integer` / `boolean` / `object` / `array`), nothing else
(no `enum`, `pattern`, `minimum`/`maximum`, nested object/array
sub-schemas, `oneOf`/`anyOf`, etc.). A real JSON Schema validator was
considered for this correction pass and deliberately NOT added: the
obvious library (`jsonschema` on PyPI) could not be verified compatible
with the project's selected Python 3.14 baseline (ADR-0015), because
Python 3.14 is not installed anywhere this work was done, and
installing a third-party package or a new Python version was out of
scope for this correction (and would violate the instruction not to
install anything globally or alter the environment without being asked).
Full JSON Schema validation therefore remains an explicitly reported
limitation, not a silently narrowed "completion" -- see the M1
audit-correction report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flowforge.domain.effects import EffectClass
from flowforge.domain.ports import InputPort, OutputPort


@dataclass(frozen=True)
class ParamSpec:
    """A minimal, intentionally small parameter-schema entry.

    This is a deliberate subset of what a real JSON Schema parameter
    definition would support (docs/architecture.md section 5: node forms
    are generated from "the node's JSON Schema"). M1 implements only
    ``type`` + ``required`` checks -- enough to make save-time validation
    meaningfully reject bad params without building a JSON Schema engine.
    """

    name: str
    type: str  # "string" | "number" | "integer" | "boolean" | "object" | "array"
    required: bool = False

    _ALLOWED_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array"})

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ParamSpec.name must be non-empty")
        if self.type not in self._ALLOWED_TYPES:
            raise ValueError(f"unsupported param type: {self.type!r}")


_PY_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, (list, tuple)),
}


@dataclass(frozen=True)
class NodeTypeSpec:
    """A node type's declared contract, as the validator and readiness
    engine need it. Mirrors docs/architecture.md section 5's ``NodeType``
    sketch, minus the ``execute`` callable."""

    type: str
    version: int
    is_trigger: bool
    inputs: tuple[InputPort, ...] = field(default_factory=tuple)
    outputs: tuple[OutputPort, ...] = field(default_factory=tuple)
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    credential_types: tuple[str, ...] = field(default_factory=tuple)
    effect_class: EffectClass = EffectClass.PURE
    supports_per_item: bool = False

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("NodeTypeSpec.type must be non-empty")
        if self.version < 1:
            raise ValueError("NodeTypeSpec.version must be >= 1")

        # Defensive tuple normalization (M1 audit correction): a caller
        # may pass a list for any of these; coerce so the stored value is
        # genuinely immutable, not merely type-hinted as a tuple, and so
        # mutating the caller's original list afterwards cannot reach
        # this object.
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "params", tuple(self.params))
        object.__setattr__(self, "credential_types", tuple(self.credential_types))

        if self.is_trigger and self.inputs:
            raise ValueError("a trigger node type must declare no input ports")
        input_names = [p.name for p in self.inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError(f"duplicate input port names on type {self.type!r}")
        output_names = [p.name for p in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise ValueError(f"duplicate output port names on type {self.type!r}")
        param_names = [p.name for p in self.params]
        if len(param_names) != len(set(param_names)):
            dupes = sorted({n for n in param_names if param_names.count(n) > 1})
            raise ValueError(f"duplicate parameter declarations on type {self.type!r}: {dupes}")

    def input_port(self, name: str) -> InputPort | None:
        for p in self.inputs:
            if p.name == name:
                return p
        return None

    def output_port(self, name: str) -> OutputPort | None:
        for p in self.outputs:
            if p.name == name:
                return p
        return None

    def validate_params(self, params: dict) -> list[str]:
        """Minimal schema check: required-key presence and primitive type
        matching. Returns a list of human-readable error strings (empty
        if valid). This is not a general JSON Schema validator."""
        errors: list[str] = []
        declared = {p.name: p for p in self.params}
        for name, spec in declared.items():
            if spec.required and name not in params:
                errors.append(f"missing required parameter '{name}'")
        for name, value in params.items():
            spec = declared.get(name)
            if spec is None:
                errors.append(f"unknown parameter '{name}'")
                continue
            check = _PY_TYPE_CHECKS[spec.type]
            if not check(value):
                errors.append(f"parameter '{name}' must be of type {spec.type}")
        return errors
