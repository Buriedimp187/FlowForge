"""``load_run_snapshot`` against PostgreSQL 17: one consistent, read-only database snapshot."""

from __future__ import annotations

import threading
import uuid

from sqlalchemy import create_engine, event, text

from flowforge.domain.states import NodeRunStatus
from flowforge.engine.readiness import NodeReadiness, evaluate_readiness
from flowforge.worker.codec import CodecError
from flowforge.worker.snapshot import load_run_snapshot
from tests.integration.support import Sql, make_node_run, make_version
from tests.integration.worker_support import WorkerDatabaseTestCase, as_id
from tests.unit.support import new_catalog

DEFINITION = {
    "nodes": [
        {"key": "trigger", "type": "manual_trigger", "type_version": 1},
        {"key": "a", "type": "set", "type_version": 1, "params": {"value": "x"}},
    ],
    "edges": [{"source_key": "trigger", "source_port": "main", "target_key": "a", "target_port": "main"}],
    "ui": {"positions": {}},
}


class SnapshotTestCase(WorkerDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as conn:
            self.version_id = make_version(conn, self.workflow_id, version_number=2, schema_version=1, definition=DEFINITION)

    def _snapshot(self, run_id):
        with self.engine.connect() as conn:
            snapshot = load_run_snapshot(conn, str(run_id))
            self.assertFalse(conn.in_transaction(), "the snapshot must leave the connection clean")
        return snapshot

    def _tables(self) -> tuple:
        with self.engine.connect() as conn:
            return tuple(
                tuple(tuple(r) for r in conn.execute(text(f"SELECT * FROM {t} ORDER BY 1, 2")).all())
                for t in ("runs", "node_runs", "run_events")
            )

    def _run_with_nodes(self):
        run_id = self.add_run(workflow_version_id=self.version_id)
        with self.engine.begin() as conn:
            make_node_run(conn, run_id, "trigger", status="succeeded", output={"main": [{"json": {"x": 1}}]}, started_epoch=1, attempt=1)
            make_node_run(
                conn, run_id, "a", status="retry_wait", started_epoch=1, attempt=2, failures=1,
                retry_at=Sql("now() + interval '1 minute'"), error={"code": "boom", "message": "m", "cause": None},
            )
        return run_id

    def test_reads_the_run_the_definition_and_every_node_row(self) -> None:
        run_id = self._run_with_nodes()
        snap = self._snapshot(run_id)
        self.assertEqual((snap.run_id, snap.workflow_version_id, snap.status), (str(run_id), str(self.version_id), "queued"))
        self.assertEqual([n.key for n in snap.definition.nodes], ["trigger", "a"])
        self.assertEqual(snap.definition.nodes[1].params["value"], "x")
        self.assertEqual(snap.definition.edges[0].id, "trigger.main->a.main")  # derived: the stored edge has no id
        self.assertEqual([r.node_key for r in snap.node_runs], ["a", "trigger"])  # deterministic: ordered by node_key
        a = snap.node_run("a")
        self.assertEqual((a.status, a.attempt, a.failures, a.started_epoch), (NodeRunStatus.RETRY_WAIT, 2, 1, 1))
        self.assertEqual(a.error.code, "boom")
        self.assertIn("main", snap.node_run("trigger").output)

    def test_run_facts_reflect_a_real_claim(self) -> None:
        claimed = self.start_run("w1", workflow_version_id=self.version_id)
        snap = self._snapshot(claimed.run_id)
        self.assertEqual((snap.status, snap.lease_owner, snap.lease_epoch, snap.recovery_count), ("running", "w1", 1, 0))
        self.assertEqual(snap.deadline_at, claimed.deadline_at)
        self.assertEqual(snap.queue_deadline_at, claimed.queue_deadline_at)

    def test_db_now_is_one_database_clock_sample_with_a_timezone(self) -> None:
        run_id = self._run_with_nodes()
        with self.engine.connect() as conn:
            before = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        snap = self._snapshot(run_id)
        with self.engine.connect() as conn:
            after = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        self.assertIsNotNone(snap.db_now.tzinfo)
        self.assertLessEqual(before, snap.db_now)
        self.assertLessEqual(snap.db_now, after)

    def test_readiness_is_decided_by_the_snapshots_database_time(self) -> None:
        run_id = self.add_run(workflow_version_id=self.version_id)
        with self.engine.begin() as conn:
            make_node_run(conn, run_id, "trigger", status="succeeded", output={"main": [{"json": {}}]}, started_epoch=1, attempt=1)
            make_node_run(conn, run_id, "a", status="retry_wait", started_epoch=1, attempt=1, failures=1,
                          retry_at=Sql("now() - interval '1 second'"))  # fmt: skip
        snap = self._snapshot(run_id)
        verdict = evaluate_readiness("a", snap.definition, str(run_id), snap.run_store(), snap.clock(), new_catalog())
        self.assertIs(verdict, NodeReadiness.READY)  # a due retry, judged against db_now
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE node_runs SET retry_at = now() + interval '1 hour' WHERE node_key = 'a'"))
        snap = self._snapshot(run_id)
        verdict = evaluate_readiness("a", snap.definition, str(run_id), snap.run_store(), snap.clock(), new_catalog())
        self.assertIs(verdict, NodeReadiness.WAITING)

    # -- read-only: no write, no lock, no lease renewal ----------------------------------

    def test_a_snapshot_writes_nothing_and_renews_no_lease(self) -> None:
        claimed = self.start_run("w1", workflow_version_id=self.version_id)
        self.expire_lease(claimed.run_id)
        with self.engine.begin() as conn:
            make_node_run(conn, as_id(claimed.run_id), "a", status="running", started_epoch=1)
        before = self._tables()
        self._snapshot(claimed.run_id)
        self.assertEqual(self._tables(), before)  # visible_at is still expired: nothing renewed it

    def test_a_snapshot_does_not_wait_for_a_row_lock_held_by_the_lease_holder(self) -> None:
        run_id = self._run_with_nodes()
        holder = self.engine.connect()
        self.addCleanup(holder.close)
        holder.execute(text("SELECT id FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id})
        holder.execute(text("SELECT 1 FROM node_runs FOR UPDATE"))
        results, errors = [], []

        def read() -> None:
            try:
                results.append(self._snapshot(run_id))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "a read-only snapshot must never block behind a writer")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        holder.rollback()

    def test_the_isolation_level_does_not_leak_to_later_work_on_the_connection(self) -> None:
        run_id = self._run_with_nodes()
        with self.engine.connect() as conn:
            load_run_snapshot(conn, str(run_id))
            self.assertEqual(conn.execute(text("SHOW transaction_isolation")).scalar_one(), "read committed")
            self.assertEqual(conn.execute(text("SHOW transaction_read_only")).scalar_one(), "off")
            conn.rollback()

    # -- one consistent snapshot -----------------------------------------------------------

    def _interleave_a_commit_after_the_run_read(self, run_id) -> None:
        """Fire a concurrent writer commit right after the run row has been read."""
        fired = []

        def writer(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            if statement.lstrip().startswith("SELECT clock_timestamp()") and not fired:
                fired.append(True)
                with self.engine.begin() as other:  # a different connection, committed immediately
                    make_node_run(other, run_id, "a", status="succeeded", started_epoch=1, attempt=1, output={"main": []})

        event.listen(self.engine, "after_cursor_execute", writer)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", writer)
        self._fired = fired

    def test_a_commit_between_the_reads_is_not_seen_by_the_snapshot(self) -> None:
        run_id = self.add_run(workflow_version_id=self.version_id)
        self._interleave_a_commit_after_the_run_read(run_id)
        snap = self._snapshot(run_id)
        self.assertEqual(self._fired, [True], "the interleaving never happened: the test would be vacuous")
        self.assertEqual(snap.node_runs, ())  # one snapshot: the later commit is invisible
        with self.engine.connect() as conn:  # ... though the row really was committed
            self.assertEqual(conn.execute(text("SELECT count(*) FROM node_runs")).scalar_one(), 1)

    def test_the_same_interleaving_does_tear_a_read_committed_read(self) -> None:
        # Control: proves the test above can detect a torn read (it would, without REPEATABLE READ).
        run_id = self.add_run(workflow_version_id=self.version_id)
        self._interleave_a_commit_after_the_run_read(run_id)
        with self.engine.connect() as conn, conn.begin():
            conn.execute(text("SELECT clock_timestamp()")).one()  # the "run read": the writer commits after it
            rows = conn.execute(text("SELECT node_key FROM node_runs")).all()
        self.assertEqual(self._fired, [True])
        self.assertEqual([r[0] for r in rows], ["a"])  # READ COMMITTED sees the interleaved commit

    # -- failures ----------------------------------------------------------------------------

    def test_an_unknown_run_is_a_lookup_error_and_leaves_the_connection_clean(self) -> None:
        with self.engine.connect() as conn:
            with self.assertRaises(LookupError):
                load_run_snapshot(conn, str(uuid.uuid7()))
            self.assertFalse(conn.in_transaction())
            with self.assertRaises(ValueError):
                load_run_snapshot(conn, "not-a-uuid")

    def test_requires_a_clean_connection(self) -> None:
        run_id = self._run_with_nodes()
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            with self.assertRaisesRegex(RuntimeError, "clean connection"):
                load_run_snapshot(conn, str(run_id))

    def test_stored_data_the_domain_rejects_is_a_codec_error(self) -> None:
        with self.engine.begin() as conn:
            bad_version = make_version(conn, self.workflow_id, version_number=3, definition={"nodes": [{"key": "a", "type": "t", "type_version": 1, "bogus": 1}]})
        run_id = self.add_run(workflow_version_id=bad_version)
        with self.engine.connect() as conn:
            with self.assertRaisesRegex(CodecError, "unknown field"):
                load_run_snapshot(conn, str(run_id))
            self.assertFalse(conn.in_transaction())
        run_ok = self.add_run(workflow_version_id=self.version_id)
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO node_runs (run_id, node_key, status, effect_class, outcome) VALUES (:r, 'x', 'running', 'pure', 'known')"), {"r": run_ok})
            conn.execute(text("UPDATE node_runs SET error = CAST('{\"message\": \"no code\"}' AS jsonb) WHERE node_key = 'x'"))
        with self.engine.connect() as conn:
            with self.assertRaisesRegex(CodecError, "error.code"):
                load_run_snapshot(conn, str(run_ok))

    # -- the isolation guarantee is verified, not assumed -------------------------------

    def _engine_with(self, isolation_level: str):
        engine = create_engine(self._temp_db.url, isolation_level=isolation_level)
        self.addCleanup(engine.dispose)
        return engine

    def test_an_autocommit_connection_is_refused_before_any_data_is_read(self) -> None:
        # SET TRANSACTION is only a warning outside a transaction block: without the check every
        # statement would run in its own snapshot with no read-only protection.
        run_id = self._run_with_nodes()
        engine = self._engine_with("AUTOCOMMIT")
        statements: list[str] = []
        event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: statements.append(stmt))
        with engine.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "AUTOCOMMIT"):
                load_run_snapshot(conn, str(run_id))
            self.assertFalse(conn.in_transaction())
            self.assertEqual(conn.execute(text("SELECT 1")).scalar_one(), 1)  # the connection is still usable
        self.assertFalse([s for s in statements if "FROM runs" in s or "FROM node_runs" in s], statements)

    def test_every_transactional_engine_level_gets_an_effective_repeatable_read_read_only_snapshot(self) -> None:
        run_id = self._run_with_nodes()
        for level in ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"):
            with self.subTest(level=level):
                engine = self._engine_with(level)
                seen: list[tuple[str, str]] = []

                def spy(conn, cursor, statement, parameters, context, executemany, seen=seen):  # noqa: ANN001
                    if "current_setting('transaction_isolation')" in statement:
                        with cursor.connection.cursor() as probe:  # a second cursor: never disturb the one observed
                            probe.execute("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')")
                            seen.append(tuple(probe.fetchone()))

                event.listen(engine, "after_cursor_execute", spy)
                with engine.connect() as conn:
                    snapshot = load_run_snapshot(conn, str(run_id))
                    self.assertEqual([n.node_key for n in snapshot.node_runs], ["a", "trigger"])
                    # nothing leaked to later work on the same connection
                    self.assertEqual(conn.execute(text("SHOW transaction_isolation")).scalar_one(), level.lower())
                    self.assertEqual(conn.execute(text("SHOW transaction_read_only")).scalar_one(), "off")
                    conn.rollback()
                self.assertEqual(seen, [("repeatable read", "on")])

    def test_the_refusal_names_what_it_found(self) -> None:
        engine = self._engine_with("AUTOCOMMIT")
        run_id = self._run_with_nodes()
        with engine.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, r"isolation='read committed', read_only='off'"):
                load_run_snapshot(conn, str(run_id))

    def test_a_large_run_is_read_completely(self) -> None:
        run_id = self.add_run(workflow_version_id=self.version_id)
        with self.engine.begin() as conn:
            for n in range(60):
                make_node_run(conn, run_id, f"n{n:03d}", status="succeeded", started_epoch=1, attempt=1)
        snap = self._snapshot(run_id)
        self.assertEqual([r.node_key for r in snap.node_runs], [f"n{n:03d}" for n in range(60)])
        self.assertEqual(snap.run_store().list_for_run(str(run_id))[0].node_key, "n000")
