"""The heartbeat service against PostgreSQL 17 (design sections 3.1, 4.7; rows 6 and 11)."""

from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import text

from flowforge.worker.guard import run_fenced_guard
from flowforge.worker.heartbeat import HeartbeatService, LeaseState
from tests.integration.worker_support import TTL, WorkerDatabaseTestCase, as_id

JOIN_SECONDS = 60


class HeartbeatTestCase(WorkerDatabaseTestCase):
    def _service(self, **kwargs) -> HeartbeatService:
        service = HeartbeatService(self.engine, TTL, **kwargs)
        self.addCleanup(service.stop)
        return service

    def _remaining(self, run_id) -> timedelta:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT visible_at - clock_timestamp() FROM runs WHERE id = :id"), {"id": as_id(run_id)}
            ).scalar_one()

    # -- renewal ---------------------------------------------------------------------

    def test_a_tick_renews_in_its_own_committed_transaction_and_holds_no_lock(self) -> None:
        claimed = self.start_run()
        service = self._service()
        handle = service.register(claimed)
        self.expire_lease(claimed.run_id)
        self.assertLess(self._remaining(claimed.run_id), timedelta(0))
        service.tick()
        self.assertGreater(self._remaining(claimed.run_id), TTL - timedelta(seconds=30))  # visible to another connection: committed
        self.assertIs(handle.state, LeaseState.HELD)
        self.assertFalse(service._conn.in_transaction(), "the heartbeat must not keep a transaction open")
        with self.engine.connect() as other:  # the row lock is free: NOWAIT would fail otherwise
            other.execute(text("SELECT id FROM runs WHERE id = :id FOR UPDATE NOWAIT"), {"id": as_id(claimed.run_id)})
            other.rollback()

    def test_the_cancel_flag_is_published_stickily_and_renewal_continues(self) -> None:
        claimed = self.start_run()
        service = self._service()
        handle = service.register(claimed)
        service.tick()
        self.assertFalse(handle.cancel_requested.is_set())
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET cancel_requested_at = now() WHERE id = :id"), {"id": as_id(claimed.run_id)})
        self.expire_lease(claimed.run_id)
        service.tick()
        self.assertTrue(handle.cancel_requested.is_set())
        self.assertGreater(self._remaining(claimed.run_id), TTL - timedelta(seconds=30))  # still renewing while it drains
        service.tick()
        self.assertTrue(handle.cancel_requested.is_set())  # sticky
        self.assertIs(handle.state, LeaseState.HELD)

    def test_after_a_heartbeat_the_loop_uses_a_fresh_transaction_and_guard(self) -> None:
        # design 4.7 steps 3-6: nothing database-shaped crosses the thread boundary.
        claimed = self.start_run()
        service = self._service()
        handle = service.register(claimed)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET cancel_requested_at = now() WHERE id = :id"), {"id": as_id(claimed.run_id)})
        service.tick()
        self.assertTrue(handle.cancel_requested.wait(0))
        with self.engine.connect() as conn, conn.begin():  # the loop's own connection and transaction
            fresh = run_fenced_guard(conn, claimed, TTL)  # the cached ClaimedRun epoch, no heartbeat proof
            self.assertIsNotNone(fresh)
            self.assertIsNotNone(fresh.cancel_requested_at)

    # -- confirmed lease loss ---------------------------------------------------------

    def test_a_reclaimed_run_is_reported_lost_and_the_stale_tick_writes_nothing(self) -> None:
        old = self.start_run("w1")
        service = self._service()
        handle = service.register(old)
        self.take_over(old.run_id, "w2")
        before = tuple(self.run_row(old.run_id))
        service.tick()
        self.assertTrue(handle.lost.is_set())
        self.assertIs(handle.state, LeaseState.LOST)
        self.assertEqual(tuple(self.run_row(old.run_id)), before)  # nothing written by the stale holder
        self.assertEqual(service.handles(), ())  # no longer renewed
        service.tick()
        self.assertEqual(tuple(self.run_row(old.run_id)), before)

    def test_a_parked_run_is_reported_lost_to_a_late_heartbeat(self) -> None:
        claimed = self.start_run()
        service = self._service()
        handle = service.register(claimed)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET status = 'waiting', lease_owner = NULL WHERE id = :id"), {"id": as_id(claimed.run_id)})
        service.tick()
        self.assertIs(handle.state, LeaseState.LOST)

    # -- races, rows 6 and 11 ---------------------------------------------------------

    def test_reclaim_versus_heartbeat_never_yields_two_owners(self) -> None:
        for _ in range(20):
            claimed = self.start_run("w1")
            self.expire_lease(claimed.run_id)
            service = HeartbeatService(self.engine, TTL)
            handle = service.register(claimed)
            barrier = threading.Barrier(2)
            reclaimed, errors = [], []

            def beat() -> None:
                try:
                    barrier.wait(JOIN_SECONDS)
                    service.tick()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def reclaim() -> None:
                try:
                    barrier.wait(JOIN_SECONDS)
                    reclaimed.append(self.claim("w2"))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=beat, daemon=True), threading.Thread(target=reclaim, daemon=True)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(JOIN_SECONDS)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            row = self.run_row(claimed.run_id)
            (winner,) = reclaimed
            if winner is not None:  # the reclaim took the row lock first: the heartbeat must see the loss
                self.assertEqual((row.lease_owner, row.lease_epoch), ("w2", 2))
                self.assertIs(handle.state, LeaseState.LOST)
            else:  # the heartbeat won: lease renewed, claimer skipped or found it unexpired
                self.assertEqual((row.lease_owner, row.lease_epoch), ("w1", 1))
                self.assertIs(handle.state, LeaseState.HELD)
            service.stop()  # after the assertions: stopping marks every handle UNCERTAIN
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM run_events"))
                conn.execute(text("DELETE FROM runs"))

    # -- database errors are not lease loss: fail closed ---------------------------------

    def _kill_heartbeat_backend(self, service: HeartbeatService) -> None:
        pid = service._conn.connection.driver_connection.info.backend_pid
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

    def test_a_database_error_is_uncertain_not_lost_and_the_next_tick_recovers(self) -> None:
        claimed = self.start_run()
        service = self._service()
        handle = service.register(claimed)
        service.tick()
        self._kill_heartbeat_backend(service)
        service.tick()  # the connection is dead: a database error
        self.assertIs(handle.state, LeaseState.UNCERTAIN)
        self.assertFalse(handle.is_confirmed)
        self.assertFalse(handle.lost.is_set())  # an error proves nothing about ownership
        self.assertEqual(handle.consecutive_failures, 1)
        self.assertIsNotNone(handle.last_error)
        self.assertEqual(len(service.handles()), 1)  # still registered: retried
        service.tick()  # fresh connection
        self.assertIs(handle.state, LeaseState.HELD)
        self.assertEqual((handle.consecutive_failures, handle.last_error), (0, None))

    def test_an_error_followed_by_a_reclaim_resolves_to_confirmed_loss(self) -> None:
        old = self.start_run("w1")
        service = self._service()
        handle = service.register(old)
        service.tick()
        self._kill_heartbeat_backend(service)
        service.tick()
        self.assertIs(handle.state, LeaseState.UNCERTAIN)
        self.take_over(old.run_id, "w2")
        service.tick()  # reconnects and gets the database's answer
        self.assertIs(handle.state, LeaseState.LOST)

    # -- registry, release, thread -------------------------------------------------------

    def test_each_owned_run_is_renewed_in_turn_and_one_loss_does_not_stop_the_others(self) -> None:
        first = self.start_run("w1")
        self.add_run()
        second = self.claim("w1")
        service = self._service()
        h1, h2 = service.register(first), service.register(second)
        self.take_over(first.run_id, "w2")
        self.expire_lease(second.run_id)
        service.tick()
        self.assertIs(h1.state, LeaseState.LOST)
        self.assertIs(h2.state, LeaseState.HELD)
        self.assertGreater(self._remaining(second.run_id), TTL - timedelta(seconds=30))

    def test_a_released_run_is_no_longer_renewed(self) -> None:
        claimed = self.start_run()
        service = self._service()
        handle = service.register(claimed)
        service.release(claimed.run_id)
        self.assertTrue(handle.released)
        self.expire_lease(claimed.run_id)
        service.tick()
        self.assertLess(self._remaining(claimed.run_id), timedelta(0))  # not renewed
        self.assertFalse(handle.lost.is_set())

    def test_the_thread_renews_periodically_and_stops_cleanly(self) -> None:
        claimed = self.start_run()
        service = self._service(period=timedelta(milliseconds=50))
        handle = service.register(claimed)
        self.expire_lease(claimed.run_id)
        service.start()
        self.assertTrue(service.wait_for_ticks(3, timeout=30))
        self.assertGreater(self._remaining(claimed.run_id), TTL - timedelta(seconds=30))
        self.assertIs(handle.state, LeaseState.HELD)
        service.stop()
        self.assertFalse(service._thread.is_alive())
        ticks = service.ticks
        self.expire_lease(claimed.run_id)
        self.assertLess(self._remaining(claimed.run_id), timedelta(0))
        self.assertEqual(service.ticks, ticks)  # no ticking after stop

    def test_stop_returns_the_real_connection_to_the_pool(self) -> None:
        claimed = self.start_run()
        service = self._service(period=timedelta(milliseconds=50))
        service.register(claimed)
        service.start()
        self.assertTrue(service.wait_for_ticks(2, timeout=30))
        service.stop(timeout=30)
        self.assertEqual(self.engine.pool.checkedout(), 0)  # closed by the heartbeat thread, none leaked
        manual = self._service()
        manual.register(self.start_run_again())
        manual.tick()
        self.assertEqual(self.engine.pool.checkedout(), 1)  # a manual tick holds one connection
        manual.stop()
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def start_run_again(self):
        self.add_run()
        return self.claim("w9")

    def test_the_thread_survives_a_lease_loss_and_keeps_serving_other_runs(self) -> None:
        lost_run = self.start_run("w1")
        self.add_run()
        kept_run = self.claim("w1")
        service = self._service(period=timedelta(milliseconds=50))
        lost_handle, kept_handle = service.register(lost_run), service.register(kept_run)
        self.take_over(lost_run.run_id, "w2")
        service.start()
        self.assertTrue(lost_handle.lost.wait(30))
        before = service.ticks
        self.assertTrue(service.wait_for_ticks(before + 2, timeout=30))
        self.assertIs(kept_handle.state, LeaseState.HELD)
