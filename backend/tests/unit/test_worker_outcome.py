"""Stage 1 of M2.5: ``NodeFinish`` and its builders (no database).

The builders must TRANSLATE the pure engine's decisions, not restate them. Every
expectation below is therefore computed by calling the engine function directly and
comparing, never by hard-coding the retry or interruption policy a second time (the
literal matrix cells are pinned by the engine's own M1 tests).
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import json
import re
import unittest
from pathlib import Path

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.domain.items import items_from
from flowforge.domain.node_run import NodeError
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.engine.clock import FakeClock
from flowforge.engine.interruption import InterruptionCause, resolve_interruption
from flowforge.engine.retry import FailureKind, decide_retry
from flowforge.worker import outcome
from flowforge.worker.codec import CodecError
from flowforge.worker.outcome import POST_DISPATCH_AMBIGUOUS_CAUSE, NodeFinish

SRC = Path(__file__).resolve().parents[2] / "src" / "flowforge" / "worker"
POLICIES = (
    RetryPolicy(max_attempts=1),
    RetryPolicy(max_attempts=3, backoff_base_s=1.5, backoff_multiplier=2.0, backoff_max_s=4.0),
    RetryPolicy(max_attempts=5, backoff_base_s=0.25, backoff_multiplier=3.0, backoff_max_s=100.0),
)
ERROR = NodeError("boom", "it failed")


def expected_ordinary_outcome(effect: EffectClass, kind: FailureKind) -> Outcome:
    """The M3 decision, stated once: only this combination leaves the external write in doubt."""
    if effect is EffectClass.UNSAFE_WRITE and kind is FailureKind.POST_DISPATCH_AMBIGUOUS:
        return Outcome.UNKNOWN
    return Outcome.KNOWN
BILLING_WORDS = re.compile(r"charge|bill|cost|price|usage", re.IGNORECASE)


class NodeFinishValidationTestCase(unittest.TestCase):
    def test_only_t3_target_statuses_are_allowed(self) -> None:
        for status in (NodeRunStatus.RUNNING, NodeRunStatus.SKIPPED):
            with self.subTest(status=status), self.assertRaises(ValueError):
                NodeFinish(status=status)
        for status in (NodeRunStatus.SUCCEEDED, NodeRunStatus.FAILED, NodeRunStatus.CANCELLED):
            NodeFinish(status=status)
        NodeFinish(status=NodeRunStatus.RETRY_WAIT, delay_s=0)

    def test_retry_wait_needs_a_finite_non_negative_delay_and_nothing_else_may_have_one(self) -> None:
        for bad in (None, -1, float("inf"), float("nan"), True, "5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                NodeFinish(status=NodeRunStatus.RETRY_WAIT, delay_s=bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            NodeFinish(status=NodeRunStatus.FAILED, delay_s=1.0)

    def test_failure_increment_is_zero_or_one_and_never_a_bool(self) -> None:
        for bad in (2, -1, True, 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                NodeFinish(status=NodeRunStatus.FAILED, failure_inc=bad)  # type: ignore[arg-type]

    def test_an_unknown_outcome_is_never_a_success_and_a_success_records_no_failure(self) -> None:
        with self.assertRaisesRegex(ValueError, "never a success"):
            NodeFinish(status=NodeRunStatus.SUCCEEDED, outcome=Outcome.UNKNOWN)
        with self.assertRaises(ValueError):
            NodeFinish(status=NodeRunStatus.SUCCEEDED, failure_inc=1)
        with self.assertRaises(ValueError):
            NodeFinish(status=NodeRunStatus.SUCCEEDED, error=ERROR)

    def test_it_is_immutable_and_defensively_copied(self) -> None:
        source = {"main": [items_from({"a": 1})[0]]}
        finish = NodeFinish(status=NodeRunStatus.SUCCEEDED, output=source)  # type: ignore[arg-type]
        source["main"].append(items_from({"b": 2})[0])
        source["other"] = []
        self.assertEqual(list(finish.output), ["main"])
        self.assertEqual(len(finish.output["main"]), 1)
        with self.assertRaises(TypeError):
            finish.output["x"] = ()  # type: ignore[index]
        with self.assertRaises(Exception):
            finish.status = NodeRunStatus.FAILED  # type: ignore[misc]


class SqlParamsTestCase(unittest.TestCase):
    def test_success_params(self) -> None:
        params = outcome.succeeded({"main": items_from({"é": [1]})}).to_sql_params()
        self.assertEqual(params["status"], "succeeded")
        self.assertEqual(params["outcome"], "known")
        self.assertEqual(json.loads(params["output"]), {"main": [{"json": {"é": [1]}}]})
        self.assertEqual((params["error"], params["failure_inc"], params["delay"]), (None, 0, None))

    def test_empty_output_is_null_and_an_empty_port_is_kept(self) -> None:
        self.assertIsNone(outcome.succeeded({}).to_sql_params()["output"])
        self.assertEqual(json.loads(outcome.succeeded({"main": ()}).to_sql_params()["output"]), {"main": []})

    def test_failure_params_carry_the_error_and_the_delay(self) -> None:
        finish = NodeFinish(status=NodeRunStatus.RETRY_WAIT, error=ERROR, failure_inc=1, delay_s=2.5)
        params = finish.to_sql_params()
        self.assertEqual(json.loads(params["error"]), {"code": "boom", "message": "it failed", "cause": None})
        self.assertEqual((params["status"], params["failure_inc"], params["delay"]), ("retry_wait", 1, 2.5))

    def test_the_parameters_are_exactly_the_approved_t3_statements(self) -> None:
        # design 4.4 binds :status :outcome :output :error :failure_inc :delay (plus the row identity).
        # Anything else would be silently ignored by the driver: a data-loss trap.
        expected = {"status", "outcome", "output", "error", "failure_inc", "delay"}
        for finish in (
            outcome.succeeded({"main": items_from({"a": 1})}),
            NodeFinish(status=NodeRunStatus.RETRY_WAIT, error=ERROR, failure_inc=1, delay_s=1.0),
            NodeFinish(status=NodeRunStatus.CANCELLED),
        ):
            self.assertEqual(set(finish.to_sql_params()), expected)

    def test_item_results_are_not_part_of_the_write_contract(self) -> None:
        # Option A (Stage 1 audit): the approved T3 statement does not persist node_runs.item_results,
        # so neither NodeFinish nor any builder may advertise it.
        self.assertNotIn("item_results", {f.name for f in dataclasses.fields(NodeFinish)})
        for builder in (outcome.succeeded, outcome.from_ordinary_failure, outcome.from_interruption):
            self.assertNotIn("item_results", inspect.signature(builder).parameters, builder.__name__)
        with self.assertRaises(TypeError):
            NodeFinish(status=NodeRunStatus.SUCCEEDED, item_results=())  # type: ignore[call-arg]

    def test_a_string_postgresql_cannot_store_is_refused_before_any_sql(self) -> None:
        nul, surrogate = "a\x00b", "a\ud800b"
        for finish in (
            outcome.succeeded({"main": items_from({"v": nul})}),
            outcome.succeeded({"main": items_from({"v": surrogate})}),
            outcome.succeeded({"main": items_from({nul: 1})}),
            NodeFinish(status=NodeRunStatus.FAILED, error=NodeError("c", f"bad {nul} message"), failure_inc=1),
        ):
            with self.assertRaises(CodecError):
                finish.to_sql_params()

    def test_parameters_are_deterministic_text(self) -> None:
        a = NodeFinish(status=NodeRunStatus.SUCCEEDED, output={"b": items_from({"z": 1, "a": 2}), "a": ()})
        self.assertEqual(a.to_sql_params(), a.to_sql_params())
        self.assertEqual(a.to_sql_params()["output"], '{"a":[],"b":[{"json":{"a":2,"z":1}}]}')

    def test_forces_run_failure_is_derived_and_never_persisted_as_such(self) -> None:
        finish = NodeFinish(status=NodeRunStatus.FAILED, outcome=Outcome.UNKNOWN, error=NodeError("c", "m", "crash"))
        self.assertTrue(finish.forces_run_failure)
        self.assertNotIn("forces_run_failure", finish.to_sql_params())  # only status/outcome/error carry it
        with self.assertRaises(TypeError):
            NodeFinish(status=NodeRunStatus.FAILED, forces_run_failure=True)  # type: ignore[call-arg]
        with self.assertRaises(AttributeError):
            finish.forces_run_failure = False  # type: ignore[misc]


class OrdinaryFailureBuilderTestCase(unittest.TestCase):
    def test_the_decision_is_exactly_decide_retrys(self) -> None:
        for effect, kind, policy, failures in itertools.product(EffectClass, FailureKind, POLICIES, (1, 2, 3, 5, 6)):
            with self.subTest(effect=effect, kind=kind, policy=policy.max_attempts, failures=failures):
                finish = outcome.from_ordinary_failure(
                    ERROR, effect_class=effect, failure_kind=kind, failures_after_this_one=failures, policy=policy
                )
                decision = decide_retry(
                    effect_class=effect, failure_kind=kind, failures_after_this_one=failures, policy=policy, clock=FakeClock(0.0)
                )
                self.assertEqual(finish.status is NodeRunStatus.RETRY_WAIT, decision.should_retry)
                self.assertEqual(finish.delay_s, decision.retry_at if decision.should_retry else None)
                unknown = expected_ordinary_outcome(effect, kind) is Outcome.UNKNOWN
                expected_error = NodeError(ERROR.code, ERROR.message, POST_DISPATCH_AMBIGUOUS_CAUSE) if unknown else ERROR
                self.assertEqual((finish.failure_inc, finish.error), (1, expected_error))
                self.assertIs(finish.outcome, expected_ordinary_outcome(effect, kind))  # the M3 decision
                self.assertEqual(finish.forces_run_failure, unknown)  # forced exactly when the outcome is unknown
                if not decision.should_retry:
                    self.assertIs(finish.status, NodeRunStatus.FAILED)

    def test_the_delay_equals_the_engines_backoff_with_no_rounding(self) -> None:
        policy = RetryPolicy(max_attempts=9, backoff_base_s=0.1, backoff_multiplier=1.1, backoff_max_s=1000.0)
        for failures in range(1, 9):
            finish = outcome.from_ordinary_failure(
                ERROR, effect_class=EffectClass.PURE, failure_kind=FailureKind.ORDINARY, failures_after_this_one=failures, policy=policy
            )
            expected = decide_retry(
                effect_class=EffectClass.PURE, failure_kind=FailureKind.ORDINARY, failures_after_this_one=failures,
                policy=policy, clock=FakeClock(0.0),
            ).retry_at
            self.assertEqual(finish.delay_s, expected)  # exact float equality

    # -- M3 (user decision): an unsafe_write failing POST_DISPATCH_AMBIGUOUS has an UNKNOWN outcome ----

    def test_m3_an_unsafe_write_that_failed_post_dispatch_ambiguous_is_unknown_and_failed(self) -> None:
        for policy, failures in itertools.product(POLICIES, (1, 2, 3, 5, 6)):
            with self.subTest(policy=policy.max_attempts, failures=failures):
                finish = outcome.from_ordinary_failure(
                    ERROR, effect_class=EffectClass.UNSAFE_WRITE, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
                    failures_after_this_one=failures, policy=policy,
                )
                # decide_retry never retries it, so it is failed (never retry_wait: an unknown unsafe_write
                # must not wait for a retry, architecture 6.2 / design 4.10) with the caller's own error.
                self.assertEqual((finish.status, finish.outcome), (NodeRunStatus.FAILED, Outcome.UNKNOWN))
                self.assertEqual((finish.failure_inc, finish.delay_s), (1, None))
                self.assertEqual(finish.error, NodeError(ERROR.code, ERROR.message, POST_DISPATCH_AMBIGUOUS_CAUSE))
                self.assertTrue(finish.forces_run_failure)  # decided: on_error must not override this
                self.assertEqual(finish.output, {})
                params = finish.to_sql_params()
                self.assertEqual((params["outcome"], params["output"]), ("unknown", None))
                self.assertEqual(json.loads(params["error"])["cause"], "post_dispatch_ambiguous")

    def test_m3_every_other_combination_stays_known(self) -> None:
        for effect, kind in itertools.product(EffectClass, FailureKind):
            if (effect, kind) == (EffectClass.UNSAFE_WRITE, FailureKind.POST_DISPATCH_AMBIGUOUS):
                continue
            with self.subTest(effect=effect, kind=kind):
                finish = outcome.from_ordinary_failure(
                    ERROR, effect_class=effect, failure_kind=kind, failures_after_this_one=1, policy=POLICIES[1]
                )
                self.assertIs(finish.outcome, Outcome.KNOWN)
                self.assertEqual(finish.to_sql_params()["outcome"], "known")

    def test_m3_the_other_effect_classes_failing_post_dispatch_ambiguous_remain_known(self) -> None:
        for effect in (EffectClass.PURE, EffectClass.READ, EffectClass.IDEMPOTENT_WRITE):
            finish = outcome.from_ordinary_failure(
                ERROR, effect_class=effect, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
                failures_after_this_one=1, policy=POLICIES[0],
            )
            self.assertEqual((finish.status, finish.outcome), (NodeRunStatus.FAILED, Outcome.KNOWN), effect)

    def test_m3_forces_the_run_to_fail_and_the_builder_never_overwrites_a_conflicting_cause(self) -> None:
        kwargs = dict(
            effect_class=EffectClass.UNSAFE_WRITE, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
            failures_after_this_one=1, policy=POLICIES[0],
        )
        self.assertTrue(outcome.from_ordinary_failure(ERROR, **kwargs).forces_run_failure)
        same = NodeError("c", "m", POST_DISPATCH_AMBIGUOUS_CAUSE)
        self.assertEqual(outcome.from_ordinary_failure(same, **kwargs).error, same)  # an identical cause is fine
        for conflicting in ("timeout", "cancel", "deadline", "crash", "other"):
            with self.subTest(cause=conflicting), self.assertRaisesRegex(ValueError, "conflicts"):
                outcome.from_ordinary_failure(NodeError("c", "m", conflicting), **kwargs)

    def test_m3_the_retry_decision_is_still_the_engines(self) -> None:
        # Unknown is an outcome, not a retry rule: the status comes from decide_retry, which refuses a
        # post-dispatch retry of an unsafe_write at any budget.
        for policy in POLICIES:
            decision = decide_retry(
                effect_class=EffectClass.UNSAFE_WRITE, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
                failures_after_this_one=1, policy=policy, clock=FakeClock(0.0),
            )
            self.assertFalse(decision.should_retry)
            finish = outcome.from_ordinary_failure(
                ERROR, effect_class=EffectClass.UNSAFE_WRITE, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
                failures_after_this_one=1, policy=policy,
            )
            self.assertIs(finish.status, NodeRunStatus.FAILED)


class InterruptionBuilderTestCase(unittest.TestCase):
    def test_cancel_is_exactly_resolve_interruptions_with_no_failure_counted(self) -> None:
        for effect, dispatched in itertools.product(EffectClass, (None, True, False)):
            with self.subTest(effect=effect, dispatched=dispatched):
                finish = outcome.from_interruption(effect, InterruptionCause.CANCEL, dispatched=dispatched)
                resolution = resolve_interruption(effect, InterruptionCause.CANCEL, dispatched=dispatched)
                self.assertEqual((finish.status, finish.outcome), (resolution.status, resolution.outcome))
                self.assertEqual(finish.forces_run_failure, resolution.forces_run_failure)
                self.assertEqual(finish.failure_inc, 0)  # a cancellation is not a failure (Stage 3 to confirm)
                self.assertIsNone(finish.delay_s)
                self.assertEqual(finish.error is not None, resolution.error_code is not None)
                if finish.error is not None:
                    self.assertEqual((finish.error.code, finish.error.cause), (resolution.error_code, "cancel"))

    def test_node_timeout_follows_the_engines_resolution_and_budget(self) -> None:
        for effect, dispatched, policy, failures in itertools.product(EffectClass, (None, True, False), POLICIES, (1, 2, 3, 6)):
            with self.subTest(effect=effect, dispatched=dispatched, policy=policy.max_attempts, failures=failures):
                finish = outcome.from_interruption(
                    effect, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=failures, policy=policy, dispatched=dispatched
                )
                resolution = resolve_interruption(
                    effect, InterruptionCause.NODE_TIMEOUT, dispatched=dispatched,
                    failures_after_this_one=failures, policy=policy, clock=FakeClock(0.0),
                )
                # Outcome and run-forcing come straight from the engine.
                self.assertEqual(finish.outcome, resolution.outcome)
                self.assertEqual(finish.forces_run_failure, resolution.forces_run_failure)
                self.assertEqual(finish.failure_inc, 1)  # a timeout is a failed attempt (Stage 3 to confirm)
                self.assertEqual(finish.error.cause, "timeout")
                if resolution.status is NodeRunStatus.RETRY_WAIT:  # the dedicated idempotent_write rule
                    self.assertEqual((finish.status, finish.delay_s), (NodeRunStatus.RETRY_WAIT, resolution.retry_at))
                elif resolution.auto_retryable:  # pure/read/unsafe-not-dispatched: the ordinary budget decides
                    kind = FailureKind.PRE_DISPATCH if effect is EffectClass.UNSAFE_WRITE else FailureKind.ORDINARY
                    decision = decide_retry(effect_class=effect, failure_kind=kind, failures_after_this_one=failures, policy=policy, clock=FakeClock(0.0))
                    self.assertEqual(finish.status is NodeRunStatus.RETRY_WAIT, decision.should_retry)
                    self.assertEqual(finish.delay_s, decision.retry_at if decision.should_retry else None)
                else:  # not retryable: the engine's terminal status stands
                    self.assertEqual((finish.status, finish.delay_s), (resolution.status, None))
                self.assertEqual(finish.error.code, resolution.error_code or outcome.TIMEOUT_ERROR_CODE)

    def test_the_idempotent_write_timeout_never_becomes_a_success_and_keeps_the_unknown_outcome(self) -> None:
        for failures in (1, 2):
            finish = outcome.from_interruption(
                EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=failures, policy=POLICIES[2]
            )
            self.assertIs(finish.status, NodeRunStatus.RETRY_WAIT)
            self.assertIs(finish.outcome, Outcome.UNKNOWN)
        exhausted = outcome.from_interruption(
            EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=5, policy=POLICIES[2]
        )
        self.assertEqual((exhausted.status, exhausted.outcome, exhausted.forces_run_failure), (NodeRunStatus.FAILED, Outcome.UNKNOWN, True))
        self.assertEqual(exhausted.error.code, "outcome_unknown_retries_exhausted")

    def test_an_unprovable_unsafe_write_timeout_is_failed_unknown_and_never_retried(self) -> None:
        for dispatched in (None, True):
            finish = outcome.from_interruption(
                EffectClass.UNSAFE_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1,
                policy=RetryPolicy(max_attempts=9), dispatched=dispatched,
            )
            self.assertEqual((finish.status, finish.outcome, finish.forces_run_failure), (NodeRunStatus.FAILED, Outcome.UNKNOWN, True))
            self.assertEqual(finish.error.code, "outcome_unknown")
            self.assertIsNone(finish.delay_s)

    def test_a_node_timeout_needs_the_retry_budget(self) -> None:
        with self.assertRaises(TypeError):
            outcome.from_interruption(EffectClass.PURE, InterruptionCause.NODE_TIMEOUT)
        with self.assertRaises(TypeError):
            outcome.from_interruption(EffectClass.PURE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1)

    def test_an_unsupported_cause_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            outcome.from_interruption(EffectClass.PURE, "crash")  # type: ignore[arg-type]


class ConventionsTestCase(unittest.TestCase):
    def test_no_message_mentions_billing_or_usage(self) -> None:
        # docs/message-catalog.md allowlist rule: only the three catalog ids may discuss it.
        texts = [outcome.TIMEOUT_MESSAGE, outcome.CANCEL_MESSAGE, outcome.UNKNOWN_OUTCOME_MESSAGE, outcome.TIMEOUT_ERROR_CODE]
        for effect, cause in itertools.product(EffectClass, InterruptionCause):
            finish = outcome.from_interruption(effect, cause, failures_after_this_one=1, policy=POLICIES[1], dispatched=None)
            if finish.error is not None:
                texts += [finish.error.code, finish.error.message, finish.error.cause or ""]
        for text in texts:
            self.assertIsNone(BILLING_WORDS.search(text), text)

    def test_the_module_has_no_database_imports_and_never_touches_the_clock(self) -> None:
        source = (SRC / "outcome.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(?:from|import)\s+(?:sqlalchemy|psycopg|alembic)\b", source, re.MULTILINE))
        self.assertNotIn("time.time", source)
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("monotonic", source)

    def test_it_restates_no_policy(self) -> None:
        source = (SRC / "outcome.py").read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", source, flags=re.S)  # prose may mention the engine's vocabulary
        for fragment in ("max_attempts", "backoff", "compute_backoff", "<= policy", ">= policy"):
            self.assertNotIn(fragment, code.replace("RetryPolicy", ""), fragment)


if __name__ == "__main__":
    unittest.main()
