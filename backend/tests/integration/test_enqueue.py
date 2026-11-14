"""T0 ``enqueue_run`` against PostgreSQL 17 (design section 4.1)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import text

from flowforge.worker.enqueue import enqueue_run
from tests.integration.support import FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, assert_db_error, make_version
from tests.integration.worker_support import QUEUE_TIMEOUT, WorkerDatabaseTestCase, as_id


class EnqueueTestCase(WorkerDatabaseTestCase):
    def _enqueue(self, **kwargs):
        kwargs.setdefault("workflow_version_id", self.version_id)
        kwargs.setdefault("trigger_payload", {"k": "v"})
        kwargs.setdefault("queue_timeout", QUEUE_TIMEOUT)
        with self.engine.connect() as conn:
            run_id = enqueue_run(conn, **kwargs)
            self.assertFalse(conn.in_transaction(), "enqueue_run must leave the connection clean")
        return run_id

    def test_inserts_a_visible_queued_run_and_returns_a_canonical_uuidv7_string(self) -> None:
        run_id = self._enqueue()
        self.assertIsInstance(run_id, str)
        self.assertEqual(str(uuid.UUID(run_id)), run_id)
        self.assertEqual(uuid.UUID(run_id).version, 7)
        row = self.run_row(run_id)
        self.assertEqual(row.status, "queued")
        self.assertIsNone(row.lease_owner)
        self.assertEqual((row.lease_epoch, row.recovery_count, row.has_unknown_outcome), (0, 0, False))
        self.assertEqual(row.workflow_version_id, self.version_id)
        for column in ("started_at", "deadline_at", "finished_at", "cancel_requested_at", "idempotency_key"):
            self.assertIsNone(getattr(row, column), column)

    def test_timestamps_come_from_one_database_clock_reading(self) -> None:
        run_id = self._enqueue(queue_timeout=timedelta(minutes=17, seconds=3))
        with self.engine.connect() as conn:
            visible_equals_created, queue_window = conn.execute(
                text("SELECT visible_at = created_at, queue_deadline_at - created_at FROM runs WHERE id = :id"),
                {"id": as_id(run_id)},
            ).one()
        self.assertTrue(visible_equals_created)  # visible immediately, same now()
        self.assertEqual(queue_window, timedelta(minutes=17, seconds=3))

    def test_the_trigger_payload_round_trips_as_jsonb(self) -> None:
        payload = {"name": "Zoë ✓", "n": [1, 2.5, None, True], "nested": {"a": {"b": []}}}
        self.assertEqual(self.run_row(self._enqueue(trigger_payload=payload)).trigger_payload, payload)
        self.assertEqual(self.run_row(self._enqueue(trigger_payload=[1, 2])).trigger_payload, [1, 2])

    def test_accepts_the_version_id_as_a_string_or_a_uuid(self) -> None:
        self._enqueue(workflow_version_id=str(self.version_id))
        self._enqueue(workflow_version_id=self.version_id)
        self.assertEqual(self.count_runs(), 2)

    def test_a_duplicate_idempotency_key_is_a_unique_violation_and_leaves_one_row(self) -> None:
        self._enqueue(idempotency_key="k1")
        with self.engine.connect() as conn:
            assert_db_error(
                self,
                UNIQUE_VIOLATION,
                lambda: enqueue_run(conn, self.version_id, {}, QUEUE_TIMEOUT, idempotency_key="k1"),
            )
            self.assertFalse(conn.in_transaction(), "a failed enqueue must roll back and leave the connection clean")
        self.assertEqual(self.count_runs(), 1)

    def test_null_keys_never_collide_and_the_key_is_scoped_to_the_version(self) -> None:
        self._enqueue()
        self._enqueue()
        with self.engine.begin() as conn:
            other_version = make_version(conn, self.workflow_id, version_number=2)
        self._enqueue(idempotency_key="same")
        self._enqueue(workflow_version_id=other_version, idempotency_key="same")
        self.assertEqual(self.count_runs(), 4)

    def test_an_unknown_workflow_version_is_a_foreign_key_violation(self) -> None:
        with self.engine.connect() as conn:
            assert_db_error(self, FOREIGN_KEY_VIOLATION, lambda: enqueue_run(conn, uuid.uuid7(), {}, QUEUE_TIMEOUT))
            self.assertFalse(conn.in_transaction())
        self.assertEqual(self.count_runs(), 0)

    def test_invalid_input_is_rejected_before_anything_is_written(self) -> None:
        with self.engine.connect() as conn:
            with self.assertRaises(ValueError):
                enqueue_run(conn, self.version_id, {"x": float("nan")}, QUEUE_TIMEOUT)
            with self.assertRaises(ValueError):
                enqueue_run(conn, self.version_id, {}, timedelta(0))
            self.assertFalse(conn.in_transaction())
        self.assertEqual(self.count_runs(), 0)

    def test_requires_a_clean_connection(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))  # autobegin: no longer clean
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                enqueue_run(conn, self.version_id, {}, QUEUE_TIMEOUT)
        with self.engine.connect() as conn:
            conn.begin()
            conn.begin_nested()
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                enqueue_run(conn, self.version_id, {}, QUEUE_TIMEOUT)
            conn.rollback()
        self.assertEqual(self.count_runs(), 0)
