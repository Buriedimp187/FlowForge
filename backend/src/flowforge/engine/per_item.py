"""Per-item execution semantics.

docs/architecture.md section 3.2 ("Iteration is the node's own per-item
execution mode plus a Split Out node") and section 6.4 (idempotency keys
derived from a stable ``item_index``).

This module provides the generic per-item *iteration* behavior only: it
takes the item-processing operation as an injected callable, since M1
explicitly does not implement any real node (HTTP, LLM, Set, ...) --
see docs/milestones.md, M3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from flowforge.domain.items import Item, ItemList
from flowforge.domain.node_run import ItemResult, NodeError


@dataclass(frozen=True)
class PerItemExecutionResult:
    """The outcome of running one node in per-item mode over one batch of
    items.

    ``results`` is always index-aligned with the input ``items`` up to
    the point execution stopped: ``results[i].index == i`` for every
    element present. When ``continue_on_item_error`` is False and an item
    fails, iteration stops and ``results`` is shorter than ``items`` --
    the association between each *attempted* item and its result is still
    exact; items never attempted simply have no entry.
    """

    results: tuple[ItemResult, ...]
    stopped_early: bool

    @property
    def any_failed(self) -> bool:
        return any(r.status == "failed" for r in self.results)

    @property
    def all_succeeded(self) -> bool:
        return len(self.results) > 0 and all(r.status == "succeeded" for r in self.results)


def run_per_item(
    items: ItemList,
    process: Callable[[int, Item], ItemResult],
    *,
    continue_on_item_error: bool,
) -> PerItemExecutionResult:
    """Iterate ``items`` in order, calling ``process(index, item)`` for
    each one.

    Ordering is exactly ``enumerate(items)`` order -- the same order every
    time for the same ``items`` tuple, which is what keeps per-item
    idempotency keys (section 6.4) and the index-to-item association
    stable across repeated calls (e.g. a retry re-invoking this function
    with the same, already-persisted ``items``).

    ``process`` must return an :class:`ItemResult` whose ``index`` equals
    the index it was called with; this is asserted defensively so a
    caller bug (e.g. returning the wrong index) fails loudly rather than
    silently corrupting the association docs/architecture.md section 6.4
    relies on.
    """
    results: list[ItemResult] = []
    stopped_early = False

    for index, item in enumerate(items):
        result = process(index, item)
        if result.index != index:
            raise ValueError(
                f"process() returned a result for index {result.index}, "
                f"but was invoked for index {index} -- per-item identity "
                f"must not be reassigned"
            )
        results.append(result)
        if result.status == "failed" and not continue_on_item_error:
            stopped_early = True
            break

    return PerItemExecutionResult(tuple(results), stopped_early)


def succeeded(index: int, output: ItemList) -> ItemResult:
    """Convenience constructor used by callers/tests building a `process`
    callable."""
    return ItemResult(index=index, status="succeeded", output=output)


def failed(index: int, code: str, message: str = "") -> ItemResult:
    """Convenience constructor for a failed item result."""
    return ItemResult(index=index, status="failed", error=NodeError(code=code, message=message))
