"""HeartbeatService start/stop/register lifecycle (no database).

A fake engine and a gated ``run_fenced_guard`` simulate a heartbeat blocked in a
database call, using only synchronization primitives (events), never sleeps for
ordering. Covers: ``stop(timeout)`` is genuinely bounded, a connection is never
closed from a second thread, the heartbeat thread closes its own connection, repeated
stops are well defined, and registration is rejected once shutdown has begun.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from flowforge.worker import heartbeat
from flowforge.worker.claim import ClaimedRun
from flowforge.worker.heartbeat import HeartbeatService, LeaseState

TTL = timedelta(seconds=30)
NOW = datetime.now(timezone.utc)
FAST = timedelta(milliseconds=5)


def _claimed(n: int = 1) -> ClaimedRun:
    return ClaimedRun(f"00000000-0000-7000-8000-{n:012d}", "w1", 1, NOW, NOW, 0, "queued")


class FakeEngine:
    """Hands out one MagicMock connection per connect(), recording which thread closes it."""

    def __init__(self) -> None:
        self.connections: list[mock.MagicMock] = []
        self.closed_by: list[str] = []

    def connect(self) -> mock.MagicMock:
        conn = mock.MagicMock(name=f"conn{len(self.connections)}")
        conn.begin.return_value.__exit__.return_value = False  # never swallow exceptions
        conn.close.side_effect = lambda: self.closed_by.append(threading.current_thread().name)
        self.connections.append(conn)
        return conn


class Gate:
    """A guard stand-in that blocks until released, to hold a heartbeat 'inside the database'."""

    def __init__(self, result=None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result

    def __call__(self, conn, claimed, ttl):
        self.entered.set()
        self.release.wait(60)
        return self.result


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FakeEngine()
        self.gate = Gate()
        patcher = mock.patch.object(heartbeat, "run_fenced_guard", self.gate)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.gate.release.set)  # never leave a thread blocked after a test

    def _service(self, **kwargs) -> HeartbeatService:
        service = HeartbeatService(self.engine, TTL, **kwargs)
        self.addCleanup(lambda: self._quiet_stop(service))
        return service

    def _quiet_stop(self, service: HeartbeatService) -> None:
        self.gate.release.set()  # cleanups run last-in-first-out: unblock before stopping
        try:
            service.stop(timeout=30)
        except TimeoutError:
            pass

    def _blocked_service(self) -> HeartbeatService:
        service = self._service(period=FAST)
        service.register(_claimed())
        service.start()
        self.assertTrue(self.gate.entered.wait(30), "the heartbeat never reached the gated database call")
        return service

    # -- stop(timeout) is bounded -------------------------------------------------------

    def test_stop_respects_its_timeout_while_a_heartbeat_is_blocked(self) -> None:
        service = self._blocked_service()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            service.stop(timeout=0.3)
        self.assertLess(time.monotonic() - started, 5.0)  # bounded, not "until the database answers"
        self.assertTrue(service._thread.is_alive())  # reported honestly, not claimed stopped

    def test_stop_never_closes_a_connection_the_blocked_thread_may_be_using(self) -> None:
        service = self._blocked_service()
        with self.assertRaises(TimeoutError):
            service.stop(timeout=0.2)
        (conn,) = self.engine.connections
        conn.close.assert_not_called()  # not from the stopping thread, not while in use
        self.assertEqual(self.engine.closed_by, [])

    def test_the_heartbeat_thread_closes_its_own_connection_when_it_finally_exits(self) -> None:
        service = self._blocked_service()
        with self.assertRaises(TimeoutError):
            service.stop(timeout=0.2)
        self.gate.release.set()  # the blocked database call returns
        service._thread.join(30)
        self.assertFalse(service._thread.is_alive())
        (conn,) = self.engine.connections
        conn.close.assert_called_once()
        self.assertEqual(self.engine.closed_by, ["flowforge-heartbeat"])  # closed by its owner thread

    def test_repeated_stops_are_well_defined(self) -> None:
        service = self._blocked_service()
        for _ in range(2):  # still blocked: each call is bounded and reports the same thing
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                service.stop(timeout=0.2)
            self.assertLess(time.monotonic() - started, 5.0)
        self.gate.release.set()
        service.stop(timeout=30)  # now it completes...
        service.stop(timeout=30)  # ...and further calls are harmless no-ops
        service.stop(timeout=0)
        self.assertEqual(self.engine.closed_by, ["flowforge-heartbeat"])  # closed exactly once

    def test_a_clean_stop_closes_the_connection_in_the_heartbeat_thread(self) -> None:
        self.gate.release.set()  # no blocking this time
        service = self._service(period=FAST)
        service.register(_claimed())
        service.start()
        self.assertTrue(service.wait_for_ticks(2, timeout=30))
        service.stop(timeout=30)
        self.assertFalse(service._thread.is_alive())
        self.assertEqual(self.engine.closed_by, ["flowforge-heartbeat"])

    def test_stop_validates_its_timeout(self) -> None:
        service = self._service()
        for bad in (-1, None, "5", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                service.stop(timeout=bad)  # type: ignore[arg-type]

    # -- a service that was never started -------------------------------------------------

    def test_a_never_started_service_releases_the_connection_its_manual_ticks_created(self) -> None:
        self.gate.release.set()
        service = self._service()
        service.register(_claimed())
        service.tick()
        service.tick()
        self.assertEqual(len(self.engine.connections), 1)  # reused across ticks
        service.stop()
        self.engine.connections[0].close.assert_called_once()
        self.assertEqual(self.engine.closed_by, [threading.current_thread().name])  # no thread existed
        service.stop()  # repeated: nothing left to close
        self.engine.connections[0].close.assert_called_once()

    def test_stopping_a_never_started_service_with_no_connection_is_a_no_op(self) -> None:
        service = self._service()
        service.stop()
        service.stop()
        self.assertEqual(self.engine.connections, [])

    def test_a_manual_tick_in_flight_is_never_interrupted_and_closes_its_own_connection(self) -> None:
        service = self._service()
        service.register(_claimed())
        ticker = threading.Thread(target=service.tick, name="manual-ticker", daemon=True)
        ticker.start()
        self.assertTrue(self.gate.entered.wait(30))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            service.stop(timeout=0.2)  # cannot take the tick lock: the tick is mid-flight
        self.assertLess(time.monotonic() - started, 5.0)
        self.engine.connections[0].close.assert_not_called()  # not closed under the ticker's feet
        self.gate.release.set()
        ticker.join(30)
        self.assertFalse(ticker.is_alive())
        self.engine.connections[0].close.assert_called_once()
        self.assertEqual(self.engine.closed_by, ["manual-ticker"])  # the tick closed it itself

    def test_ticks_after_stop_do_not_open_new_connections(self) -> None:
        self.gate.release.set()
        service = self._service()
        service.register(_claimed())
        service.stop()
        service.tick()
        self.assertEqual(self.engine.connections, [])


class RegistrationLifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FakeEngine()
        patcher = mock.patch.object(heartbeat, "run_fenced_guard", Gate())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _service(self, **kwargs) -> HeartbeatService:
        service = HeartbeatService(self.engine, TTL, **kwargs)
        self.addCleanup(service.stop)
        return service

    def test_registration_before_start_is_allowed(self) -> None:
        service = self._service(period=FAST)
        handle = service.register(_claimed())  # the intended workflow: register, then start
        service.start()
        self.assertEqual(service.handles(), (handle,))
        self.assertIs(handle.state, LeaseState.HELD)

    def test_registration_while_running_is_allowed(self) -> None:
        service = self._service(period=FAST)
        service.start()
        handle = service.register(_claimed())
        self.assertEqual(service.handles(), (handle,))

    def test_registration_after_stop_is_rejected(self) -> None:
        service = self._service()
        service.stop()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            service.register(_claimed())
        self.assertEqual(service.handles(), ())

    def test_registration_is_rejected_as_soon_as_stop_is_requested_even_if_stop_times_out(self) -> None:
        gate = Gate()
        with mock.patch.object(heartbeat, "run_fenced_guard", gate):
            service = self._service(period=FAST)
            service.register(_claimed(1))
            service.start()
            self.assertTrue(gate.entered.wait(30))
            try:
                with self.assertRaises(TimeoutError):
                    service.stop(timeout=0.2)
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    service.register(_claimed(2))  # shutdown has begun, though the thread is still alive
            finally:
                gate.release.set()
                service.stop(timeout=30)

    def test_a_stopped_service_cannot_be_started(self) -> None:
        service = self._service()
        service.stop()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            service.start()
        self.assertIsNone(service._thread)

    def test_stop_marks_remaining_handles_so_consumers_fail_closed_immediately(self) -> None:
        service = self._service()
        handle = service.register(_claimed())
        self.assertIs(handle.state, LeaseState.HELD)
        service.stop()
        self.assertIs(handle.state, LeaseState.UNCERTAIN)  # nothing renews it any more
        self.assertFalse(handle.is_confirmed)
        self.assertFalse(handle.lost.is_set())  # uncertain, not lost
        self.assertEqual(service.handles(), ())

    def test_a_concurrent_register_and_stop_never_accepts_a_handle_without_a_heartbeat(self) -> None:
        for round_number in range(60):
            service = HeartbeatService(self.engine, TTL, period=FAST)
            service.start()
            registrars = 6
            barrier = threading.Barrier(registrars + 1)
            accepted, rejected, errors = [], [], []

            def register(n: int) -> None:
                try:
                    barrier.wait(30)
                    accepted.append(service.register(_claimed(round_number * 100 + n)))
                except RuntimeError:
                    rejected.append(n)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=register, args=(n,), daemon=True) for n in range(registrars)]
            for thread in threads:
                thread.start()
            barrier.wait(30)
            service.stop(timeout=30)
            for thread in threads:
                thread.join(30)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(accepted) + len(rejected), registrars)
            # Whatever was accepted before shutdown began was swept by stop(): none may look
            # healthy, and none may still be registered with a thread that has exited.
            self.assertEqual(service.handles(), ())
            for handle in accepted:
                self.assertIsNot(handle.state, LeaseState.HELD, "accepted without a functioning heartbeat")
            self.assertFalse(service._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
