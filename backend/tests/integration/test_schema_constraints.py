"""Database constraints enforce the invariants of docs/m2-persistence-design.md.

Each test inserts rows through SQLAlchemy Core and asserts the PostgreSQL
SQLSTATE (and constraint name where one is specified) of the violation, so the
invariants are proven to live in the database and not in application code.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from flowforge.db.ids import new_id
from tests.integration.support import (
    CHECK_VIOLATION,
    FOREIGN_KEY_VIOLATION,
    NOT_NULL_VIOLATION,
    RAISE_EXCEPTION,
    UNIQUE_VIOLATION,
    DatabaseTestCase,
    Sql,
    assert_db_error,
    insert,
    make_event,
    make_node_run,
    make_run,
    make_run_tree,
    make_version,
    make_workflow,
)


class ApplicationGeneratedIdTestCase(DatabaseTestCase):
    """Design D1: ids come from the application; the database never invents one."""

    def test_inserting_without_an_id_is_rejected_for_every_id_table(self) -> None:
        def omit_workflow_id() -> None:
            with self.engine.begin() as c:
                insert(c, "workflows", name="no-id")

        assert_db_error(self, NOT_NULL_VIOLATION, omit_workflow_id)

        with self.engine.begin() as c:
            workflow_id = make_workflow(c)

        def omit_version_id() -> None:
            with self.engine.begin() as c:
                insert(c, "workflow_versions", workflow_id=workflow_id, version_number=1, schema_version=1, definition={})

        assert_db_error(self, NOT_NULL_VIOLATION, omit_version_id)

        with self.engine.begin() as c:
            version_id = make_version(c, workflow_id)

        def omit_run_id() -> None:
            with self.engine.begin() as c:
                insert(
                    c, "runs", workflow_version_id=version_id, status="queued", visible_at=Sql("now()"),
                    queue_deadline_at=Sql("now()"), trigger_payload={},
                )

        assert_db_error(self, NOT_NULL_VIOLATION, omit_run_id)

    def test_uuidv7_ids_round_trip(self) -> None:
        with self.engine.begin() as c:
            _, _, run_id = make_run_tree(c)
        with self.engine.connect() as c:
            stored = c.execute(text("SELECT id FROM runs")).scalar_one()
        self.assertEqual(stored, run_id)
        self.assertEqual(stored.version, 7)


class ActiveVersionIntegrityTestCase(DatabaseTestCase):
    def test_active_version_must_belong_to_the_same_workflow(self) -> None:
        with self.engine.begin() as c:
            wf_a = make_workflow(c, name="a")
            wf_b = make_workflow(c, name="b")
            version_of_b = make_version(c, wf_b)

        def activate_foreign_version() -> None:
            with self.engine.begin() as c:
                c.execute(
                    text("UPDATE workflows SET active_version_id = :v WHERE id = :w"),
                    {"v": version_of_b, "w": wf_a},
                )

        assert_db_error(
            self, FOREIGN_KEY_VIOLATION, activate_foreign_version, "fk_active_version_belongs_to_workflow"
        )

    def test_own_version_and_null_are_accepted(self) -> None:
        with self.engine.begin() as c:
            wf = make_workflow(c)  # NULL active_version_id: a brand-new workflow
            version = make_version(c, wf)
            c.execute(text("UPDATE workflows SET active_version_id = :v WHERE id = :w"), {"v": version, "w": wf})
        with self.engine.connect() as c:
            self.assertEqual(
                c.execute(text("SELECT active_version_id FROM workflows WHERE id = :w"), {"w": wf}).scalar_one(),
                version,
            )

    def test_a_referenced_active_version_cannot_be_deleted(self) -> None:
        with self.engine.begin() as c:
            wf = make_workflow(c)
            version = make_version(c, wf)
            c.execute(text("UPDATE workflows SET active_version_id = :v WHERE id = :w"), {"v": version, "w": wf})

        def delete_active_version() -> None:
            with self.engine.begin() as c:
                c.execute(text("DELETE FROM workflow_versions WHERE id = :v"), {"v": version})

        assert_db_error(self, FOREIGN_KEY_VIOLATION, delete_active_version)

    def test_version_numbers_are_unique_per_workflow(self) -> None:
        with self.engine.begin() as c:
            wf = make_workflow(c)
            make_version(c, wf, version_number=1)
            other = make_workflow(c, name="other")
            make_version(c, other, version_number=1)  # same number, different workflow: fine

        def duplicate() -> None:
            with self.engine.begin() as c:
                make_version(c, wf, version_number=1)

        assert_db_error(self, UNIQUE_VIOLATION, duplicate)


class RunsConstraintTestCase(DatabaseTestCase):
    def test_status_check(self) -> None:
        for status in ("queued", "waiting", "succeeded", "failed", "cancelled", "timed_out"):
            with self.subTest(status=status), self.engine.begin() as c:
                make_run_tree(c, status=status)
        with self.engine.begin() as c:
            make_run_tree(c, status="running", lease_owner="w1")

        def bogus() -> None:
            with self.engine.begin() as c:
                make_run_tree(c, status="bogus")

        assert_db_error(self, CHECK_VIOLATION, bogus)

    def test_lease_owner_matches_status_in_both_directions(self) -> None:
        def running_without_owner() -> None:
            with self.engine.begin() as c:
                make_run_tree(c, status="running")

        def queued_with_owner() -> None:
            with self.engine.begin() as c:
                make_run_tree(c, status="queued", lease_owner="w1")

        def terminal_with_owner() -> None:
            with self.engine.begin() as c:
                make_run_tree(c, status="succeeded", lease_owner="w1")

        for action in (running_without_owner, queued_with_owner, terminal_with_owner):
            with self.subTest(action=action.__name__):
                assert_db_error(self, CHECK_VIOLATION, action, "lease_owner_matches_status")

    def test_lease_epoch_must_not_be_negative(self) -> None:
        with self.engine.begin() as c:
            make_run_tree(c, lease_epoch=0)

        def negative() -> None:
            with self.engine.begin() as c:
                make_run_tree(c, lease_epoch=-1)

        assert_db_error(self, CHECK_VIOLATION, negative)

    def test_defaults(self) -> None:
        with self.engine.begin() as c:
            make_run_tree(c)
        with self.engine.connect() as c:
            row = c.execute(text("SELECT lease_epoch, recovery_count, has_unknown_outcome, lease_owner FROM runs")).one()
        self.assertEqual(tuple(row), (0, 0, False, None))

    def test_required_columns_are_not_null(self) -> None:
        for column in ("visible_at", "queue_deadline_at", "trigger_payload", "status"):
            with self.subTest(column=column):

                def omit() -> None:
                    with self.engine.begin() as c:
                        wf = make_workflow(c)
                        version = make_version(c, wf)
                        row = {
                            "id": new_id(), "workflow_version_id": version, "status": "queued",
                            "visible_at": Sql("now()"), "queue_deadline_at": Sql("now()"), "trigger_payload": {},
                        }
                        del row[column]
                        insert(c, "runs", **row)

                assert_db_error(self, NOT_NULL_VIOLATION, omit)

    def test_idempotency_key_is_unique_per_workflow_version_but_null_is_free(self) -> None:
        with self.engine.begin() as c:
            wf = make_workflow(c)
            v1 = make_version(c, wf, version_number=1)
            v2 = make_version(c, wf, version_number=2)
            make_run(c, v1, idempotency_key="k")
            make_run(c, v2, idempotency_key="k")  # same key, other version: fine
            make_run(c, v1)
            make_run(c, v1)  # NULL keys never collide

        def duplicate() -> None:
            with self.engine.begin() as c:
                make_run(c, v1, idempotency_key="k")

        assert_db_error(self, UNIQUE_VIOLATION, duplicate)

    def test_a_run_requires_an_existing_workflow_version(self) -> None:
        def orphan() -> None:
            with self.engine.begin() as c:
                make_run(c, new_id())

        assert_db_error(self, FOREIGN_KEY_VIOLATION, orphan)

    def test_runs_is_the_queue_so_terminal_rows_are_ordinary_rows(self) -> None:
        # There is no separate queue table and no reaper: a queued run is simply
        # a runs row, claimable through the partial index (see structure test).
        with self.engine.begin() as c:
            make_run_tree(c, status="queued")
            make_run_tree(c, status="succeeded")
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM runs")).scalar_one(), 2)


class NodeRunsConstraintTestCase(DatabaseTestCase):
    def _run_id(self) -> uuid.UUID:
        with self.engine.begin() as c:
            return make_run_tree(c)[2]

    def test_status_effect_class_and_outcome_checks(self) -> None:
        run_id = self._run_id()
        cases = {
            "status": {"status": "bogus"},
            "effect_class": {"effect_class": "bogus"},
            "outcome": {"outcome": "bogus"},
        }
        for name, overrides in cases.items():
            with self.subTest(column=name):

                def bad(overrides=overrides) -> None:
                    with self.engine.begin() as c:
                        make_node_run(c, run_id, "bad", **overrides)

                assert_db_error(self, CHECK_VIOLATION, bad)

    def test_every_legal_status_and_effect_class_is_accepted(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            for i, status in enumerate(("running", "succeeded", "failed", "cancelled")):
                make_node_run(c, run_id, f"s{i}", status=status)
            make_node_run(c, run_id, "retry", status="retry_wait", retry_at=Sql("now()"))
            for i, effect in enumerate(("pure", "read", "idempotent_write", "unsafe_write")):
                make_node_run(c, run_id, f"e{i}", effect_class=effect)
            make_node_run(c, run_id, "unknown", outcome="unknown")

    def test_only_skipped_rows_may_lack_an_effect_class(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            make_node_run(c, run_id, "skipped", status="skipped", effect_class=None)

        def unclassified_running() -> None:
            with self.engine.begin() as c:
                make_node_run(c, run_id, "x", status="running", effect_class=None)

        assert_db_error(self, CHECK_VIOLATION, unclassified_running)

    def test_retry_wait_requires_retry_at(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            make_node_run(c, run_id, "ok", status="retry_wait", retry_at=Sql("now() + interval '5 seconds'"))

        def missing_retry_at() -> None:
            with self.engine.begin() as c:
                make_node_run(c, run_id, "bad", status="retry_wait")

        assert_db_error(self, CHECK_VIOLATION, missing_retry_at, "retry_wait_requires_retry_at")

    def test_primary_key_is_run_and_node(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            make_node_run(c, run_id, "n1")
            make_node_run(c, run_id, "n2")

        def duplicate() -> None:
            with self.engine.begin() as c:
                make_node_run(c, run_id, "n1")

        assert_db_error(self, UNIQUE_VIOLATION, duplicate)

    def test_a_node_run_requires_an_existing_run(self) -> None:
        def orphan() -> None:
            with self.engine.begin() as c:
                make_node_run(c, new_id())

        assert_db_error(self, FOREIGN_KEY_VIOLATION, orphan)

    def test_defaults(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            make_node_run(c, run_id)
        with self.engine.connect() as c:
            row = c.execute(text("SELECT attempt, failures, outcome FROM node_runs")).one()
        self.assertEqual(tuple(row), (1, 0, "known"))


class EffectClassImmutabilityTestCase(DatabaseTestCase):
    """Architecture section 5.2: enforced by a database trigger."""

    def _node(self, effect_class: str | None = "unsafe_write", status: str = "running") -> uuid.UUID:
        with self.engine.begin() as c:
            run_id = make_run_tree(c)[2]
            make_node_run(c, run_id, "n1", effect_class=effect_class, status=status)
        return run_id

    def _update(self, run_id: uuid.UUID, assignment: str) -> None:
        with self.engine.begin() as c:
            c.execute(text(f"UPDATE node_runs SET {assignment} WHERE run_id = :r"), {"r": run_id})

    def test_changing_the_effect_class_is_rejected_by_the_trigger(self) -> None:
        run_id = self._node("unsafe_write")
        for new_class in ("pure", "read", "idempotent_write"):
            with self.subTest(new_class=new_class):
                assert_db_error(
                    self, RAISE_EXCEPTION, lambda c=new_class: self._update(run_id, f"effect_class = '{c}'")
                )
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT effect_class FROM node_runs")).scalar_one(), "unsafe_write")

    def test_clearing_or_assigning_a_class_is_also_a_change(self) -> None:
        run_id = self._node("pure")
        assert_db_error(self, RAISE_EXCEPTION, lambda: self._update(run_id, "effect_class = NULL"))
        skipped = self._node(None, status="skipped")
        assert_db_error(self, RAISE_EXCEPTION, lambda: self._update(skipped, "effect_class = 'pure'"))

    def test_the_error_message_names_the_row_and_both_classes(self) -> None:
        run_id = self._node("read")
        with self.assertRaises(Exception) as caught:
            self._update(run_id, "effect_class = 'pure'")
        message = str(caught.exception.orig)
        self.assertIn("effect_class is immutable", message)
        self.assertIn("n1", message)
        self.assertIn("read -> pure", message)

    def test_unrelated_updates_and_same_value_writes_are_allowed(self) -> None:
        run_id = self._node("idempotent_write")
        self._update(run_id, "effect_class = 'idempotent_write'")  # unchanged value
        self._update(run_id, "status = 'retry_wait', retry_at = now(), failures = failures + 1, attempt = attempt + 1")
        self._update(run_id, "status = 'running', retry_at = NULL, outcome = 'unknown'")
        with self.engine.connect() as c:
            row = c.execute(text("SELECT status, effect_class, attempt, failures, outcome FROM node_runs")).one()
        self.assertEqual(tuple(row), ("running", "idempotent_write", 2, 1, "unknown"))


class RunEventsConstraintTestCase(DatabaseTestCase):
    def _run_id(self) -> uuid.UUID:
        with self.engine.begin() as c:
            return make_run_tree(c)[2]

    def test_primary_key_is_run_and_seq(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            make_event(c, run_id, 1)
            make_event(c, run_id, 2)

        def duplicate_seq() -> None:
            with self.engine.begin() as c:
                make_event(c, run_id, 1)

        assert_db_error(self, UNIQUE_VIOLATION, duplicate_seq)

    def test_seq_may_repeat_across_runs(self) -> None:
        with self.engine.begin() as c:
            first = make_run_tree(c)[2]
            second = make_run_tree(c)[2]
            make_event(c, first, 1)
            make_event(c, second, 1)

    def test_level_check_and_not_null_columns(self) -> None:
        run_id = self._run_id()
        with self.engine.begin() as c:
            for i, level in enumerate(("debug", "info", "warning", "error"), start=1):
                make_event(c, run_id, i, level=level)

        def bad_level() -> None:
            with self.engine.begin() as c:
                make_event(c, run_id, 99, level="fatal")

        def no_message() -> None:
            with self.engine.begin() as c:
                insert(c, "run_events", run_id=run_id, seq=100, level="info")

        assert_db_error(self, CHECK_VIOLATION, bad_level)
        assert_db_error(self, NOT_NULL_VIOLATION, no_message)

    def test_an_event_requires_an_existing_run(self) -> None:
        def orphan() -> None:
            with self.engine.begin() as c:
                make_event(c, new_id())

        assert_db_error(self, FOREIGN_KEY_VIOLATION, orphan)
