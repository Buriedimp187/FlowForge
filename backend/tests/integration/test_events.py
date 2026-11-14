"""``LeaseProof`` and ``append_event`` against PostgreSQL 17 (design section 3)."""

from __future__ import annotations

import threading

from sqlalchemy import text

from flowforge.worker.events import LeaseProof, _check_proof, append_event
from tests.integration.support import CHECK_VIOLATION, assert_db_error, make_event
from tests.integration.worker_support import WorkerDatabaseTestCase, as_id


def _proof(conn, run_id) -> LeaseProof:
    return LeaseProof(str(run_id), "w1", 1, conn, conn.get_transaction())


class AppendEventTestCase(WorkerDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_id = self.add_expired_running_run(lease_owner="w1")

    def test_sequence_is_gapless_and_the_proof_is_reusable_in_its_transaction(self) -> None:
        with self.engine.connect() as conn, conn.begin():
            proof = _proof(conn, self.run_id)
            append_event(conn, proof, "info", None, "one")
            append_event(conn, proof, "warning", "n1", "two")
            append_event(conn, proof, "error", None, "three")
        self.assertEqual(
            self.events(self.run_id),
            [(1, "info", None, "one"), (2, "warning", "n1", "two"), (3, "error", None, "three")],
        )

    def test_sequence_continues_after_existing_events(self) -> None:
        with self.engine.begin() as conn:
            make_event(conn, self.run_id, seq=1)
            make_event(conn, self.run_id, seq=2)
        with self.engine.connect() as conn, conn.begin():
            append_event(conn, _proof(conn, self.run_id), "info", None, "next")
        self.assertEqual([e[0] for e in self.events(self.run_id)], [1, 2, 3])

    def test_events_are_atomic_with_their_transaction(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            append_event(conn, _proof(conn, self.run_id), "info", None, "never committed")
            trans.rollback()
        self.assertEqual(self.events(self.run_id), [])

    def test_the_level_check_is_enforced_by_the_database(self) -> None:
        with self.engine.connect() as conn:
            conn.begin()
            assert_db_error(self, CHECK_VIOLATION, lambda: append_event(conn, _proof(conn, self.run_id), "fatal", None, "x"))
            conn.rollback()

    def test_a_proof_is_stale_once_its_transaction_has_ended(self) -> None:
        with self.engine.connect() as conn:
            with conn.begin():
                proof = _proof(conn, self.run_id)
            with conn.begin():
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    append_event(conn, proof, "info", None, "late")
        self.assertEqual(self.events(self.run_id), [])

    def test_a_proof_is_refused_on_a_different_connection(self) -> None:
        with self.engine.connect() as issuer, self.engine.connect() as other:
            with issuer.begin():
                proof = _proof(issuer, self.run_id)
                with other.begin():
                    with self.assertRaisesRegex(RuntimeError, "different connection"):
                        append_event(other, proof, "info", None, "x")

    def test_a_savepoint_is_rejected_at_the_moment_of_use(self) -> None:
        with self.engine.connect() as conn:
            with conn.begin():
                proof = _proof(conn, self.run_id)
                nested = conn.begin_nested()
                with self.assertRaisesRegex(RuntimeError, "SAVEPOINT"):
                    append_event(conn, proof, "info", None, "x")
                with self.assertRaisesRegex(RuntimeError, "SAVEPOINT"):
                    _check_proof(conn, proof)
                nested.rollback()
                append_event(conn, proof, "info", None, "after the savepoint closed")  # no longer nested: allowed
        self.assertEqual([e[3] for e in self.events(self.run_id)], ["after the savepoint closed"])

    def test_concurrent_writers_for_one_run_never_duplicate_a_sequence_number(self) -> None:
        # Design section 8, row 15: writers serialize on the shared `runs` row lock.
        writers, per_writer = 8, 4
        barrier = threading.Barrier(writers)
        errors: list[BaseException] = []

        def write(number: int) -> None:
            try:
                barrier.wait(timeout=60)
                with self.engine.connect() as conn, conn.begin():
                    conn.execute(text("SELECT id FROM runs WHERE id = :id FOR UPDATE"), {"id": as_id(self.run_id)})
                    proof = _proof(conn, self.run_id)
                    for step in range(per_writer):
                        append_event(conn, proof, "info", None, f"w{number}-{step}")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,), daemon=True) for n in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([e[0] for e in self.events(self.run_id)], list(range(1, writers * per_writer + 1)))
