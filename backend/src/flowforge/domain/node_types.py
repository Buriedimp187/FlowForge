"""The data shape of a node *type* declaration.

This is deliberately pure data (ports, a JSON Schema document for
parameters, credential types, effect class) with NO ``execute``
implementation. The node registry that loads these for real built-in
nodes (HTTP, LLM, Set, IF, ...) is explicitly M3 scope
(docs/milestones.md); M1 only needs the *shape* a validator and the
readiness engine can check definitions against.

See flowforge.engine.ports.NodeTypeCatalog for the lookup interface, and
flowforge.engine.inmemory.InMemoryNodeTypeCatalog for a minimal in-memory
implementation used by tests -- not a production registry.

M1 final-correction history: an earlier pass here was a small custom
``ParamSpec`` type (required-key + primitive-type-tag checks only) and
was explicitly documented as NOT JSON Schema. Per the user-approved
decision to implement full JSON Schema Draft 2020-12 validation
(docs/architecture.md section 5), ``ParamSpec`` has been **removed** --
not kept alongside the real thing as a second, parallel, incompatible
validation system. A node type now declares ``params_schema`` directly:
a plain JSON Schema document (itself JSON-shaped data, so it is
deep-frozen the same way any other params/output value is). The actual
Draft 2020-12 validator is in ``flowforge.engine.param_schema`` --
intentionally in the ``engine`` layer, not here, so this ``domain``
module keeps the zero-third-party-dependency property the rest of
``flowforge.domain`` has (ADR-0001); only ``engine`` depends on the
`jsonschema` library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from flowforge.domain._immutable import deep_freeze
from flowforge.domain.effects import EffectClass
from flowforge.domain.ports import InputPort, OutputPort


@dataclass(frozen=True)
class NodeTypeSpec:
    """A node type's declared contract, as the validator and readiness
    engine need it. Mirrors docs/architecture.md section 5's ``NodeType``
    sketch, minus the ``execute`` callable.

    ``params_schema``, if given, is a JSON Schema (Draft 2020-12)
    document describing this node type's parameters. ``None`` means the
    node type declares no parameter schema at all, in which case
    parameter validation is skipped entirely (any params are accepted) --
    a node-type author who wants to reject unknown parameters declares
    ``"additionalProperties": False`` in their own schema, exactly as
    any JSON-Schema-based system would require; this module does not
    impose that as an implicit default. The schema itself is checked for
    well-formedness (``Draft202012Validator.check_schema``) when a type
    is registered with a real :class:`~flowforge.engine.ports.NodeTypeCatalog`
    implementation -- see ``flowforge.engine.inmemory.InMemoryNodeTypeCatalog.register``
    -- not here, to keep this module dependency-free.
    """

    type: str
    version: int
    is_trigger: bool
    inputs: tuple[InputPort, ...] = field(default_factory=tuple)
    outputs: tuple[OutputPort, ...] = field(default_factory=tuple)
    params_schema: Optional[Mapping[str, Any]] = None
    credential_types: tuple[str, ...] = field(default_factory=tuple)
    effect_class: EffectClass = EffectClass.PURE
    supports_per_item: bool = False

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("NodeTypeSpec.type must be non-empty")
        if self.version < 1:
            raise ValueError("NodeTypeSpec.version must be >= 1")

        # Defensive tuple normalization: a caller may pass a list for
        # any of these; coerce so the stored value is genuinely
        # immutable, not merely type-hinted as a tuple.
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "credential_types", tuple(self.credential_types))

        if self.params_schema is not None:
            if not isinstance(self.params_schema, Mapping):
                raise TypeError("NodeTypeSpec.params_schema must be a mapping (a JSON Schema document) or None")
            object.__setattr__(self, "params_schema", deep_freeze(dict(self.params_schema)))

        if self.is_trigger and self.inputs:
            raise ValueError("a trigger node type must declare no input ports")
        input_names = [p.name for p in self.inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError(f"duplicate input port names on type {self.type!r}")
        output_names = [p.name for p in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise ValueError(f"duplicate output port names on type {self.type!r}")

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
