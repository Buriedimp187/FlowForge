"""Regression tests for NodeRun state-transition validity (M1 audit
finding 3).

``start_attempt()`` previously allowed a terminal (or already-``running``)
NodeRun to be "restarted" unconditionally. docs/architecture.md section
3.1's state diagram permits exactly two edges into ``running``:
``(absent) -> running`` and ``retry_wait -> running``. Everything else is
illegal within a single run (a Replay is a new ``run_id``, and resuming a
crashed/leased node is M2's orphan-takeover mechanism, not this function).
"""

from __future__ import annotations

import dataclasses
import unittest

from flowforge.domain.effects import EffectClass
from flowforge.domain.node_run import NodeRun, start_attempt
from flowforge.domain.states import NodeRunStatus, is_terminal


class ValidTransitionsTestCase(unittest.TestCase):
    def test_valid_initial_attempt(self) -> None:
        run, drift = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.PURE)
        self.assertEqual(run.status, NodeRunStatus.RUNNING)
        self.assertEqual(run.attempt, 1)
        self.assertEqual(run.failures, 0)
        self.assertFalse(drift)

    def test_valid_retry_from_retry_wait(self) -> None:
        first, _ = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.READ)
        waiting = dataclasses.replace(first, status=NodeRunStatus.RETRY_WAIT, retry_at=10.0, failures=1)
        retried, drift = start_attempt(waiting, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.READ)
        self.assertEqual(retried.status, NodeRunStatus.RUNNING)
        self.assertEqual(retried.attempt, 2)
        self.assertIsNone(retried.retry_at)
        self.assertFalse(drift)

    def test_failure_counter_is_not_reset_or_corrupted_by_a_retry(self) -> None:
        first, _ = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.READ)
        waiting = dataclasses.replace(first, status=NodeRunStatus.RETRY_WAIT, retry_at=10.0, failures=3)
        retried, _ = start_attempt(waiting, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.READ)
        self.assertEqual(retried.failures, 3)  # unchanged
        self.assertEqual(retried.attempt, 2)  # attempt still advances

    def test_effect_class_drift_during_a_valid_retry(self) -> None:
        first, _ = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.READ)
        waiting = dataclasses.replace(first, status=NodeRunStatus.RETRY_WAIT, retry_at=10.0)
        retried, drift = start_attempt(
            waiting, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.UNSAFE_WRITE
        )
        self.assertTrue(drift)
        self.assertEqual(retried.effect_class, EffectClass.READ)  # persisted class wins, unchanged


class InvalidTransitionsTestCase(unittest.TestCase):
    def test_restarting_every_terminal_status_is_rejected(self) -> None:
        for status in (
            NodeRunStatus.SUCCEEDED,
            NodeRunStatus.FAILED,
            NodeRunStatus.SKIPPED,
            NodeRunStatus.CANCELLED,
        ):
            with self.subTest(status=status):
                self.assertTrue(is_terminal(status))  # sanity on the fixture itself
                terminal = NodeRun(
                    run_id="run-1",
                    node_key="n1",
                    status=status,
                    attempt=1,
                    effect_class=EffectClass.PURE,
                )
                with self.assertRaises(ValueError):
                    start_attempt(terminal, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.PURE)

    def test_restarting_a_running_record_is_rejected(self) -> None:
        running, _ = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.PURE)
        with self.assertRaises(ValueError):
            start_attempt(running, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.PURE)

    def test_mismatched_run_id_is_rejected(self) -> None:
        first, _ = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.PURE)
        waiting = dataclasses.replace(first, status=NodeRunStatus.RETRY_WAIT, retry_at=1.0)
        with self.assertRaises(ValueError):
            start_attempt(waiting, run_id="run-DIFFERENT", node_key="n1", resolved_effect_class=EffectClass.PURE)

    def test_mismatched_node_key_is_rejected(self) -> None:
        first, _ = start_attempt(None, run_id="run-1", node_key="n1", resolved_effect_class=EffectClass.PURE)
        waiting = dataclasses.replace(first, status=NodeRunStatus.RETRY_WAIT, retry_at=1.0)
        with self.assertRaises(ValueError):
            start_attempt(waiting, run_id="run-1", node_key="n-DIFFERENT", resolved_effect_class=EffectClass.PURE)


if __name__ == "__main__":
    unittest.main()
