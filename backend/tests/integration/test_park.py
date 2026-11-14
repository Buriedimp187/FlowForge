"""Ordinary park and shutdown park against PostgreSQL 17 (design sections 4.5 and 4.11)."""

from __future__ import annotations

from unittest import mock

from sqlalchemy import text

from flowforge.worker import park as park_module
from flowforge.worker.claim import ClaimedRun
from flowforge.worker.guard import run_fenced_guard
from flowforge.worker.park import (
    CLEAN_RELEASE_MESSAGE,
    LIVE_ATTEMPTS_MESSAGE,
    ParkResult,
    park_for_shutdown,
    park_run,
)
from tests.integration.support import Sql, make_node_run
from tests.integration.worker_support import TTL, WorkerDatabaseTestCase, as_id


class ParkTestCase(WorkerDatabaseTestCase):
    def _park(self, claimed) -> ParkResult:
        with self.engine.connect() as conn:
            result = park_run(conn, claimed)
            self.assertFalse(conn.in_transaction())
        return result

    def _shutdown_park(self, claimed, live: bool = False) -> ParkResult:
        with self.engine.connect() as conn:
            result = park_for_shutdown(conn, claimed, TTL, live_attempts=live)
            self.assertFalse(conn.in_transaction())
        return result

    def _retry_wait(self, run_id, node_key: str, interval: str) -> None:
        with self.engine.begin() as conn:
            make_node_run(
                conn, as_id(run_id), node_key, status="retry_wait", started_epoch=1,
                retry_at=Sql(f"now() + interval '{interval}'"),
            )

    def _retry_at(self, run_id, node_key: str):
        return self.node_row_value(run_id, node_key, "retry_at")

    def node_row_value(self, run_id, node_key, column):
        with self.engine.connect() as conn:
            return conn.execute(
                text(f"SELECT {column} FROM node_runs WHERE run_id = :id AND node_key = :k"),
                {"id": as_id(run_id), "k": node_key},
            ).scalar_one()

    # -- ordinary park ----------------------------------------------------------

    def test_parks_with_the_wake_time_of_the_earliest_pending_retry(self) -> None:
        claimed = self.start_run()
        self._retry_wait(claimed.run_id, "late", "5 minutes")
        self._retry_wait(claimed.run_id, "early", "2 minutes")
        self.assertIs(self._park(claimed), ParkResult.PARKED)
        row = self.run_row(claimed.run_id)
        self.assertEqual((row.status, row.lease_owner, row.lease_epoch, row.recovery_count), ("waiting", None, 1, 0))
        self.assertEqual(row.visible_at, self._retry_at(claimed.run_id, "early"))

    def test_the_deadline_wins_when_it_comes_before_every_retry(self) -> None:
        claimed = self.start_run()  # run timeout is 7 minutes
        self._retry_wait(claimed.run_id, "n1", "1 hour")
        self.assertIs(self._park(claimed), ParkResult.PARKED)
        row = self.run_row(claimed.run_id)
        self.assertEqual(row.visible_at, row.deadline_at)

    def test_without_pending_retries_the_wake_time_is_the_deadline(self) -> None:
        claimed = self.start_run()
        self.assertIs(self._park(claimed), ParkResult.PARKED)
        row = self.run_row(claimed.run_id)
        self.assertEqual(row.visible_at, row.deadline_at)

    def test_only_this_runs_pending_retries_count(self) -> None:
        claimed = self.start_run()
        other = self.add_run(status="waiting", visible_at=Sql("now() + interval '1 hour'"))
        with self.engine.begin() as conn:
            make_node_run(conn, other, "n1", status="retry_wait", started_epoch=1, retry_at=Sql("now() + interval '1 second'"))
        self._retry_wait(claimed.run_id, "n1", "3 minutes")
        self.assertIs(self._park(claimed), ParkResult.PARKED)
        self.assertEqual(self.run_row(claimed.run_id).visible_at, self._retry_at(claimed.run_id, "n1"))

    def test_a_pending_cancel_blocks_the_ordinary_park_and_changes_nothing(self) -> None:
        # (s4b): the ordinary park keeps its cancel predicate.
        claimed = self.start_run()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET cancel_requested_at = now() WHERE id = :id"), {"id": as_id(claimed.run_id)})
        before = tuple(self.run_row(claimed.run_id))
        self.assertIs(self._park(claimed), ParkResult.NOT_PARKED)
        self.assertEqual(tuple(self.run_row(claimed.run_id)), before)

    def test_only_the_current_owner_and_epoch_can_park(self) -> None:
        old = self.start_run("w1")
        new = self.take_over(old.run_id, "w2")
        before = tuple(self.run_row(old.run_id))
        self.assertIs(self._park(old), ParkResult.NOT_PARKED)  # superseded epoch
        wrong_owner = ClaimedRun(new.run_id, "intruder", new.lease_epoch, new.deadline_at, new.queue_deadline_at, 0, "running")
        self.assertIs(self._park(wrong_owner), ParkResult.NOT_PARKED)
        self.assertEqual(tuple(self.run_row(old.run_id)), before)
        self.assertIs(self._park(new), ParkResult.PARKED)

    def test_a_run_that_is_not_running_cannot_be_parked(self) -> None:
        claimed = self.start_run()
        self.assertIs(self._park(claimed), ParkResult.PARKED)
        self.assertIs(self._park(claimed), ParkResult.NOT_PARKED)  # already waiting

    def test_a_parked_run_wakes_through_the_ordinary_claim_without_a_recovery(self) -> None:
        claimed = self.start_run()
        self._retry_wait(claimed.run_id, "n1", "1 minute")
        self.assertIs(self._park(claimed), ParkResult.PARKED)
        self.assertIsNone(self.claim("w2"))  # not yet due: wake time is in the future
        with self.engine.begin() as conn:  # time passes: the retry becomes due
            conn.execute(text("UPDATE node_runs SET retry_at = now() - interval '1 second'"))
            conn.execute(text("UPDATE runs SET visible_at = now() - interval '1 second' WHERE id = :id"), {"id": as_id(claimed.run_id)})
        woken = self.claim("w2")
        self.assertEqual((woken.prev_status, woken.lease_epoch, woken.recovery_count), ("waiting", 2, 0))
        self.assertEqual(self.events(claimed.run_id), [])  # no "reclaimed" event for a wake

    def test_the_parked_row_satisfies_the_owner_status_invariant(self) -> None:
        claimed = self.start_run()
        self._park(claimed)
        row = self.run_row(claimed.run_id)
        self.assertEqual((row.status, row.lease_owner), ("waiting", None))

    def test_a_fenced_guard_from_the_old_holder_fails_after_a_park(self) -> None:
        claimed = self.start_run()
        self._park(claimed)
        with self.engine.connect() as conn, conn.begin():
            self.assertIsNone(run_fenced_guard(conn, claimed, TTL))

    def test_requires_a_clean_connection(self) -> None:
        claimed = self.start_run()
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                park_run(conn, claimed)
        self.assertEqual(self.run_row(claimed.run_id).status, "running")

    # -- shutdown park ------------------------------------------------------------

    def test_s2_a_live_attempt_is_left_running_and_the_run_is_parked_at_once(self) -> None:
        claimed = self.start_run()
        with self.engine.begin() as conn:
            make_node_run(conn, as_id(claimed.run_id), "n1", status="running", started_epoch=1, effect_class="unsafe_write", attempt=2, failures=1)
        node_before = self.node_row(claimed.run_id)
        self.assertIs(self._shutdown_park(claimed, live=True), ParkResult.PARKED)
        row = self.run_row(claimed.run_id)
        self.assertEqual((row.status, row.lease_owner, row.lease_epoch, row.recovery_count), ("waiting", None, 1, 0))
        with self.engine.connect() as conn:
            claimable_now = conn.execute(text("SELECT visible_at <= now() FROM runs WHERE id = :id"), {"id": as_id(claimed.run_id)}).scalar_one()
        self.assertTrue(claimable_now)
        self.assertEqual(self.node_row(claimed.run_id), node_before)  # byte-identical: nothing fabricated
        self.assertEqual(self.events(claimed.run_id), [(1, "warning", None, LIVE_ATTEMPTS_MESSAGE)])

    def test_a_clean_release_writes_an_info_event(self) -> None:
        claimed = self.start_run()
        self.assertIs(self._shutdown_park(claimed, live=False), ParkResult.PARKED)
        self.assertEqual(self.events(claimed.run_id), [(1, "info", None, CLEAN_RELEASE_MESSAGE)])

    def test_s3_the_old_holder_can_never_write_after_the_park_or_after_a_new_claim(self) -> None:
        claimed = self.start_run("w1")
        self._shutdown_park(claimed)
        with self.engine.connect() as conn, conn.begin():
            self.assertIsNone(run_fenced_guard(conn, claimed, TTL))  # run is 'waiting'
        successor = self.claim("w2")  # epoch + 1
        self.assertEqual(successor.lease_epoch, 2)
        with self.engine.connect() as conn, conn.begin():
            self.assertIsNone(run_fenced_guard(conn, claimed, TTL))  # epoch check now
        with self.engine.connect() as conn:
            self.assertIs(park_for_shutdown(conn, claimed, TTL, live_attempts=False), ParkResult.LEASE_LOST)
        self.assertIs(self._park(claimed), ParkResult.NOT_PARKED)

    def test_s4_a_pending_cancel_does_not_block_the_shutdown_park_and_stays_sticky(self) -> None:
        claimed = self.start_run()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET cancel_requested_at = now() WHERE id = :id"), {"id": as_id(claimed.run_id)})
        flag = self.run_row(claimed.run_id).cancel_requested_at
        self.assertIs(self._shutdown_park(claimed), ParkResult.PARKED)
        row = self.run_row(claimed.run_id)
        self.assertEqual((row.status, row.cancel_requested_at), ("waiting", flag))
        successor = self.claim("w2")  # the next claimant takes the run at once, flag still set
        self.assertEqual(successor.prev_status, "waiting")
        self.assertEqual(self.run_row(claimed.run_id).cancel_requested_at, flag)

    def test_s4c_immediate_reclaim_needs_no_lease_wait_and_is_not_a_recovery(self) -> None:
        claimed = self.start_run("w1")
        self._shutdown_park(claimed, live=True)
        successor = self.claim("w2")  # straight away: no expire_lease, no TTL wait
        self.assertEqual((successor.prev_status, successor.lease_epoch, successor.recovery_count), ("waiting", 2, 0))
        events = self.events(claimed.run_id)
        self.assertEqual([e[3] for e in events], [LIVE_ATTEMPTS_MESSAGE])  # no "run reclaimed" event
        with self.engine.connect() as conn, conn.begin():
            self.assertIsNone(run_fenced_guard(conn, claimed, TTL))

    def test_a_lost_lease_writes_nothing_at_all(self) -> None:
        old = self.start_run("w1")
        self.take_over(old.run_id, "w2")
        events_before = self.events(old.run_id)
        row_before = tuple(self.run_row(old.run_id))
        self.assertIs(self._shutdown_park(old, live=True), ParkResult.LEASE_LOST)
        self.assertEqual(self.events(old.run_id), events_before)  # no event from a non-holder
        self.assertEqual(tuple(self.run_row(old.run_id)), row_before)

    def test_the_event_rolls_back_with_a_failed_park(self) -> None:
        claimed = self.start_run()
        row_before = tuple(self.run_row(claimed.run_id))
        no_match = "UPDATE runs SET status = 'waiting', lease_owner = NULL WHERE false RETURNING status"
        with self.engine.connect() as conn:
            with mock.patch.object(park_module, "_PARK_FOR_SHUTDOWN", no_match):
                with self.assertRaisesRegex(RuntimeError, "matched no row"):
                    park_for_shutdown(conn, claimed, TTL, live_attempts=True)
            self.assertFalse(conn.in_transaction())
        self.assertEqual(self.events(claimed.run_id), [])  # the warning rolled back with the park
        self.assertEqual(tuple(self.run_row(claimed.run_id)), row_before)  # so did the guard's renewal

    def test_the_shutdown_park_requires_a_clean_connection(self) -> None:
        claimed = self.start_run()
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                park_for_shutdown(conn, claimed, TTL, live_attempts=False)
        self.assertEqual(self.events(claimed.run_id), [])
