"""M2.1 persistence foundation: checks that need no database.

Covers application-generated UUIDv7 ids, database URL normalization, the
migration script's scope (five tables, no database-side id defaults), and the
architectural boundaries: the pure M1 layers never import persistence code, and
``flowforge.db`` stays Core-only with no parallel schema definition.
"""

from __future__ import annotations

import os
import re
import unittest
import uuid
from pathlib import Path
from unittest import mock

from alembic.script import ScriptDirectory

from flowforge.db import engine as db_engine
from flowforge.db import ids, migrate

SRC = Path(__file__).resolve().parents[2] / "src" / "flowforge"


class NewIdTestCase(unittest.TestCase):
    def test_is_a_uuidv7(self) -> None:
        value = ids.new_id()
        self.assertIsInstance(value, uuid.UUID)
        self.assertEqual(value.version, 7)
        self.assertEqual(value.variant, uuid.RFC_4122)

    def test_ids_are_unique_and_time_ordered(self) -> None:
        values = [ids.new_id() for _ in range(2000)]
        self.assertEqual(len(set(values)), len(values))
        # UUIDv7 leads with a millisecond timestamp, and the standard library
        # keeps a counter for monotonicity within a millisecond.
        self.assertEqual(values, sorted(values))

    def test_uses_the_standard_library_generator(self) -> None:
        sentinel = uuid.UUID(int=7)
        with mock.patch.object(uuid, "uuid7", return_value=sentinel) as generator:
            self.assertIs(ids.new_id(), sentinel)
        generator.assert_called_once_with()


class DatabaseUrlTestCase(unittest.TestCase):
    def test_plain_postgresql_urls_get_the_psycopg_driver(self) -> None:
        for raw in ("postgresql://u:p@h:5433/db", "postgres://u:p@h:5433/db", "postgresql+psycopg://u:p@h:5433/db"):
            with self.subTest(raw=raw):
                url = db_engine.normalize_database_url(raw)
                self.assertEqual(url.drivername, "postgresql+psycopg")
                self.assertEqual(
                    (url.username, url.password, url.host, url.port, url.database), ("u", "p", "h", 5433, "db")
                )

    def test_other_drivers_and_databases_are_rejected(self) -> None:
        for raw in (
            "postgresql+psycopg2://u:p@h/db",
            "postgresql+asyncpg://u:p@h/db",
            "postgresql+pg8000://u:p@h/db",
            "sqlite:///x.db",
            "mysql+pymysql://u:p@h/db",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                db_engine.normalize_database_url(raw)

    def test_url_is_read_from_the_environment_and_absence_fails_loudly(self) -> None:
        with mock.patch.dict(os.environ, {db_engine.DATABASE_URL_ENV: "postgresql://u:p@h/db"}):
            self.assertEqual(db_engine.database_url_from_env(), "postgresql://u:p@h/db")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, db_engine.DATABASE_URL_ENV):
                db_engine.database_url_from_env()

    def test_engine_requests_read_committed_and_psycopg_without_connecting(self) -> None:
        engine = db_engine.create_db_engine("postgresql://u:p@localhost:1/db")  # lazy: nothing connects
        try:
            self.assertEqual(engine.dialect.driver, "psycopg")
            self.assertEqual(db_engine.ISOLATION_LEVEL, "READ COMMITTED")
            self.assertEqual(engine.get_execution_options(), {})
        finally:
            engine.dispose()


class MigrationScriptTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = ScriptDirectory.from_config(migrate.alembic_config())
        cls.revision = cls.script.get_revision("0001")
        cls.upgrade_sql = "\n".join(cls.revision.module._UPGRADE)

    def test_there_is_a_single_linear_head(self) -> None:
        self.assertEqual(self.script.get_heads(), ["0001"])
        self.assertIsNone(self.revision.down_revision)
        self.assertEqual(migrate.head_revision(), "0001")

    def test_script_location_is_inside_the_package(self) -> None:
        # docs/architecture.md section 12: Alembic lives inside the Python package.
        self.assertEqual(migrate.MIGRATIONS_DIR, SRC / "db" / "migrations")
        self.assertTrue((migrate.MIGRATIONS_DIR / "env.py").is_file())
        self.assertTrue((migrate.MIGRATIONS_DIR / "script.py.mako").is_file())

    def test_creates_exactly_the_five_approved_tables(self) -> None:
        created = set(re.findall(r"CREATE TABLE (\w+)", self.upgrade_sql))
        self.assertEqual(created, {"workflows", "workflow_versions", "runs", "node_runs", "run_events"})

    def test_deferred_tables_and_a_separate_queue_table_are_absent(self) -> None:
        for name in ("admin_user", "sessions", "credentials", "webhook_endpoints", "app_settings", "queue", "run_leases"):
            # Match table creation only: 'queue' is a substring of the column queue_deadline_at.
            self.assertNotRegex(self.upgrade_sql, rf"CREATE TABLE {name}\b")
            self.assertNotRegex(self.upgrade_sql, rf"REFERENCES {name}\b")

    def test_ids_have_no_database_side_default(self) -> None:
        # Design D1: application-generated UUIDv7; gen_random_uuid() would be UUIDv4 on PG 17.
        self.assertNotIn("gen_random_uuid", self.upgrade_sql)
        self.assertNotIn("uuid_generate", self.upgrade_sql)
        id_columns = re.findall(r"^\s*id\s+uuid[^\n]*$", self.upgrade_sql, flags=re.MULTILINE)
        self.assertEqual(len(id_columns), 3)  # workflows, workflow_versions, runs
        for line in id_columns:
            self.assertRegex(line, r"id\s+uuid PRIMARY KEY,$")

    def test_no_sql_text_contains_a_colon_bind_hazard(self) -> None:
        # Statements go through sa.text(); a ':word' would be read as a bind parameter.
        self.assertIsNone(re.search(r"(?<![:\w]):[A-Za-z_]", self.upgrade_sql))

    def test_every_object_created_is_dropped_by_the_downgrade(self) -> None:
        down = "\n".join(self.revision.module._DOWNGRADE)
        for table in re.findall(r"CREATE TABLE (\w+)", self.upgrade_sql):
            self.assertIn(f"DROP TABLE {table}", down)
        self.assertIn("DROP INDEX runs_claimable_idx", down)
        self.assertIn("DROP TRIGGER node_runs_effect_class_immutable", down)
        self.assertIn("DROP FUNCTION enforce_effect_class_immutable()", down)
        self.assertIn("DROP CONSTRAINT fk_active_version_belongs_to_workflow", down)


class ArchitecturalBoundaryTestCase(unittest.TestCase):
    _PERSISTENCE_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:sqlalchemy|psycopg|alembic|flowforge\.db)\b", re.MULTILINE)

    def _sources(self, package: str) -> list[Path]:
        files = sorted((SRC / package).rglob("*.py"))
        self.assertTrue(files, package)
        return files

    def test_pure_m1_layers_never_import_persistence_code(self) -> None:
        for package in ("domain", "engine"):
            for path in self._sources(package):
                with self.subTest(file=str(path.relative_to(SRC))):
                    self.assertIsNone(self._PERSISTENCE_IMPORT.search(path.read_text(encoding="utf-8")))

    def test_db_package_is_core_only_with_no_parallel_schema_definition(self) -> None:
        forbidden = re.compile(
            r"sqlalchemy\.orm|declarative_base|DeclarativeBase|\bsessionmaker\b|\bMetaData\(|\bTable\(|\bmapped_column\("
        )
        for path in self._sources("db"):
            with self.subTest(file=str(path.relative_to(SRC))):
                self.assertIsNone(forbidden.search(path.read_text(encoding="utf-8")))

    def test_no_worker_logic_has_been_added_yet(self) -> None:
        # Claiming, leases, heartbeats, fencing, retries, cancellation and
        # finalization belong to later M2 stages.
        self.assertFalse((SRC / "worker").exists())
        for path in self._sources("db"):
            source = path.read_text(encoding="utf-8")
            for banned in ("FOR UPDATE SKIP LOCKED", "lease_epoch +", "LeaseProof", "ClaimedRun"):
                with self.subTest(file=path.name, banned=banned):
                    self.assertNotIn(banned, source)
