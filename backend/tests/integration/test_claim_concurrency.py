"""T1 under real concurrency: PostgreSQL 17, separate connections, no mocks.

Interleavings are forced with held row locks and barriers, never with sleeps.
Design section 8, row 7 (two workers' claim queries fire simultaneously).
"""

from __future__ import annotations

import threading

from sqlalchemy import text

from flowforge.worker.claim import ClaimedRun, claim_run
from tests.integration.support import Sql
from tests.integration.worker_support import RUN_TIMEOUT, TTL, WorkerDatabaseTestCase

JOIN_SECONDS = 60


def _claim_in_thread(engine, worker, results, errors, barrier=None, repeat=False):
    def run() -> None:
        try:
            if barrier is not None:
                barrier.wait(timeout=JOIN_SECONDS)
            with engine.connect() as conn:
                while True:
                    claimed = claim_run(conn, worker, TTL, RUN_TIMEOUT)
                    results.append((worker, claimed))
                    if claimed is None or not repeat:
                        return
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread by the test
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _join(test, threads) -> None:
    for thread in threads:
        thread.join(timeout=JOIN_SECONDS)
        test.assertFalse(thread.is_alive(), "a claimer is blocked: SKIP LOCKED must never wait")


class ClaimConcurrencyTestCase(WorkerDatabaseTestCase):
    def test_a_row_locked_by_another_transaction_is_skipped_not_waited_for(self) -> None:
        older = self.add_run(visible_at=Sql("now() - interval '2 minutes'"))
        newer = self.add_run(visible_at=Sql("now() - interval '1 minute'"))
        holder = self.engine.connect()
        self.addCleanup(holder.close)
        holder.execute(text("SELECT id FROM runs WHERE id = :id FOR UPDATE"), {"id": older})  # held open

        results, errors = [], []
        _join(self, [_claim_in_thread(self.engine, "b", results, errors)])
        self.assertEqual(errors, [])
        (_, claimed), = results
        self.assertEqual(claimed.run_id, str(newer))  # the older row is skipped, the next one taken
        self.assertEqual(self.run_row(older).status, "queued")

        results.clear()
        _join(self, [_claim_in_thread(self.engine, "b", results, errors)])
        self.assertEqual(results, [("b", None)])  # only the locked row remains: nothing to claim, no waiting

        holder.rollback()
        self.assertEqual(self._claim_once("c").run_id, str(older))

    def test_many_workers_claim_each_run_exactly_once(self) -> None:
        run_ids = {str(self.add_run()) for _ in range(12)}
        workers = 8
        barrier = threading.Barrier(workers)
        results, errors = [], []
        threads = [
            _claim_in_thread(self.engine, f"w{n}", results, errors, barrier=barrier, repeat=True)
            for n in range(workers)
        ]
        _join(self, threads)
        self.assertEqual(errors, [])
        claimed = [c for _, c in results if c is not None]
        self.assertEqual(len(claimed), 12)
        self.assertEqual({c.run_id for c in claimed}, run_ids)  # every run, none twice
        self.assertTrue(all(c.lease_epoch == 1 and c.recovery_count == 0 for c in claimed))
        with self.engine.connect() as conn:
            owners = conn.execute(text("SELECT status, lease_owner FROM runs")).all()
        self.assertTrue(all(status == "running" and owner for status, owner in owners))

    def test_racing_for_one_expired_lease_has_exactly_one_winner(self) -> None:
        for _ in range(20):
            run_id = self.add_expired_running_run()
            barrier = threading.Barrier(2)
            results, errors = [], []
            threads = [_claim_in_thread(self.engine, w, results, errors, barrier=barrier) for w in ("a", "b")]
            _join(self, threads)
            self.assertEqual(errors, [])
            winners = [c for _, c in results if isinstance(c, ClaimedRun)]
            self.assertEqual(len(winners), 1, results)
            self.assertEqual((winners[0].lease_epoch, winners[0].recovery_count), (2, 1))
            self.assertEqual(self.run_row(run_id).lease_epoch, 2)  # one epoch bump, never two
            self.assertEqual(len(self.events(run_id)), 1)  # one reclaim event
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM run_events"))
                conn.execute(text("DELETE FROM runs"))

    # -- helpers -------------------------------------------------------------

    def _claim_once(self, worker: str):
        with self.engine.connect() as conn:
            return claim_run(conn, worker, TTL, RUN_TIMEOUT)
