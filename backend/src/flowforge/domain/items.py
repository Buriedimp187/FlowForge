"""Items: the data-passing unit between nodes.

docs/architecture.md section 2 ("items are `{ json: {...} }`") and section 5
("Items are `{ json: {...} }`; binary data is explicitly deferred").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from flowforge.domain._immutable import deep_freeze


@dataclass(frozen=True)
class Item:
    """A single item flowing between node ports.

    Binary data is explicitly out of scope for v0.1 (docs/mvp-scope.md), so
    an Item carries only a JSON-shaped payload.

    ``json`` is deep-frozen at construction (M1 audit correction: a
    shallow ``MappingProxyType`` previously left nested dicts/lists
    mutable, both from the caller's original object after construction
    and through the Item itself). See flowforge.domain._immutable.
    """

    json: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "json", deep_freeze(dict(self.json)))


# An ordered, immutable sequence of items. Order is significant: it is what
# makes per-item indexes (docs/architecture.md section 6.4) stable and
# deterministic.
ItemList = tuple[Item, ...]


def items_from(*payloads: Mapping[str, Any]) -> ItemList:
    """Convenience constructor: build an ItemList from raw JSON mappings."""
    return tuple(Item(json=p) for p in payloads)
