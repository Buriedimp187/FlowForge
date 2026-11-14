"""M2.3 worker layer: checks that need no database.

Argument validation, frozen types, and the architectural boundaries of
``flowforge.worker``: Core-only SQL, no ORM, no ``::`` casts inside ``text()``
(design D2), claim logic confined to the worker package, and nothing from the
later M2 stages (T2-T4, finalization) added yet.
"""

from __future__ import annotations

import dataclasses
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from flowforge.worker import claim, connection, enqueue, events
from flowforge.worker.claim import ClaimedRun, claim_run
from flowforge.worker.connection import as_uuid, require_clean_connection, require_positive_interval
from flowforge.worker.enqueue import enqueue_run
from flowforge.worker.events import LeaseProof

SRC = Path(__file__).resolve().parents[2] / "src" / "flowforge"
OK = timedelta(seconds=30)


def _dirty_connection() -> mock.Mock:
    conn = mock.Mock()
    conn.in_transaction.return_value = True
    return conn


class ArgumentConventionsTestCase(unittest.TestCase):
    def test_intervals_must_be_positive_timedeltas(self) -> None:
        self.assertEqual(require_positive_interval("ttl", OK), OK)
        for bad in (30, 30.0, "30s", None, True):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                require_positive_interval("ttl", bad)
        for bad in (timedelta(0), timedelta(seconds=-1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                require_positive_interval("ttl", bad)

    def test_ids_accept_uuid_or_canonical_string_only(self) -> None:
        value = uuid.uuid7()
        self.assertEqual(as_uuid("id", value), value)
        self.assertEqual(as_uuid("id", str(value)), value)
        with self.assertRaises(ValueError):
            as_uuid("id", "not-a-uuid")
        for bad in (123, None, b"x"):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                as_uuid("id", bad)

    def test_a_connection_with_a_transaction_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "clean connection"):
            require_clean_connection(_dirty_connection())
        clean = mock.Mock()
        clean.in_transaction.return_value = False
        require_clean_connection(clean)


class ClaimArgumentsTestCase(unittest.TestCase):
    def test_worker_must_be_a_non_empty_string(self) -> None:
        for bad in ("", None, 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                claim_run(mock.Mock(), bad, OK, OK)  # type: ignore[arg-type]

    def test_ttl_and_run_timeout_have_no_defaults_and_are_validated(self) -> None:
        with self.assertRaises(TypeError):
            claim_run(mock.Mock(), "w")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            claim_run(mock.Mock(), "w", 30, OK)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            claim_run(mock.Mock(), "w", OK, timedelta(0))

    def test_a_dirty_connection_is_refused_before_any_sql(self) -> None:
        conn = _dirty_connection()
        with self.assertRaisesRegex(RuntimeError, "clean connection"):
            claim_run(conn, "w", OK, OK)
        conn.execute.assert_not_called()
        conn.begin.assert_not_called()


class EnqueueArgumentsTestCase(unittest.TestCase):
    def test_queue_timeout_has_no_default_and_is_validated(self) -> None:
        with self.assertRaises(TypeError):
            enqueue_run(mock.Mock(), uuid.uuid7(), {})  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            enqueue_run(mock.Mock(), uuid.uuid7(), {}, 3600)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            enqueue_run(mock.Mock(), uuid.uuid7(), {}, timedelta(0))

    def test_ids_and_keys_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            enqueue_run(mock.Mock(), "nope", {}, OK)
        with self.assertRaises(TypeError):
            enqueue_run(mock.Mock(), uuid.uuid7(), {}, OK, idempotency_key=5)  # type: ignore[arg-type]

    def test_payload_must_be_strict_json(self) -> None:
        for bad in (float("nan"), {"x": float("inf")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                enqueue_run(mock.Mock(), uuid.uuid7(), bad, OK)
        with self.assertRaises(TypeError):
            enqueue_run(mock.Mock(), uuid.uuid7(), {"x": object()}, OK)

    def test_a_dirty_connection_is_refused_before_any_sql(self) -> None:
        conn = _dirty_connection()
        with self.assertRaisesRegex(RuntimeError, "clean connection"):
            enqueue_run(conn, uuid.uuid7(), {}, OK)
        conn.execute.assert_not_called()


class FrozenTypesTestCase(unittest.TestCase):
    def test_claimed_run_and_lease_proof_are_frozen(self) -> None:
        now = datetime.now(timezone.utc)
        claimed = ClaimedRun("r", "w", 1, now, now, 0, "queued")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            claimed.lease_epoch = 2  # type: ignore[misc]
        proof = LeaseProof("r", "w", 1, mock.Mock(), object())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proof.lease_epoch = 2  # type: ignore[misc]
        self.assertNotIn("Mock", repr(proof))  # the connection never leaks into logs

    def test_claimed_run_has_exactly_the_design_fields(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(ClaimedRun)],
            ["run_id", "lease_owner", "lease_epoch", "deadline_at", "queue_deadline_at", "recovery_count", "prev_status"],
        )


class SourceBoundaryTestCase(unittest.TestCase):
    def _worker_sources(self) -> dict[str, str]:
        files = sorted((SRC / "worker").glob("*.py"))
        self.assertEqual(
            {f.name for f in files},
            {
                "__init__.py", "claim.py", "connection.py", "enqueue.py", "events.py",  # M2.3
                "guard.py", "heartbeat.py", "park.py", "shutdown.py",  # M2.4
            },
        )
        return {f.name: f.read_text(encoding="utf-8") for f in files}

    def test_the_t1_statements_keep_the_approved_shape(self) -> None:
        self.assertIn("FOR UPDATE SKIP LOCKED", claim._SELECT_CANDIDATE)
        self.assertIn("status IN ('queued','waiting','running') AND visible_at <= now()", claim._SELECT_CANDIDATE)
        self.assertIn("ORDER BY visible_at LIMIT 1", claim._SELECT_CANDIDATE)
        self.assertIn("lease_epoch = lease_epoch + 1", claim._CLAIM)
        self.assertIn("CASE WHEN status = 'running' THEN 1 ELSE 0 END", claim._CLAIM)
        self.assertIn("COALESCE(started_at, now())", claim._CLAIM)
        self.assertIn("COALESCE(deadline_at, now() + :run_timeout)", claim._CLAIM)

    def test_no_double_colon_casts_in_sql(self) -> None:
        for name, source in self._worker_sources().items():
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"(?<!:):{2}(?!:)\s*\w", source.replace('"""', "")))

    def test_only_t1_gates_on_visible_at(self) -> None:
        for name, source in self._worker_sources().items():
            if name != "claim.py":
                with self.subTest(file=name):
                    self.assertNotIn("visible_at <=", source)

    def test_worker_package_is_core_only(self) -> None:
        forbidden = re.compile(r"sqlalchemy\.orm|declarative_base|DeclarativeBase|\bsessionmaker\b|\bMetaData\(|\bTable\(|\bmapped_column\(")
        for name, source in self._worker_sources().items():
            with self.subTest(file=name):
                self.assertIsNone(forbidden.search(source))

    def test_pure_layers_never_import_the_worker_package(self) -> None:
        pattern = re.compile(r"^\s*(?:from|import)\s+flowforge\.worker\b", re.MULTILINE)
        for package in ("domain", "engine"):
            for path in sorted((SRC / package).rglob("*.py")):
                with self.subTest(file=str(path.relative_to(SRC))):
                    self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_later_stage_functionality_is_not_present_yet(self) -> None:
        banned = re.compile(
            r"def (finalize_run|run_t4|start_node|finish_node)\b"  # T2-T4 and finalization: M2.5+
        )
        for name, source in self._worker_sources().items():
            with self.subTest(file=name):
                self.assertIsNone(banned.search(source))

    def test_modules_expose_the_approved_names(self) -> None:
        for module, names in (
            (connection, ("require_clean_connection", "worker_connection")),
            (events, ("LeaseProof", "_reject_if_nested", "_check_proof", "append_event")),
            (claim, ("ClaimedRun", "claim_run")),
            (enqueue, ("enqueue_run",)),
        ):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(hasattr(module, name))


if __name__ == "__main__":
    unittest.main()
