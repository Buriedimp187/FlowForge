"""Effect classes.

docs/architecture.md section 6.1 and ADR-0005. The classification of a node's
external effect is the single input that governs retry eligibility and
outcome handling throughout the engine (sections 6.2, 6.3, 7.3a).
"""

from __future__ import annotations

from enum import Enum


class EffectClass(str, Enum):
    """How a node's execution may affect external state.

    HTTP method or node "kind" never implies a class by itself
    (docs/architecture.md section 6.1: "HTTP classification is never
    derived from the method alone") -- a node type only ever *declares*
    one of these four values, explicitly.
    """

    PURE = "pure"
    READ = "read"
    IDEMPOTENT_WRITE = "idempotent_write"
    UNSAFE_WRITE = "unsafe_write"

    @property
    def is_write(self) -> bool:
        return self in (EffectClass.IDEMPOTENT_WRITE, EffectClass.UNSAFE_WRITE)
