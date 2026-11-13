"""Deep-immutability regression tests (M1 audit finding 4).

Each test exercises BOTH failure modes the audit identified:
(1) mutating the caller's original nested input after construction must
    not affect the domain object, and
(2) mutating a nested value reached THROUGH the domain object must be
    impossible (a shallow MappingProxyType only blocked (1) at the top
    level and did nothing for either failure mode on nested structures).
"""

from __future__ import annotations

import math
import unittest

from flowforge.domain.graph import Edge, NodeDefinition, RetryPolicy, WorkflowDefinition
from flowforge.domain.items import Item
from flowforge.domain.node_run import NodeRun
from flowforge.domain.node_types import NodeTypeSpec, ParamSpec
from flowforge.domain.ports import InputPort, OutputPort
from flowforge.domain.states import NodeRunStatus


class ItemImmutabilityTestCase(unittest.TestCase):
    def test_mutating_original_source_dict_after_construction_has_no_effect(self) -> None:
        source = {"a": {"nested": [1, 2, 3]}}
        item = Item(json=source)
        source["a"]["nested"].append(999)
        source["b"] = "new"
        self.assertEqual(item.json["a"]["nested"], (1, 2, 3))
        self.assertNotIn("b", item.json)

    def test_nested_dict_through_the_item_is_read_only(self) -> None:
        item = Item(json={"a": {"b": 1}})
        with self.assertRaises(TypeError):
            item.json["a"]["c"] = 2  # type: ignore[index]

    def test_nested_list_through_the_item_becomes_an_immutable_tuple(self) -> None:
        item = Item(json={"values": [1, 2, 3]})
        self.assertEqual(item.json["values"], (1, 2, 3))
        with self.assertRaises(AttributeError):
            item.json["values"].append(4)  # type: ignore[attr-defined]

    def test_list_of_dicts_is_frozen_at_every_level(self) -> None:
        item = Item(json={"rows": [{"x": 1}, {"x": 2}]})
        with self.assertRaises(TypeError):
            item.json["rows"][0]["x"] = 999  # type: ignore[index]


class NodeDefinitionImmutabilityTestCase(unittest.TestCase):
    def test_mutating_original_params_dict_after_construction_has_no_effect(self) -> None:
        params = {"filters": {"tags": ["a", "b"]}}
        node = NodeDefinition(key="n1", type="set", type_version=1, params=params)
        params["filters"]["tags"].append("c")
        self.assertEqual(node.params["filters"]["tags"], ("a", "b"))

    def test_nested_params_structure_is_read_only_through_the_node(self) -> None:
        node = NodeDefinition(key="n1", type="set", type_version=1, params={"filters": {"tags": ["a"]}})
        with self.assertRaises(TypeError):
            node.params["filters"]["tags"] = []  # type: ignore[index]


class WorkflowDefinitionImmutabilityTestCase(unittest.TestCase):
    def test_list_input_for_nodes_and_edges_is_normalized_to_tuple(self) -> None:
        nodes_list = [NodeDefinition(key="a", type="manual_trigger", type_version=1)]
        edges_list: list[Edge] = []
        definition = WorkflowDefinition(schema_version=1, nodes=nodes_list, edges=edges_list)  # type: ignore[arg-type]
        self.assertIsInstance(definition.nodes, tuple)
        self.assertIsInstance(definition.edges, tuple)

    def test_mutating_original_list_after_construction_has_no_effect(self) -> None:
        nodes_list = [NodeDefinition(key="a", type="manual_trigger", type_version=1)]
        definition = WorkflowDefinition(schema_version=1, nodes=nodes_list, edges=[])  # type: ignore[arg-type]
        nodes_list.append(NodeDefinition(key="b", type="set", type_version=1))
        self.assertEqual(len(definition.nodes), 1)


class NodeRunOutputImmutabilityTestCase(unittest.TestCase):
    def test_mutating_original_output_mapping_after_construction_has_no_effect(self) -> None:
        output = {"main": [Item(json={"n": 1})]}
        run = NodeRun(run_id="r1", node_key="n1", status=NodeRunStatus.SUCCEEDED, output=output)
        output["main"].append(Item(json={"n": 2}))
        output["error"] = []
        self.assertEqual(len(run.output["main"]), 1)
        self.assertNotIn("error", run.output)

    def test_output_mapping_is_read_only_through_the_node_run(self) -> None:
        run = NodeRun(run_id="r1", node_key="n1", status=NodeRunStatus.SUCCEEDED, output={"main": ()})
        with self.assertRaises(TypeError):
            run.output["main"] = (Item(json={}),)  # type: ignore[index]

    def test_per_port_item_list_becomes_an_immutable_tuple(self) -> None:
        run = NodeRun(run_id="r1", node_key="n1", status=NodeRunStatus.SUCCEEDED, output={"main": [Item(json={})]})
        self.assertIsInstance(run.output["main"], tuple)
        with self.assertRaises(AttributeError):
            run.output["main"].append(Item(json={}))  # type: ignore[attr-defined]

    def test_item_results_list_input_normalized_to_tuple(self) -> None:
        from flowforge.domain.node_run import ItemResult

        results_list = [ItemResult(index=0, status="succeeded")]
        run = NodeRun(
            run_id="r1",
            node_key="n1",
            status=NodeRunStatus.SUCCEEDED,
            item_results=results_list,  # type: ignore[arg-type]
        )
        self.assertIsInstance(run.item_results, tuple)
        results_list.append(ItemResult(index=1, status="succeeded"))
        self.assertEqual(len(run.item_results), 1)


class NodeTypeSpecImmutabilityTestCase(unittest.TestCase):
    def test_list_inputs_outputs_params_normalized_to_tuple(self) -> None:
        spec = NodeTypeSpec(
            type="demo",
            version=1,
            is_trigger=False,
            inputs=[InputPort("main", required=True)],  # type: ignore[arg-type]
            outputs=[OutputPort("main")],  # type: ignore[arg-type]
            params=[ParamSpec("x", "string")],  # type: ignore[arg-type]
            credential_types=["http_bearer"],  # type: ignore[arg-type]
        )
        self.assertIsInstance(spec.inputs, tuple)
        self.assertIsInstance(spec.outputs, tuple)
        self.assertIsInstance(spec.params, tuple)
        self.assertIsInstance(spec.credential_types, tuple)

    def test_mutating_original_list_after_construction_has_no_effect(self) -> None:
        inputs_list = [InputPort("main", required=True)]
        spec = NodeTypeSpec(type="demo", version=1, is_trigger=False, inputs=inputs_list)  # type: ignore[arg-type]
        inputs_list.append(InputPort("extra", required=False))
        self.assertEqual(len(spec.inputs), 1)


class GeneralInvariantTestCase(unittest.TestCase):
    """M1 audit finding 7: narrow, objective constructor invariants."""

    def test_node_definition_rejects_non_positive_type_version(self) -> None:
        with self.assertRaises(ValueError):
            NodeDefinition(key="n1", type="set", type_version=0)

    def test_node_definition_rejects_non_positive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            NodeDefinition(key="n1", type="set", type_version=1, timeout_s=0)
        with self.assertRaises(ValueError):
            NodeDefinition(key="n1", type="set", type_version=1, timeout_s=-5)

    def test_workflow_definition_rejects_non_positive_schema_version(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowDefinition(schema_version=0, nodes=(), edges=())

    def test_node_type_spec_rejects_non_positive_version(self) -> None:
        with self.assertRaises(ValueError):
            NodeTypeSpec(type="demo", version=0, is_trigger=False)

    def test_node_type_spec_rejects_duplicate_parameter_names(self) -> None:
        with self.assertRaises(ValueError):
            NodeTypeSpec(
                type="demo",
                version=1,
                is_trigger=False,
                params=(ParamSpec("x", "string"), ParamSpec("x", "number")),
            )

    def test_retry_policy_rejects_non_finite_backoff_values(self) -> None:
        for bad in (math.inf, -math.inf, math.nan):
            with self.subTest(field="backoff_base_s", value=bad):
                with self.assertRaises(ValueError):
                    RetryPolicy(backoff_base_s=bad)
            with self.subTest(field="backoff_multiplier", value=bad):
                with self.assertRaises(ValueError):
                    RetryPolicy(backoff_multiplier=bad)
            with self.subTest(field="backoff_max_s", value=bad):
                with self.assertRaises(ValueError):
                    RetryPolicy(backoff_max_s=bad)

    def test_node_run_rejects_negative_retry_at(self) -> None:
        with self.assertRaises(ValueError):
            NodeRun(run_id="r1", node_key="n1", status=NodeRunStatus.RETRY_WAIT, retry_at=-1.0)


if __name__ == "__main__":
    unittest.main()
