"""Graceful release and abnormal-termination recovery against PostgreSQL 17 (design 4.11)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import timedelta
from unittest import mock

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from flowforge.worker import shutdown as shutdown_module
from flowforge.worker.claim import claim_run
from flowforge.worker.heartbeat import HeartbeatService, LeaseState
from flowforge.worker.park import CLEAN_RELEASE_MESSAGE, LIVE_ATTEMPTS_MESSAGE, ParkResult
from flowforge.worker.shutdown import release_owned_runs
from tests.integration.support import make_node_run
from tests.integration.worker_support import RUN_TIMEOUT, TTL, WorkerDatabaseTestCase, as_id


class ReleaseOwnedRunsTestCase(WorkerDatabaseTestCase):
    def _three_runs(self):
        a = self.start_run("w1")
        self.add_run()
        b = self.claim("w1")
        self.add_run()
        c = self.claim("w1")
        with self.engine.begin() as conn:
            make_node_run(conn, as_id(b.run_id), "n1", status="running", started_epoch=1, effect_class="idempotent_write")
        return a, b, c

    def test_each_owned_run_is_released_by_its_own_evidence(self) -> None:
        a, b, c = self._three_runs()
        self.take_over(c.run_id, "w2")  # c's lease was lost before shutdown
        node_before = self.node_row(b.run_id)
        service = HeartbeatService(self.engine, TTL)
        self.addCleanup(service.stop)
        handles = {r.run_id: service.register(r) for r in (a, b, c)}

        outcomes = release_owned_runs(self.engine, [a, b, c], TTL, lambda r: r.run_id == b.run_id, heartbeat=service)

        self.assertEqual({o.run_id: o.result for o in outcomes}, {
            a.run_id: ParkResult.PARKED, b.run_id: ParkResult.PARKED, c.run_id: ParkResult.LEASE_LOST,
        })
        self.assertEqual(self.events(a.run_id), [(1, "info", None, CLEAN_RELEASE_MESSAGE)])
        self.assertEqual(self.events(b.run_id), [(1, "warning", None, LIVE_ATTEMPTS_MESSAGE)])
        self.assertEqual(self.node_row(b.run_id), node_before)  # the live attempt's row is untouched
        for run in (a, b):
            row = self.run_row(run.run_id)
            self.assertEqual((row.status, row.lease_owner, row.lease_epoch, row.recovery_count), ("waiting", None, 1, 0))
        self.assertEqual(self.run_row(c.run_id).lease_owner, "w2")  # the new owner is undisturbed
        self.assertEqual([e[3] for e in self.events(c.run_id)], ["run reclaimed (recovery_count=1)"])  # only the claimant's

        # Heartbeats were stopped first, so a late tick neither renews nor reports a spurious loss.
        service.tick()
        self.assertTrue(all(h.released for h in handles.values()))
        self.assertFalse(handles[a.run_id].lost.is_set())
        self.assertFalse(handles[b.run_id].lost.is_set())

    def test_released_runs_can_be_claimed_at_once_by_another_worker(self) -> None:
        a, b, _ = self._three_runs()
        release_owned_runs(self.engine, [a, b], TTL, lambda r: False)
        first, second = self.claim("w2"), self.claim("w2")
        self.assertEqual({first.run_id, second.run_id}, {a.run_id, b.run_id})
        self.assertTrue(all(c.prev_status == "waiting" and c.recovery_count == 0 for c in (first, second)))

    def test_a_database_error_on_one_run_does_not_stop_the_others(self) -> None:
        a, b, _ = self._three_runs()
        real = shutdown_module.park_for_shutdown

        def flaky(conn, claimed, ttl, *, live_attempts):
            if claimed.run_id == a.run_id:
                raise OperationalError("park", {}, Exception("connection lost"))
            return real(conn, claimed, ttl, live_attempts=live_attempts)

        with mock.patch.object(shutdown_module, "park_for_shutdown", flaky):
            outcomes = {o.run_id: o for o in release_owned_runs(self.engine, [a, b], TTL, lambda r: False)}
        self.assertIsNone(outcomes[a.run_id].result)
        self.assertIsInstance(outcomes[a.run_id].error, OperationalError)
        self.assertIs(outcomes[b.run_id].result, ParkResult.PARKED)
        self.assertEqual(self.run_row(a.run_id).status, "running")  # left for lease expiry: crash recovery

    def test_nothing_owned_is_a_no_op(self) -> None:
        self.assertEqual(release_owned_runs(self.engine, [], TTL, lambda r: False), [])


_CHILD = """
import os, sys, threading, time
from datetime import timedelta
from flowforge.db.engine import create_db_engine
from flowforge.worker.claim import claim_run
from flowforge.worker.heartbeat import HeartbeatService

engine = create_db_engine(os.environ["FF_URL"])
ttl = timedelta(seconds=float(os.environ["FF_TTL"]))
with engine.connect() as conn:
    claimed = claim_run(conn, "doomed", ttl, timedelta(minutes=5))
service = HeartbeatService(engine, ttl, period=timedelta(milliseconds=300))
service.register(claimed)
service.start()
service.wait_for_ticks(2, timeout=30)   # it really was renewing before it died
print("READY", flush=True)
os._exit(9)                              # abnormal termination: no cleanup, no release, no park
"""


class AbnormalTerminationTestCase(WorkerDatabaseTestCase):
    def test_a_stopped_heartbeat_lets_the_lease_lapse_and_the_run_is_reclaimed(self) -> None:
        old = self.start_run("w1")
        service = HeartbeatService(self.engine, TTL)
        self.addCleanup(service.stop)
        handle = service.register(old)
        service.tick()
        service.stop()  # the holder stops renewing without releasing anything
        successor = self.take_over(old.run_id, "w2")  # lease lapses, another worker reclaims
        self.assertEqual((successor.lease_epoch, successor.recovery_count, successor.prev_status), (2, 1, "running"))
        service2 = HeartbeatService(self.engine, TTL)
        self.addCleanup(service2.stop)
        stale = service2.register(old)
        service2.tick()
        self.assertIs(stale.state, LeaseState.LOST)  # the old holder can never renew again
        self.assertIsNotNone(handle)

    def test_a_killed_process_is_recovered_as_a_crash_once_its_lease_lapses(self) -> None:
        run_id = self.add_run()
        ttl = 5.0
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        env["FF_URL"] = self._temp_db.url.render_as_string(hide_password=False)
        env["FF_TTL"] = str(ttl)
        child = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(child.returncode, 9, child.stderr)
        self.assertIn("READY", child.stdout)
        row = self.run_row(run_id)
        self.assertEqual((row.status, row.lease_owner, row.lease_epoch), ("running", "doomed", 1))

        with self.engine.connect() as conn:
            self.assertIsNone(claim_run(conn, "rescuer", timedelta(seconds=ttl), RUN_TIMEOUT))  # lease not lapsed yet
        deadline = time.monotonic() + 60
        claimed = None
        while claimed is None and time.monotonic() < deadline:
            time.sleep(0.25)
            with self.engine.connect() as conn:
                claimed = claim_run(conn, "rescuer", timedelta(seconds=ttl), RUN_TIMEOUT)
        self.assertIsNotNone(claimed, "the abandoned run was never reclaimed")
        self.assertEqual((claimed.lease_epoch, claimed.recovery_count, claimed.prev_status), (2, 1, "running"))
        self.assertEqual([e[3] for e in self.events(run_id)], ["run reclaimed (recovery_count=1)"])
