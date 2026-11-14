"""The migrated schema matches docs/m2-persistence-design.md section 2.

Structure is read back from the PostgreSQL catalogs, so these tests fail if the
migration drifts from the approved design. CHECK semantics are covered
behaviorally in test_schema_constraints.py, since ``pg_get_constraintdef``
renders them in a form that is not worth string-matching.
"""

from __future__ import annotations

from sqlalchemy import text

from tests.integration.support import FIVE_TABLES, DatabaseTestCase, public_tables

TS = "timestamp with time zone"

#: table -> column -> (data_type, nullable)   [design section 2]
EXPECTED_COLUMNS = {
    "workflows": {
        "id": ("uuid", False),
        "name": ("text", False),
        "active_version_id": ("uuid", True),
        "webhook_enabled": ("boolean", False),
        "created_at": (TS, False),
    },
    "workflow_versions": {
        "id": ("uuid", False),
        "workflow_id": ("uuid", False),
        "version_number": ("integer", False),
        "schema_version": ("integer", False),
        "definition": ("jsonb", False),
        "created_at": (TS, False),
    },
    "runs": {
        "id": ("uuid", False),
        "workflow_version_id": ("uuid", False),
        "status": ("text", False),
        "visible_at": (TS, False),
        "lease_owner": ("text", True),
        "lease_epoch": ("bigint", False),
        "recovery_count": ("integer", False),
        "cancel_requested_at": (TS, True),
        "queue_deadline_at": (TS, False),
        "deadline_at": (TS, True),
        "started_at": (TS, True),
        "finished_at": (TS, True),
        "error": ("jsonb", True),
        "has_unknown_outcome": ("boolean", False),
        "trigger_payload": ("jsonb", False),
        "idempotency_key": ("text", True),
        "created_at": (TS, False),
    },
    "node_runs": {
        "run_id": ("uuid", False),
        "node_key": ("text", False),
        "status": ("text", False),
        "attempt": ("integer", False),
        "failures": ("integer", False),
        "outcome": ("text", False),
        "effect_class": ("text", True),
        "started_epoch": ("bigint", True),
        "started_at": (TS, True),
        "retry_at": (TS, True),
        "resolved_params": ("jsonb", True),
        "output": ("jsonb", True),
        "error": ("jsonb", True),
        "item_results": ("jsonb", True),
    },
    "run_events": {
        "run_id": ("uuid", False),
        "seq": ("bigint", False),
        "level": ("text", False),
        "node_key": ("text", True),
        "message": ("text", False),
        "ts": (TS, False),
    },
}

#: Every column that has a DEFAULT, and exactly that default. Notably absent:
#: every ``id`` -- ids are application-generated UUIDv7 (design D1).
EXPECTED_DEFAULTS = {
    ("workflows", "webhook_enabled"): "false",
    ("workflows", "created_at"): "now()",
    ("workflow_versions", "created_at"): "now()",
    ("runs", "lease_epoch"): "0",
    ("runs", "recovery_count"): "0",
    ("runs", "has_unknown_outcome"): "false",
    ("runs", "created_at"): "now()",
    ("node_runs", "attempt"): "1",
    ("node_runs", "failures"): "0",
    ("node_runs", "outcome"): "'known'::text",
    ("run_events", "ts"): "now()",
}

EXPECTED_PRIMARY_KEYS = {
    "workflows": ["id"],
    "workflow_versions": ["id"],
    "runs": ["id"],
    "node_runs": ["run_id", "node_key"],
    "run_events": ["run_id", "seq"],
}

#: (table, columns, referenced table, referenced columns)
EXPECTED_FOREIGN_KEYS = {
    ("workflow_versions", ("workflow_id",), "workflows", ("id",)),
    ("workflows", ("id", "active_version_id"), "workflow_versions", ("workflow_id", "id")),
    ("runs", ("workflow_version_id",), "workflow_versions", ("id",)),
    ("node_runs", ("run_id",), "runs", ("id",)),
    ("run_events", ("run_id",), "runs", ("id",)),
}

#: (table, columns) of the UNIQUE constraints
EXPECTED_UNIQUES = {
    ("workflow_versions", ("workflow_id", "version_number")),
    ("workflow_versions", ("workflow_id", "id")),
    ("runs", ("workflow_version_id", "idempotency_key")),
}

EXPECTED_NAMED_CHECKS = {
    ("runs", "lease_owner_matches_status"),
    ("node_runs", "retry_wait_requires_retry_at"),
}


class SchemaStructureTestCase(DatabaseTestCase):
    def _rows(self, sql: str, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).all()

    def test_exactly_the_five_m2_1_tables_exist(self) -> None:
        with self.engine.connect() as connection:
            tables = public_tables(connection)
        # alembic_version is Alembic's own bookkeeping table. Everything the
        # design defers (admin_user, sessions, credentials, webhook_endpoints,
        # app_settings) and any separate queue table must be absent.
        self.assertEqual(tables, FIVE_TABLES | {"alembic_version"})

    def test_columns_types_and_nullability_match_the_design(self) -> None:
        rows = self._rows(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(:tables)",
            tables=sorted(FIVE_TABLES),
        )
        actual: dict[str, dict[str, tuple[str, bool]]] = {}
        for table, column, data_type, nullable in rows:
            actual.setdefault(table, {})[column] = (data_type, nullable == "YES")
        self.assertEqual(actual, EXPECTED_COLUMNS)

    def test_defaults_match_the_design_and_ids_have_none(self) -> None:
        rows = self._rows(
            "SELECT table_name, column_name, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(:tables) AND column_default IS NOT NULL",
            tables=sorted(FIVE_TABLES),
        )
        self.assertEqual({(t, c): d for t, c, d in rows}, EXPECTED_DEFAULTS)
        # Design D1: the ids are generated by the application, never by the database.
        for table in ("workflows", "workflow_versions", "runs"):
            self.assertNotIn((table, "id"), {(t, c) for t, c, _ in rows})

    def test_primary_keys(self) -> None:
        rows = self._rows(
            "SELECT CAST(CAST(c.conrelid AS regclass) AS text), array_agg(a.attname ORDER BY k.ord) "
            "FROM pg_constraint c "
            "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.contype = 'p' AND c.connamespace = CAST('public' AS regnamespace) "
            "GROUP BY c.conrelid"
        )
        self.assertEqual({t: cols for t, cols in rows if t in FIVE_TABLES}, EXPECTED_PRIMARY_KEYS)

    def test_foreign_keys_including_the_composite_active_version_key(self) -> None:
        rows = self._rows(
            "SELECT CAST(CAST(c.conrelid AS regclass) AS text), "
            "  (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum), "
            "  CAST(CAST(c.confrelid AS regclass) AS text), "
            "  (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
            "     JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum) "
            "FROM pg_constraint c WHERE c.contype = 'f' AND c.connamespace = CAST('public' AS regnamespace)"
        )
        actual = {(t, tuple(cols), rt, tuple(rcols)) for t, cols, rt, rcols in rows}
        self.assertEqual(actual, EXPECTED_FOREIGN_KEYS)
        names = {n for (n,) in self._rows("SELECT conname FROM pg_constraint WHERE contype = 'f'")}
        self.assertIn("fk_active_version_belongs_to_workflow", names)

    def test_unique_constraints(self) -> None:
        rows = self._rows(
            "SELECT CAST(CAST(c.conrelid AS regclass) AS text), array_agg(a.attname ORDER BY k.ord) "
            "FROM pg_constraint c "
            "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.contype = 'u' AND c.connamespace = CAST('public' AS regnamespace) GROUP BY c.oid, c.conrelid"
        )
        self.assertEqual({(t, tuple(cols)) for t, cols in rows}, EXPECTED_UNIQUES)

    def test_named_check_constraints_exist(self) -> None:
        rows = self._rows(
            "SELECT CAST(CAST(conrelid AS regclass) AS text), conname FROM pg_constraint "
            "WHERE contype = 'c' AND connamespace = CAST('public' AS regnamespace)"
        )
        self.assertTrue(EXPECTED_NAMED_CHECKS <= {(t, n) for t, n in rows})

    def test_runs_claimable_index_is_partial_on_the_three_active_statuses(self) -> None:
        (definition,) = self._rows(
            "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'runs_claimable_idx'"
        )[0]
        self.assertIn("ON public.runs USING btree (visible_at)", definition)
        self.assertIn("WHERE", definition)
        for status in ("queued", "waiting", "running"):
            self.assertIn(f"'{status}'", definition)
        for status in ("succeeded", "failed", "cancelled", "timed_out"):
            self.assertNotIn(f"'{status}'", definition)

    def test_effect_class_immutability_trigger_is_before_update_per_row(self) -> None:
        rows = self._rows(
            "SELECT event_manipulation, action_timing, action_orientation FROM information_schema.triggers "
            "WHERE event_object_table = 'node_runs' AND trigger_name = 'node_runs_effect_class_immutable'"
        )
        self.assertEqual(rows, [("UPDATE", "BEFORE", "ROW")])
