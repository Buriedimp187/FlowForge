"""M2.2: ``NodeRun.started_epoch`` and the orphan-reclaim edge of ``start_attempt``.

docs/m2-persistence-design.md section 6. Pure and DB-free. The orphan edge is
guarded by the epoch alone; effect-class and outcome gating belong to the M2.5
orphan resolution and are deliberately NOT tested here as behavior of this
function (only that this function does not apply them).
"""

from __future__ import annotations

import dataclasses
import itertools
import unittest

from flowforge.domain.effects import EffectClass
from flowforge.domain.items import Item
from flowforge.domain.node_run import ItemResult, NodeError, NodeRun, start_attempt
from flowforge.domain.states import NodeRunStatus, Outcome, is_terminal
from flowforge.engine.inmemory import InMemoryRunStore

KW = {"run_id": "r1", "node_key": "n1"}


def _running(epoch: int | None, **overrides) -> NodeRun:
    values = dict(
        run_id="r1",
        node_key="n1",
        status=NodeRunStatus.RUNNING,
        attempt=2,
        failures=1,
        effect_class=EffectClass.READ,
        started_epoch=epoch,
    )
    values.update(overrides)
    return NodeRun(**values)


class StartedEpochFieldTestCase(unittest.TestCase):
    def test_defaults_to_none_and_accepts_non_negative_ints(self) -> None:
        self.assertIsNone(NodeRun(run_id="r", node_key="n", status=NodeRunStatus.RUNNING).started_epoch)
        for value in (0, 1, 2**40):
            self.assertEqual(NodeRun(run_id="r", node_key="n", status=NodeRunStatus.RUNNING, started_epoch=value).started_epoch, value)

    def test_rejects_bool_non_int_and_negative(self) -> None:
        for bad in (True, False, 1.0, "1", 1.5):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                NodeRun(run_id="r", node_key="n", status=NodeRunStatus.RUNNING, started_epoch=bad)
        with self.assertRaises(ValueError):
            NodeRun(run_id="r", node_key="n", status=NodeRunStatus.RUNNING, started_epoch=-1)

    def test_replace_preserves_it_and_the_record_stays_frozen(self) -> None:
        run = _running(3)
        self.assertEqual(dataclasses.replace(run, attempt=9).started_epoch, 3)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            run.started_epoch = 4  # type: ignore[misc]


class OrdinaryEdgesTestCase(unittest.TestCase):
    def test_absent_without_epoch_is_unchanged_m1_behavior(self) -> None:
        run, drift = start_attempt(None, resolved_effect_class=EffectClass.PURE, **KW)
        self.assertIsNone(run.started_epoch)
        self.assertEqual((run.status, run.attempt, run.failures, drift), (NodeRunStatus.RUNNING, 1, 0, False))

    def test_absent_with_epoch_records_it(self) -> None:
        run, _ = start_attempt(None, resolved_effect_class=EffectClass.PURE, current_lease_epoch=4, **KW)
        self.assertEqual(run.started_epoch, 4)

    def test_retry_wait_without_epoch_preserves_the_previous_value(self) -> None:
        waiting = _running(2, status=NodeRunStatus.RETRY_WAIT, retry_at=5.0)
        run, _ = start_attempt(waiting, resolved_effect_class=EffectClass.READ, **KW)
        self.assertEqual((run.started_epoch, run.attempt, run.failures, run.retry_at), (2, 3, 1, None))

    def test_retry_wait_with_epoch_records_it_whatever_the_old_value(self) -> None:
        for old in (None, 1, 5):
            with self.subTest(old=old):
                waiting = _running(old, status=NodeRunStatus.RETRY_WAIT, retry_at=5.0)
                run, _ = start_attempt(waiting, resolved_effect_class=EffectClass.READ, current_lease_epoch=3, **KW)
                self.assertEqual((run.started_epoch, run.attempt, run.failures), (3, 3, 1))


class OrphanReclaimTestCase(unittest.TestCase):
    def test_older_epoch_running_row_is_restarted_as_the_next_attempt(self) -> None:
        orphan = _running(1)
        run, drift = start_attempt(orphan, resolved_effect_class=EffectClass.READ, current_lease_epoch=2, **KW)
        self.assertEqual(
            (run.status, run.attempt, run.failures, run.started_epoch, run.retry_at, drift),
            (NodeRunStatus.RUNNING, 3, 1, 2, None, False),
        )
        self.assertEqual(orphan.started_epoch, 1)  # input never mutated
        self.assertEqual(orphan.attempt, 2)

    def test_everything_else_is_carried_over_and_unknown_stays_unknown(self) -> None:
        orphan = _running(
            1,
            outcome=Outcome.UNKNOWN,
            error=NodeError(code="x", message="m", cause="timeout"),
            output={"main": (Item(json={"a": 1}),)},
            item_results=(ItemResult(index=0, status="succeeded"),),
            effect_class=EffectClass.IDEMPOTENT_WRITE,
        )
        run, _ = start_attempt(orphan, resolved_effect_class=EffectClass.IDEMPOTENT_WRITE, current_lease_epoch=7, **KW)
        self.assertIs(run.outcome, Outcome.UNKNOWN)
        self.assertEqual(run.error, orphan.error)
        self.assertEqual(dict(run.output), dict(orphan.output))
        self.assertEqual(run.item_results, orphan.item_results)

    def test_effect_class_stays_immutable_and_drift_is_reported(self) -> None:
        run, drift = start_attempt(_running(1), resolved_effect_class=EffectClass.PURE, current_lease_epoch=2, **KW)
        self.assertEqual(run.effect_class, EffectClass.READ)
        self.assertTrue(drift)

    def test_non_orphan_running_rows_are_rejected(self) -> None:
        cases = {
            "no epoch supplied": (_running(1), None),
            "no recorded started_epoch": (_running(None), 2),
            "equal epoch (live attempt)": (_running(2), 2),
            "newer started_epoch": (_running(3), 2),
        }
        for label, (row, current) in cases.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                start_attempt(row, resolved_effect_class=EffectClass.READ, current_lease_epoch=current, **KW)

    def test_terminal_rows_stay_immutable_even_with_an_epoch(self) -> None:
        for status in NodeRunStatus:
            if not is_terminal(status):
                continue
            row = NodeRun(run_id="r1", node_key="n1", status=status, attempt=1, effect_class=EffectClass.PURE, started_epoch=1)
            with self.subTest(status=status), self.assertRaises(ValueError):
                start_attempt(row, resolved_effect_class=EffectClass.PURE, current_lease_epoch=9, **KW)

    def test_mismatched_identity_is_rejected_on_the_orphan_edge(self) -> None:
        with self.assertRaises(ValueError):
            start_attempt(_running(1), run_id="other", node_key="n1", resolved_effect_class=EffectClass.READ, current_lease_epoch=2)
        with self.assertRaises(ValueError):
            start_attempt(_running(1), run_id="r1", node_key="other", resolved_effect_class=EffectClass.READ, current_lease_epoch=2)

    def test_invalid_epochs_are_rejected_on_every_edge(self) -> None:
        rows = (None, _running(1, status=NodeRunStatus.RETRY_WAIT, retry_at=1.0), _running(1))
        for row in rows:
            for bad in (True, 1.0, "2"):
                with self.subTest(row=row is not None, bad=bad), self.assertRaises(TypeError):
                    start_attempt(row, resolved_effect_class=EffectClass.READ, current_lease_epoch=bad, **KW)  # type: ignore[arg-type]
            with self.subTest(row=row is not None, bad=-1), self.assertRaises(ValueError):
                start_attempt(row, resolved_effect_class=EffectClass.READ, current_lease_epoch=-1, **KW)

    def test_function_applies_no_effect_class_or_outcome_gating(self) -> None:
        # M2.5 decides whether an orphan may be re-executed; this primitive checks the epoch only.
        for effect_class, outcome in itertools.product(EffectClass, Outcome):
            with self.subTest(effect_class=effect_class, outcome=outcome):
                orphan = _running(1, effect_class=effect_class, outcome=outcome)
                run, _ = start_attempt(orphan, resolved_effect_class=effect_class, current_lease_epoch=2, **KW)
                self.assertEqual((run.attempt, run.outcome), (3, outcome))


class LegalEdgeMatrixTestCase(unittest.TestCase):
    def test_exactly_three_edges_into_running_are_legal(self) -> None:
        legal = set()
        for status in NodeRunStatus:
            for started in (None, 1, 2, 3):
                for current in (None, 2):
                    row = NodeRun(
                        run_id="r1", node_key="n1", status=status, attempt=1,
                        effect_class=EffectClass.PURE, started_epoch=started,
                        retry_at=1.0 if status is NodeRunStatus.RETRY_WAIT else None,
                    )
                    try:
                        start_attempt(row, resolved_effect_class=EffectClass.PURE, current_lease_epoch=current, **KW)
                    except ValueError:
                        continue
                    legal.add((status, started, current))
        expected = {(NodeRunStatus.RETRY_WAIT, s, c) for s in (None, 1, 2, 3) for c in (None, 2)}
        expected |= {(NodeRunStatus.RUNNING, 1, 2)}  # the only orphan: started_epoch 1 < current 2
        self.assertEqual(legal, expected)


class StoreRoundTripTestCase(unittest.TestCase):
    def test_in_memory_store_keeps_started_epoch(self) -> None:
        store = InMemoryRunStore()
        store.put(_running(5))
        self.assertEqual(store.get("r1", "n1").started_epoch, 5)


if __name__ == "__main__":
    unittest.main()
