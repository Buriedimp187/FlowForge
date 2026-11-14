"""Alembic migration infrastructure: upgrade, downgrade, atomicity, locking."""

from __future__ import annotations

import threading
import time
import unittest

from sqlalchemy import Engine, text

from flowforge.db import migrate
from flowforge.db.engine import create_db_engine
from tests.integration.support import FIVE_TABLES, TempDatabase, admin_database_url, public_tables


class MigrationTestCase(unittest.TestCase):
    """A brand-new, UNMIGRATED database per test (the schema tests share one)."""

    engine: Engine

    def setUp(self) -> None:
        self._temp_db = TempDatabase(admin_database_url()).create()
        self.addCleanup(self._temp_db.drop)
        self.engine = create_db_engine(self._temp_db.url)
        self.addCleanup(self.engine.dispose)

    def _tables(self) -> set[str]:
        with self.engine.connect() as connection:
            return public_tables(connection)

    def _count(self, sql: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(text(sql)).scalar_one()

    def test_a_new_database_is_unmigrated(self) -> None:
        self.assertIsNone(migrate.current_revision(self.engine))
        self.assertEqual(self._tables(), set())

    def test_upgrade_to_head_creates_the_schema_and_records_the_revision(self) -> None:
        migrate.upgrade(self.engine)
        self.assertEqual(migrate.current_revision(self.engine), migrate.head_revision())
        self.assertEqual(migrate.head_revision(), "0001")
        self.assertEqual(self._tables(), FIVE_TABLES | {"alembic_version"})

    def test_upgrade_is_idempotent(self) -> None:
        migrate.upgrade(self.engine)
        migrate.upgrade(self.engine)  # already at head: a no-op, not an error
        self.assertEqual(migrate.current_revision(self.engine), "0001")

    def test_downgrade_to_base_removes_every_object_it_created(self) -> None:
        migrate.upgrade(self.engine)
        migrate.downgrade(self.engine, "base")
        self.assertIsNone(migrate.current_revision(self.engine))
        self.assertEqual(self._tables(), {"alembic_version"})
        self.assertEqual(self._count("SELECT count(*) FROM pg_proc WHERE proname = 'enforce_effect_class_immutable'"), 0)
        self.assertEqual(self._count("SELECT count(*) FROM pg_trigger WHERE tgname = 'node_runs_effect_class_immutable'"), 0)
        self.assertEqual(self._count("SELECT count(*) FROM pg_indexes WHERE indexname = 'runs_claimable_idx'"), 0)
        # Only Alembic's own bookkeeping table (its primary key) may remain.
        self.assertEqual(
            self._count(
                "SELECT count(*) FROM pg_constraint WHERE connamespace = CAST('public' AS regnamespace) "
                "AND conrelid <> CAST('alembic_version' AS regclass)"
            ),
            0,
        )

    def test_the_schema_can_be_recreated_after_a_downgrade(self) -> None:
        migrate.upgrade(self.engine)
        migrate.downgrade(self.engine, "base")
        migrate.upgrade(self.engine)
        self.assertEqual(migrate.current_revision(self.engine), "0001")
        self.assertEqual(self._tables(), FIVE_TABLES | {"alembic_version"})

    def test_a_failing_upgrade_is_atomic_and_releases_the_lock(self) -> None:
        # run_events is the LAST table the migration creates. Pre-creating it
        # makes the migration fail at its final statement; transactional DDL
        # must roll back everything created before it.
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE run_events (x integer)"))
        with self.assertRaises(Exception):
            migrate.upgrade(self.engine)
        self.assertEqual(self._tables(), {"run_events"})  # nothing else survived
        self.assertIsNone(migrate.current_revision(self.engine))
        # The advisory lock must be free again, or the next starter would hang.
        with self.engine.connect() as connection:
            got = connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": migrate.MIGRATION_LOCK_KEY}).scalar_one()
            self.assertTrue(got)
            connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": migrate.MIGRATION_LOCK_KEY})
            connection.commit()

    def test_concurrent_migrations_are_serialized_by_the_advisory_lock(self) -> None:
        holder = self.engine.connect()
        self.addCleanup(holder.close)
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": migrate.MIGRATION_LOCK_KEY})
        holder.commit()

        errors: list[BaseException] = []

        def run() -> None:
            try:
                migrate.upgrade(self.engine)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(1.5)
        self.assertTrue(thread.is_alive(), "upgrade should block while another session holds the migration lock")
        self.assertIsNone(migrate.current_revision(self.engine))

        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": migrate.MIGRATION_LOCK_KEY})
        holder.commit()
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(migrate.current_revision(self.engine), "0001")
