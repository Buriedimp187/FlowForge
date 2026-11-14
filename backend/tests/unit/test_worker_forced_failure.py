"""The durable forced-failure rule (no database).

A node forces the run to fail regardless of ``on_error`` iff it is ``failed`` with an
``unknown`` outcome and its ``error.cause`` is not ``cancel`` / ``deadline``. The rule reads
only what T3 persists, so a worker that takes over after a crash reconstructs it from the
node rows. These tests pin the rule itself, that it preserves the ``idempotent_write`` retry
policy, that it agrees with the engine in every resolution, and the output invariant.
"""

from __future__ import annotations

import dataclasses
import itertools
import unittest
from unittest import mock

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.domain.items import items_from
from flowforge.domain.node_run import NodeError, NodeRun
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.engine.clock import FakeClock
from flowforge.engine.interruption import InterruptionCause, resolve_interruption
from flowforge.engine.retry import FailureKind
from flowforge.worker import outcome
from flowforge.worker.outcome import NodeFinish, node_forces_run_failure, run_must_fail

CAUSES = (None, "timeout", "crash", "lease_lost", "post_dispatch_ambiguous", "cancel", "deadline", "mystery", "")
EXEMPT = {"cancel", "deadline"}


def _node(status=NodeRunStatus.FAILED, outcome_=Outcome.UNKNOWN, cause="timeout", *, error=True, effect=EffectClass.UNSAFE_WRITE, **kw):
    return NodeRun(
        run_id="r", node_key=kw.pop("node_key", "n"), status=status, attempt=1, failures=1, effect_class=effect,
        outcome=outcome_, started_epoch=1,
        retry_at=1.0 if status is NodeRunStatus.RETRY_WAIT else None,
        error=NodeError("c", "m", cause) if error else None, **kw,
    )  # fmt: skip


class PredicateTestCase(unittest.TestCase):
    def test_the_rule_over_every_status_outcome_and_cause(self) -> None:
        for status, out, cause in itertools.product(NodeRunStatus, Outcome, CAUSES):
            expected = status is NodeRunStatus.FAILED and out is Outcome.UNKNOWN and cause not in EXEMPT
            with self.subTest(status=status.value, outcome=out.value, cause=cause):
                self.assertEqual(node_forces_run_failure(_node(status, out, cause)), expected)

    def test_a_missing_error_or_cause_fails_closed(self) -> None:
        self.assertTrue(node_forces_run_failure(_node(error=False)))
        self.assertTrue(node_forces_run_failure(_node(cause=None)))
        self.assertTrue(node_forces_run_failure(_node(cause="a cause nobody has heard of")))

    def test_only_cancel_and_deadline_are_exempt(self) -> None:
        self.assertEqual(outcome.NON_FORCING_CAUSES, frozenset({"cancel", "deadline"}))
        for cause in ("cancel", "deadline"):
            self.assertFalse(node_forces_run_failure(_node(cause=cause)))
        for cause in ("timeout", "crash", "lease_lost", outcome.POST_DISPATCH_AMBIGUOUS_CAUSE):
            self.assertTrue(node_forces_run_failure(_node(cause=cause)))

    def test_a_known_outcome_never_forces_and_neither_does_a_non_failed_status(self) -> None:
        for cause in CAUSES:
            self.assertFalse(node_forces_run_failure(_node(outcome_=Outcome.KNOWN, cause=cause)))
            for status in (NodeRunStatus.SUCCEEDED, NodeRunStatus.RETRY_WAIT, NodeRunStatus.CANCELLED, NodeRunStatus.RUNNING, NodeRunStatus.SKIPPED):
                self.assertFalse(node_forces_run_failure(_node(status, Outcome.UNKNOWN, cause)), (status, cause))

    def test_the_rule_needs_neither_the_effect_class_nor_the_retry_state(self) -> None:
        for effect in (*EffectClass, None):
            self.assertTrue(node_forces_run_failure(_node(effect=effect)))
        self.assertTrue(node_forces_run_failure(dataclasses.replace(_node(), failures=0, attempt=1)))

    def test_run_must_fail_is_true_when_any_row_forces(self) -> None:
        quiet = [_node(NodeRunStatus.SUCCEEDED, Outcome.KNOWN, None, node_key="a"), _node(cause="cancel", node_key="b")]
        self.assertFalse(run_must_fail(quiet))
        self.assertFalse(run_must_fail(()))
        self.assertTrue(run_must_fail([*quiet, _node(cause="crash", node_key="c")]))
        self.assertTrue(run_must_fail(tuple(quiet) + (_node(cause=None, node_key="d"),)))


class NonStringCauseFailsClosedTestCase(unittest.TestCase):
    """``NodeError.cause`` is not type-checked by the domain; the rule must never raise on it.

    Persisted rows cannot hold a non-string cause (the codec refuses to decode one), but an
    in-memory ``NodeError`` can, and an unhashable value used to raise ``TypeError``.
    """

    NON_STRINGS = (
        None, 0, 5, -1, 1.5, True, False, [], ["x"], ["cancel"], {}, {"a": 1}, {"cause": "cancel"}, (), ("cancel",),
        b"cancel", object(), frozenset({"cancel"}),
    )  # fmt: skip

    def test_every_non_string_cause_forces_a_failed_unknown_node(self) -> None:
        for cause in self.NON_STRINGS:
            with self.subTest(cause=cause):
                row = _node(cause=cause)
                self.assertTrue(node_forces_run_failure(row))
                self.assertTrue(run_must_fail([row]))
                finish = NodeFinish(status=NodeRunStatus.FAILED, outcome=Outcome.UNKNOWN, error=NodeError("c", "m", cause))
                self.assertTrue(finish.forces_run_failure)

    def test_it_never_raises_whatever_the_status_and_outcome(self) -> None:
        for cause, status, out in itertools.product(self.NON_STRINGS, NodeRunStatus, Outcome):
            expected = status is NodeRunStatus.FAILED and out is Outcome.UNKNOWN
            with self.subTest(cause=cause, status=status.value, outcome=out.value):
                self.assertEqual(node_forces_run_failure(_node(status, out, cause)), expected)

    def test_only_the_exact_cancel_and_deadline_strings_exempt(self) -> None:
        for cause in ("cancel", "deadline"):
            self.assertFalse(node_forces_run_failure(_node(cause=cause)))
        for cause in ("Cancel", "CANCEL", " cancel", "cancel ", "deadline\n", "dead line", "cancelled", "timed_out"):
            with self.subTest(cause=cause):
                self.assertTrue(node_forces_run_failure(_node(cause=cause)))

    def test_a_non_string_equal_to_an_exempt_string_does_not_exempt(self) -> None:
        # bytes/containers that merely contain or compare near the exempt words must not be mistaken for them
        for cause in (b"cancel", ("cancel",), ["deadline"], frozenset({"deadline"})):
            self.assertTrue(node_forces_run_failure(_node(cause=cause)))


class IdempotentWritePolicyIsPreservedTestCase(unittest.TestCase):
    """An unknown idempotent_write may be repeated while its budget lasts; only exhaustion forces."""

    def test_only_the_attempt_that_exhausts_the_budget_forces_the_run_to_fail(self) -> None:
        for max_attempts in range(1, 6):
            policy = RetryPolicy(max_attempts=max_attempts)
            for failures in range(1, 8):
                finish = outcome.from_interruption(
                    EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=failures, policy=policy
                )
                exhausted = failures >= max_attempts
                with self.subTest(max_attempts=max_attempts, failures=failures):
                    self.assertIs(finish.outcome, Outcome.UNKNOWN)
                    self.assertIs(finish.status, NodeRunStatus.FAILED if exhausted else NodeRunStatus.RETRY_WAIT)
                    self.assertEqual(finish.forces_run_failure, exhausted)
                    self.assertEqual(finish.output, {})

    def test_the_persisted_in_budget_state_does_not_force_and_the_exhausted_one_does(self) -> None:
        in_budget = _node(NodeRunStatus.RETRY_WAIT, Outcome.UNKNOWN, "timeout", effect=EffectClass.IDEMPOTENT_WRITE)
        exhausted = _node(NodeRunStatus.FAILED, Outcome.UNKNOWN, "timeout", effect=EffectClass.IDEMPOTENT_WRITE)
        self.assertFalse(node_forces_run_failure(in_budget))
        self.assertTrue(node_forces_run_failure(exhausted))

    def test_a_crash_restarts_the_attempt_so_its_row_stays_running_and_does_not_force(self) -> None:
        # Crash column for idempotent_write: "re-execute with the same key" -- no failed/unknown row exists.
        for cause in (None, "crash", "timeout"):
            restarted = _node(NodeRunStatus.RUNNING, Outcome.UNKNOWN, cause, effect=EffectClass.IDEMPOTENT_WRITE)
            self.assertFalse(node_forces_run_failure(restarted))

    def test_suppressed_and_cancelled_unknown_rows_do_not_force(self) -> None:
        # finalization turns a retry_wait/unknown sibling into cancelled/unknown (outcome preserved)
        suppressed = _node(NodeRunStatus.CANCELLED, Outcome.UNKNOWN, "timeout", effect=EffectClass.IDEMPOTENT_WRITE)
        self.assertFalse(node_forces_run_failure(suppressed))

    def test_no_engine_resolution_ever_writes_failed_unknown_for_idempotent_write_before_exhaustion(self) -> None:
        for cause, failures in itertools.product(InterruptionCause, range(1, 5)):
            policy = RetryPolicy(max_attempts=5)
            finish = outcome.from_interruption(EffectClass.IDEMPOTENT_WRITE, cause, failures_after_this_one=failures, policy=policy)
            self.assertFalse(finish.status is NodeRunStatus.FAILED and finish.outcome is Outcome.UNKNOWN, (cause, failures))


class EngineAgreementTestCase(unittest.TestCase):
    POLICIES = (RetryPolicy(max_attempts=1), RetryPolicy(max_attempts=3), RetryPolicy(max_attempts=5))

    def test_the_derived_flag_equals_the_engines_in_every_resolution(self) -> None:
        checked = 0
        for effect, cause, dispatched, policy, failures in itertools.product(
            EffectClass, InterruptionCause, (None, True, False), self.POLICIES, (1, 2, 3, 6)
        ):
            finish = outcome.from_interruption(effect, cause, failures_after_this_one=failures, policy=policy, dispatched=dispatched)
            if cause is InterruptionCause.NODE_TIMEOUT:
                resolution = resolve_interruption(
                    effect, cause, dispatched=dispatched, failures_after_this_one=failures, policy=policy, clock=FakeClock(0.0)
                )
            else:
                resolution = resolve_interruption(effect, cause, dispatched=dispatched)
            with self.subTest(effect=effect, cause=cause, dispatched=dispatched, policy=policy.max_attempts, failures=failures):
                self.assertEqual(finish.forces_run_failure, resolution.forces_run_failure)
            checked += 1
        self.assertGreater(checked, 100)

    def test_the_engines_own_unsafe_write_cells(self) -> None:
        timeout = outcome.from_interruption(
            EffectClass.UNSAFE_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1, policy=self.POLICIES[1]
        )
        cancel = outcome.from_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.CANCEL)
        self.assertEqual((timeout.status, timeout.outcome, timeout.error.cause, timeout.forces_run_failure),
                         (NodeRunStatus.FAILED, Outcome.UNKNOWN, "timeout", True))  # fmt: skip
        self.assertEqual((cancel.status, cancel.outcome, cancel.error.cause, cancel.forces_run_failure),
                         (NodeRunStatus.FAILED, Outcome.UNKNOWN, "cancel", False))  # fmt: skip

    def test_a_disagreement_with_the_engine_fails_loudly(self) -> None:
        # If a writer used the wrong cause (or the matrix changed), the builder must not pass it on.
        with mock.patch.object(outcome, "NON_FORCING_CAUSES", frozenset()):
            with self.assertRaisesRegex(AssertionError, "disagrees with the engine"):
                outcome.from_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.CANCEL)

    def test_the_node_timeout_path_detects_a_disagreement_in_both_directions(self) -> None:
        policy = RetryPolicy(max_attempts=5)

        def unsafe_timeout():  # the engine says: forces the run to fail
            return outcome.from_interruption(EffectClass.UNSAFE_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1, policy=policy)

        def exhausted_idempotent():  # the engine says: forces (budget exhausted)
            return outcome.from_interruption(EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=5, policy=policy)

        def pure_timeout():  # the engine says: does not force
            return outcome.from_interruption(EffectClass.PURE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1, policy=policy)

        def in_budget_idempotent():  # the engine says: does not force (retry_wait)
            return outcome.from_interruption(EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1, policy=policy)

        for build in (unsafe_timeout, exhausted_idempotent, pure_timeout, in_budget_idempotent):
            build()  # control: with the real rule every one of them agrees with the engine

        with mock.patch.object(outcome, "_forces_run_failure", lambda status, out, cause: False):
            for build in (unsafe_timeout, exhausted_idempotent):  # derived says no, the engine says yes
                with self.subTest(derived="never", build=build.__name__), self.assertRaisesRegex(AssertionError, "disagrees with the engine"):
                    build()
        with mock.patch.object(outcome, "_forces_run_failure", lambda status, out, cause: True):
            for build in (pure_timeout, in_budget_idempotent):  # derived says yes, the engine says no
                with self.subTest(derived="always", build=build.__name__), self.assertRaisesRegex(AssertionError, "disagrees with the engine"):
                    build()

    def test_the_m3_case_has_no_engine_flag_and_is_forced_by_decision(self) -> None:
        finish = outcome.from_ordinary_failure(
            NodeError("reset", "connection reset"), effect_class=EffectClass.UNSAFE_WRITE,
            failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS, failures_after_this_one=1, policy=self.POLICIES[0],
        )
        self.assertTrue(finish.forces_run_failure)
        self.assertEqual(finish.error, NodeError("reset", "connection reset", "post_dispatch_ambiguous"))


class UnknownOutcomeCarriesNoOutputTestCase(unittest.TestCase):
    def test_constructing_one_with_output_is_refused(self) -> None:
        for status in (NodeRunStatus.FAILED, NodeRunStatus.CANCELLED):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "unknown outcome carries no output"):
                NodeFinish(status=status, outcome=Outcome.UNKNOWN, output={"error": items_from({"x": 1})})
        with self.assertRaises(ValueError):
            NodeFinish(status=NodeRunStatus.RETRY_WAIT, outcome=Outcome.UNKNOWN, delay_s=1.0, output={"main": ()})

    def test_even_an_empty_port_counts_as_output(self) -> None:
        # `{"error": ()}` means "emitted on the error port, zero items": enough to route downstream.
        with self.assertRaises(ValueError):
            NodeFinish(status=NodeRunStatus.FAILED, outcome=Outcome.UNKNOWN, output={"error": ()})

    def test_it_cannot_be_bypassed_with_replace(self) -> None:
        unknown = NodeFinish(status=NodeRunStatus.FAILED, outcome=Outcome.UNKNOWN, error=NodeError("c", "m", "crash"))
        with self.assertRaises(ValueError):
            dataclasses.replace(unknown, output={"error": items_from({"x": 1})})
        known = NodeFinish(status=NodeRunStatus.FAILED, error=NodeError("c"), output={"error": items_from({"x": 1})})
        with self.assertRaises(ValueError):
            dataclasses.replace(known, outcome=Outcome.UNKNOWN)

    def test_a_known_failure_may_still_carry_error_port_output(self) -> None:
        finish = NodeFinish(status=NodeRunStatus.FAILED, error=NodeError("c"), failure_inc=1, output={"error": items_from({"x": 1})})
        self.assertFalse(finish.forces_run_failure)
        self.assertIn("error", finish.output)

    def test_every_builder_path_with_an_unknown_outcome_has_no_output(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        finishes = [
            outcome.from_interruption(e, c, failures_after_this_one=f, policy=policy, dispatched=d)
            for e, c, f, d in itertools.product(EffectClass, InterruptionCause, (1, 2, 3, 4), (None, True, False))
        ] + [
            outcome.from_ordinary_failure(NodeError("c"), effect_class=e, failure_kind=k, failures_after_this_one=f, policy=policy)
            for e, k, f in itertools.product(EffectClass, FailureKind, (1, 2, 3, 4))
        ]
        unknown = [f for f in finishes if f.outcome is Outcome.UNKNOWN]
        self.assertGreater(len(unknown), 10)
        for finish in unknown:
            self.assertEqual(dict(finish.output), {})
            self.assertIsNone(finish.to_sql_params()["output"])  # T3 therefore writes NULL


if __name__ == "__main__":
    unittest.main()
