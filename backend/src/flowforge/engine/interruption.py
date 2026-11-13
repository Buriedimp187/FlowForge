"""Cancellation and node-timeout outcome resolution.

docs/architecture.md section 6.2, the cause x effect-class interruption
matrix -- restricted here to the two causes that are pure, in-process
engine concepts not requiring a worker, a lease, or PostgreSQL: CANCEL
and NODE_TIMEOUT.

Deliberately NOT implemented in M1 (left to M2, per the task's explicit
exclusions): the CRASH/LEASE_LOST and RUN_DEADLINE causes. Both require
detecting that a *different* process's lease has expired or that a run
has been claimed past its deadline -- concepts that do not exist without
the worker and the `runs`/lease machinery. Modelling them here would mean
guessing at semantics M2 has not been built to support yet.

The central safety rule this module exists to enforce (M1 task
instructions, section F):

    "Ensure cancellation does not silently turn an unknown external
    outcome into a known outcome... Preserve the distinction between
    cancelled and failed with an unknown outcome."

M1 final-correction history (user-approved resolution of the
``idempotent_write``/Node-timeout ambiguity): docs/architecture.md
section 6.2 previously carried a textual tension between its general
"an outcome_unknown from crash or timeout fails the run" rule and its
own ``idempotent_write``/Node-timeout matrix cell ("unknown; retryable
with the same key"). The user has now explicitly approved a single,
unambiguous rule for this case, implemented by
:func:`resolve_idempotent_write_timeout` below: retry (into
``retry_wait``, never ``succeeded``, with the same idempotency key)
while the retry budget allows it, and fail the run unconditionally --
regardless of the node's ``on_error`` policy -- only once that budget is
exhausted with the outcome still unknown. See
docs/architecture.md section 6.2 and ADR-0005 for the updated normative
text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.engine.clock import Clock
from flowforge.engine.retry import compute_backoff


class InterruptionCause(str, Enum):
    CANCEL = "cancel"
    NODE_TIMEOUT = "node_timeout"


@dataclass(frozen=True)
class InterruptionResolution:
    """The pure result of resolving one interrupted node attempt."""

    status: NodeRunStatus
    outcome: Outcome
    auto_retryable: bool
    error_code: Optional[str] = None
    forces_run_failure: bool = False
    retry_at: Optional[float] = None


def resolve_interruption(
    effect_class: EffectClass,
    cause: InterruptionCause,
    *,
    dispatched: Optional[bool] = None,
) -> InterruptionResolution:
    """Resolve an interrupted node attempt, per the cause x effect-class
    matrix in docs/architecture.md section 6.2.

    ``dispatched`` is only meaningful for ``EffectClass.UNSAFE_WRITE``:
    ``False`` means the dispatch marker (M3, not implemented here) proved
    no external bytes left the process; ``True`` or ``None``
    (unprovable) means the conservative "may have been sent" case
    applies, per the architecture's "whenever dispatch cannot be
    disproven" rule.

    ``auto_retryable`` meaning (M1 audit correction): the architecture's
    section 6.2 matrix uses the word "retryable" ONLY in the "Node
    timeout" column -- never in the "Cancel" column, for any effect
    class. ``CANCELLED`` is itself one of the four terminal statuses
    (section 3.1); a cancelled node is DONE, not a candidate for
    automatic re-execution. A prior version of this function set
    ``auto_retryable=True`` for CANCEL across pure/read/idempotent_write,
    which would have let a user's cancellation accidentally trigger
    automatic re-execution. ``auto_retryable`` is therefore unconditionally
    ``False`` whenever ``cause is InterruptionCause.CANCEL`` -- it is
    meaningful only for ``NODE_TIMEOUT``, where it signals *eligibility*
    for the ordinary retry-budget decision in ``flowforge.engine.retry``
    (not an unconditional retry).
    """
    is_cancel = cause is InterruptionCause.CANCEL

    # pure / read: both causes resolve to a KNOWN outcome. The *status*
    # differs by cause (cancel -> CANCELLED, timeout -> FAILED), matching
    # the architecture's table exactly. Only a NODE_TIMEOUT outcome is
    # ever retry-eligible.
    if effect_class in (EffectClass.PURE, EffectClass.READ):
        status = NodeRunStatus.CANCELLED if is_cancel else NodeRunStatus.FAILED
        return InterruptionResolution(status=status, outcome=Outcome.KNOWN, auto_retryable=not is_cancel)

    if effect_class is EffectClass.IDEMPOTENT_WRITE:
        # The architecture marks both causes UNKNOWN for this class (the
        # user asserted repeatability, but an in-flight write's outcome
        # is still genuinely unknown to us). Only the Node-timeout row's
        # text says "retryable with the same key" -- the Cancel row never
        # does, and CANCELLED is terminal regardless.
        #
        # NODE_TIMEOUT NOTE: the `auto_retryable=True` returned here is
        # only an ELIGIBILITY signal, not the full approved state
        # transition -- it does not know the retry budget, so it cannot
        # decide between "go to retry_wait" and "exhausted, fail the
        # run unconditionally". Real orchestration must use
        # :func:`resolve_idempotent_write_timeout` instead, which is
        # budget-aware and is the single authoritative implementation of
        # the approved rule. This branch is kept only for API continuity
        # with the simpler cause x class classification other effect
        # classes use.
        status = NodeRunStatus.CANCELLED if is_cancel else NodeRunStatus.FAILED
        return InterruptionResolution(status=status, outcome=Outcome.UNKNOWN, auto_retryable=not is_cancel)

    if effect_class is EffectClass.UNSAFE_WRITE:
        if dispatched is False:
            # Provably nothing was sent: safe to treat as known, exactly
            # like pure/read -- including the same cancel-is-never-
            # retryable rule.
            status = NodeRunStatus.CANCELLED if is_cancel else NodeRunStatus.FAILED
            return InterruptionResolution(status=status, outcome=Outcome.KNOWN, auto_retryable=not is_cancel)

        # dispatched True, or unprovable (None): the conservative,
        # mandatory case. NEVER "cancelled", NEVER auto-retried, for
        # either cause.
        return InterruptionResolution(
            status=NodeRunStatus.FAILED,
            outcome=Outcome.UNKNOWN,
            auto_retryable=False,
            error_code="outcome_unknown",
            forces_run_failure=(cause is InterruptionCause.NODE_TIMEOUT),
        )

    raise AssertionError(f"unhandled EffectClass: {effect_class}")  # pragma: no cover


def resolve_idempotent_write_timeout(
    *,
    failures_after_this_one: int,
    policy: RetryPolicy,
    clock: Clock,
) -> InterruptionResolution:
    """The full, user-approved state machine for an ``idempotent_write``
    node interrupted by ``NODE_TIMEOUT`` with an unknown outcome.

    This is the authoritative implementation of the approved rule (see
    the module docstring): unlike :func:`resolve_interruption`'s general
    cause x class table, which reports only an eligibility signal for a
    higher layer to combine with the ordinary retry budget, this
    function performs that composition itself, because the exact
    behavior here is now a specific, approved normative rule rather
    than a general pattern shared with other effect classes:

    - While ``failures_after_this_one < policy.max_attempts``: the node
      goes to **`retry_wait`** (non-terminal, outcome ``unknown``, the
      SAME idempotency key reused automatically on the next attempt --
      ``flowforge.engine.idempotency`` never varies a key by attempt
      number, so key stability holds without any special handling
      here). Because `retry_wait` is non-terminal
      (docs/architecture.md section 3.1), downstream nodes cannot
      execute: readiness (``flowforge.engine.readiness``) only
      proceeds once every upstream node is terminal. The node is never,
      under any circumstance, written as `succeeded` by this function
      -- an unknown outcome is not success (approved rule 3/8).
    - Once ``failures_after_this_one >= policy.max_attempts``: the node
      becomes **`failed`**, outcome ``unknown``, and
      ``forces_run_failure=True`` **unconditionally** -- the run must
      fail regardless of the node's ``on_error`` policy (approved rule
      7). ``error_code='outcome_unknown_retries_exhausted'`` distinguishes
      this from a definitive failure.

    What this function does NOT decide, because they are the ordinary,
    pre-existing engine flow and not special to this rule: a *later*
    attempt (started via :func:`flowforge.domain.node_run.start_attempt`
    from the `retry_wait` row this function produced) that returns a
    confirmed success writes `succeeded` through the normal T3 path
    (approved rule 5); one that returns a definitive, non-ambiguous
    failure is handled by the ordinary :func:`flowforge.engine.retry.decide_retry`
    /``on_error`` path, since its outcome is no longer unknown (approved
    rule 6).
    """
    if failures_after_this_one < policy.max_attempts:
        delay = compute_backoff(policy, failures_after_this_one)
        return InterruptionResolution(
            status=NodeRunStatus.RETRY_WAIT,
            outcome=Outcome.UNKNOWN,
            auto_retryable=True,
            forces_run_failure=False,
            retry_at=clock.now() + delay,
        )

    return InterruptionResolution(
        status=NodeRunStatus.FAILED,
        outcome=Outcome.UNKNOWN,
        auto_retryable=False,
        error_code="outcome_unknown_retries_exhausted",
        forces_run_failure=True,
    )
