"""Readiness, skip propagation, and terminal-state classification tests.

Covers every scenario listed in the M1 task instructions, section 4
("Readiness and state").
"""

from __future__ import annotations

import unittest

from flowforge.domain.graph import WorkflowDefinition
from flowforge.domain.node_run import NodeRun
from flowforge.domain.states import NodeRunStatus, is_terminal
from flowforge.engine.clock import FakeClock
from flowforge.engine.inmemory import InMemoryRunStore
from flowforge.engine.readiness import NodeReadiness, evaluate_readiness
from flowforge.engine.scheduler import plan_next_step
from tests.unit.support import (
    branching_merge_workflow,
    edge,
    linear_workflow,
    merge_node,
    merge_with_unconnected_required_port_workflow,
    new_catalog,
    set_node,
    trigger_node,
)


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


class RequiredPortReadinessRegressionTestCase(unittest.TestCase):
    """M1 audit correction regression tests: a required input port with
    NO connected edge at all must not be silently ignored just because
    some other, optional, connected port received data. Previously,
    `required_port_connected` only tracked required ports that had at
    least one incoming edge, so a node could become READY purely on an
    optional port's data while a declared-but-unconnected required port
    was never checked at all."""

    def setUp(self) -> None:
        self.catalog = new_catalog()
        self.clock = FakeClock()
        self.store = InMemoryRunStore()
        self.run_id = "run-1"

    def _ready(self, node_key: str, definition):
        return evaluate_readiness(node_key, definition, self.run_id, self.store, self.clock, self.catalog)

    def test_1_required_port_unconnected_optional_port_receives_data(self) -> None:
        # joiner.required_in has NO edge at all; joiner.optional_in does,
        # and its upstream (set_a) succeeds with data. Readiness must be
        # SKIPPED, never READY, because the required port can never be
        # satisfied.
        definition = merge_with_unconnected_required_port_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_a", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.SKIPPED)

    def test_2_required_port_connected_but_upstream_did_not_emit_on_it(self) -> None:
        # Both of Merge's ports are wired (branching_merge_workflow), but
        # the branch feeding the REQUIRED port is skipped, so
        # set_true never emits on "main" -- required_in is connected
        # but never satisfied. This duplicates
        # test_merge_skipped_when_only_required_input_unavailable above,
        # restated explicitly as an audit regression case.
        definition = branching_merge_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="branch", status=NodeRunStatus.SUCCEEDED, output={"false": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_true", status=NodeRunStatus.SKIPPED))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_false", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.SKIPPED)

    def test_3_all_required_satisfied_optional_port_absent_entirely(self) -> None:
        # required_in is wired and satisfied; optional_in has no edge at
        # all in this definition (not merely "didn't fire"). The node
        # must still be READY: an absent optional port is not a missing
        # required port.
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("set_a"), merge_node("joiner")),
            edges=(
                edge("e1", "trigger", "main", "set_a", "main"),
                edge("e2", "set_a", "main", "joiner", "required_in"),
            ),
        )
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_a", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.READY)

    def test_4_empty_but_explicitly_emitted_output_counts_as_received(self) -> None:
        # set_a succeeds with an explicit, empty item list on "main"
        # (as opposed to the key being absent from output entirely).
        # This must count as "received data" per
        # docs/architecture.md section 3.2 ("even if with zero items").
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(trigger_node(), set_node("set_a"), merge_node("joiner")),
            edges=(
                edge("e1", "trigger", "main", "set_a", "main"),
                edge("e2", "set_a", "main", "joiner", "required_in"),
            ),
        )
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        succeeded_empty = NodeRun(run_id=self.run_id, node_key="set_a", status=NodeRunStatus.SUCCEEDED, output={"main": ()})
        self.assertTrue(succeeded_empty.emitted_on("main"))  # sanity: present-but-empty
        self.store.put(succeeded_empty)
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.READY)

    def test_5_existing_branching_and_merge_scenarios_still_hold(self) -> None:
        # Re-assert the pre-existing, still-valid branching/merge
        # behavior under the corrected implementation.
        definition = branching_merge_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="branch", status=NodeRunStatus.SUCCEEDED, output={"true": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_true", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(NodeRun(run_id=self.run_id, node_key="set_false", status=NodeRunStatus.SKIPPED))
        # required_in satisfied via set_true; optional_in's branch was
        # skipped -- Merge must still be READY (optional, not required).
        self.assertEqual(self._ready("joiner", definition), NodeReadiness.READY)


if __name__ == "__main__":
    unittest.main()
