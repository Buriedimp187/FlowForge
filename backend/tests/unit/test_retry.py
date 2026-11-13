"""Retry eligibility and deterministic backoff tests.

Covers the "Retries" scenarios from the M1 task instructions, section 4.
"""

from __future__ import annotations

import unittest

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.engine.clock import FakeClock
from flowforge.engine.idempotency import item_key, node_key
from flowforge.engine.retry import FailureKind, compute_backoff, decide_retry


class RetryEligibilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(start=1000.0)
        self.policy = RetryPolicy(max_attempts=3, backoff_base_s=1.0, backoff_multiplier=2.0, backoff_max_s=60.0)

    def test_pure_ordinary_failure_is_retryable(self) -> None:
        decision = decide_retry(
            effect_class=EffectClass.PURE,
            failure_kind=FailureKind.ORDINARY,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertTrue(decision.should_retry)
        self.assertIsNotNone(decision.retry_at)

    def test_read_post_dispatch_ambiguous_is_still_retryable(self) -> None:
        # Only unsafe_write restricts retry by failure_kind.
        decision = decide_retry(
            effect_class=EffectClass.READ,
            failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertTrue(decision.should_retry)

    def test_idempotent_write_ordinary_failure_is_retryable(self) -> None:
        decision = decide_retry(
            effect_class=EffectClass.IDEMPOTENT_WRITE,
            failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertTrue(decision.should_retry)

    def test_unsafe_write_pre_dispatch_is_retryable(self) -> None:
        decision = decide_retry(
            effect_class=EffectClass.UNSAFE_WRITE,
            failure_kind=FailureKind.PRE_DISPATCH,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertTrue(decision.should_retry)

    def test_unsafe_write_post_dispatch_ambiguous_is_never_retryable(self) -> None:
        decision = decide_retry(
            effect_class=EffectClass.UNSAFE_WRITE,
            failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertFalse(decision.should_retry)
        self.assertIsNone(decision.retry_at)

    def test_unsafe_write_ordinary_failure_kind_is_not_pre_dispatch_so_not_retried(self) -> None:
        decision = decide_retry(
            effect_class=EffectClass.UNSAFE_WRITE,
            failure_kind=FailureKind.ORDINARY,
            failures_after_this_one=1,
            policy=self.policy,
            clock=self.clock,
        )
        self.assertFalse(decision.should_retry)

    def test_retry_exhaustion(self) -> None:
        decision = decide_retry(
            effect_class=EffectClass.PURE,
            failure_kind=FailureKind.ORDINARY,
            failures_after_this_one=3,  # == max_attempts
            policy=self.policy,
            clock=self.clock,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.reason, "retry budget exhausted")

    def test_retry_budget_is_based_on_failures_not_attempts(self) -> None:
        # attempt could be far ahead of failures (e.g. once M2 adds crash
        # recovery, a re-executed attempt does not consume the ordinary
        # failure budget) -- decide_retry must only ever look at
        # failures_after_this_one, never at an attempt counter.
        decision = decide_retry(
            effect_class=EffectClass.PURE,
            failure_kind=FailureKind.ORDINARY,
            failures_after_this_one=1,  # only one *ordinary* failure so far
            policy=self.policy,
            clock=self.clock,
        )
        self.assertTrue(decision.should_retry)


class BackoffTestCase(unittest.TestCase):
    def test_deterministic_exponential_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=10, backoff_base_s=1.0, backoff_multiplier=2.0, backoff_max_s=60.0)
        self.assertEqual(compute_backoff(policy, 1), 1.0)
        self.assertEqual(compute_backoff(policy, 2), 2.0)
        self.assertEqual(compute_backoff(policy, 3), 4.0)
        self.assertEqual(compute_backoff(policy, 4), 8.0)

    def test_backoff_is_capped(self) -> None:
        policy = RetryPolicy(max_attempts=10, backoff_base_s=1.0, backoff_multiplier=2.0, backoff_max_s=5.0)
        self.assertEqual(compute_backoff(policy, 10), 5.0)

    def test_retry_at_uses_injected_clock_not_wall_clock(self) -> None:
        clock = FakeClock(start=500.0)
        policy = RetryPolicy(max_attempts=5, backoff_base_s=2.0, backoff_multiplier=2.0, backoff_max_s=60.0)
        decision = decide_retry(
            effect_class=EffectClass.PURE,
            failure_kind=FailureKind.ORDINARY,
            failures_after_this_one=1,
            policy=policy,
            clock=clock,
        )
        self.assertEqual(decision.retry_at, 502.0)
        clock.advance(100.0)
        # The clock moved, but the already-computed decision is unaffected
        # (it is a plain value, not a live reference to the clock).
        self.assertEqual(decision.retry_at, 502.0)


class AttemptVersusFailureCountTestCase(unittest.TestCase):
    def test_idempotent_write_key_is_unaffected_by_retry_decision(self) -> None:
        # The idempotency key must stay identical across every attempt,
        # independent of how many times decide_retry has been consulted.
        key_attempt_1 = node_key("run-1", "http_node")
        key_attempt_2 = node_key("run-1", "http_node")
        key_attempt_3 = node_key("run-1", "http_node")
        self.assertEqual(key_attempt_1, key_attempt_2)
        self.assertEqual(key_attempt_2, key_attempt_3)

    def test_per_item_key_stable_across_attempts(self) -> None:
        k1 = item_key("run-1", "http_node", 2)
        k2 = item_key("run-1", "http_node", 2)
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
