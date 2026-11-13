"""Items: the data-passing unit between nodes.

docs/architecture.md section 2 ("items are `{ json: {...} }`") and section 5
("Items are `{ json: {...} }`; binary data is explicitly deferred").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _frozen_mapping(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only view over a shallow copy of ``data``.

    This does not deep-freeze nested structures (that would require
    recursively wrapping every list/dict, which is unnecessary complexity
    for M1). Callers must not mutate nested values after constructing an
    Item; this is a documented convention, not a runtime guarantee below
    the top level.
    """
    return MappingProxyType(dict(data))


@dataclass(frozen=True)
class Item:
    """A single item flowing between node ports.

    Binary data is explicitly out of scope for v0.1 (docs/mvp-scope.md), so
    an Item carries only a JSON-shaped payload.
    """

    json: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "json", _frozen_mapping(self.json))


# An ordered, immutable sequence of items. Order is significant: it is what
# makes per-item indexes (docs/architecture.md section 6.4) stable and
# deterministic.
ItemList = tuple[Item, ...]


def items_from(*payloads: Mapping[str, Any]) -> ItemList:
    """Convenience constructor: build an ItemList from raw JSON mappings."""
    return tuple(Item(json=p) for p in payloads)
