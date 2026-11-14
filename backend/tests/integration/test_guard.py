"""The fenced guard against PostgreSQL 17 (design section 3.1, test (cc); architecture 4.4)."""

from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import text

from flowforge.worker.events import LeaseProof, append_event
from flowforge.worker.guard import FencedGuardResult, run_fenced_guard
from tests.integration.worker_support import TTL, WorkerDatabaseTestCase, as_id

LOCK_WAIT = 1.5  # seconds a blocker holds the `runs` row lock in test (cc)


class GuardTestCase(WorkerDatabaseTestCase):
    def _guard(self, claimed, ttl=TTL):
        with self.engine.connect() as conn, conn.begin():
            return run_fenced_guard(conn, claimed, ttl)

    def test_returns_the_proof_and_renews_the_lease_from_one_database_sample(self) -> None:
        claimed = self.start_run()
        result = self._guard(claimed)
        self.assertIsInstance(result, FencedGuardResult)
        self.assertIsInstance(result.proof, LeaseProof)
        self.assertEqual((result.proof.run_id, result.proof.lease_owner, result.proof.lease_epoch), (claimed.run_id, "w1", 1))
        row = self.run_row(claimed.run_id)
        self.assertEqual(row.visible_at, result.db_now + TTL)  # renewal and reading are one instant
        self.assertEqual(result.deadline_at, row.deadline_at)
        self.assertIsNone(result.cancel_requested_at)

    def test_reports_a_pending_cancellation(self) -> None:
        claimed = self.start_run()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET cancel_requested_at = now() WHERE id = :id"), {"id": as_id(claimed.run_id)})
        self.assertIsNotNone(self._guard(claimed).cancel_requested_at)

    def test_zero_rows_means_lease_lost_for_every_non_matching_state(self) -> None:
        claimed = self.start_run()
        run_id = as_id(claimed.run_id)
        stale = type(claimed)(claimed.run_id, "w1", claimed.lease_epoch - 1, claimed.deadline_at, claimed.queue_deadline_at, 0, "queued")
        wrong_owner = type(claimed)(claimed.run_id, "someone-else", claimed.lease_epoch, claimed.deadline_at, claimed.queue_deadline_at, 0, "queued")
        before = tuple(self.run_row(run_id))
        self.assertIsNone(self._guard(stale))
        self.assertIsNone(self._guard(wrong_owner))
        self.assertEqual(tuple(self.run_row(run_id)), before)  # a refused guard writes nothing
        for status in ("waiting", "queued"):
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE runs SET status = :s, lease_owner = NULL WHERE id = :id"), {"s": status, "id": run_id})
            self.assertIsNone(self._guard(claimed), status)
        for status in ("succeeded", "failed", "cancelled", "timed_out"):
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE runs SET status = :s, lease_owner = NULL WHERE id = :id"), {"s": status, "id": run_id})
            self.assertIsNone(self._guard(claimed), status)

    def test_a_superseded_epoch_is_refused_after_a_reclaim(self) -> None:
        old = self.start_run("w1")
        new = self.take_over(old.run_id, "w2")
        self.assertIsNone(self._guard(old))
        self.assertIsNotNone(self._guard(new))

    def test_an_expired_but_unreclaimed_lease_still_matches_and_renews(self) -> None:
        # visible_at is never an authorization predicate: only a reclaim revokes ownership.
        claimed = self.start_run()
        self.expire_lease(claimed.run_id)
        result = self._guard(claimed)
        self.assertIsNotNone(result)
        with self.engine.connect() as conn:
            remaining = conn.execute(
                text("SELECT visible_at - clock_timestamp() FROM runs WHERE id = :id"), {"id": as_id(claimed.run_id)}
            ).scalar_one()
        self.assertGreater(remaining, TTL - timedelta(seconds=30))

    def test_the_guard_does_not_commit_the_callers_transaction_owns_it(self) -> None:
        claimed = self.start_run()
        before = self.run_row(claimed.run_id).visible_at
        with self.engine.connect() as conn:
            trans = conn.begin()
            self.assertIsNotNone(run_fenced_guard(conn, claimed, TTL))
            trans.rollback()
        self.assertEqual(self.run_row(claimed.run_id).visible_at, before)

    def test_requires_an_open_transaction_and_rejects_nested_ones(self) -> None:
        claimed = self.start_run()
        with self.engine.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "explicitly open"):
                run_fenced_guard(conn, claimed, TTL)
            self.assertFalse(conn.in_transaction(), "a refused guard must not autobegin")
        with self.engine.connect() as conn, conn.begin():
            nested = conn.begin_nested()
            with self.assertRaisesRegex(RuntimeError, "SAVEPOINT"):
                run_fenced_guard(conn, claimed, TTL)
            nested.rollback()

    def test_the_proof_authorizes_events_in_the_guards_transaction_only(self) -> None:
        claimed = self.start_run()
        with self.engine.connect() as conn:
            with conn.begin():
                result = run_fenced_guard(conn, claimed, TTL)
                append_event(conn, result.proof, "info", None, "under the guard")
            with conn.begin():
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    append_event(conn, result.proof, "info", None, "after commit")
        self.assertEqual([e[3] for e in self.events(claimed.run_id)], ["under the guard"])

    # -- test (cc): the renewal uses the fresh clock, not the transaction start ----------

    def _guard_behind_blocker(self, modifying: bool):
        claimed = self.start_run()
        run_id = as_id(claimed.run_id)
        locked, errors, blocker_info = threading.Event(), [], {}

        def blocker() -> None:
            try:
                with self.engine.connect() as conn:
                    trans = conn.begin()
                    if modifying:
                        conn.execute(text("UPDATE runs SET recovery_count = recovery_count WHERE id = :id"), {"id": run_id})
                    else:
                        conn.execute(text("SELECT id FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id})
                    locked.set()
                    threading.Event().wait(LOCK_WAIT)
                    blocker_info["before_release"] = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
                    if modifying:
                        trans.commit()
                    else:
                        trans.rollback()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)
                locked.set()

        thread = threading.Thread(target=blocker, daemon=True)
        thread.start()
        self.assertTrue(locked.wait(30))
        with self.engine.connect() as conn:
            with conn.begin():
                txn_start = conn.execute(text("SELECT now()")).scalar_one()
                result = run_fenced_guard(conn, claimed, TTL)  # blocks behind the holder
                after = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        thread.join(30)
        self.assertEqual(errors, [])
        return result, txn_start, after, blocker_info["before_release"], self.run_row(run_id)

    def test_cc_behind_a_modifying_blocker_the_sample_is_post_wait(self) -> None:
        result, txn_start, after, released_at, row = self._guard_behind_blocker(modifying=True)
        self.assertGreaterEqual(result.db_now, released_at)  # taken after the blocker's commit
        self.assertEqual(row.visible_at, result.db_now + TTL)  # one sample for renewal and reading
        # Distinguishes the fresh clock from transaction-start now(): ~LOCK_WAIT later.
        self.assertGreater(result.db_now - txn_start, timedelta(seconds=LOCK_WAIT * 0.6))
        self.assertGreater(row.visible_at, txn_start + TTL + timedelta(seconds=LOCK_WAIT * 0.6))
        self.assertLessEqual(result.db_now, after)

    def test_cc_behind_a_lock_only_blocker_only_the_safe_bound_is_asserted(self) -> None:
        # PostgreSQL does not document SET-expression re-evaluation after a wait for a
        # blocker that releases without modifying the row, so assert only the safe bound:
        # never earlier than the transaction start, never later than a fresh reading.
        result, txn_start, after, _, row = self._guard_behind_blocker(modifying=False)
        self.assertGreaterEqual(row.visible_at, txn_start + TTL)
        self.assertLessEqual(row.visible_at, after + TTL)
        self.assertEqual(row.visible_at, result.db_now + TTL)

    def test_a_guard_never_waits_on_visible_at(self) -> None:
        claimed = self.start_run()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET visible_at = now() + interval '1 hour' WHERE id = :id"), {"id": as_id(claimed.run_id)})
        self.assertIsNotNone(self._guard(claimed))  # a far-future visible_at changes nothing
