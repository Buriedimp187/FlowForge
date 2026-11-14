"""The configured Core engine: driver, isolation level, time zone, transactions."""

from __future__ import annotations

from sqlalchemy import text

from tests.integration.support import DatabaseTestCase, make_workflow


class EngineTestCase(DatabaseTestCase):
    def test_uses_psycopg_3(self) -> None:
        self.assertEqual(self.engine.dialect.name, "postgresql")
        self.assertEqual(self.engine.dialect.driver, "psycopg")

    def test_runs_against_postgresql_17(self) -> None:
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SHOW server_version_num")).scalar_one()[:2], "17")

    def test_isolation_level_is_read_committed(self) -> None:
        # The queue/lease/fencing design assumes READ COMMITTED (architecture
        # section 4, Risk R7); the engine requests it explicitly.
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SHOW transaction_isolation")).scalar_one(), "read committed")

    def test_session_time_zone_is_utc(self) -> None:
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SHOW timezone")).scalar_one(), "UTC")

    def test_connections_are_not_autocommit_so_boundaries_are_explicit(self) -> None:
        # A statement run on a plain connection that is never committed is rolled back.
        with self.engine.connect() as c:
            make_workflow(c)
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM workflows")).scalar_one(), 0)

    def test_begin_commits_on_success_and_rolls_back_on_error(self) -> None:
        with self.engine.begin() as c:
            make_workflow(c, name="kept")
        with self.assertRaises(RuntimeError):
            with self.engine.begin() as c:
                make_workflow(c, name="discarded")
                raise RuntimeError("abort")
        with self.engine.connect() as c:
            names = [r[0] for r in c.execute(text("SELECT name FROM workflows"))]
        self.assertEqual(names, ["kept"])
