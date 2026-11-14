"""The durable forced-failure rule against PostgreSQL 17.

Every statement below is the APPROVED SQL of docs/m2-persistence-design.md (T2 section 4.3,
T3 section 4.4, the finalization statements of section 4.10), copied from the design with its
inline SQL comments and indentation omitted; the statement text is otherwise unchanged and is
executed as written: the rule must work with the approved contract, not with a modified one.
State is re-read through a brand-new engine/connection, standing in for a worker that took over
after a crash and has nothing but the database; ``test_..._survives_total_loss_of_process_memory``
goes further and uses separate OS processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from sqlalchemy import text

from flowforge.db.engine import create_db_engine
from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import Edge, NodeDefinition, RetryPolicy, WorkflowDefinition
from flowforge.domain.items import items_from
from flowforge.domain.node_run import NodeError
from flowforge.engine.interruption import InterruptionCause
from flowforge.engine.retry import FailureKind
from flowforge.engine.scheduler import plan_next_step
from flowforge.worker import codec, outcome
from flowforge.worker.outcome import node_forces_run_failure, run_must_fail
from flowforge.worker.snapshot import load_run_snapshot
from tests.integration.support import Sql, make_node_run, make_version
from tests.integration.worker_support import WorkerDatabaseTestCase, as_id
from tests.unit.support import new_catalog

# -- the approved statements (verbatim) ---------------------------------------------------

T3 = """
    UPDATE node_runs SET
           status   = :status,
           outcome  = :outcome,
           output   = :output,
           error    = :error,
           failures = failures + :failure_inc,
           retry_at = CASE WHEN CAST(:status AS text) = 'retry_wait'
                           THEN now() + CAST(:delay AS double precision)
                                       * interval '1 second'
                           ELSE NULL END
     WHERE run_id = :run_id AND node_key = :node_key
       AND status = 'running'
       AND started_epoch = :epoch
       AND attempt = :attempt
    RETURNING status, attempt
"""

T2 = """
    INSERT INTO node_runs (run_id, node_key, status, attempt, failures,
                           outcome, started_epoch, effect_class, started_at)
    VALUES (:run_id, :node_key, 'running', 1, 0, 'known', :epoch,
            :resolved_class, now())
    ON CONFLICT (run_id, node_key) DO UPDATE SET
           status        = 'running',
           attempt        = node_runs.attempt + 1,
           started_epoch  = :epoch,
           started_at     = now(),
           retry_at       = NULL
     WHERE (node_runs.status = 'retry_wait' AND node_runs.retry_at <= now())
        OR (node_runs.status = 'running'    AND node_runs.started_epoch < :epoch)
    RETURNING run_id, node_key, status, attempt, effect_class
"""

RESOLVE_DEADLINE_ORPHANS = """
    UPDATE node_runs n SET
           status   = CASE WHEN n.effect_class = 'unsafe_write' THEN 'failed' ELSE 'cancelled' END,
           outcome  = CASE WHEN n.effect_class IN ('idempotent_write', 'unsafe_write')
                           THEN 'unknown' ELSE n.outcome END,
           error    = CASE WHEN n.effect_class = 'unsafe_write'
                           THEN jsonb_build_object('code', 'outcome_unknown', 'cause', 'deadline',
                                                   'message', 'orphaned attempt; dispatch cannot be disproven')
                           ELSE n.error END,
           retry_at = NULL
      FROM runs r
     WHERE n.run_id = :run_id
       AND r.id = n.run_id
       AND r.status = 'running'
       AND r.lease_owner = :worker AND r.lease_epoch = :epoch
       AND r.deadline_at <= clock_timestamp()
       AND n.status = 'running'
       AND n.started_epoch < r.lease_epoch
    RETURNING n.node_key, n.status, n.outcome, n.effect_class, n.attempt
"""

RESOLVE_PENDING_RETRIES = """
    UPDATE node_runs SET
           status   = CASE WHEN effect_class = 'unsafe_write' AND outcome = 'unknown'
                           THEN 'failed' ELSE 'cancelled' END,
           retry_at = NULL
     WHERE run_id = :run_id AND status = 'retry_wait'
    RETURNING node_key, status, outcome, effect_class, attempt
"""

POLICY = RetryPolicy(max_attempts=3)

# Two throw-away processes, following the M2.4 subprocess convention (test_shutdown.py): the first
# commits a forced failure through the approved T3 statement and dies abnormally (no cleanup); the
# second, a different process, rebuilds the decision knowing nothing but the database URL.
_WRITER = """
import os, sys, uuid
from sqlalchemy import text
from flowforge.db.engine import create_db_engine
from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import RetryPolicy
from flowforge.domain.node_run import NodeError
from flowforge.engine.retry import FailureKind
from flowforge.worker import outcome

engine = create_db_engine(os.environ["FF_URL"])
finish = outcome.from_ordinary_failure(
    NodeError("connection_reset", "reset after the request was sent"),
    effect_class=EffectClass.UNSAFE_WRITE, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
    failures_after_this_one=1, policy=RetryPolicy(max_attempts=3),
)
with engine.begin() as conn:  # committed when this block exits
    row = conn.execute(
        text(os.environ["FF_T3"]),
        {**finish.to_sql_params(), "run_id": uuid.UUID(os.environ["FF_RUN"]), "node_key": "pay", "epoch": 1, "attempt": 1},
    ).fetchone()
print("WROTE", os.getpid(), tuple(row), flush=True)
os._exit(9)  # abnormal termination: no cleanup, no release, nothing flushed but the commit above
"""

_READER = """
import json, os, sys
from flowforge.db.engine import create_db_engine
from flowforge.worker.outcome import node_forces_run_failure, run_must_fail
from flowforge.worker.snapshot import load_run_snapshot

engine = create_db_engine(os.environ["FF_URL"])
result = {"pid": os.getpid(), "imported_test_modules": any(name.split(".")[0] == "tests" for name in sys.modules)}
for run_id in os.environ["FF_RUNS"].split(","):
    with engine.connect() as conn:
        snapshot = load_run_snapshot(conn, run_id)
    result[run_id] = {
        "must_fail": run_must_fail(snapshot.node_runs),
        "nodes": {n.node_key: node_forces_run_failure(n) for n in snapshot.node_runs},
    }
print(json.dumps(result), flush=True)
"""


class ForcedFailureTestCase(WorkerDatabaseTestCase):
    # -- helpers -------------------------------------------------------------------------------

    def _t3(self, run_id, node_key, finish, *, epoch=1, attempt=1):
        with self.engine.begin() as conn:
            row = conn.execute(
                text(T3), {**finish.to_sql_params(), "run_id": as_id(run_id), "node_key": node_key, "epoch": epoch, "attempt": attempt}
            ).fetchone()
        return None if row is None else tuple(row)

    def _t2(self, run_id, node_key, effect_class, *, epoch=1):
        with self.engine.begin() as conn:
            row = conn.execute(
                text(T2), {"run_id": as_id(run_id), "node_key": node_key, "epoch": epoch, "resolved_class": effect_class}
            ).fetchone()
        return None if row is None else tuple(row)[2:4]  # (status, attempt)

    def _stored(self, run_id, node_key):
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT * FROM node_runs WHERE run_id = :id AND node_key = :k"), {"id": as_id(run_id), "k": node_key}
            ).mappings().one()

    def _after_a_crash(self, run_id):
        """A brand-new engine and connection: nothing survives but the database."""
        fresh = create_db_engine(self._temp_db.url)
        self.addCleanup(fresh.dispose)
        with fresh.connect() as conn:
            return load_run_snapshot(conn, str(run_id))

    def _make_due(self, run_id, node_key):
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE node_runs SET retry_at = now() - interval '1 second' WHERE run_id = :id AND node_key = :k"),
                {"id": as_id(run_id), "k": node_key},
            )

    def _running(self, run_id, node_key, effect_class, **overrides):
        fields = dict(status="running", started_epoch=1, attempt=1, effect_class=effect_class) | overrides
        with self.engine.begin() as conn:
            make_node_run(conn, as_id(run_id), node_key, **fields)

    # -- the M3 case, written by the approved T3 statement ----------------------------------------

    def test_an_unsafe_write_that_failed_post_dispatch_ambiguous_is_persisted_and_forces_failure_after_a_crash(self) -> None:
        run_id = self.add_run()
        self._running(run_id, "pay", "unsafe_write")
        finish = outcome.from_ordinary_failure(
            NodeError("connection_reset", "reset after the request was sent"),
            effect_class=EffectClass.UNSAFE_WRITE, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
            failures_after_this_one=1, policy=POLICY,
        )
        self.assertEqual(self._t3(run_id, "pay", finish), ("failed", 1))

        row = self._stored(run_id, "pay")
        self.assertEqual((row["status"], row["outcome"], row["failures"]), ("failed", "unknown", 1))
        self.assertIsNone(row["output"])  # an unknown outcome carries no output: nothing for a route to read
        self.assertIsNone(row["retry_at"])  # never retried
        self.assertEqual(row["error"], {"code": "connection_reset", "message": "reset after the request was sent", "cause": "post_dispatch_ambiguous"})

        snapshot = self._after_a_crash(run_id)  # the in-memory NodeFinish is long gone
        self.assertTrue(run_must_fail(snapshot.node_runs))
        self.assertTrue(node_forces_run_failure(snapshot.node_run("pay")))

    def _child_env(self, **extra):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        env["FF_URL"] = self._temp_db.url.render_as_string(hide_password=False)
        env.update(extra)
        return env

    def test_forced_failure_reconstruction_survives_total_loss_of_process_memory(self) -> None:
        forced = self.add_run()
        self._running(forced, "pay", "unsafe_write")  # the writer process finishes this attempt
        with self.engine.begin() as conn:
            make_node_run(conn, as_id(forced), "ok", status="succeeded", effect_class="pure", attempt=1, started_epoch=1,
                          output={"main": [{"json": {"x": 1}}]})  # fmt: skip
        quiet = self.add_run()  # a control run holding only rows that must NOT force failure
        with self.engine.begin() as conn:
            make_node_run(conn, as_id(quiet), "retry", status="retry_wait", effect_class="idempotent_write", outcome="unknown", attempt=2,
                          failures=1, started_epoch=1, retry_at=Sql("now() + interval '1 minute'"),
                          error={"code": "node_timeout", "message": "m", "cause": "timeout"})  # fmt: skip
            make_node_run(conn, as_id(quiet), "cancelled_unknown", status="cancelled", effect_class="idempotent_write", outcome="unknown",
                          attempt=1, failures=0, started_epoch=1)  # fmt: skip
            make_node_run(conn, as_id(quiet), "deadline_unknown", status="failed", effect_class="unsafe_write", outcome="unknown",
                          attempt=1, failures=1, started_epoch=1,
                          error={"code": "outcome_unknown", "message": "m", "cause": "deadline"})  # fmt: skip
            make_node_run(conn, as_id(quiet), "plain_failure", status="failed", effect_class="pure", attempt=1, failures=1, started_epoch=1,
                          error={"code": "boom", "message": "m", "cause": None})  # fmt: skip

        # Process 1 writes the forced failure with the approved T3 statement, then dies without cleanup.
        writer = subprocess.run(
            [sys.executable, "-c", _WRITER], capture_output=True, text=True, timeout=120,
            env=self._child_env(FF_RUN=str(forced), FF_T3=" ".join(T3.split())),
        )  # fmt: skip
        self.assertEqual(writer.returncode, 9, writer.stderr)
        self.assertIn("WROTE", writer.stdout)
        writer_pid = int(writer.stdout.split()[1])
        self.assertEqual(self._stored(forced, "pay")["outcome"], "unknown")  # committed before the process died

        # Process 2 is a different process: it has no object, cache or connection from process 1 or from this test.
        reader = subprocess.run(
            [sys.executable, "-c", _READER], capture_output=True, text=True, timeout=120,
            env=self._child_env(FF_RUNS=f"{forced},{quiet}"),
        )  # fmt: skip
        self.assertEqual(reader.returncode, 0, reader.stderr)
        result = json.loads(reader.stdout.strip().splitlines()[-1])

        self.assertNotIn(result["pid"], (os.getpid(), writer_pid))  # genuinely a third process
        self.assertFalse(result["imported_test_modules"])  # it ran library code only, none of this test's state
        self.assertTrue(result[str(forced)]["must_fail"])
        self.assertEqual(result[str(forced)]["nodes"], {"ok": False, "pay": True})
        self.assertFalse(result[str(quiet)]["must_fail"])
        self.assertEqual(set(result[str(quiet)]["nodes"].values()), {False})

    def test_t3_clears_a_previously_non_null_output_when_a_later_attempt_finishes_unknown(self) -> None:
        # The approved T3 statement always writes `output = :output`. A finish with an UNKNOWN outcome has no
        # output (NodeFinish invariant), so the column becomes NULL and nothing from an earlier attempt survives
        # for a route to read.
        run_id = self.add_run()
        previous = {"error": [{"json": {"status": 502}}], "main": []}
        unsafe, idempotent = EffectClass.UNSAFE_WRITE, EffectClass.IDEMPOTENT_WRITE
        cases = {
            "post_dispatch": (unsafe, "failed", outcome.from_ordinary_failure(
                NodeError("reset", "m"), effect_class=unsafe, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
                failures_after_this_one=1, policy=POLICY)),
            "unsafe_timeout": (unsafe, "failed", outcome.from_interruption(
                unsafe, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1, policy=POLICY)),
            "unsafe_cancel": (unsafe, "failed", outcome.from_interruption(unsafe, InterruptionCause.CANCEL)),
            "idempotent_timeout": (idempotent, "retry_wait", outcome.from_interruption(
                idempotent, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=1, policy=POLICY)),
            "idempotent_cancel": (idempotent, "cancelled", outcome.from_interruption(idempotent, InterruptionCause.CANCEL)),
        }  # fmt: skip
        for key, (effect, expected_status, finish) in cases.items():
            with self.subTest(case=key):
                self.assertEqual(finish.outcome.value, "unknown")
                self._running(run_id, key, effect.value, output=previous)
                self.assertEqual(self._stored(run_id, key)["output"], previous)  # non-vacuous: it really was non-NULL
                self.assertEqual(self._t3(run_id, key, finish)[0], expected_status)
                row = self._stored(run_id, key)
                self.assertIsNone(row["output"])
                self.assertEqual(row["outcome"], "unknown")

    def test_the_other_effect_classes_failing_the_same_way_stay_known_and_do_not_force(self) -> None:
        run_id = self.add_run()
        for key, effect in (("p", EffectClass.PURE), ("r", EffectClass.READ), ("w", EffectClass.IDEMPOTENT_WRITE)):
            self._running(run_id, key, effect.value)
            finish = outcome.from_ordinary_failure(
                NodeError("boom", "failed"), effect_class=effect, failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS,
                failures_after_this_one=3, policy=POLICY,
            )
            self.assertEqual(self._t3(run_id, key, finish), ("failed", 1))
            self.assertEqual(self._stored(run_id, key)["outcome"], "known")
        self.assertFalse(run_must_fail(self._after_a_crash(run_id).node_runs))

    # -- idempotent_write: retry while the budget lasts, force only when it is exhausted -------------

    def test_an_idempotent_write_timeout_is_retried_until_the_budget_is_exhausted_and_only_then_forces(self) -> None:
        run_id = self.add_run()
        self._running(run_id, "w", "idempotent_write")
        for attempt in (1, 2, 3):
            failures = self._stored(run_id, "w")["failures"]
            finish = outcome.from_interruption(
                EffectClass.IDEMPOTENT_WRITE, InterruptionCause.NODE_TIMEOUT, failures_after_this_one=failures + 1, policy=POLICY
            )
            self.assertEqual(self._t3(run_id, "w", finish, attempt=attempt)[0], "failed" if attempt == 3 else "retry_wait")
            snapshot = self._after_a_crash(run_id)
            node = snapshot.node_run("w")
            self.assertEqual(node.outcome.value, "unknown")  # the outcome stays unknown throughout
            self.assertEqual(run_must_fail(snapshot.node_runs), attempt == 3, f"attempt {attempt}")
            if attempt < 3:
                self.assertEqual(node.status.value, "retry_wait")  # still repeatable with the same key
                self._make_due(run_id, "w")
                self.assertEqual(self._t2(run_id, "w", "idempotent_write"), ("running", attempt + 1))
        last = self._stored(run_id, "w")
        self.assertEqual((last["status"], last["outcome"], last["failures"]), ("failed", "unknown", 3))
        self.assertEqual(last["error"]["cause"], "timeout")
        self.assertEqual(last["error"]["code"], "outcome_unknown_retries_exhausted")

    def test_a_crash_during_an_unknown_idempotent_write_restarts_it_and_never_forces(self) -> None:
        # Crash column for idempotent_write: re-execute with the same key. The row is restarted by
        # T2's orphan branch, stays running and keeps its recorded outcome until the next T3.
        run_id = self.add_run()
        self._running(run_id, "w", "idempotent_write", outcome="unknown", attempt=2, failures=1)
        self.assertFalse(run_must_fail(self._after_a_crash(run_id).node_runs))  # an orphan awaiting restart

        self.assertEqual(self._t2(run_id, "w", "idempotent_write", epoch=2), ("running", 3))  # the new epoch restarts it
        row = self._stored(run_id, "w")
        self.assertEqual((row["status"], row["outcome"], row["started_epoch"], row["failures"]), ("running", "unknown", 2, 1))
        self.assertFalse(run_must_fail(self._after_a_crash(run_id).node_runs))

        done = outcome.succeeded({"main": items_from({"ok": True})})
        self.assertEqual(self._t3(run_id, "w", done, epoch=2, attempt=3), ("succeeded", 3))  # a definitive result
        self.assertEqual(self._stored(run_id, "w")["outcome"], "known")
        self.assertFalse(run_must_fail(self._after_a_crash(run_id).node_runs))

    # -- the approved finalization statements never create a forced failure for the real cases -------

    def test_retry_wait_suppression_resolves_an_unknown_idempotent_write_to_cancelled_unknown_without_forcing(self) -> None:
        run_id = self.add_run()
        error = {"code": "node_timeout", "message": "attempt exceeded its timeout", "cause": "timeout"}
        with self.engine.begin() as conn:
            make_node_run(conn, run_id, "idem_unknown", status="retry_wait", effect_class="idempotent_write", outcome="unknown",
                          attempt=2, failures=1, started_epoch=1, retry_at=Sql("now() + interval '1 minute'"), error=error)  # fmt: skip
            for key, effect in (("pure", "pure"), ("read", "read"), ("idem_known", "idempotent_write"), ("unsafe_known", "unsafe_write")):
                make_node_run(conn, run_id, key, status="retry_wait", effect_class=effect, attempt=2, failures=1,
                              started_epoch=1, retry_at=Sql("now() + interval '1 minute'"))  # fmt: skip
        before = self._stored(run_id, "idem_unknown")

        with self.engine.begin() as conn:
            resolved = {r[0]: tuple(r[1:4]) for r in conn.execute(text(RESOLVE_PENDING_RETRIES), {"run_id": as_id(run_id)})}

        self.assertEqual(resolved["idem_unknown"], ("cancelled", "unknown", "idempotent_write"))  # outcome preserved
        for key in ("pure", "read", "idem_known", "unsafe_known"):
            self.assertEqual(resolved[key][:2], ("cancelled", "known"))
        after = self._stored(run_id, "idem_unknown")
        for column in ("outcome", "error", "output", "attempt", "failures", "started_epoch", "effect_class"):
            self.assertEqual(after[column], before[column], column)  # the approved SET list touches only status/retry_at
        self.assertIsNone(after["retry_at"])
        snapshot = self._after_a_crash(run_id)
        self.assertFalse(node_forces_run_failure(snapshot.node_run("idem_unknown")))
        self.assertFalse(run_must_fail(snapshot.node_runs))

    def test_the_approved_deadline_orphan_resolution_writes_a_non_forcing_cause(self) -> None:
        run_id = self.add_run(
            status="running", lease_owner="w2", lease_epoch=2, started_at=Sql("now() - interval '2 hours'"),
            deadline_at=Sql("now() - interval '1 second'"), visible_at=Sql("now() + interval '30 seconds'"),
        )  # fmt: skip
        self._running(run_id, "unsafe", "unsafe_write")
        self._running(run_id, "pure", "pure")
        self._running(run_id, "idem", "idempotent_write")
        with self.engine.begin() as conn:
            resolved = {
                r[0]: tuple(r[1:3])
                for r in conn.execute(text(RESOLVE_DEADLINE_ORPHANS), {"run_id": as_id(run_id), "worker": "w2", "epoch": 2})
            }
        self.assertEqual(resolved, {"unsafe": ("failed", "unknown"), "pure": ("cancelled", "known"), "idem": ("cancelled", "unknown")})
        row = self._stored(run_id, "unsafe")
        self.assertEqual((row["error"]["code"], row["error"]["cause"]), ("outcome_unknown", "deadline"))
        snapshot = self._after_a_crash(run_id)
        self.assertFalse(node_forces_run_failure(snapshot.node_run("unsafe")))  # that run ends timed_out, not failed
        self.assertFalse(run_must_fail(snapshot.node_runs))

    # -- fail-closed controls: rows a faulty or older writer could leave behind ------------------------

    def test_a_failed_unknown_row_with_a_missing_or_unrecognized_cause_forces_failure(self) -> None:
        run_id = self.add_run()
        errors = {"no_error": None, "null_cause": {"code": "x", "message": "m", "cause": None}, "no_cause_key": {"code": "x"},
                  "mystery_cause": {"code": "x", "message": "m", "cause": "mystery"}}  # fmt: skip
        with self.engine.begin() as conn:
            for key, error in errors.items():
                make_node_run(conn, run_id, key, status="failed", outcome="unknown", effect_class="unsafe_write",
                              attempt=1, failures=1, started_epoch=1, error=error)  # fmt: skip
        snapshot = self._after_a_crash(run_id)
        for key in errors:
            with self.subTest(row=key):
                self.assertTrue(node_forces_run_failure(snapshot.node_run(key)))

    def test_failed_unknown_rows_with_the_exempt_causes_do_not_force_and_known_rows_never_do(self) -> None:
        run_id = self.add_run()
        with self.engine.begin() as conn:
            for key, cause in (("cancelled_run", "cancel"), ("timed_out_run", "deadline")):
                make_node_run(conn, run_id, key, status="failed", outcome="unknown", effect_class="unsafe_write", attempt=1,
                              failures=1, started_epoch=1, error={"code": "outcome_unknown", "message": "m", "cause": cause})  # fmt: skip
            make_node_run(conn, run_id, "known_failure", status="failed", outcome="known", effect_class="unsafe_write", attempt=1,
                          failures=1, started_epoch=1, error={"code": "x", "message": "m", "cause": "timeout"})  # fmt: skip
        self.assertFalse(run_must_fail(self._after_a_crash(run_id).node_runs))

    # -- the route that must not fire --------------------------------------------------------------------

    def _route_definition(self):
        return WorkflowDefinition(
            schema_version=1,
            nodes=(
                NodeDefinition(key="trigger", type="manual_trigger", type_version=1),
                NodeDefinition(key="post", type="http_request", type_version=1, params={"url": "https://example.test"}),
                NodeDefinition(key="notify", type="generic", type_version=1),
                NodeDefinition(key="other", type="set", type_version=1),
            ),
            edges=(
                Edge("e1", "trigger", "main", "post", "main"),
                Edge("e2", "post", "error", "notify", "main"),  # on_error = route: the error port feeds "notify"
                Edge("e3", "trigger", "main", "other", "main"),  # an independent branch
            ),
        )

    def _route_run(self, post_row):
        with self.engine.begin() as conn:
            version = make_version(conn, self.workflow_id, version_number=9, definition=codec.definition_to_json(self._route_definition()))
        run_id = self.add_run(workflow_version_id=version)
        with self.engine.begin() as conn:
            make_node_run(conn, run_id, "trigger", status="succeeded", effect_class="pure", attempt=1, started_epoch=1,
                          output={"main": [{"json": {"x": 1}}]})  # fmt: skip
            make_node_run(conn, run_id, "post", effect_class="unsafe_write", attempt=1, failures=1, started_epoch=1, **post_row)
        return run_id

    def test_an_unknown_outcome_feeds_no_error_route_but_the_loop_must_still_stop_the_independent_branch(self) -> None:
        unknown = outcome.from_ordinary_failure(
            NodeError("connection_reset", "reset"), effect_class=EffectClass.UNSAFE_WRITE,
            failure_kind=FailureKind.POST_DISPATCH_AMBIGUOUS, failures_after_this_one=1, policy=POLICY,
        )
        run_id = self._route_run({"status": "failed", "outcome": "unknown", "error": codec.error_to_json(unknown.error)})
        snapshot = self._after_a_crash(run_id)
        self.assertTrue(run_must_fail(snapshot.node_runs))
        step = plan_next_step(snapshot.definition, str(run_id), snapshot.run_store(), snapshot.clock(), new_catalog())
        self.assertEqual(step.newly_skipped, ("notify",))  # no error-port output: the route cannot fire
        self.assertEqual(step.ready, ("other",))  # nothing in the pure scheduler stops this: the loop must (run_must_fail)

    def test_the_same_failure_with_a_known_outcome_may_route_which_is_what_the_rule_protects(self) -> None:
        run_id = self._route_run(
            {"status": "failed", "outcome": "known", "error": {"code": "refused", "message": "4xx", "cause": None},
             "output": {"error": [{"json": {"status": 422}}]}}  # fmt: skip
        )
        snapshot = self._after_a_crash(run_id)
        self.assertFalse(run_must_fail(snapshot.node_runs))
        step = plan_next_step(snapshot.definition, str(run_id), snapshot.run_store(), snapshot.clock(), new_catalog())
        self.assertEqual(set(step.ready), {"notify", "other"})  # a known failure is handled by its route
