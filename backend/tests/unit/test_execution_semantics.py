"""Per-item execution semantics and cancellation transition tests.

Covers the "Execution semantics" scenarios from the M1 task instructions,
section 4, plus the pure cancellation rules from section F.
"""

from __future__ import annotations

import unittest

from flowforge.domain.effects import EffectClass
from flowforge.domain.items import items_from
from flowforge.domain.node_run import start_attempt
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.engine.interruption import InterruptionCause, resolve_interruption
from flowforge.engine.per_item import failed, run_per_item, succeeded


class BatchModeTestCase(unittest.TestCase):
    def test_batch_mode_is_a_single_call_over_all_items(self) -> None:
        # "Batch" mode (as opposed to per-item) just means the node's
        # own `execute` receives the whole ItemList at once -- there is
        # no special engine machinery for it beyond NOT invoking the
        # per-item iterator. This test documents that distinction.
        items = items_from({"n": 1}, {"n": 2}, {"n": 3})
        calls = []

        def batch_execute(all_items):
            calls.append(tuple(i.json["n"] for i in all_items))
            return {"main": all_items}

        output = batch_execute(items)
        self.assertEqual(calls, [(1, 2, 3)])
        self.assertEqual(output["main"], items)


class PerItemModeTestCase(unittest.TestCase):
    def test_per_item_mode_processes_each_item_independently(self) -> None:
        items = items_from({"n": 1}, {"n": 2}, {"n": 3})

        def process(index, item):
            return succeeded(index, items_from({"doubled": item.json["n"] * 2}))

        result = run_per_item(items, process, continue_on_item_error=False)
        self.assertEqual(len(result.results), 3)
        self.assertTrue(result.all_succeeded)

    def test_stable_item_ordering(self) -> None:
        items = items_from(*({"n": i} for i in range(10)))
        seen_order = []

        def process(index, item):
            seen_order.append(index)
            return succeeded(index, items_from({}))

        run_per_item(items, process, continue_on_item_error=True)
        self.assertEqual(seen_order, list(range(10)))

    def test_item_level_failure_without_continuation_stops_iteration(self) -> None:
        items = items_from({"n": 1}, {"n": 2}, {"n": 3})

        def process(index, item):
            if index == 1:
                return failed(index, "boom")
            return succeeded(index, items_from({}))

        result = run_per_item(items, process, continue_on_item_error=False)
        self.assertTrue(result.stopped_early)
        self.assertEqual(len(result.results), 2)  # items 0 and 1 attempted; 2 never reached
        self.assertEqual(result.results[0].status, "succeeded")
        self.assertEqual(result.results[1].status, "failed")

    def test_item_level_failure_with_continuation_processes_all_items(self) -> None:
        items = items_from({"n": 1}, {"n": 2}, {"n": 3})

        def process(index, item):
            if index == 1:
                return failed(index, "boom")
            return succeeded(index, items_from({}))

        result = run_per_item(items, process, continue_on_item_error=True)
        self.assertFalse(result.stopped_early)
        self.assertEqual(len(result.results), 3)
        self.assertTrue(result.any_failed)
        statuses = [r.status for r in result.results]
        self.assertEqual(statuses, ["succeeded", "failed", "succeeded"])

    def test_result_index_matches_input_index(self) -> None:
        items = items_from({"n": 1}, {"n": 2})

        def process(index, item):
            return succeeded(index, items_from({}))

        result = run_per_item(items, process, continue_on_item_error=True)
        for i, r in enumerate(result.results):
            self.assertEqual(r.index, i)

    def test_process_returning_wrong_index_is_rejected(self) -> None:
        items = items_from({"n": 1}, {"n": 2})

        def broken_process(index, item):
            return succeeded(index + 1, items_from({}))  # wrong on purpose

        with self.assertRaises(ValueError):
            run_per_item(items, broken_process, continue_on_item_error=True)


class CancellationTransitionTestCase(unittest.TestCase):
    """docs/architecture.md section 6.2 restricted to the CANCEL cause --
    see flowforge.engine.interruption for why CRASH/DEADLINE are excluded.
    """

    def test_pure_cancel_is_known_and_cancelled(self) -> None:
        resolution = resolve_interruption(EffectClass.PURE, InterruptionCause.CANCEL)
        self.assertEqual(resolution.status, NodeRunStatus.CANCELLED)
        self.assertEqual(resolution.outcome, Outcome.KNOWN)
        self.assertTrue(resolution.auto_retryable)

    def test_read_cancel_is_known_and_cancelled(self) -> None:
        resolution = resolve_interruption(EffectClass.READ, InterruptionCause.CANCEL)
        self.assertEqual(resolution.status, NodeRunStatus.CANCELLED)
        self.assertEqual(resolution.outcome, Outcome.KNOWN)

    def test_idempotent_write_cancel_is_unknown_but_still_cancelled(self) -> None:
        resolution = resolve_interruption(EffectClass.IDEMPOTENT_WRITE, InterruptionCause.CANCEL)
        self.assertEqual(resolution.status, NodeRunStatus.CANCELLED)
        self.assertEqual(resolution.outcome, Outcome.UNKNOWN)
        self.assertTrue(resolution.auto_retryable)  # "retry allowed with the same key"

    def test_unsafe_write_cancel_not_dispatched_is_known_and_cancelled(self) -> None:
        resolution = resolve_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.CANCEL, dispatched=False)
        self.assertEqual(resolution.status, NodeRunStatus.CANCELLED)
        self.assertEqual(resolution.outcome, Outcome.KNOWN)
        self.assertTrue(resolution.auto_retryable)

    def test_unsafe_write_cancel_dispatched_is_unknown_and_failed_not_cancelled(self) -> None:
        # This is the central safety property of section F: an
        # interrupted unsafe write is NEVER silently labeled "cancelled"
        # (which would read as a known, safe outcome).
        resolution = resolve_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.CANCEL, dispatched=True)
        self.assertEqual(resolution.status, NodeRunStatus.FAILED)
        self.assertNotEqual(resolution.status, NodeRunStatus.CANCELLED)
        self.assertEqual(resolution.outcome, Outcome.UNKNOWN)
        self.assertFalse(resolution.auto_retryable)
        self.assertEqual(resolution.error_code, "outcome_unknown")

    def test_unsafe_write_cancel_unprovable_defaults_to_unknown(self) -> None:
        # dispatched=None ("cannot be disproven") must take the
        # conservative branch, identical to dispatched=True.
        resolution = resolve_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.CANCEL, dispatched=None)
        self.assertEqual(resolution.status, NodeRunStatus.FAILED)
        self.assertEqual(resolution.outcome, Outcome.UNKNOWN)
        self.assertFalse(resolution.auto_retryable)

    def test_unsafe_write_cancel_unknown_does_not_force_run_failure(self) -> None:
        # Per docs/architecture.md section 6.2: an outcome_unknown caused
        # by CANCEL ends the run as cancelled (not forced to failed) --
        # only CRASH/TIMEOUT force the run itself to fail.
        resolution = resolve_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.CANCEL, dispatched=True)
        self.assertFalse(resolution.forces_run_failure)

    def test_unsafe_write_timeout_unknown_forces_run_failure(self) -> None:
        resolution = resolve_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.NODE_TIMEOUT, dispatched=True)
        self.assertTrue(resolution.forces_run_failure)
        self.assertEqual(resolution.status, NodeRunStatus.FAILED)
        self.assertEqual(resolution.outcome, Outcome.UNKNOWN)


class EffectClassImmutabilityTestCase(unittest.TestCase):
    def test_effect_class_set_on_first_attempt(self) -> None:
        new_run, drift = start_attempt(
            None, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.READ
        )
        self.assertEqual(new_run.effect_class, EffectClass.READ)
        self.assertFalse(drift)
        self.assertEqual(new_run.attempt, 1)

    def test_effect_class_preserved_across_retries_even_if_reclassified(self) -> None:
        first, _ = start_attempt(None, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.READ)
        # Simulate a later attempt where the (hypothetical) application
        # code would now classify this node differently.
        second, drift = start_attempt(
            first, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.UNSAFE_WRITE
        )
        self.assertEqual(second.effect_class, EffectClass.READ)  # unchanged
        self.assertTrue(drift)
        self.assertEqual(second.attempt, 2)

    def test_effect_class_unchanged_when_consistent(self) -> None:
        first, _ = start_attempt(None, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.PURE)
        second, drift = start_attempt(first, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.PURE)
        self.assertEqual(second.effect_class, EffectClass.PURE)
        self.assertFalse(drift)


if __name__ == "__main__":
    unittest.main()
