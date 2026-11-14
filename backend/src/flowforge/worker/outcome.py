"""``NodeFinish``: what T3 persists for one node attempt, built from pure decisions.

Stage 1 of M2.5 (docs/m2-persistence-design.md section 4.4). The design passes a
``NodeFinish`` to ``finish_node`` that "carries the status/outcome/output/error/
failure_inc/delay decided by the pure M1 engine". This module defines that value type
and builders that TRANSLATE existing engine decisions into it -- they never restate
retry or interruption policy:

* ordinary failure  -> :func:`flowforge.engine.retry.decide_retry`
* interruption      -> :func:`flowforge.engine.interruption.resolve_interruption`
  (which itself delegates the ``idempotent_write`` timeout rule)

Both engine functions compute ``retry_at`` against a :class:`~flowforge.engine.clock.Clock`.
The persisted ``retry_at`` is database-authoritative (``now() + delay`` in SQL, design
4.4), so the builders evaluate them against a clock fixed at ``0.0``: the returned
``retry_at`` is then exactly the DELAY in seconds, with no subtraction and no rounding.

``forces_run_failure`` (the run must fail regardless of ``on_error``) is DERIVED, never
stored: it is a pure function of ``status``, ``outcome`` and ``error.cause``, exactly what
T3 persists, so a worker that takes over after a crash reaches the same decision from the
node rows alone (:func:`node_forces_run_failure`, :func:`run_must_fail`). The rule:

    a node forces run failure  <=>  status is ``failed``  and  outcome is ``unknown``
                                    and  error.cause is not ``cancel`` / ``deadline``

An unrecognized or missing cause forces (fail closed: failing a run is always safe,
continuing past an unknown write is not). ``cancel`` and ``deadline`` are exempt because
those runs end ``cancelled`` / ``timed_out`` (architecture 6.2). The rule needs neither the
effect class nor the retry state: an ``idempotent_write`` that may still be repeated is
``retry_wait`` (not ``failed``) while its budget lasts, a crash restarts it (no row is
written), and ``failed``/``unknown`` exists for it only once the budget is exhausted.
``error.cause`` values: ``timeout``, ``crash``, ``lease_lost``, ``post_dispatch_ambiguous``
(force); ``cancel``, ``deadline`` (do not). A ``NodeFinish`` with an ``UNKNOWN`` outcome
can carry no output, so no error-port route can ever be fed by it.

Messages never mention billing or usage (docs/message-catalog.md allowlist rule).

``item_results`` (per-item detail) is deliberately NOT part of ``NodeFinish`` or of the
SQL parameter contract. The approved 4.4 statement does not write ``node_runs.item_results``,
so advertising it here would drop data silently. The domain ``NodeRun`` and the codec still
model the column for reading; persisting it needs a design amendment (see the Stage 1 audit).

Open decisions and obligations recorded for later stages (nothing here changes behavior):

* **DECIDED (M3, user decision):** an ordinary failure of an ``unsafe_write`` with
  ``FailureKind.POST_DISPATCH_AMBIGUOUS`` ("a response may have reached the other side") is
  ``failed`` with ``Outcome.UNKNOWN`` and FORCES the run to fail regardless of ``on_error``
  (ADR-0005): ``decide_retry`` never retries it, the run drains in-flight nodes, schedules
  nothing new, and ends ``failed`` with ``has_unknown_outcome``. The persisted marker is
  ``error.cause = "post_dispatch_ambiguous"`` (the caller's ``code`` and ``message`` are kept;
  a caller-supplied different ``cause`` is refused). Every other combination (any other failure
  kind, and every other effect class) stays ``Outcome.KNOWN``.
* **Stage 4 obligations for the forced-failure rule:** orphan resolution writes
  ``cause = "crash"`` for an ``unsafe_write`` orphan (``failed``/``unknown``); an
  ``idempotent_write`` orphan is RESTARTED (same key), never written ``failed``/``unknown``;
  resolution-only passes (cancel, deadline, forced failure) resolve an ``idempotent_write``
  orphan to ``cancelled``/``unknown`` instead of restarting it.
* **UNRESOLVED, deferred to Stage 4 (do not choose here):** (a) the run status when a reclaimer
  meets an ``unsafe_write`` crash orphan WHILE a cancellation is pending. The orphan follows the
  crash column (architecture 6.2: ``cause = crash``), which this rule treats as forcing, so the
  run would end ``failed`` rather than ``cancelled``; whether that is the intended result is
  undecided. (b) The exact resolution of an ``idempotent_write`` orphan in the ``max_recoveries``
  pass.
* **Stage 4 documentation alignment (deferred):** the post-claim list of architecture 4.3 / design
  4.2 names only the ``max_recoveries`` and cancellation resolution-only passes, while
  architecture 6.2 and ADR-0005 also describe a run already failing by this rule as one. Align the
  list at Stage 4; it is deliberately unchanged now.
* **Stage 5 obligation (execution loop):** a malformed persisted ``error`` makes snapshot decoding
  raise ``CodecError``. The loop must never treat that as a transient database failure that lets
  scheduling continue; it must fail closed (forced run failure, or an explicit
  ``engine_inconsistency`` termination), and Stage 5 must test it. Decoding stays strict.
  See docs/m2-persistence-design.md, "M2.5 staged-implementation obligations".
* **Failure counting (Stage 3 to confirm):** a node timeout is built with ``failure_inc=1``
  (every effect class, including an unknown outcome) and a cancellation with ``0``. Architecture
  5.1 says ``failures`` counts "attempts that ended in an ordinary failure"; whether an
  unknown-outcome timeout is "ordinary" is not stated. Tests pin the current counts.
* **Deferred (M4):** ``on_error`` handling is loop-level (the engine has no resolver), and
  ``from_ordinary_failure`` has no ``output`` parameter, so a failed node's error-port output
  (``on_error = route/continue``, architecture 4.3) cannot be built here yet. ``NodeFinish``
  itself accepts output on any status.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.domain.items import ItemList
from flowforge.domain.node_run import NodeError, NodeRun
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.engine.interruption import InterruptionCause, InterruptionResolution, resolve_interruption
from flowforge.engine.retry import FailureKind, decide_retry
from flowforge.worker.codec import dumps_jsonb, error_to_json, output_to_json

#: The only statuses a ``running`` attempt can be written to by T3.
FINISH_STATUSES = frozenset(
    {NodeRunStatus.SUCCEEDED, NodeRunStatus.FAILED, NodeRunStatus.RETRY_WAIT, NodeRunStatus.CANCELLED}
)

TIMEOUT_ERROR_CODE = "node_timeout"
TIMEOUT_MESSAGE = "node attempt exceeded its timeout"
CANCEL_MESSAGE = "node attempt was cancelled"
UNKNOWN_OUTCOME_MESSAGE = "attempt interrupted; dispatch cannot be disproven"

#: ``error.cause`` of an ordinary post-dispatch-ambiguous failure of an ``unsafe_write`` (the
#: ``FailureKind`` value). With ``timeout``, ``crash`` and ``lease_lost`` it forces run failure.
POST_DISPATCH_AMBIGUOUS_CAUSE = FailureKind.POST_DISPATCH_AMBIGUOUS.value

#: The only causes of a ``failed``/``unknown`` node that do NOT force run failure: those runs end
#: ``cancelled`` / ``timed_out`` instead (architecture 6.2). Anything else, including a missing or
#: unrecognized cause, forces (fail closed).
NON_FORCING_CAUSES = frozenset({"cancel", "deadline"})


def _forces_run_failure(status: NodeRunStatus, outcome: Outcome, cause: object) -> bool:
    """The durable forced-failure rule, over exactly what T3 persists.

    Only an actual ``cancel`` / ``deadline`` STRING exempts a ``failed``/``unknown`` node. Any other
    ``cause`` (``None``, an unrecognized string, or a non-string such as an int, a list or a dict --
    ``NodeError`` does not type-check it, and an unhashable value must not raise) fails closed.
    """
    exempt = isinstance(cause, str) and cause in NON_FORCING_CAUSES
    return status is NodeRunStatus.FAILED and outcome is Outcome.UNKNOWN and not exempt


def node_forces_run_failure(node_run: NodeRun) -> bool:
    """Whether this PERSISTED node row forces the run to fail regardless of ``on_error``."""
    cause = node_run.error.cause if node_run.error is not None else None
    return _forces_run_failure(node_run.status, node_run.outcome, cause)


def run_must_fail(node_runs: "tuple[NodeRun, ...] | list[NodeRun]") -> bool:
    """Reconstruct the forced-failure decision from node rows alone (e.g. after a crash)."""
    return any(node_forces_run_failure(node_run) for node_run in node_runs)


class _ZeroClock:
    """``now() == 0.0``: makes the engine's ``retry_at`` equal the delay itself."""

    def now(self) -> float:
        return 0.0


_ZERO_CLOCK = _ZeroClock()


@dataclass(frozen=True)
class NodeFinish:
    """One attempt's result, ready for the T3 statement (design 4.4)."""

    status: NodeRunStatus
    outcome: Outcome = Outcome.KNOWN
    output: Mapping[str, ItemList] = field(default_factory=dict)
    error: Optional[NodeError] = None
    failure_inc: int = 0
    delay_s: Optional[float] = None

    @property
    def forces_run_failure(self) -> bool:
        """Derived, never stored or persisted: see the module docstring for the rule.

        Because it is computed from ``status``, ``outcome`` and ``error.cause`` on every access,
        ``dataclasses.replace`` that changes any of those changes this value too. The agreement with
        the engine's own flag is asserted once, when a builder constructs the finish
        (:func:`_checked`), not on later ``replace`` calls.
        """
        cause = self.error.cause if self.error is not None else None
        return _forces_run_failure(self.status, self.outcome, cause)

    def __post_init__(self) -> None:
        if self.status not in FINISH_STATUSES:
            raise ValueError(f"a running attempt cannot be finished as {self.status!r}")
        if isinstance(self.failure_inc, bool) or not isinstance(self.failure_inc, int) or self.failure_inc not in (0, 1):
            raise ValueError(f"failure_inc must be 0 or 1, got {self.failure_inc!r}")
        if self.status is NodeRunStatus.RETRY_WAIT:
            delay = self.delay_s
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
                raise ValueError(f"retry_wait needs a finite delay_s >= 0, got {delay!r}")
        elif self.delay_s is not None:
            raise ValueError("delay_s is only meaningful for retry_wait")
        if self.status is NodeRunStatus.SUCCEEDED:
            if self.outcome is not Outcome.KNOWN:
                raise ValueError("an unknown outcome is never a success")
            if self.failure_inc != 0 or self.error is not None:
                raise ValueError("a succeeded attempt records no failure")
        object.__setattr__(
            self,
            "output",
            MappingProxyType({port: tuple(items) for port, items in self.output.items()}),
        )
        if self.outcome is Outcome.UNKNOWN and self.output:
            # Enforced here, not by convention in the loop: an error-port output would let
            # on_error = route/continue schedule downstream nodes past a write nobody can verify.
            raise ValueError("a finish with an unknown outcome carries no output")

    def to_sql_params(self) -> dict[str, Any]:
        """Bind parameters for the approved 4.4 T3 statement, and exactly those.

        JSON columns are deterministic JSON text. Raises :class:`~flowforge.worker.codec.CodecError`
        if the output or error holds a string PostgreSQL jsonb cannot store (before any SQL).
        """
        output = output_to_json(self.output)
        error = error_to_json(self.error)
        return {
            "status": self.status.value,
            "outcome": self.outcome.value,
            "output": None if output is None else dumps_jsonb(output),
            "error": None if error is None else dumps_jsonb(error),
            "failure_inc": self.failure_inc,
            "delay": self.delay_s,
        }


def succeeded(output: Mapping[str, ItemList]) -> NodeFinish:
    """A confirmed success (known outcome, no failure recorded)."""
    return NodeFinish(status=NodeRunStatus.SUCCEEDED, output=output)


def from_ordinary_failure(
    error: NodeError,
    *,
    effect_class: EffectClass,
    failure_kind: FailureKind,
    failures_after_this_one: int,
    policy: RetryPolicy,
) -> NodeFinish:
    """An ordinary, definitive failure: ``retry_wait`` or ``failed`` per ``decide_retry``.

    ``failures_after_this_one`` includes this failure (T3 increments ``failures``
    before the retry decision, architecture 5.1). The outcome is ``UNKNOWN`` only for an
    ``unsafe_write`` failing ``POST_DISPATCH_AMBIGUOUS`` (see the DECIDED note in the module
    docstring), and ``KNOWN`` otherwise. In that one case the persisted error gets
    ``cause = "post_dispatch_ambiguous"`` (``code`` and ``message`` are kept), which is what
    makes the run-failing decision reconstructible from the row; a caller-supplied ``cause``
    other than that value is refused rather than silently overwritten.
    """
    unknown = effect_class is EffectClass.UNSAFE_WRITE and failure_kind is FailureKind.POST_DISPATCH_AMBIGUOUS
    outcome = Outcome.UNKNOWN if unknown else Outcome.KNOWN
    if unknown:
        if error.cause not in (None, POST_DISPATCH_AMBIGUOUS_CAUSE):
            raise ValueError(
                f"an unsafe_write post-dispatch-ambiguous failure is persisted with cause "
                f"{POST_DISPATCH_AMBIGUOUS_CAUSE!r}; the supplied cause {error.cause!r} conflicts"
            )
        error = NodeError(code=error.code, message=error.message, cause=POST_DISPATCH_AMBIGUOUS_CAUSE)
    decision = decide_retry(
        effect_class=effect_class,
        failure_kind=failure_kind,
        failures_after_this_one=failures_after_this_one,
        policy=policy,
        clock=_ZERO_CLOCK,
    )
    if decision.should_retry:
        return NodeFinish(
            status=NodeRunStatus.RETRY_WAIT,
            outcome=outcome,
            error=error,
            failure_inc=1,
            delay_s=decision.retry_at,
        )
    return NodeFinish(status=NodeRunStatus.FAILED, outcome=outcome, error=error, failure_inc=1)


def _checked(finish: NodeFinish, resolution: "InterruptionResolution") -> NodeFinish:
    """The engine stays the authority: the derived flag must equal the engine's own decision.

    A disagreement means a writer set the wrong ``error.cause`` (or the engine matrix changed
    without this module following), and would make the persisted rule diverge from the
    engine's. It fails loudly instead.
    """
    if finish.forces_run_failure != resolution.forces_run_failure:
        raise AssertionError(
            f"derived forces_run_failure={finish.forces_run_failure} disagrees with the engine's "
            f"{resolution.forces_run_failure} for status={finish.status.value}, "
            f"outcome={finish.outcome.value}, cause={finish.error.cause if finish.error else None!r}"
        )
    return finish


def from_interruption(
    effect_class: EffectClass,
    cause: InterruptionCause,
    *,
    failures_after_this_one: Optional[int] = None,
    policy: Optional[RetryPolicy] = None,
    dispatched: Optional[bool] = None,
) -> NodeFinish:
    """Resolve an interrupted attempt (cancel, node timeout) per the 6.2 matrix.

    Node timeout needs ``failures_after_this_one`` and ``policy``: every effect class
    consults the retry budget, and the ``idempotent_write`` rule is budget-aware.
    """
    if cause is InterruptionCause.CANCEL:
        resolution = resolve_interruption(effect_class, cause, dispatched=dispatched)
        error = (
            NodeError(code=resolution.error_code, message=UNKNOWN_OUTCOME_MESSAGE, cause="cancel")
            if resolution.error_code
            else None
        )
        return _checked(
            NodeFinish(status=resolution.status, outcome=resolution.outcome, error=error), resolution
        )

    if cause is not InterruptionCause.NODE_TIMEOUT:
        raise ValueError(f"unsupported interruption cause: {cause!r}")
    if failures_after_this_one is None or policy is None:
        raise TypeError("a node timeout needs failures_after_this_one and policy (the retry budget decides)")

    resolution = resolve_interruption(
        effect_class,
        cause,
        dispatched=dispatched,
        failures_after_this_one=failures_after_this_one,
        policy=policy,
        clock=_ZERO_CLOCK,
    )
    code = resolution.error_code or TIMEOUT_ERROR_CODE
    message = UNKNOWN_OUTCOME_MESSAGE if resolution.outcome is Outcome.UNKNOWN else TIMEOUT_MESSAGE
    error = NodeError(code=code, message=message, cause="timeout")

    status, delay = resolution.status, resolution.retry_at  # retry_at == delay (zero clock)
    if status is NodeRunStatus.FAILED and resolution.auto_retryable:
        # pure/read, or unsafe_write provably not dispatched: the ordinary retry budget decides.
        kind = (
            FailureKind.PRE_DISPATCH
            if effect_class is EffectClass.UNSAFE_WRITE and dispatched is False
            else FailureKind.ORDINARY
        )
        decision = decide_retry(
            effect_class=effect_class,
            failure_kind=kind,
            failures_after_this_one=failures_after_this_one,
            policy=policy,
            clock=_ZERO_CLOCK,
        )
        if decision.should_retry:
            status, delay = NodeRunStatus.RETRY_WAIT, decision.retry_at

    return _checked(
        NodeFinish(
            status=status,
            outcome=resolution.outcome,
            error=error,
            failure_inc=1,
            delay_s=delay if status is NodeRunStatus.RETRY_WAIT else None,
        ),
        resolution,
    )
