"""Shared helpers for freezing JSON-shaped data at domain-object
construction boundaries.

M1 final-correction note (second audit pass): the previous version of
this module froze whatever it was given -- a ``dict``/``list``/``tuple``
recursively, and anything else (a custom class instance, a ``set``, raw
``bytes``, a dict with a non-string key, a non-finite float) was passed
through **unchanged**, silently, as if it were already immutable. That is
false for any genuinely mutable custom object, and is also not a value
JSON can represent at all (``bytes``, ``set``, ``NaN``/``Infinity`` have
no JSON encoding; a non-string dict key cannot be a JSON object key).

:func:`deep_freeze` now enforces a strict JSON-value boundary and
**raises** ``TypeError``/``ValueError`` on anything outside it, rather
than accepting and silently mis-labeling it as frozen. This is
deliberately the exact value space JSON itself defines -- not a
general "freeze anything" framework:

    null | true | false | integer | finite-float | string
    | object-with-string-keys | array

``bool`` is checked *before* ``int`` everywhere in this module, because
``isinstance(True, int)`` is ``True`` in Python -- without that ordering
a boolean would be silently treated as (and could be miscounted as) a
plain integer.

:func:`thaw` is the exact inverse, used at the boundary to a library
(such as a JSON Schema validator) that expects ordinary, mutable
``dict``/``list`` values and does not recognize ``MappingProxyType`` as
a mapping. It returns a fresh plain structure; it never mutates, and
never exposes, the frozen original.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping


def deep_freeze(value: Any, *, _path: str = "$") -> Any:
    """Recursively convert a JSON-compatible ``value`` into its
    immutable equivalent, or raise if ``value`` (or anything nested
    inside it) is not JSON-compatible.

    Accepted: ``None``, ``bool``, ``int`` (excluding ``bool``, handled
    separately), a finite ``float``, ``str``, a ``dict``/``Mapping``
    whose keys are all ``str`` (recursively frozen to a read-only
    mapping), and a ``list``/``tuple`` (recursively frozen to a
    ``tuple``).

    Rejected (raises): ``set``/``frozenset``, ``bytes``/``bytearray``,
    a non-finite ``float`` (``NaN``, ``+inf``, ``-inf``), a ``Mapping``
    with any non-``str`` key, and any other object type (including a
    mutable custom class instance) -- there is no silent pass-through
    for anything this function does not explicitly recognize.

    ``_path`` is an internal JSON-pointer-like breadcrumb used only to
    make the raised error identify where in a nested structure the
    rejected value was found; callers should not pass it.
    """
    # bool first: isinstance(True, int) is True in Python, so this check
    # MUST precede the int check below, or every boolean would silently
    # be treated as (and accepted like) a plain integer.
    if isinstance(value, bool):
        return value
    if value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"non-finite float at {_path}: {value!r} has no JSON representation "
                f"(NaN/Infinity/-Infinity are rejected)"
            )
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"non-string mapping key at {_path}: {k!r} ({type(k).__name__}) -- "
                    f"a JSON object key must be a string"
                )
            frozen[k] = deep_freeze(v, _path=f"{_path}.{k}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v, _path=f"{_path}[{i}]") for i, v in enumerate(value))
    # Everything else -- set/frozenset, bytes/bytearray, a custom class
    # instance, a function, etc. -- is explicitly rejected. There is no
    # fallback "return value unchanged" branch.
    raise TypeError(
        f"unsupported value at {_path}: {value!r} ({type(value).__name__}) is not a "
        f"JSON-compatible type (expected None, bool, int, a finite float, str, a "
        f"string-keyed mapping, or a list/tuple)"
    )


def thaw(value: Any) -> Any:
    """The exact inverse of :func:`deep_freeze`: a read-only mapping
    becomes a plain ``dict``, a ``tuple`` becomes a plain ``list``,
    everything else is returned unchanged.

    Used at boundaries that need an ordinary, mutable, JSON-shaped
    structure back out of a frozen domain value -- e.g. a JSON Schema
    validator's default type checker, which does not recognize
    ``MappingProxyType`` as a mapping (see
    ``flowforge.engine.param_schema``), or a future API/persistence
    layer (docs/architecture.md section 2.3: node outputs are JSONB).

    This always allocates a fresh structure: the caller can mutate the
    result freely without that mutation reaching the frozen original,
    and nothing internal to the frozen value is exposed by reference.
    """
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value
