"""M2.4 guard, heartbeat, park and shutdown: checks that need no database.

Pins the approved SQL shapes (owner/epoch fencing, no ``visible_at`` authorization
predicate, ordinary park versus shutdown park), the heartbeat lease-state
semantics (fail closed), and argument validation.
"""

from __future__ import annotations

import re
import signal
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from flowforge.worker import guard, heartbeat, park, shutdown
from flowforge.worker.claim import ClaimedRun
from flowforge.worker.guard import run_fenced_guard
from flowforge.worker.heartbeat import HeartbeatService, LeaseState
from flowforge.worker.park import park_for_shutdown, park_run
from flowforge.worker.shutdown import ShutdownCoordinator

SRC = Path(__file__).resolve().parents[2] / "src" / "flowforge" / "worker"
TTL = timedelta(seconds=30)
NOW = datetime.now(timezone.utc)


def _claimed(run_id: str = "00000000-0000-7000-8000-000000000001") -> ClaimedRun:
    return ClaimedRun(run_id, "w1", 1, NOW, NOW, 0, "queued")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SqlShapeTestCase(unittest.TestCase):
    def test_guard_keeps_the_approved_statement_and_never_authorizes_on_visible_at(self) -> None:
        sql = guard._GUARD
        self.assertIn("visible_at = clock_timestamp() + :ttl", sql)
        self.assertIn("status = 'running'", sql)
        self.assertIn("lease_owner = :worker AND lease_epoch = :epoch", sql)
        self.assertIn("visible_at - :ttl AS db_now", sql)
        where = sql.split("WHERE", 1)[1].split("RETURNING", 1)[0]
        self.assertNotIn("visible_at", where)

    def test_no_authorization_predicate_in_this_stage_uses_visible_at(self) -> None:
        for name in ("guard.py", "heartbeat.py", "park.py", "shutdown.py"):
            source = (SRC / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotRegex(source, r"visible_at\s*(<=|>=|<|>)(?!=)")
                self.assertNotIn("::", re.sub(r'""".*?"""', "", source, flags=re.S))

    def test_ordinary_park_keeps_its_cancel_predicate_and_wake_time(self) -> None:
        sql = park._PARK
        self.assertIn("AND cancel_requested_at IS NULL", sql)
        self.assertIn("LEAST(", sql)
        self.assertIn("min(retry_at)", sql)
        self.assertIn("status = 'retry_wait'", sql)
        self.assertIn("lease_owner = :worker AND lease_epoch = :epoch", sql)
        self.assertIn("lease_owner = NULL", sql)
        self.assertNotIn("lease_epoch =  lease_epoch", sql)
        self.assertNotRegex(sql, r"lease_epoch\s*=\s*lease_epoch")  # epoch is never written by a park

    def test_shutdown_park_is_a_distinct_statement_without_cancel_predicate_or_least(self) -> None:
        sql = park._PARK_FOR_SHUTDOWN
        self.assertIsNot(park._PARK, park._PARK_FOR_SHUTDOWN)
        self.assertIn("visible_at = now()", sql)
        self.assertNotIn("cancel_requested_at", sql)
        self.assertNotIn("LEAST", sql)
        self.assertIn("status = 'running'", sql)
        self.assertIn("lease_owner = :worker AND lease_epoch = :epoch", sql)
        self.assertNotRegex(sql, r"lease_epoch\s*=\s*lease_epoch")

    def test_parks_never_write_node_rows(self) -> None:
        for sql in (park._PARK, park._PARK_FOR_SHUTDOWN):
            self.assertNotRegex(sql, r"(?i)(update|insert\s+into|delete\s+from)\s+node_runs")

    def test_the_two_parks_are_separate_functions(self) -> None:
        self.assertIsNot(park_run, park_for_shutdown)
        self.assertNotIn("live_attempts", park_run.__code__.co_varnames[: park_run.__code__.co_argcount])


class ArgumentValidationTestCase(unittest.TestCase):
    def test_guard_requires_an_open_transaction_and_rejects_nested(self) -> None:
        conn = mock.Mock()
        conn.get_nested_transaction.return_value = None
        conn.in_transaction.return_value = False
        with self.assertRaisesRegex(RuntimeError, "explicitly open"):
            run_fenced_guard(conn, _claimed(), TTL)
        conn.execute.assert_not_called()
        conn.get_nested_transaction.return_value = object()
        conn.in_transaction.return_value = True
        with self.assertRaisesRegex(RuntimeError, "SAVEPOINT"):
            run_fenced_guard(conn, _claimed(), TTL)
        conn.execute.assert_not_called()

    def test_guard_ttl_is_a_positive_timedelta(self) -> None:
        conn = mock.Mock()
        conn.get_nested_transaction.return_value = None
        conn.in_transaction.return_value = True
        with self.assertRaises(TypeError):
            run_fenced_guard(conn, _claimed(), 30)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_fenced_guard(conn, _claimed(), timedelta(0))

    def test_park_operations_require_a_clean_connection_and_valid_ttl(self) -> None:
        dirty = mock.Mock()
        dirty.in_transaction.return_value = True
        with self.assertRaisesRegex(RuntimeError, "clean connection"):
            park_run(dirty, _claimed())
        with self.assertRaisesRegex(RuntimeError, "clean connection"):
            park_for_shutdown(dirty, _claimed(), TTL, live_attempts=False)
        dirty.execute.assert_not_called()
        with self.assertRaises(ValueError):
            park_for_shutdown(mock.Mock(), _claimed(), timedelta(0), live_attempts=False)
        with self.assertRaises(TypeError):
            park_for_shutdown(mock.Mock(), _claimed(), TTL)  # type: ignore[call-arg]


class HeartbeatStateTestCase(unittest.TestCase):
    def _service(self, clock: FakeClock, **kwargs) -> HeartbeatService:
        return HeartbeatService(mock.Mock(), TTL, clock=clock, **kwargs)

    def test_default_period_is_a_third_of_the_ttl(self) -> None:
        self.assertEqual(self._service(FakeClock()).period, timedelta(seconds=10))
        self.assertEqual(self._service(FakeClock(), period=timedelta(seconds=2)).period, timedelta(seconds=2))
        with self.assertRaises(ValueError):
            self._service(FakeClock(), period=timedelta(0))

    def test_a_fresh_registration_is_held_then_goes_uncertain_when_unconfirmed_for_a_ttl(self) -> None:
        clock = FakeClock()
        handle = self._service(clock).register(_claimed())
        self.assertIs(handle.state, LeaseState.HELD)
        self.assertTrue(handle.is_confirmed)
        clock.now += TTL.total_seconds()  # exactly one ttl: still within it
        self.assertIs(handle.state, LeaseState.HELD)
        clock.now += 0.001
        self.assertIs(handle.state, LeaseState.UNCERTAIN)  # fail closed: ownership no longer proven
        self.assertFalse(handle.is_confirmed)
        self.assertFalse(handle.lost.is_set())  # uncertain is not lost

    def test_a_recorded_failure_makes_it_uncertain_and_a_confirmation_restores_it(self) -> None:
        clock = FakeClock()
        handle = self._service(clock).register(_claimed())
        handle._record_failure(RuntimeError("db down"))
        self.assertIs(handle.state, LeaseState.UNCERTAIN)
        self.assertEqual(handle.consecutive_failures, 1)
        self.assertIsInstance(handle.last_error, RuntimeError)
        handle._record_failure(RuntimeError("still down"))
        self.assertEqual(handle.consecutive_failures, 2)
        handle._confirm()
        self.assertIs(handle.state, LeaseState.HELD)
        self.assertEqual((handle.consecutive_failures, handle.last_error), (0, None))

    def test_lost_is_terminal_and_outranks_everything(self) -> None:
        handle = self._service(FakeClock()).register(_claimed())
        handle.lost.set()
        handle._confirm()
        self.assertIs(handle.state, LeaseState.LOST)

    def test_registry_rules(self) -> None:
        service = self._service(FakeClock())
        service.register(_claimed("00000000-0000-7000-8000-000000000001"))
        with self.assertRaises(ValueError):
            service.register(_claimed("00000000-0000-7000-8000-000000000001"))
        with self.assertRaises(TypeError):
            service.register("not-a-claim")  # type: ignore[arg-type]
        service.release("00000000-0000-7000-8000-000000000001")
        service.release("never-registered")  # idempotent
        self.assertEqual(service.handles(), ())

    def test_a_service_cannot_be_started_twice(self) -> None:
        service = self._service(FakeClock(), period=timedelta(milliseconds=10))
        service.start()
        try:
            with self.assertRaises(RuntimeError):
                service.start()
        finally:
            service.stop()
        self.assertFalse(service._thread.is_alive())

    def test_the_heartbeat_thread_is_a_daemon_with_its_own_name(self) -> None:
        service = self._service(FakeClock(), period=timedelta(milliseconds=10))
        service.start()
        try:
            self.assertTrue(service._thread.daemon)
            self.assertEqual(service._thread.name, "flowforge-heartbeat")
        finally:
            service.stop()


class ShutdownCoordinatorTestCase(unittest.TestCase):
    def test_flag_is_idempotent_and_stops_claiming(self) -> None:
        coordinator = ShutdownCoordinator()
        self.assertTrue(coordinator.should_claim())
        self.assertFalse(coordinator.requested)
        self.assertFalse(coordinator.wait(0))
        coordinator.request_shutdown()
        coordinator.request_shutdown()
        self.assertFalse(coordinator.should_claim())
        self.assertTrue(coordinator.requested)
        self.assertTrue(coordinator.wait(0))

    def test_the_flag_can_be_set_from_another_thread(self) -> None:
        coordinator = ShutdownCoordinator()
        thread = threading.Thread(target=coordinator.request_shutdown)
        thread.start()
        thread.join()
        self.assertTrue(coordinator.requested)

    @unittest.skipUnless(threading.current_thread() is threading.main_thread(), "signals need the main thread")
    def test_sigterm_sets_the_flag_and_the_previous_handler_is_restored(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        coordinator = ShutdownCoordinator()
        with coordinator.installed():
            self.assertIsNot(signal.getsignal(signal.SIGTERM), previous)
            signal.raise_signal(signal.SIGTERM)
            self.assertTrue(coordinator.requested)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_interval_validation_on_release(self) -> None:
        with self.assertRaises(ValueError):
            shutdown.release_owned_runs(mock.Mock(), [], timedelta(0), lambda c: False)
        self.assertEqual(shutdown.release_owned_runs(mock.Mock(), [], TTL, lambda c: False), [])


class BoundaryTestCase(unittest.TestCase):
    def test_modules_expose_the_approved_names(self) -> None:
        for module, names in (
            (guard, ("FencedGuardResult", "run_fenced_guard")),
            (heartbeat, ("HeartbeatService", "LeaseHandle", "LeaseState")),
            (park, ("ParkResult", "park_run", "park_for_shutdown")),
            (shutdown, ("ShutdownCoordinator", "release_owned_runs", "ReleaseOutcome")),
        ):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(hasattr(module, name))

    def test_no_t2_t3_t4_or_finalization_code_exists_yet(self) -> None:
        banned = re.compile(r"def (start_node|finish_node|finalize_run|run_t4)\b")
        for path in sorted(SRC.glob("*.py")):
            with self.subTest(file=path.name):
                self.assertIsNone(banned.search(path.read_text(encoding="utf-8")))

    def test_only_the_guard_module_issues_the_lease_renewal(self) -> None:
        # The renewal is `visible_at = clock_timestamp() + :ttl`. Reading the database
        # clock elsewhere (the read-only snapshot samples db_now) is not a renewal.
        renewal = re.compile(r"visible_at\s*=\s*clock_timestamp\(\)")
        self.assertRegex((SRC / "guard.py").read_text(encoding="utf-8"), renewal)
        for path in sorted(SRC.glob("*.py")):
            if path.name != "guard.py":
                with self.subTest(file=path.name):
                    self.assertIsNone(renewal.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
