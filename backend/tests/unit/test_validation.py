"""Save-time workflow validation tests.

Covers every scenario listed in the M1 task instructions, section 4
("Workflow validation").
"""

from __future__ import annotations

import unittest

from flowforge.domain.graph import Edge, NodeDefinition, WorkflowDefinition
from flowforge.engine.validator import validate
from tests.unit.support import (
    branching_merge_workflow,
    edge,
    if_node,
    linear_workflow,
    merge_node,
    new_catalog,
    new_credentials,
    new_expression_checker,
    set_node,
    trigger_node,
)


class ValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = new_catalog()
        self.credentials = new_credentials()
        self.expressions = new_expression_checker()

    def _validate(self, definition: WorkflowDefinition):
        return validate(
            definition,
            node_types=self.catalog,
            credentials=self.credentials,
            expressions=self.expressions,
        )

    def test_valid_linear_workflow(self) -> None:
        result = self._validate(linear_workflow())
        self.assertTrue(result.is_valid, result.errors)
        self.assertEqual(result.errors, ())

    def test_valid_branching_workflow(self) -> None:
        result = self._validate(branching_merge_workflow())
        self.assertTrue(result.is_valid, result.errors)

    def test_multiple_source_nodes(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node("t1"), trigger_node("t2"), set_node("s1")),
            edges=(edge("e1", "t1", "main", "s1", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("multiple_source_nodes", result.error_codes())

    def test_missing_source_node(self) -> None:
        # Every node has an incoming edge -> no source node at all.
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(set_node("s1"), set_node("s2")),
            edges=(
                edge("e1", "s1", "main", "s2", "main"),
                edge("e2", "s2", "main", "s1", "main"),
            ),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("no_source_node", result.error_codes())

    def test_cyclic_graph(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a"), set_node("b")),
            edges=(
                edge("e1", "trigger", "main", "a", "main"),
                edge("e2", "a", "main", "b", "main"),
                edge("e3", "b", "main", "a", "main"),  # back edge
            ),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("cycle_detected", result.error_codes())

    def test_unreachable_node(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("reachable"), set_node("orphan")),
            edges=(edge("e1", "trigger", "main", "reachable", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("unreachable_node", result.error_codes())
        unreachable_issues = [e for e in result.errors if e.code == "unreachable_node"]
        self.assertEqual(unreachable_issues[0].node_key, "orphan")

    def test_duplicate_node_keys_rejected_at_construction(self) -> None:
        # WorkflowDefinition itself enforces this invariant (domain layer),
        # so the validator never even sees a definition with duplicates.
        with self.assertRaises(ValueError):
            WorkflowDefinition(
                schema_version=1,
                nodes=(trigger_node("dup"), set_node("dup")),
                edges=(),
            )

    def test_invalid_output_port_on_edge(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a")),
            edges=(edge("e1", "trigger", "nonexistent_port", "a", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("edge_unknown_source_port", result.error_codes())

    def test_invalid_input_port_on_edge(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a")),
            edges=(edge("e1", "trigger", "main", "a", "nonexistent_port"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("edge_unknown_target_port", result.error_codes())

    def test_invalid_edge_unknown_nodes(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(),),
            edges=(edge("e1", "trigger", "main", "does_not_exist", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("edge_unknown_target_node", result.error_codes())

    def test_unknown_node_type_rejected(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), NodeDefinition(key="mystery", type="no_such_type", type_version=1)),
            edges=(edge("e1", "trigger", "main", "mystery", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("unknown_node_type", result.error_codes())

    def test_source_node_must_be_a_trigger(self) -> None:
        # "set" is a valid type but is not a trigger; as the sole node
        # with no incoming edge, it becomes the (invalid) source.
        definition = WorkflowDefinition(schema_version=1, nodes=(set_node("a"),), edges=())
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("source_node_not_a_trigger", result.error_codes())

    def test_invalid_parameters_missing_required(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(
                trigger_node(),
                NodeDefinition(key="h", type="http_request", type_version=1, params={}),
            ),
            edges=(edge("e1", "trigger", "main", "h", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("invalid_parameter", result.error_codes())

    def test_invalid_parameters_wrong_type(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(
                trigger_node(),
                NodeDefinition(key="h", type="http_request", type_version=1, params={"url": 12345}),
            ),
            edges=(edge("e1", "trigger", "main", "h", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("invalid_parameter", result.error_codes())

    def test_unknown_parameter_rejected(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(
                trigger_node(),
                NodeDefinition(
                    key="h",
                    type="http_request",
                    type_version=1,
                    params={"url": "https://example.test", "bogus": 1},
                ),
            ),
            edges=(edge("e1", "trigger", "main", "h", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("invalid_parameter", result.error_codes())

    def test_credential_reference_valid(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(
                trigger_node(),
                NodeDefinition(
                    key="h",
                    type="http_request",
                    type_version=1,
                    params={"url": "https://example.test"},
                    credential_id="cred-1",
                ),
            ),
            edges=(edge("e1", "trigger", "main", "h", "main"),),
        )
        result = self._validate(definition)
        self.assertTrue(result.is_valid, result.errors)

    def test_credential_reference_not_found(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(
                trigger_node(),
                NodeDefinition(
                    key="h",
                    type="http_request",
                    type_version=1,
                    params={"url": "https://example.test"},
                    credential_id="does-not-exist",
                ),
            ),
            edges=(edge("e1", "trigger", "main", "h", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("credential_not_found", result.error_codes())

    def test_credential_reference_on_node_that_does_not_accept_one(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), NodeDefinition(key="a", type="set", type_version=1, credential_id="cred-1")),
            edges=(edge("e1", "trigger", "main", "a", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("credential_not_applicable", result.error_codes())

    def test_expression_syntax_valid(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a", value="hello {{ trigger.body.name }}")),
            edges=(edge("e1", "trigger", "main", "a", "main"),),
        )
        result = self._validate(definition)
        self.assertTrue(result.is_valid, result.errors)

    def test_expression_syntax_invalid_unbalanced(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a", value="hello {{ trigger.body.name")),
            edges=(edge("e1", "trigger", "main", "a", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("invalid_expression_syntax", result.error_codes())

    def test_expression_syntax_invalid_empty_body(self) -> None:
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("a", value="{{ }}")),
            edges=(edge("e1", "trigger", "main", "a", "main"),),
        )
        result = self._validate(definition)
        self.assertFalse(result.is_valid)
        self.assertIn("invalid_expression_syntax", result.error_codes())

    def test_validation_is_deterministic(self) -> None:
        definition = branching_merge_workflow()
        first = self._validate(definition)
        second = self._validate(definition)
        self.assertEqual(first.error_codes(), second.error_codes())
        self.assertEqual(first.warning_codes(), second.warning_codes())


if __name__ == "__main__":
    unittest.main()
