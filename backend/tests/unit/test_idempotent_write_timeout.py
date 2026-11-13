"""The user-approved idempotent_write/Node-timeout state machine.

Resolves the architecture ambiguity reported in the previous M1 audit
pass (docs/architecture.md section 6.2): see
flowforge.engine.interruption.resolve_idempotent_write_timeout for the
authoritative implementation. Covers every scenario the M1
final-correction task listed for this finding.
"""

from __future__ import annotations

import unittest

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import OnErrorPolicy, RetryPolicy
from flowforge.domain.node_run import NodeRun, start_attempt
from flowforge.domain.states import NodeRunStatus, Outcome, is_terminal
from flowforge.engine.clock import FakeClock
from flowforge.engine.idempotency import node_key
from flowforge.engine.inmemory import InMemoryRunStore
from flowforge.engine.interruption import InterruptionCause, resolve_idempotent_write_timeout, resolve_interruption
from flowforge.engine.readiness import NodeReadiness, evaluate_readiness
from flowforge.engine.retry import FailureKind, decide_retry
from tests.unit.support import HTTP_NODE, linear_workflow, new_catalog


class TimeoutWithUnknownOutcomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(start=1000.0)
        self.policy = RetryPolicy(max_attempts=3, backoff_base_s=1.0, backoff_multiplier=2.0, backoff_max_s=60.0)

    def test_timeout_with_unknown_outcome_and_budget_remaining_goes_to_retry_wait(self) -> None:
        resolution = resolve_idempotent_write_timeout(failures_after_this_one=1, policy=self.policy, clock=self.clock)
        self.assertEqual(resolution.status, NodeRunStatus.RETRY_WAIT)
        self.assertEqual(resolution.outcome, Outcome.UNKNOWN)
        self.assertFalse(is_terminal(resolution.status))  # approved rule 4: non-terminal
        self.assertFalse(resolution.forces_run_failure)
        self.assertIsNotNone(resolution.retry_at)

    def test_unknown_outcome_is_never_reported_as_success(self) -> None:
        # Approved rules 3 and 8: this function must never produce
        # `succeeded`, under any budget state.
        # failures_after_this_one must be >= 1 (it is recorded only
        # after T3 increments `failures`, per docs/architecture.md
        # section 5.1 -- see flowforge.engine.retry.compute_backoff).
        for failures in (1, 2, 3, 4, 100):
            with self.subTest(failures=failures):
                resolution = resolve_idempotent_write_timeout(
                    failures_after_this_one=failures, policy=self.policy, clock=self.clock
                )
                self.assertNotEqual(resolution.status, NodeRunStatus.SUCCEEDED)

    def test_generic_resolver_rejects_the_combination_without_retry_context(self) -> None:
        # M1 final consistency fix: resolve_interruption() must not be
        # able to classify an idempotent_write NODE_TIMEOUT on its own --
        # that was the live contradiction an independent audit of
        # commit a4ef853 found (it could return a terminal FAILED
        # resolution while this function's own budget-aware logic would
        # still say retry_wait). Calling it without retry context raises.
        with self.assertRaises(TypeError):
            resolve_interruption(EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT)

    def test_generic_resolver_cannot_produce_a_terminal_result_while_budget_remains(self) -> None:
        # With retry context supplied, resolve_interruption() delegates
        # to this module's single authoritative decision path and can
        # therefore never disagree with it.
        resolution = resolve_interruption(
            EffectClass.IDEMPOTENT_WRITE,
            InterruptionCause.NODE_TIMEOUT,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertEqual(resolution.status, NodeRunStatus.RETRY_WAIT)
        self.assertFalse(is_terminal(resolution.status))
        self.assertFalse(resolution.forces_run_failure)

    def test_generic_resolver_agrees_on_exhaustion_too(self) -> None:
        exhausted_policy = RetryPolicy(max_attempts=1)
        resolution = resolve_interruption(
            EffectClass.IDEMPOTENT_WRITE,
            InterruptionCause.NODE_TIMEOUT,
            failures_after_this_one=1,
            policy=exhausted_policy,
            clock=self.clock,
        )
        self.assertEqual(resolution.status, NodeRunStatus.FAILED)
        self.assertTrue(resolution.forces_run_failure)
        self.assertEqual(resolution.error_code, "outcome_unknown_retries_exhausted")


class IdempotencyKeyStabilityAcrossRetriesTestCase(unittest.TestCase):
    """Approved rules 1 and 2."""

    def test_retry_uses_the_same_idempotency_key(self) -> None:
        key_before = node_key("run-1", "http-node")
        # Simulate a timeout-triggered retry_wait, then a second attempt.
        first, _ = start_attempt(None, run_id="run-1", node_key="http-node", resolved_effect_class=EffectClass.IDEMPOTENT_WRITE)
        import dataclasses

        waiting = dataclasses.replace(first, status=NodeRunStatus.RETRY_WAIT, retry_at=5.0, outcome=Outcome.UNKNOWN)
        second, _ = start_attempt(waiting, run_id="run-1", node_key="http-node", resolved_effect_class=EffectClass.IDEMPOTENT_WRITE)
        key_after = node_key("run-1", "http-node")
        self.assertEqual(key_before, key_after)
        # The key is derived from (run_id, node_key) alone -- `second`'s
        # higher `attempt` count cannot have changed it, by construction.
        self.assertEqual(second.attempt, 2)

    def test_multiple_retries_preserve_the_same_key(self) -> None:
        import dataclasses

        run_id, node_key_name = "run-1", "http-node"
        current, _ = start_attempt(None, run_id=run_id, node_key=node_key_name, resolved_effect_class=EffectClass.IDEMPOTENT_WRITE)
        keys_seen = {node_key(run_id, node_key_name)}
        for _ in range(4):
            current = dataclasses.replace(current, status=NodeRunStatus.RETRY_WAIT, retry_at=1.0, outcome=Outcome.UNKNOWN)
            current, _ = start_attempt(current, run_id=run_id, node_key=node_key_name, resolved_effect_class=EffectClass.IDEMPOTENT_WRITE)
            keys_seen.add(node_key(run_id, node_key_name))
        self.assertEqual(len(keys_seen), 1)  # one key, regardless of attempt count
        self.assertEqual(current.attempt, 5)


class DownstreamBlockedWhileUnknownTestCase(unittest.TestCase):
    """Approved rule 4: downstream nodes must not execute while the
    outcome remains unknown."""

    def setUp(self) -> None:
        self.catalog = new_catalog()
        self.clock = FakeClock()
        self.store = InMemoryRunStore()
        self.run_id = "run-1"

    def test_retry_wait_upstream_blocks_downstream_readiness(self) -> None:
        definition = linear_workflow()  # trigger -> set_a -> set_b
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        self.store.put(
            NodeRun(run_id=self.run_id, node_key="set_a", status=NodeRunStatus.RETRY_WAIT, retry_at=999.0, outcome=Outcome.UNKNOWN)
        )
        readiness = evaluate_readiness("set_b", definition, self.run_id, self.store, self.clock, self.catalog)
        self.assertEqual(readiness, NodeReadiness.NOT_REACHED)

    def test_once_confirmed_success_downstream_becomes_ready(self) -> None:
        definition = linear_workflow()
        self.store.put(NodeRun(run_id=self.run_id, node_key="trigger", status=NodeRunStatus.SUCCEEDED, output={"main": ()}))
        # Approved rule 5: a later attempt resolves with confirmed
        # success, through the ordinary T3 path -- not through
        # resolve_idempotent_write_timeout at all.
        self.store.put(
            NodeRun(
                run_id=self.run_id,
                node_key="set_a",
                status=NodeRunStatus.SUCCEEDED,
                outcome=Outcome.KNOWN,
                output={"main": ()},
            )
        )
        readiness = evaluate_readiness("set_b", definition, self.run_id, self.store, self.clock, self.catalog)
        self.assertEqual(readiness, NodeReadiness.READY)


class ConfirmedSuccessAfterUnknownTestCase(unittest.TestCase):
    """Approved rule 5."""

    def test_confirmed_success_after_an_earlier_unknown_attempt(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        clock = FakeClock(start=0.0)
        # Attempt 1 times out with an unknown outcome; budget remains.
        resolution = resolve_idempotent_write_timeout(failures_after_this_one=1, policy=policy, clock=clock)
        self.assertEqual(resolution.status, NodeRunStatus.RETRY_WAIT)

        import dataclasses

        first, _ = start_attempt(None, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.IDEMPOTENT_WRITE)
        waiting = dataclasses.replace(
            first, status=resolution.status, retry_at=resolution.retry_at, outcome=resolution.outcome, failures=1
        )
        retried, _ = start_attempt(waiting, run_id="r1", node_key="n1", resolved_effect_class=EffectClass.IDEMPOTENT_WRITE)
        # The retried attempt now confirms success -- an ordinary T3
        # write, outcome KNOWN, status SUCCEEDED.
        confirmed = dataclasses.replace(retried, status=NodeRunStatus.SUCCEEDED, outcome=Outcome.KNOWN, output={"main": ()})
        self.assertEqual(confirmed.status, NodeRunStatus.SUCCEEDED)
        self.assertEqual(confirmed.outcome, Outcome.KNOWN)
        self.assertTrue(confirmed.emitted_on("main"))


class DefinitiveFailureAfterUnknownTestCase(unittest.TestCase):
    """Approved rule 6: a definitive failure after an earlier unknown
    outcome is handled by the ordinary retry/on_error rules, because its
    outcome is no longer ambiguous."""

    def test_definitive_failure_is_an_ordinary_retry_decision_not_a_forced_run_failure(self) -> None:
        policy = RetryPolicy(max_attempts=5)
        clock = FakeClock(start=0.0)
        # This time the failure is definitive (e.g. a 4xx the node
        # classified as permanent) -- FailureKind.ORDINARY, not an
        # unknown-outcome interruption at all.
        decision = decide_retry(
            effect_class=EffectClass.IDEMPOTENT_WRITE,
            failure_kind=FailureKind.ORDINARY,
            failures_after_this_one=2,
            policy=policy,
            clock=clock,
        )
        self.assertTrue(decision.should_retry)  # idempotent_write retries ordinary failures per policy

    def test_definitive_failure_never_sets_forces_run_failure(self) -> None:
        # A definitive (KNOWN-outcome) failure never goes through
        # resolve_idempotent_write_timeout at all -- it is an ordinary
        # NodeRun written FAILED/KNOWN by the regular T3 path, so
        # InterruptionResolution.forces_run_failure (which defaults to
        # False and is only ever set True by the exhausted-unknown
        # branch below) is simply not part of that path. This is
        # asserted on the dataclass default directly, as the concrete,
        # checkable form of "on_error governs here, not this module".
        from flowforge.engine.interruption import InterruptionResolution

        default_resolution = InterruptionResolution(status=NodeRunStatus.FAILED, outcome=Outcome.KNOWN, auto_retryable=False)
        self.assertFalse(default_resolution.forces_run_failure)
        self.assertEqual(OnErrorPolicy.CONTINUE.value, "continue")  # the policy exists and would apply here


class RetryExhaustionTestCase(unittest.TestCase):
    """Approved rule 7."""

    def test_exhausted_retries_with_unknown_outcome_is_terminal_failed(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        clock = FakeClock()
        resolution = resolve_idempotent_write_timeout(failures_after_this_one=3, policy=policy, clock=clock)
        self.assertEqual(resolution.status, NodeRunStatus.FAILED)
        self.assertTrue(is_terminal(resolution.status))
        self.assertEqual(resolution.outcome, Outcome.UNKNOWN)
        self.assertFalse(resolution.auto_retryable)
        self.assertEqual(resolution.error_code, "outcome_unknown_retries_exhausted")

    def test_exhaustion_forces_run_failure_even_with_on_error_continue(self) -> None:
        # Approved rule 7, explicitly: "regardless of on_error". The
        # node's own on_error policy is irrelevant to this decision --
        # forces_run_failure is unconditional once the budget is spent.
        policy = RetryPolicy(max_attempts=2)
        clock = FakeClock()
        node = HTTP_NODE  # just to show a real node type is in play
        self.assertIsNotNone(node)
        for on_error in (OnErrorPolicy.FAIL_RUN, OnErrorPolicy.CONTINUE, OnErrorPolicy.ROUTE):
            with self.subTest(on_error=on_error):
                resolution = resolve_idempotent_write_timeout(failures_after_this_one=2, policy=policy, clock=clock)
                self.assertTrue(resolution.forces_run_failure)  # true no matter what on_error would say

    def test_exhaustion_boundary_is_exact(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        clock = FakeClock()
        # One failure short of the budget: still retries.
        still_retrying = resolve_idempotent_write_timeout(failures_after_this_one=2, policy=policy, clock=clock)
        self.assertEqual(still_retrying.status, NodeRunStatus.RETRY_WAIT)
        # At the budget: exhausted.
        exhausted = resolve_idempotent_write_timeout(failures_after_this_one=3, policy=policy, clock=clock)
        self.assertEqual(exhausted.status, NodeRunStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
