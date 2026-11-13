"""Node input/output ports.

docs/architecture.md section 5: "inputs[]: {name, required}" / "outputs[]: {name}".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InputPort:
    """A declared input port on a node type.

    ``required`` drives the skip rule in docs/architecture.md section 3.2:
    a node is skipped unless every *connected* required input port received
    data.
    """

    name: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("InputPort.name must be non-empty")


@dataclass(frozen=True)
class OutputPort:
    """A declared output port on a node type."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("OutputPort.name must be non-empty")
