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

from flowforge.domain._immutable import deep_freeze, thaw
from flowforge.domain.graph import Edge, NodeDefinition, RetryPolicy, WorkflowDefinition
from flowforge.domain.items import Item
from flowforge.domain.node_run import NodeRun
from flowforge.domain.node_types import NodeTypeSpec
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


class _MutableThing:
    """A deliberately plain, mutable custom object -- not a dict, list,
    str, int, float, or bool, and not JSON-representable in any way."""

    def __init__(self) -> None:
        self.value = 1


class StrictJsonValueBoundaryTestCase(unittest.TestCase):
    """M1 final-correction finding 2: deep_freeze() must REJECT anything
    outside the JSON value space, not silently pass it through
    unchanged as if it were already immutable."""

    def test_accepts_every_plain_json_type(self) -> None:
        value = {
            "n": None,
            "t": True,
            "f": False,
            "i": 42,
            "x": 3.5,
            "s": "hello",
            "arr": [1, "two", 3.0, None, True],
            "obj": {"nested": {"deep": [1, 2, 3]}},
        }
        frozen = deep_freeze(value)
        self.assertEqual(thaw(frozen), value)

    def test_bool_is_not_coerced_into_or_confused_with_int(self) -> None:
        frozen_true = deep_freeze(True)
        frozen_false = deep_freeze(False)
        self.assertIs(frozen_true, True)
        self.assertIs(frozen_false, False)
        self.assertIsInstance(frozen_true, bool)
        # A bool nested inside a list must round-trip as a bool, not as
        # the integers 1/0.
        frozen_list = deep_freeze([True, False, 1, 0])
        self.assertEqual([type(v) for v in frozen_list], [bool, bool, int, int])

    def test_rejects_set(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze({"tags": {"a", "b"}})

    def test_rejects_frozenset(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze(frozenset({"a", "b"}))

    def test_rejects_bytes(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze(b"raw-bytes")

    def test_rejects_bytearray(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze(bytearray(b"raw-bytes"))

    def test_rejects_custom_mutable_object(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze(_MutableThing())

    def test_rejects_custom_mutable_object_nested_inside_a_dict(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze({"thing": _MutableThing()})

    def test_rejects_custom_mutable_object_nested_inside_a_list(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze([1, 2, _MutableThing()])

    def test_rejects_non_string_mapping_key(self) -> None:
        with self.assertRaises(TypeError):
            deep_freeze({1: "a"})
        with self.assertRaises(TypeError):
            deep_freeze({(1, 2): "a"})
        with self.assertRaises(TypeError):
            deep_freeze({None: "a"})

    def test_rejects_nan(self) -> None:
        with self.assertRaises(ValueError):
            deep_freeze(math.nan)

    def test_rejects_positive_infinity(self) -> None:
        with self.assertRaises(ValueError):
            deep_freeze(math.inf)

    def test_rejects_negative_infinity(self) -> None:
        with self.assertRaises(ValueError):
            deep_freeze(-math.inf)

    def test_rejects_non_finite_number_nested_deep_inside_valid_structure(self) -> None:
        with self.assertRaises(ValueError):
            deep_freeze({"a": [1, 2, {"b": math.nan}]})

    def test_item_construction_rejects_a_set_value(self) -> None:
        with self.assertRaises(TypeError):
            Item(json={"tags": {"a", "b"}})

    def test_item_construction_rejects_bytes(self) -> None:
        with self.assertRaises(TypeError):
            Item(json={"payload": b"raw"})

    def test_item_construction_rejects_non_finite_number(self) -> None:
        with self.assertRaises(ValueError):
            Item(json={"score": math.inf})

    def test_node_definition_construction_rejects_a_custom_object_in_params(self) -> None:
        with self.assertRaises(TypeError):
            NodeDefinition(key="n1", type="set", type_version=1, params={"thing": _MutableThing()})

    def test_thaw_round_trips_every_plain_json_type(self) -> None:
        value = {"a": [1, 2.5, "x", None, True, {"b": [False]}]}
        self.assertEqual(thaw(deep_freeze(value)), value)

    def test_thaw_output_is_mutable_and_independent_of_the_frozen_source(self) -> None:
        frozen = deep_freeze({"a": [1, 2, 3]})
        plain = thaw(frozen)
        plain["a"].append(4)
        plain["b"] = "new"
        # The frozen structure (and a second thaw() of it) must be
        # unaffected by mutating the thawed copy.
        self.assertEqual(thaw(frozen), {"a": [1, 2, 3]})


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
    def test_list_inputs_outputs_credential_types_normalized_to_tuple(self) -> None:
        spec = NodeTypeSpec(
            type="demo",
            version=1,
            is_trigger=False,
            inputs=[InputPort("main", required=True)],  # type: ignore[arg-type]
            outputs=[OutputPort("main")],  # type: ignore[arg-type]
            params_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            credential_types=["http_bearer"],  # type: ignore[arg-type]
        )
        self.assertIsInstance(spec.inputs, tuple)
        self.assertIsInstance(spec.outputs, tuple)
        self.assertIsInstance(spec.credential_types, tuple)

    def test_mutating_original_list_after_construction_has_no_effect(self) -> None:
        inputs_list = [InputPort("main", required=True)]
        spec = NodeTypeSpec(type="demo", version=1, is_trigger=False, inputs=inputs_list)  # type: ignore[arg-type]
        inputs_list.append(InputPort("extra", required=False))
        self.assertEqual(len(spec.inputs), 1)

    def test_params_schema_is_deep_frozen(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        spec = NodeTypeSpec(type="demo", version=1, is_trigger=False, params_schema=schema)
        schema["properties"]["x"]["type"] = "number"  # mutate the caller's original
        self.assertEqual(spec.params_schema["properties"]["x"]["type"], "string")
        with self.assertRaises(TypeError):
            spec.params_schema["properties"]["x"]["type"] = "boolean"  # type: ignore[index]


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

    def test_node_definition_rejects_non_finite_timeout(self) -> None:
        # M1 final-correction finding 5: `math.inf > 0` is True, so a
        # prior version of this check silently accepted an infinite
        # timeout.
        for bad in (math.inf, -math.inf, math.nan):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    NodeDefinition(key="n1", type="set", type_version=1, timeout_s=bad)

    def test_node_definition_rejects_boolean_timeout(self) -> None:
        # isinstance(True, int) is True, and True > 0 -- a bool must be
        # rejected explicitly, not accepted as "timeout_s=1".
        with self.assertRaises(TypeError):
            NodeDefinition(key="n1", type="set", type_version=1, timeout_s=True)

    def test_node_definition_accepts_a_small_positive_finite_timeout(self) -> None:
        node = NodeDefinition(key="n1", type="set", type_version=1, timeout_s=0.001)
        self.assertEqual(node.timeout_s, 0.001)

    def test_workflow_definition_rejects_non_positive_schema_version(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowDefinition(schema_version=0, nodes=(), edges=())

    def test_node_type_spec_rejects_non_positive_version(self) -> None:
        with self.assertRaises(ValueError):
            NodeTypeSpec(type="demo", version=0, is_trigger=False)

    def test_node_type_spec_rejects_a_non_mapping_params_schema(self) -> None:
        # The former ParamSpec-based "duplicate parameter name" check no
        # longer applies -- a JSON Schema's `properties` is itself a
        # mapping, which cannot have duplicate keys in Python. The
        # equivalent narrow structural invariant now is simply that
        # params_schema, when given, must be a mapping at all.
        with self.assertRaises(TypeError):
            NodeTypeSpec(type="demo", version=1, is_trigger=False, params_schema="not-a-mapping")  # type: ignore[arg-type]

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

    def test_node_run_rejects_non_finite_retry_at(self) -> None:
        for bad in (math.inf, -math.inf, math.nan):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    NodeRun(run_id="r1", node_key="n1", status=NodeRunStatus.RETRY_WAIT, retry_at=bad)

    def test_retry_policy_rejects_boolean_max_attempts(self) -> None:
        # M1 final-correction finding 5: isinstance(True, int) is True
        # and True < 1 is False, so a prior check would have silently
        # accepted max_attempts=True as max_attempts=1.
        with self.assertRaises(TypeError):
            RetryPolicy(max_attempts=True)

    def test_retry_policy_rejects_float_max_attempts(self) -> None:
        with self.assertRaises(TypeError):
            RetryPolicy(max_attempts=3.5)

    def test_retry_policy_rejects_boolean_backoff_fields(self) -> None:
        with self.assertRaises(TypeError):
            RetryPolicy(backoff_base_s=True)
        with self.assertRaises(TypeError):
            RetryPolicy(backoff_multiplier=True)
        with self.assertRaises(TypeError):
            RetryPolicy(backoff_max_s=True)

    def test_retry_policy_valid_boundary_values(self) -> None:
        # max_attempts=1 (the minimum), multiplier=1 (the minimum,
        # meaning constant backoff) must both be accepted.
        policy = RetryPolicy(max_attempts=1, backoff_base_s=0.0, backoff_multiplier=1.0, backoff_max_s=0.0)
        self.assertEqual(policy.max_attempts, 1)

    def test_retry_policy_rejects_max_attempts_zero(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)

    def test_retry_policy_rejects_multiplier_below_one(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(backoff_multiplier=0.5)


if __name__ == "__main__":
    unittest.main()
