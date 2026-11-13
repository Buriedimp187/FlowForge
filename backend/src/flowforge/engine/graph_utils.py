"""Pure graph algorithms over a :class:`WorkflowDefinition`.

docs/architecture.md section 2.2 ("the graph is acyclic, and every node is
reachable from the trigger") and ADR-0007 (acyclic DAG). All functions here
are side-effect-free and operate only on the definition's own node/edge
lists, in definition order wherever order matters.
"""

from __future__ import annotations

from flowforge.domain.graph import WorkflowDefinition


def find_cycle(definition: WorkflowDefinition) -> tuple[str, ...] | None:
    """Return one cycle (as a tuple of node keys) if the graph has one,
    else None. Uses iterative DFS with an explicit stack so it never
    blows the recursion limit on a large workflow."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n.key: WHITE for n in definition.nodes}
    parent: dict[str, str | None] = {n.key: None for n in definition.nodes}

    for start in (n.key for n in definition.nodes):
        if color[start] != WHITE:
            continue
        stack = [(start, iter(e.target_key for e in definition.edges_from(start)))]
        color[start] = GRAY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child not in color:
                    continue  # dangling edge; reachability/edge validation reports this separately
                if color[child] == WHITE:
                    color[child] = GRAY
                    parent[child] = node
                    stack.append((child, iter(e.target_key for e in definition.edges_from(child))))
                    advanced = True
                    break
                if color[child] == GRAY:
                    # Found a back edge: reconstruct the cycle node -> ... -> child -> node.
                    cycle = [node]
                    cur = node
                    while cur != child:
                        cur = parent[cur]  # type: ignore[assignment]
                        cycle.append(cur)
                    cycle.reverse()
                    return tuple(cycle)
                # BLACK: already fully explored, ignore.
            if not advanced:
                color[node] = BLACK
                stack.pop()
    return None


def reachable_from(definition: WorkflowDefinition, start_key: str) -> frozenset[str]:
    """All node keys reachable from ``start_key`` (inclusive), via a plain
    BFS over outgoing edges."""
    seen = {start_key}
    queue = [start_key]
    while queue:
        current = queue.pop(0)
        for edge in definition.edges_from(current):
            if edge.target_key not in seen:
                seen.add(edge.target_key)
                queue.append(edge.target_key)
    return frozenset(seen)


def topological_order(definition: WorkflowDefinition) -> tuple[str, ...]:
    """A deterministic topological order over the node keys.

    Assumes the graph is acyclic (callers validate that separately via
    :func:`find_cycle`); a node with a cycle reachable from it will simply
    be omitted from the result rather than raising, since callers that
    care about cycles check for them explicitly first.

    Determinism: ties are broken by definition order (the order nodes
    appear in ``definition.nodes``), not by key sorting or set iteration,
    so the same definition always yields the same order.
    """
    order_index = {n.key: i for i, n in enumerate(definition.nodes)}
    indegree = {n.key: 0 for n in definition.nodes}
    for edge in definition.edges:
        if edge.target_key in indegree:
            indegree[edge.target_key] += 1

    ready = sorted(
        (key for key, deg in indegree.items() if deg == 0),
        key=lambda k: order_index[k],
    )
    result: list[str] = []
    remaining_indegree = dict(indegree)

    while ready:
        ready.sort(key=lambda k: order_index[k])
        current = ready.pop(0)
        result.append(current)
        for edge in definition.edges_from(current):
            if edge.target_key not in remaining_indegree:
                continue
            remaining_indegree[edge.target_key] -= 1
            if remaining_indegree[edge.target_key] == 0:
                ready.append(edge.target_key)

    return tuple(result)
