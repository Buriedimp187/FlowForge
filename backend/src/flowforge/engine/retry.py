"""Retry eligibility and deterministic backoff.

docs/architecture.md section 6.1/6.2, and ADR-0005:

    "pure/read retry per policy; idempotent_write retries per policy with
    the same key; unsafe_write retries ONLY for failures provably
    pre-dispatch... a post-dispatch failure is NEVER auto-retried for
    unsafe_write."

This module implements only the *decision* (should this particular
ordinary failure be retried, and if so when) using an injected
:class:`~flowforge.engine.clock.Clock`. It has no knowledge of leases,
queue polling, or database-clock SQL -- those belong to the worker (M2),
which is explicitly out of scope here.

"Attempts" vs. "ordinary failures": per docs/architecture.md section 5.1,
``attempt`` counts every execution start (crash re-execution included,
once M2 exists), while ``failures`` counts only attempts that ended in an
*ordinary* failure. The retry budget below is therefore checked against
``failures``, never ``attempt`` -- so a future crash-recovery
re-execution (M2) will not silently exhaust a node's ordinary retry
budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.engine.clock import Clock


class FailureKind(str, Enum):
    """Why an ordinary (non-interruption) attempt failed.

    This is deliberately a small, HTTP-agnostic vocabulary: the real
    HTTP node's method of classifying "was this provably pre-dispatch"
    is M3 scope (the dispatch marker, ADR-0018). M1 only needs the
    *policy* that consumes this classification.
    """

    PRE_DISPATCH = "pre_dispatch"  # provably nothing was sent (DNS/connect/validation/SSRF-block failure)
    POST_DISPATCH_AMBIGUOUS = "post_dispatch_ambiguous"  # a response may have reached the other side
    ORDINARY = "ordinary"  # any other failure with no dispatch ambiguity at all (e.g. a pure node's bug)


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    retry_at: float | None = None
    reason: str = ""


def _is_eligible(effect_class: EffectClass, failure_kind: FailureKind) -> bool:
    """The effect-class x failure-kind eligibility rule, independent of
    the attempt budget."""
    if effect_class is EffectClass.UNSAFE_WRITE:
        # Never retried automatically unless provably nothing was sent.
        return failure_kind is FailureKind.PRE_DISPATCH
    # pure / read / idempotent_write: any ordinary failure is eligible,
    # subject only to the attempt budget below.
    return True


def compute_backoff(policy: RetryPolicy, failures_after_this_one: int) -> float:
    """Exponential backoff with a hard cap, in seconds.

    Deterministic by construction (no randomness/jitter) so tests using a
    :class:`FakeClock` get a reproducible ``retry_at``. ``failures_after_this_one``
    is 1 for the first failure, 2 for the second, and so on.
    """
    if failures_after_this_one < 1:
        raise ValueError("failures_after_this_one must be >= 1")
    delay = policy.backoff_base_s * (policy.backoff_multiplier ** (failures_after_this_one - 1))
    return min(delay, policy.backoff_max_s)


def decide_retry(
    *,
    effect_class: EffectClass,
    failure_kind: FailureKind,
    failures_after_this_one: int,
    policy: RetryPolicy,
    clock: Clock,
) -> RetryDecision:
    """Decide whether an ordinary failure should be retried, and when.

    ``failures_after_this_one`` is the node's ``failures`` counter
    *including* the failure being decided on right now (i.e. call this
    after incrementing ``failures``, matching docs/architecture.md
    section 5.1's T3: "failures = failures + 1" happens before the retry
    decision).
    """
    if not _is_eligible(effect_class, failure_kind):
        return RetryDecision(
            should_retry=False,
            reason=(
                "unsafe_write failures are only retried when provably "
                "pre-dispatch (docs/architecture.md section 6.1/6.2)"
            ),
        )

    if failures_after_this_one >= policy.max_attempts:
        return RetryDecision(should_retry=False, reason="retry budget exhausted")

    delay = compute_backoff(policy, failures_after_this_one)
    return RetryDecision(should_retry=True, retry_at=clock.now() + delay, reason="eligible, within budget")
