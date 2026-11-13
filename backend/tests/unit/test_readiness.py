"""Readiness, skip propagation, and terminal-state classification tests.

Covers every scenario listed in the M1 task instructions, section 4
("Readiness and state").
"""

from __future__ import annotations

import unittest

from flowforge.domain.node_run import NodeRun
from flowforge.domain.states import NodeRunStatus, is_terminal
from flowforge.engine.clock import FakeClock
from flowforge.engine.inmemory import InMemoryRunStore
from flowforge.engine.readiness import NodeReadiness, evaluate_readiness
from flowforge.engine.scheduler import plan_next_step
from tests.unit.support import branching_merge_workflow, linear_workflow, new_catalog


class ReadinessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = new_catalog()
        self.clock = FakeClock()
        self.store = InMemoryRunStore()
        self.run_id = "run-1"

    def _ready(self, node_key: str, definition):
        return evaluate_readiness(node_key, definition, self.run_id, self.store, self.clock, self.catalog)

    def test_source_node_readiness(self) -> None:
        definition = linear_workflow()
        self.assertEqual(self._ready("trigger", definition), NodeReadiness.READY)

    def test_readiness_after_upstream_success(self) -> None:
        definition = linear_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("set_a", definition), NodeReadiness.READY)

    def test_not_reached_before_upstream_resolves(self) -> None:
        definition = linear_workflow()
        self.assertEqual(self._ready("set_a", definition), NodeReadiness.NOT_REACHED)

    def test_readiness_after_upstream_failure_is_skipped(self) -> None:
        # Upstream FAILED with no output -> downstream's only connected
        # port never received data -> SKIPPED, not NOT_REACHED (upstream
        # is terminal) and not READY (no data arrived).
        definition = linear_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.FAILED))
        self.assertEqual(self._ready("set_a", definition), NodeReadiness.SKIPPED)

    def test_missing_required_input_is_skipped(self) -> None:
        definition = branching_merge_workflow()
        # Only the optional branch produced output; the required branch
        # (set_true -> joiner.required_in) never ran.
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="branch", status=NodeRunStatus.SUCCEEDED, output={"false": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_true", status=NodeRunStatus.SKIPPED))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_false", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.SKIPPED)

    def test_optional_input_does_not_block_readiness(self) -> None:
        definition = branching_merge_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="branch", status=NodeRunStatus.SUCCEEDED, output={"true": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_true", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_false", status=NodeRunStatus.SKIPPED))
        # required_in satisfied (set_true ran); optional_in did not fire
        # (set_false was skipped) -- Merge must still be READY.
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.READY)

    def test_merge_skipped_when_only_required_input_unavailable(self) -> None:
        definition = branching_merge_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="branch", status=NodeRunStatus.SUCCEEDED, output={"false": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_true", status=NodeRunStatus.SKIPPED))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_false", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.SKIPPED)

    def test_branching_with_one_skipped_branch_via_scheduler(self) -> None:
        definition = branching_merge_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="branch", status=NodeRunStatus.SUCCEEDED, output={"true": ()}))
        pass_result = plan_next_step(definition, self.run_id, self.store, self.clock, self.catalog)
        self.assertIn("set_true", pass_result.ready)
        self.assertIn("set_false", pass_result.newly_skipped)
        self.assertEqual(self.store.get(self.run_id, "set_false").status, NodeRunStatus.SKIPPED)

    def test_skip_propagation_cascades_in_one_pass(self) -> None:
        definition = linear_workflow()  # trigger -> set_a -> set_b
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.FAILED))
        pass_result = plan_next_step(definition, self.run_id, self.store, self.clock, self.catalog)
        # Both set_a and set_b must become skipped in this single call.
        self.assertIn("set_a", pass_result.newly_skipped)
        self.assertIn("set_b", pass_result.newly_skipped)
        self.assertEqual(self.store.get(self.run_id, "set_a").status, NodeRunStatus.SKIPPED)
        self.assertEqual(self.store.get(self.run_id, "set_b").status, NodeRunStatus.SKIPPED)

    def test_running_row_is_waiting(self) -> None:
        definition = linear_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.RUNNING))
        self.assertEqual(self._ready("trigger", definition), NodeReadiness.WAITING)

    def test_retry_wait_not_yet_due_is_waiting(self) -> None:
        definition = linear_workflow()
        self.store.put(
            NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.RETRY_WAIT, retry_at=100.0)
        )
        self.clock.set(50.0)
        self.assertEqual(self._ready("trigger", definition), NodeReadiness.WAITING)

    def test_retry_wait_due_is_ready(self) -> None:
        definition = linear_workflow()
        self.store.put(
            NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.RETRY_WAIT, retry_at=100.0)
        )
        self.clock.set(100.0)
        self.assertEqual(self._ready("trigger", definition), NodeReadiness.READY)

    def test_terminal_vs_non_terminal(self) -> None:
        for status in (NodeRunStatus.SUCCEEDED, NodeRunStatus.FAILED, NodeRunStatus.SKIPPED, NodeRunStatus.CANCELLED):
            self.assertTrue(is_terminal(status), status)
        for status in (NodeRunStatus.RUNNING, NodeRunStatus.RETRY_WAIT):
            self.assertFalse(is_terminal(status), status)

    def test_terminal_row_reports_terminal_readiness(self) -> None:
        definition = linear_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("trigger", definition), NodeReadiness.TERMINAL)


if __name__ == "__main__":
    unittest.main()
