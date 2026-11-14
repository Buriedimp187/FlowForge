"""Stage 1 of M2.5: ``RunSnapshot`` / ``SnapshotClock`` against the pure engine (no database)."""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import Edge, NodeDefinition, WorkflowDefinition
from flowforge.domain.items import items_from
from flowforge.domain.node_run import NodeRun
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.engine.readiness import NodeReadiness, evaluate_readiness
from flowforge.engine.scheduler import plan_next_step
from flowforge.worker.codec import CodecError
from flowforge.worker.snapshot import RunSnapshot, SnapshotClock
from tests.unit.support import new_catalog

SRC = Path(__file__).resolve().parents[2] / "src" / "flowforge" / "worker"
DB_NOW = datetime(2026, 11, 14, 12, 0, 0, 250000, tzinfo=timezone.utc)
RUN = "00000000-0000-7000-8000-000000000001"


def _definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        nodes=(
            NodeDefinition(key="trigger", type="manual_trigger", type_version=1),
            NodeDefinition(key="a", type="set", type_version=1),
            NodeDefinition(key="b", type="set", type_version=1),
        ),
        edges=(Edge("e1", "trigger", "main", "a", "main"), Edge("e2", "a", "main", "b", "main")),
    )


def _snapshot(*rows: NodeRun, db_now: datetime = DB_NOW) -> RunSnapshot:
    return RunSnapshot(
        run_id=RUN, workflow_version_id="v", status="running", lease_owner="w1", lease_epoch=2, recovery_count=0,
        cancel_requested_at=None, deadline_at=None, queue_deadline_at=db_now, db_now=db_now,
        definition=_definition(), node_runs=tuple(rows),
    )  # fmt: skip


def _done(key: str, **kw) -> NodeRun:
    return NodeRun(run_id=RUN, node_key=key, status=NodeRunStatus.SUCCEEDED, attempt=1, effect_class=EffectClass.PURE,
                   output={"main": items_from({"x": 1})}, started_epoch=1, **kw)  # fmt: skip


class SnapshotClockTestCase(unittest.TestCase):
    def test_the_clock_is_fixed_at_database_time(self) -> None:
        clock = SnapshotClock(DB_NOW)
        self.assertEqual(clock.now(), DB_NOW.timestamp())
        self.assertEqual(clock.now(), clock.now())  # never advances on its own

    def test_a_naive_time_is_refused(self) -> None:
        with self.assertRaises(CodecError):
            SnapshotClock(datetime(2026, 1, 1))


class RunSnapshotTestCase(unittest.TestCase):
    def test_lookup_by_node_key(self) -> None:
        snap = _snapshot(_done("trigger"))
        self.assertEqual(snap.node_run("trigger").node_key, "trigger")
        self.assertIsNone(snap.node_run("a"))

    def test_each_run_store_is_a_fresh_copy_so_the_snapshot_stays_immutable(self) -> None:
        snap = _snapshot(_done("trigger"))
        first, second = snap.run_store(), snap.run_store()
        first.put(NodeRun(run_id=RUN, node_key="a", status=NodeRunStatus.SKIPPED))
        self.assertIsNotNone(first.get(RUN, "a"))
        self.assertIsNone(second.get(RUN, "a"))
        self.assertIsNone(snap.node_run("a"))
        self.assertEqual(len(snap.node_runs), 1)

    def test_the_pure_scheduler_runs_on_a_snapshot_without_a_database(self) -> None:
        catalog = new_catalog()
        snap = _snapshot(_done("trigger"))
        step = plan_next_step(snap.definition, RUN, snap.run_store(), snap.clock(), catalog)
        self.assertEqual((step.ready, step.newly_skipped), (("a",), ()))

    def test_skips_propagate_in_memory_and_leave_the_snapshot_untouched(self) -> None:
        catalog = new_catalog()
        failed = NodeRun(run_id=RUN, node_key="a", status=NodeRunStatus.FAILED, attempt=1, effect_class=EffectClass.PURE, started_epoch=1)
        snap = _snapshot(_done("trigger"), failed)
        step = plan_next_step(snap.definition, RUN, snap.run_store(), snap.clock(), catalog)
        self.assertEqual((step.ready, step.newly_skipped), ((), ("b",)))
        self.assertIsNone(snap.node_run("b"))  # persisting skips is a separate, fenced step

    def test_retry_readiness_is_decided_by_database_time_not_the_workers_clock(self) -> None:
        catalog = new_catalog()

        def waiting(retry_at: datetime) -> RunSnapshot:
            row = NodeRun(run_id=RUN, node_key="a", status=NodeRunStatus.RETRY_WAIT, attempt=1, failures=1,
                          effect_class=EffectClass.PURE, retry_at=retry_at.timestamp(), started_epoch=1)  # fmt: skip
            return _snapshot(_done("trigger"), row)

        def readiness(snap: RunSnapshot) -> NodeReadiness:
            return evaluate_readiness("a", snap.definition, RUN, snap.run_store(), snap.clock(), catalog)

        self.assertIs(readiness(waiting(DB_NOW - timedelta(microseconds=1))), NodeReadiness.READY)
        self.assertIs(readiness(waiting(DB_NOW)), NodeReadiness.READY)  # retry_at <= db_now
        self.assertIs(readiness(waiting(DB_NOW + timedelta(microseconds=1))), NodeReadiness.WAITING)

    def test_a_running_row_of_any_epoch_is_never_proposed_as_ready(self) -> None:
        catalog = new_catalog()
        orphan = NodeRun(run_id=RUN, node_key="a", status=NodeRunStatus.RUNNING, attempt=1, effect_class=EffectClass.PURE, started_epoch=1)
        snap = _snapshot(_done("trigger"), orphan)  # lease_epoch is 2: this row is an orphan
        step = plan_next_step(snap.definition, RUN, snap.run_store(), snap.clock(), catalog)
        self.assertEqual(step.ready, ())  # recovery (a later stage) handles orphans before scheduling
        self.assertIs(snap.node_run("a").outcome, Outcome.KNOWN)


class PurityTestCase(unittest.TestCase):
    def test_snapshot_never_writes_or_locks(self) -> None:
        source = (SRC / "snapshot.py").read_text(encoding="utf-8")
        statements = re.findall(r'"""\s*(.*?)\s*"""', source, flags=re.S)
        sql = "\n".join(s for s in statements if re.search(r"\bSELECT\b|\bSET TRANSACTION\b", s))
        self.assertTrue(sql)
        for forbidden in ("INSERT", "UPDATE", "DELETE", "FOR UPDATE", "FOR SHARE", "clock_timestamp() +", "visible_at ="):
            self.assertNotIn(forbidden, sql, forbidden)
        self.assertIn("REPEATABLE READ, READ ONLY", source)
        self.assertNotIn("ISOLATION LEVEL READ COMMITTED", source)

    def test_snapshot_does_not_use_the_guard_or_the_workers_clock(self) -> None:
        source = (SRC / "snapshot.py").read_text(encoding="utf-8")
        for fragment in ("run_fenced_guard", "time.time", "datetime.now", "monotonic", "execution_options"):
            self.assertNotIn(fragment, source, fragment)


if __name__ == "__main__":
    unittest.main()
