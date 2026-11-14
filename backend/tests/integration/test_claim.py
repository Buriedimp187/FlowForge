"""T1 ``claim_run`` against PostgreSQL 17 (design sections 3.1 and 4.2)."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from sqlalchemy import text

from flowforge.worker.claim import ClaimedRun, claim_run
from flowforge.worker.enqueue import enqueue_run
from tests.integration.support import Sql, make_event
from tests.integration.worker_support import (
    FUTURE,
    PAST,
    QUEUE_TIMEOUT,
    RUN_TIMEOUT,
    TTL,
    WorkerDatabaseTestCase,
    as_id,
)


class ClaimTestCase(WorkerDatabaseTestCase):
    def _claim(self, worker: str = "w1"):
        with self.engine.connect() as conn:
            claimed = claim_run(conn, worker, TTL, RUN_TIMEOUT)
            self.assertFalse(conn.in_transaction(), "claim_run must leave the connection clean")
        return claimed

    # -- eligibility ------------------------------------------------------

    def test_nothing_eligible_returns_none(self) -> None:
        self.assertIsNone(self._claim())

    def test_claims_a_queued_run(self) -> None:
        run_id = self.add_run()
        claimed = self._claim("w1")
        self.assertIsInstance(claimed, ClaimedRun)
        self.assertEqual(
            (claimed.run_id, claimed.lease_owner, claimed.lease_epoch, claimed.recovery_count, claimed.prev_status),
            (str(run_id), "w1", 1, 0, "queued"),
        )
        row = self.run_row(run_id)
        self.assertEqual((row.status, row.lease_owner, row.lease_epoch, row.recovery_count), ("running", "w1", 1, 0))
        self.assertEqual(claimed.deadline_at, row.deadline_at)
        self.assertEqual(claimed.queue_deadline_at, row.queue_deadline_at)
        self.assertEqual(self.events(run_id), [])  # an ordinary claim writes no event

    def test_claims_a_waiting_run_without_counting_a_recovery(self) -> None:
        run_id = self.add_run(status="waiting", started_at=PAST, deadline_at=FUTURE)
        claimed = self._claim()
        self.assertEqual((claimed.prev_status, claimed.recovery_count, claimed.lease_epoch), ("waiting", 0, 1))
        self.assertEqual(self.events(run_id), [])

    def test_reclaims_an_expired_running_run(self) -> None:
        run_id = self.add_expired_running_run()
        claimed = self._claim("w2")
        self.assertEqual(
            (claimed.prev_status, claimed.lease_owner, claimed.lease_epoch, claimed.recovery_count),
            ("running", "w2", 2, 1),
        )
        row = self.run_row(run_id)
        self.assertEqual((row.status, row.lease_owner, row.lease_epoch, row.recovery_count), ("running", "w2", 2, 1))
        self.assertEqual(self.events(run_id), [(1, "info", None, "run reclaimed (recovery_count=1)")])

    def test_an_unexpired_running_run_is_not_claimable(self) -> None:
        self.add_expired_running_run(visible_at=FUTURE)
        self.assertIsNone(self._claim())

    def test_a_run_that_is_not_yet_visible_is_not_claimable(self) -> None:
        self.add_run(visible_at=FUTURE)
        self.add_run(status="waiting", visible_at=FUTURE)
        self.assertIsNone(self._claim())

    def test_terminal_runs_are_never_claimable(self) -> None:
        for status in ("succeeded", "failed", "cancelled", "timed_out"):
            self.add_run(status=status, visible_at=PAST)
        self.assertIsNone(self._claim())

    def test_claims_in_visible_at_order_one_run_per_call(self) -> None:
        newest = self.add_run(visible_at=Sql("now() - interval '1 minute'"))
        oldest = self.add_run(visible_at=Sql("now() - interval '3 minutes'"))
        middle = self.add_run(visible_at=Sql("now() - interval '2 minutes'"))
        first = self._claim()
        self.assertEqual(first.run_id, str(oldest))
        self.assertEqual([self.run_row(r).status for r in (oldest, middle, newest)], ["running", "queued", "queued"])
        self.assertEqual(self._claim().run_id, str(middle))
        self.assertEqual(self._claim().run_id, str(newest))
        self.assertIsNone(self._claim())

    # -- ownership, epoch, timing ------------------------------------------

    def test_the_epoch_increases_by_exactly_one_on_every_reclaim(self) -> None:
        run_id = self.add_run()
        epochs, recoveries = [], []
        for number in range(5):
            claimed = self._claim(f"w{number}")
            epochs.append(claimed.lease_epoch)
            recoveries.append(claimed.recovery_count)
            self.expire_lease(run_id)
        self.assertEqual(epochs, [1, 2, 3, 4, 5])
        self.assertEqual(recoveries, [0, 1, 2, 3, 4])
        self.assertEqual([e[0] for e in self.events(run_id)], [1, 2, 3, 4])
        self.assertEqual(
            [e[3] for e in self.events(run_id)],
            [f"run reclaimed (recovery_count={n})" for n in (1, 2, 3, 4)],
        )

    def test_a_worker_may_reclaim_its_own_expired_lease(self) -> None:
        run_id = self.add_run()
        self._claim("same")
        self.expire_lease(run_id)
        again = self._claim("same")
        self.assertEqual((again.lease_epoch, again.recovery_count), (2, 1))

    def test_lease_and_deadline_are_stamped_from_the_database_clock(self) -> None:
        run_id = self.add_run()
        self._claim()
        with self.engine.connect() as conn:
            lease_window, run_window = conn.execute(
                text("SELECT visible_at - started_at, deadline_at - started_at FROM runs WHERE id = :id"),
                {"id": as_id(run_id)},
            ).one()
        self.assertEqual(lease_window, TTL)  # one now(): exactly ttl, whatever this machine's clock says
        self.assertEqual(run_window, RUN_TIMEOUT)

    def test_a_reclaim_renews_the_lease_but_keeps_started_at_deadline_and_cancel_flag(self) -> None:
        run_id = self.add_expired_running_run(cancel_requested_at=Sql("now() - interval '5 minutes'"))
        before = self.run_row(run_id)
        self._claim("w2")
        after = self.run_row(run_id)
        self.assertEqual(after.started_at, before.started_at)
        self.assertEqual(after.deadline_at, before.deadline_at)  # set once, at first claim
        self.assertEqual(after.cancel_requested_at, before.cancel_requested_at)
        with self.engine.connect() as conn:
            lease_remaining = conn.execute(
                text("SELECT visible_at - clock_timestamp() FROM runs WHERE id = :id"), {"id": as_id(run_id)}
            ).scalar_one()
        self.assertGreater(lease_remaining, TTL - timedelta(seconds=30))
        self.assertLessEqual(lease_remaining, TTL)

    def test_the_database_invariant_running_iff_owner_holds_after_claims(self) -> None:
        self.add_run()
        self.add_expired_running_run()
        self._claim()
        self._claim()
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT status, lease_owner FROM runs")).all()
        self.assertTrue(all(status == "running" and owner for status, owner in rows))

    def test_event_sequence_continues_after_existing_events(self) -> None:
        run_id = self.add_expired_running_run()
        with self.engine.begin() as conn:
            for seq in (1, 2, 3):
                make_event(conn, run_id, seq=seq)
        self._claim()
        self.assertEqual([e[0] for e in self.events(run_id)], [1, 2, 3, 4])

    def test_a_superseded_epoch_matches_nothing_after_a_reclaim(self) -> None:
        # The fencing precondition M2.4's guard builds on: owner AND epoch must both match.
        run_id = self.add_expired_running_run()
        claimed = self._claim("w2")
        stale = "UPDATE runs SET visible_at = now() WHERE id = :id AND lease_owner = :owner AND lease_epoch = :epoch"
        with self.engine.begin() as conn:
            old = conn.execute(text(stale), {"id": as_id(run_id), "owner": "crashed-worker", "epoch": 1})
            current = conn.execute(text(stale), {"id": as_id(run_id), "owner": "w2", "epoch": claimed.lease_epoch})
        self.assertEqual((old.rowcount, current.rowcount), (0, 1))

    # -- transaction ownership and atomicity -------------------------------

    def test_a_failure_while_appending_the_event_rolls_the_whole_claim_back(self) -> None:
        run_id = self.add_expired_running_run()
        before = tuple(self.run_row(run_id))
        with self.engine.connect() as conn:
            with mock.patch("flowforge.worker.claim.append_event", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    claim_run(conn, "w2", TTL, RUN_TIMEOUT)
            self.assertFalse(conn.in_transaction(), "the failed claim must leave the connection clean")
            self.assertEqual(tuple(self.run_row(run_id)), before)  # owner, epoch, recovery_count, visible_at untouched
            self.assertEqual(self.events(run_id), [])
            retry = claim_run(conn, "w2", TTL, RUN_TIMEOUT)  # the same connection is reusable
        self.assertEqual((retry.lease_epoch, retry.recovery_count), (2, 1))

    def test_the_claim_is_committed_before_it_returns(self) -> None:
        run_id = self.add_run()
        self._claim()
        with self.engine.connect() as other:  # a different connection already sees it
            status = other.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": as_id(run_id)}).scalar_one()
        self.assertEqual(status, "running")

    def test_requires_a_clean_connection_and_claims_nothing_otherwise(self) -> None:
        run_id = self.add_run()
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                claim_run(conn, "w1", TTL, RUN_TIMEOUT)
        with self.engine.connect() as conn:
            conn.begin()
            conn.begin_nested()
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                claim_run(conn, "w1", TTL, RUN_TIMEOUT)
            conn.rollback()
        self.assertEqual(self.run_row(run_id).status, "queued")

    def test_invalid_arguments_claim_nothing(self) -> None:
        run_id = self.add_run()
        with self.engine.connect() as conn:
            with self.assertRaises(ValueError):
                claim_run(conn, "", TTL, RUN_TIMEOUT)
            with self.assertRaises(TypeError):
                claim_run(conn, "w", 45, RUN_TIMEOUT)  # type: ignore[arg-type]
            self.assertFalse(conn.in_transaction())
        self.assertEqual(self.run_row(run_id).status, "queued")

    # -- end to end ---------------------------------------------------------

    def test_enqueue_then_claim(self) -> None:
        with self.engine.connect() as conn:
            run_id = enqueue_run(conn, self.version_id, {"hello": "world"}, QUEUE_TIMEOUT, idempotency_key="e2e")
        claimed = self._claim("worker-1")
        self.assertEqual((claimed.run_id, claimed.prev_status, claimed.lease_epoch), (run_id, "queued", 1))
        self.assertEqual(claimed.queue_deadline_at - self.run_row(run_id).created_at, QUEUE_TIMEOUT)
        self.assertEqual(self.run_row(run_id).trigger_payload, {"hello": "world"})
