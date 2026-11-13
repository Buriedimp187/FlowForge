"""Direct unit tests for the domain primitives and pure graph algorithms
that back the validator/readiness tests but are worth exercising on their
own terms (constructors with real invariants, not just "does it build")."""

from __future__ import annotations

import unittest

from flowforge.domain.items import Item, items_from
from flowforge.domain.node_run import NodeRun, start_attempt
from flowforge.domain.ports import InputPort, OutputPort
from flowforge.domain.states import NodeRunStatus
from flowforge.engine.graph_utils import find_cycle, reachable_from, topological_order
from tests.unit.support import branching_merge_workflow, edge, linear_workflow, set_node, trigger_node
from flowforge.domain.graph import WorkflowDefinition


class ItemTestCase(unittest.TestCase):
    def test_item_json_is_read_only(self) -> None:
        item = Item(json={"a": 1})
        with self.assertRaises(TypeError):
            item.json["a"] = 2  # type: ignore[index]

    def test_items_from_preserves_order(self) -> None:
        items = items_from({"n": 1}, {"n": 2}, {"n": 3})
        self.assertEqual([i.json["n"] for i in items], [1, 2, 3])


class PortTestCase(unittest.TestCase):
    def test_empty_port_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            InputPort(name="")
        with self.assertRaises(ValueError):
            OutputPort(name="")


class NodeRunTestCase(unittest.TestCase):
    def test_emitted_on_distinguishes_present_empty_from_absent(self) -> None:
        run = NodeRun(run_id="r", node_key="n", status=NodeRunStatus.SUCCEEDED, output={"main": ()})
        self.assertTrue(run.emitted_on("main"))  # present, even though empty
        self.assertFalse(run.emitted_on("error"))  # absent entirely

    def test_start_attempt_first_call_has_attempt_one_and_zero_failures(self) -> None:
        from flowforge.domain.effects import EffectClass

        run, drift = start_attempt(None, run_id="r", node_key="n", resolved_effect_class=EffectClass.PURE)
        self.assertEqual(run.attempt, 1)
        self.assertEqual(run.failures, 0)
        self.assertFalse(drift)
        self.assertEqual(run.status, NodeRunStatus.RUNNING)


class GraphUtilsTestCase(unittest.TestCase):
    def test_find_cycle_returns_none_for_acyclic_graph(self) -> None:
        self.assertIsNone(find_cycle(linear_workflow()))

    def test_find_cycle_detects_a_back_edge(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a"), set_node("b")),
            edges=(
                edge("e1", "trigger", "main", "a", "main"),
                edge("e2", "a", "main", "b", "main"),
                edge("e3", "b", "main", "a", "main"),
            ),
        )
        cycle = find_cycle(definition)
        self.assertIsNotNone(cycle)
        self.assertIn("a", cycle)
        self.assertIn("b", cycle)

    def test_reachable_from_source(self) -> None:
        definition = linear_workflow()
        reached = reachable_from(definition, "trigger")
        self.assertEqual(reached, frozenset({"trigger", "set_a", "set_b"}))

    def test_topological_order_respects_edges(self) -> None:
        order = topological_order(linear_workflow())
        self.assertEqual(order, ("trigger", "set_a", "set_b"))

    def test_topological_order_is_deterministic_across_calls(self) -> None:
        definition = branching_merge_workflow()
        first = topological_order(definition)
        second = topological_order(definition)
        self.assertEqual(first, second)
        # The trigger must come before everything it reaches.
        self.assertEqual(first[0], "trigger")
        self.assertLess(first.index("set_true"), first.index("joiner"))
        self.assertLess(first.index("set_false"), first.index("joiner"))


if __name__ == "__main__":
    unittest.main()
