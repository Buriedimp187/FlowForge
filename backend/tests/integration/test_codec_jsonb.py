"""The codec against real PostgreSQL 17 jsonb: what is storable, and what comes back.

Characterizes PostgreSQL behavior the codec's rules are built on (documented in
``flowforge.worker.codec``), and round-trips real rows through ``node_run_from_row``.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from flowforge.db.engine import create_db_engine
from flowforge.domain.effects import EffectClass
from flowforge.domain.items import items_from
from flowforge.domain.node_run import NodeError, NodeRun
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.worker import codec
from flowforge.worker.codec import CodecError
from flowforge.worker.snapshot import load_run_snapshot
from tests.integration.support import Sql, make_node_run
from tests.integration.worker_support import WorkerDatabaseTestCase, as_id


class JsonbBehaviorTestCase(WorkerDatabaseTestCase):
    def _round_trip(self, value):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT CAST(:p AS jsonb)"), {"p": codec.dumps_jsonb(value)}).scalar_one()

    # -- why the codec refuses NUL and lone surrogates ---------------------------------------

    def test_postgresql_rejects_a_nul_character_in_jsonb(self) -> None:
        raw = json.dumps({"v": "a\x00b"})  # valid JSON, bypassing the codec
        with self.engine.connect() as conn:
            with self.assertRaises(DBAPIError) as caught:
                conn.execute(text("SELECT CAST(:p AS jsonb)"), {"p": raw})
        self.assertEqual(caught.exception.orig.sqlstate, "22P05")  # untranslatable_character

    def test_the_driver_cannot_even_encode_a_lone_surrogate(self) -> None:
        raw = json.dumps({"v": "a\ud800b"}, ensure_ascii=False)
        with self.engine.connect() as conn:
            with self.assertRaises(UnicodeEncodeError):  # not a database error: SQLAlchemy cannot wrap it
                conn.execute(text("SELECT CAST(:p AS jsonb)"), {"p": raw})

    def test_the_codec_refuses_both_before_a_statement_is_sent(self) -> None:
        for bad in ("a\x00b", "a\ud800b"):
            with self.assertRaises(CodecError):
                codec.dumps_jsonb({"v": bad})

    def test_everything_else_in_the_unicode_range_is_stored_and_read_back_exactly(self) -> None:
        value = {
            "astral": "a😀b", "controls": "\x01\x02\x1f\x7f", "separators": "  ﻿",
            "replacement": "�", "literal escape text": "\\u0000", "quote": 'say "hi"\n\ttab',
            "": "empty key", "日本語": ["Zoë ✓"],
        }  # fmt: skip
        self.assertEqual(self._round_trip(value), value)

    # -- the jsonb number contract (documented in the codec) -----------------------------------

    def test_floats_below_1e16_and_ordinary_decimals_round_trip_as_the_same_float(self) -> None:
        for value in (0.0, 1.0, 2.5, 0.1 + 0.2, 1e-7, 5e-324, 1e15, 123456789012345.0, 1.7976931348623157e-300):
            with self.subTest(value=value):
                got = self._round_trip([value])[0]
                self.assertIsInstance(got, float)
                self.assertEqual(got, value)

    def test_a_float_whose_json_text_has_a_positive_exponent_comes_back_as_an_int(self) -> None:
        # PostgreSQL stores jsonb numbers as exact DECIMALS and prints them without an exponent, so
        # the Python value read back is the int spelled by the JSON text. That int equals the original
        # float when the float is exactly that integer (1e16, 1e22, 1.2345678901234568e17) ...
        for value in (1e16, 1e22, 1.2345678901234568e17):
            with self.subTest(value=value):
                got = self._round_trip([value])[0]
                self.assertIs(type(got), int)
                self.assertEqual(got, value)

    def test_a_huge_float_comes_back_as_the_exact_decimal_not_the_binary_double(self) -> None:
        # ... but not otherwise: the double nearest 1.5e300 is not exactly 15 * 10**299.
        got = self._round_trip([1.5e300])[0]
        self.assertIs(type(got), int)
        self.assertEqual(got, 15 * 10**299)
        self.assertNotEqual(got, 1.5e300)

    def test_negative_zero_loses_its_sign(self) -> None:
        got = self._round_trip([-0.0])[0]
        self.assertEqual(got, 0.0)
        self.assertEqual(repr(got), "0.0")

    def test_integers_of_any_practical_size_are_exact_and_stay_integers(self) -> None:
        for value in (0, -1, 2**53 + 1, 2**63, 2**70, -(2**200), 10**300):
            with self.subTest(value=value):
                got = self._round_trip([value])[0]
                self.assertIs(type(got), int)
                self.assertEqual(got, value)

    def test_integers_beyond_the_pythons_string_limit_are_refused_by_the_codec(self) -> None:
        with self.assertRaises(ValueError):
            codec.dumps_jsonb({"v": 10**5000})

    def test_booleans_and_nulls_keep_their_types(self) -> None:
        got = self._round_trip({"t": True, "f": False, "n": None, "z": 0, "e": [], "o": {}})
        self.assertEqual(got, {"t": True, "f": False, "n": None, "z": 0, "e": [], "o": {}})
        self.assertIs(got["t"], True)
        self.assertIsNot(got["z"], False)

    def test_equivalent_values_produce_identical_text_and_the_database_agrees(self) -> None:
        a, b = {"x": 1, "y": {"b": [1, 2], "a": None}}, {"y": {"a": None, "b": [1, 2]}, "x": 1}
        self.assertEqual(codec.dumps_jsonb(a), codec.dumps_jsonb(b))
        self.assertEqual(self._round_trip(a), self._round_trip(b))


class NodeRowRoundTripTestCase(WorkerDatabaseTestCase):
    """Real node_runs rows through ``node_run_from_row`` (the Row mapping the loop will use)."""

    def _stored(self, run_id, node_key):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {', '.join(codec.NODE_RUN_COLUMNS)} FROM node_runs WHERE run_id = :id AND node_key = :k"),
                {"id": as_id(run_id), "k": node_key},
            ).mappings().one()
        return codec.node_run_from_row(row)

    def test_a_row_written_from_a_domain_object_reads_back_equal(self) -> None:
        run_id = self.add_run()
        original = NodeRun(
            run_id=str(run_id), node_key="fetch", status=NodeRunStatus.FAILED, attempt=3, failures=2,
            effect_class=EffectClass.IDEMPOTENT_WRITE, outcome=Outcome.UNKNOWN, started_epoch=4,
            output={"main": items_from({"name": "Zoë ✓", "n": [1, 2.5, None, True], "deep": {"a": {}}}), "error": ()},
            error=NodeError("outcome_unknown", "dispatch cannot be disproven", "timeout"),
        )  # fmt: skip
        row = codec.node_run_to_row(original)
        with self.engine.begin() as conn:
            make_node_run(
                conn, run_id, "fetch", status=row["status"], attempt=row["attempt"], failures=row["failures"],
                outcome=row["outcome"], effect_class=row["effect_class"], started_epoch=row["started_epoch"],
                output=row["output"], error=row["error"],
            )  # fmt: skip
        self.assertEqual(self._stored(run_id, "fetch"), original)

    def test_a_database_computed_retry_at_converts_to_the_same_instant(self) -> None:
        run_id = self.add_run()
        with self.engine.begin() as conn:
            make_node_run(conn, run_id, "n1", status="retry_wait", started_epoch=1, attempt=2, failures=1, retry_at=Sql("now()"))
            conn.execute(
                text("UPDATE node_runs SET retry_at = now() + CAST(:d AS double precision) * interval '1 second' WHERE node_key = 'n1'"),
                {"d": 2.5},
            )
            expected = conn.execute(text("SELECT extract(epoch FROM retry_at) FROM node_runs WHERE node_key = 'n1'")).scalar_one()
        node = self._stored(run_id, "n1")
        self.assertAlmostEqual(node.retry_at, float(expected), places=5)
        self.assertEqual(codec.node_run_to_row(node)["retry_at"].timestamp(), node.retry_at)  # datetime <-> epoch is stable

    def test_a_default_skip_row_reads_back_as_attempt_one_which_is_why_stage_3_must_write_zero(self) -> None:
        # Characterization of the Stage 3 obligation recorded in the codec docstring: the column
        # default is attempt = 1, the scheduler's skipped node is attempt = 0, and the codec does
        # not normalize between them.
        run_id = self.add_run()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO node_runs (run_id, node_key, status) VALUES (:r, 'skipped_default', 'skipped')"), {"r": run_id})
            conn.execute(text("INSERT INTO node_runs (run_id, node_key, status, attempt, failures) VALUES (:r, 'skipped_explicit', 'skipped', 0, 0)"), {"r": run_id})
        self.assertEqual(self._stored(run_id, "skipped_default").attempt, 1)
        self.assertEqual(self._stored(run_id, "skipped_explicit").attempt, 0)
        self.assertEqual(self._stored(run_id, "skipped_explicit"), NodeRun(run_id=str(run_id), node_key="skipped_explicit", status=NodeRunStatus.SKIPPED))


class JsonbStringScalarFromRealRowsTestCase(WorkerDatabaseTestCase):
    """A jsonb STRING SCALAR in a real column must never be re-parsed into an object.

    The driver returns a jsonb string as a Python ``str`` (the same type raw JSON text has), so a
    decoder that parses ``str`` would accept ``"{\\"code\\": ...}"`` as an error object. Every case
    below stores a string scalar whose CONTENT is valid JSON text, through real psycopg, and loads the
    run's snapshot.
    """

    ERROR_TEXT = '{"code": "x", "message": "m", "cause": "cancel"}'
    OUTPUT_TEXT = '{"main": [{"json": {"a": 1}}]}'
    ITEM_RESULTS_TEXT = '[{"index": 0, "status": "succeeded"}]'
    DEFINITION_TEXT = '{"nodes": [{"key": "a", "type": "t", "type_version": 1}], "edges": []}'

    def _snapshot(self, run_id):
        engine = create_db_engine(self._temp_db.url)
        self.addCleanup(engine.dispose)
        with engine.connect() as conn:
            return load_run_snapshot(conn, str(run_id))

    def _node(self, key="n", **fields):
        run_id = self.add_run()
        with self.engine.begin() as conn:
            make_node_run(conn, run_id, key, status="failed", outcome="unknown", effect_class="unsafe_write", attempt=1, failures=1,
                          started_epoch=1, **fields)  # fmt: skip
        return run_id

    def _as_string_scalar(self, run_id, column, content):
        with self.engine.begin() as conn:
            conn.execute(text(f"UPDATE node_runs SET {column} = to_jsonb(CAST(:t AS text)) WHERE run_id = :id"), {"t": content, "id": as_id(run_id)})
            kind = conn.execute(text(f"SELECT jsonb_typeof({column}) FROM node_runs WHERE run_id = :id"), {"id": as_id(run_id)}).scalar_one()
        self.assertEqual(kind, "string")  # the precondition: it really is a jsonb string, not an object

    def test_an_error_stored_as_a_json_string_is_rejected(self) -> None:
        run_id = self._node(error={"code": "x", "message": "m", "cause": "crash"})
        self._as_string_scalar(run_id, "error", self.ERROR_TEXT)
        with self.assertRaisesRegex(CodecError, "error must be a JSON object, got str"):
            self._snapshot(run_id)

    def test_an_output_stored_as_a_json_string_is_rejected(self) -> None:
        run_id = self._node(error={"code": "x", "message": "m", "cause": "crash"})
        self._as_string_scalar(run_id, "output", self.OUTPUT_TEXT)
        with self.assertRaisesRegex(CodecError, "output must be a JSON object, got str"):
            self._snapshot(run_id)

    def test_item_results_stored_as_a_json_string_are_rejected(self) -> None:
        run_id = self._node(error={"code": "x", "message": "m", "cause": "crash"})
        self._as_string_scalar(run_id, "item_results", self.ITEM_RESULTS_TEXT)
        with self.assertRaisesRegex(CodecError, "item_results must be a JSON array, got str"):
            self._snapshot(run_id)

    def test_a_definition_stored_as_a_json_string_is_rejected(self) -> None:
        run_id = self.add_run()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE workflow_versions SET definition = to_jsonb(CAST(:t AS text)) WHERE id = :id"),
                         {"t": self.DEFINITION_TEXT, "id": self.version_id})  # fmt: skip
            self.assertEqual(conn.execute(text("SELECT jsonb_typeof(definition) FROM workflow_versions WHERE id = :id"), {"id": self.version_id}).scalar_one(), "string")
        with self.assertRaisesRegex(CodecError, "definition must be a JSON object, got str"):
            self._snapshot(run_id)

    def test_the_same_data_stored_as_real_jsonb_objects_decodes(self) -> None:
        # control: nothing above is rejected merely for its content
        run_id = self._node(
            error=json.loads(self.ERROR_TEXT), output=json.loads(self.OUTPUT_TEXT), item_results=json.loads(self.ITEM_RESULTS_TEXT)
        )
        node = self._snapshot(run_id).node_run("n")
        self.assertEqual(node.error, NodeError("x", "m", "cancel"))
        self.assertEqual(node.output["main"][0].json["a"], 1)
        self.assertEqual(len(node.item_results), 1)

    def test_a_string_value_that_looks_like_json_is_kept_as_a_string_inside_valid_objects(self) -> None:
        looks_like_json = '{"code": "x", "cause": "cancel"}'
        run_id = self._node(
            error={"code": "x", "message": looks_like_json, "cause": "crash"},
            output={"main": [{"json": {"body": looks_like_json}}]},
        )
        node = self._snapshot(run_id).node_run("n")
        self.assertEqual(node.error.message, looks_like_json)
        self.assertEqual(node.error.cause, "crash")  # the embedded "cancel" was never interpreted
        self.assertEqual(node.output["main"][0].json["body"], looks_like_json)
