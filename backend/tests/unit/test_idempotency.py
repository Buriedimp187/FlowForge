"""Deterministic idempotency key tests.

Covers the "Idempotency" scenarios from the M1 task instructions, section 4.
"""

from __future__ import annotations

import unittest
import uuid

from flowforge.engine.idempotency import FLOWFORGE_NAMESPACE, item_key, node_key


class NamespaceTestCase(unittest.TestCase):
    def test_namespace_is_reproducible_from_a_public_formula(self) -> None:
        # Anyone can recompute this independently -- see the module
        # docstring in flowforge.engine.idempotency.
        expected = uuid.uuid5(uuid.NAMESPACE_DNS, "flowforge.dev")
        self.assertEqual(FLOWFORGE_NAMESPACE, expected)
        self.assertEqual(str(FLOWFORGE_NAMESPACE), "37263b35-e483-5cfc-a59c-8119406691d2")

    def test_namespace_is_a_valid_uuid5(self) -> None:
        self.assertEqual(FLOWFORGE_NAMESPACE.version, 5)


class NodeKeyTestCase(unittest.TestCase):
    def test_stable_run_node_key(self) -> None:
        k1 = node_key("run-1", "node-a")
        k2 = node_key("run-1", "node-a")
        self.assertEqual(k1, k2)

    def test_is_a_valid_uuid5(self) -> None:
        self.assertIsInstance(node_key("run-1", "node-a"), uuid.UUID)
        self.assertEqual(node_key("run-1", "node-a").version, 5)

    def test_different_runs_produce_distinct_keys(self) -> None:
        self.assertNotEqual(node_key("run-1", "node-a"), node_key("run-2", "node-a"))

    def test_different_nodes_produce_distinct_keys(self) -> None:
        self.assertNotEqual(node_key("run-1", "node-a"), node_key("run-1", "node-b"))

    def test_attempt_number_is_not_part_of_the_signature(self) -> None:
        # There is no `attempt` parameter at all -- this test documents
        # that fact: the function's signature cannot express an attempt
        # number, so it can never leak into the key by construction.
        import inspect

        sig = inspect.signature(node_key)
        self.assertNotIn("attempt", sig.parameters)


class ItemKeyTestCase(unittest.TestCase):
    def test_stable_per_item_key(self) -> None:
        k1 = item_key("run-1", "node-a", 0)
        k2 = item_key("run-1", "node-a", 0)
        self.assertEqual(k1, k2)

    def test_different_item_indexes_produce_distinct_keys(self) -> None:
        self.assertNotEqual(item_key("run-1", "node-a", 0), item_key("run-1", "node-a", 1))

    def test_different_nodes_same_index_produce_distinct_keys(self) -> None:
        self.assertNotEqual(item_key("run-1", "node-a", 0), item_key("run-1", "node-b", 0))

    def test_different_runs_same_index_produce_distinct_keys(self) -> None:
        self.assertNotEqual(item_key("run-1", "node-a", 0), item_key("run-2", "node-a", 0))

    def test_item_key_distinct_from_node_key(self) -> None:
        self.assertNotEqual(node_key("run-1", "node-a"), item_key("run-1", "node-a", 0))

    def test_negative_index_rejected(self) -> None:
        with self.assertRaises(ValueError):
            item_key("run-1", "node-a", -1)

    def test_attempt_number_is_not_part_of_the_signature(self) -> None:
        import inspect

        sig = inspect.signature(item_key)
        self.assertNotIn("attempt", sig.parameters)

    def test_same_logical_operation_across_many_simulated_retries(self) -> None:
        # Simulate 5 "attempts" at the same (run, node, item) -- the key
        # must be identical every single time.
        keys = {item_key("run-1", "node-a", 3) for _ in range(5)}
        self.assertEqual(len(keys), 1)


if __name__ == "__main__":
    unittest.main()
