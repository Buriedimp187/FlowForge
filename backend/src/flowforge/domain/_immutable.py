"""Shared helpers for freezing JSON-shaped data at domain-object
construction boundaries.

M1 audit correction: the original ``Item``/``NodeDefinition`` immutability
used a *shallow* ``MappingProxyType(dict(data))`` wrapper, which stops
reassigning the top-level mapping but does nothing to a nested ``dict`` or
``list`` value inside it -- so ``item.json["a"]["b"] = 1`` or
``item.json["a"].append(1)`` could still mutate state the domain object is
supposed to own immutably, and a caller's original input dict could still
be mutated *after* construction without affecting the frozen copy (that
part already worked), but mutating a value reachable *through* the frozen
object was never actually prevented.

This module freezes recursively, but only as far as JSON itself goes
(dict / list / str / int / float / bool / None) -- it is deliberately not
a generic "freeze anything" framework. :func:`thaw` is the exact inverse,
kept for the API/persistence boundary (docs/architecture.md section 2.3:
node outputs are JSONB; a future API layer needs plain, JSON-serializable
structures back out, not ``MappingProxyType``/``tuple`` trees).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping


def deep_freeze(value: Any) -> Any:
    """Recursively convert JSON-shaped ``value`` into an immutable
    equivalent: dict -> read-only mapping (itself built from
    already-frozen values), list/tuple -> tuple of frozen values,
    everything else returned as-is (str/int/float/bool/None are already
    immutable).

    Order is preserved: dict insertion order (Python dicts are ordered)
    and list/tuple element order both carry through unchanged, which
    matters for docs/architecture.md section 6.4's "fixed edge order"
    and similar determinism requirements elsewhere in the engine.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """The exact inverse of :func:`deep_freeze`: read-only mapping ->
    plain ``dict``, tuple -> plain ``list``, everything else unchanged.

    Intended for the future API/persistence boundary, where a plain,
    ``json.dumps``-able structure is needed back out of a frozen domain
    value (docs/architecture.md section 2.3: node outputs are stored as
    JSONB).
    """
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value
