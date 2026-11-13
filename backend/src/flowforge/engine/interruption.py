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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from flowforge.domain.effects import EffectClass
from flowforge.domain.states import NodeRunStatus, Outcome


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
