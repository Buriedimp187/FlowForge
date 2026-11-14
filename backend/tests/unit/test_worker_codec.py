"""Stage 1 of M2.5: the row/JSONB/definition codec (no database)."""

from __future__ import annotations

import itertools
import json
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import (
    Edge,
    ExecutionMode,
    NodeDefinition,
    OnErrorPolicy,
    RetryPolicy,
    WorkflowDefinition,
)
from flowforge.domain.items import Item, items_from
from flowforge.domain.node_run import ItemResult, NodeError, NodeRun
from flowforge.domain.states import NodeRunStatus, Outcome
from flowforge.worker import codec
from flowforge.worker.codec import CodecError

SRC = Path(__file__).resolve().parents[2] / "src" / "flowforge" / "worker"

UNICODE_JSON = {"name": "Zoë ✓ 日本語", "nested": {"a": [1, 2.5, None, True, {"b": []}]}, "empty": {}}


def _row(**overrides):
    base = dict(
        run_id=uuid.UUID("00000000-0000-7000-8000-000000000001"),
        node_key="n1",
        status="running",
        attempt=1,
        failures=0,
        outcome="known",
        effect_class="pure",
        started_epoch=1,
        retry_at=None,
        output=None,
        error=None,
        item_results=None,
    )
    base.update(overrides)
    return base


class JsonTestCase(unittest.TestCase):
    def test_dumps_is_deterministic_compact_sorted_and_keeps_unicode(self) -> None:
        text = codec.dumps_jsonb({"b": 1, "a": {"z": [1, 2], "y": "é"}})
        self.assertEqual(text, '{"a":{"y":"é","z":[1,2]},"b":1}')
        self.assertEqual(codec.dumps_jsonb({"a": {"y": "é", "z": [1, 2]}, "b": 1}), text)  # key order is irrelevant

    def test_dumps_accepts_frozen_domain_values(self) -> None:
        item = Item(json=UNICODE_JSON)
        self.assertEqual(json.loads(codec.dumps_jsonb(item.json)), UNICODE_JSON)

    def test_the_strict_json_boundary_is_the_domains_own(self) -> None:
        for bad in (float("nan"), float("inf"), {"x": float("-inf")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                codec.dumps_jsonb(bad)
        for bad in (b"bytes", {1: "int key"}, {"s": {1, 2}}, object()):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                codec.dumps_jsonb(bad)

    def test_parse_json_text_is_the_one_explicit_text_parser(self) -> None:
        self.assertEqual(codec.parse_json_text('{"a": [1, 2.5, null, true, "é"]}'), {"a": [1, 2.5, None, True, "é"]})
        self.assertEqual(codec.parse_json_text(b'{"a": 1}'), {"a": 1})
        self.assertEqual(codec.parse_json_text("null"), None)
        self.assertEqual(codec.parse_json_text('"a string scalar"'), "a string scalar")
        for bad in ("{not json", "", "{'a': 1}", '{"a": 1,}'):
            with self.subTest(bad=bad), self.assertRaises(CodecError):
                codec.parse_json_text(bad)

    def test_parse_json_text_refuses_non_standard_constants_and_non_text(self) -> None:
        for bad in ("NaN", "Infinity", "-Infinity", '{"a": NaN}', "[Infinity]", '{"a": -Infinity}'):
            with self.subTest(bad=bad), self.assertRaises(CodecError):
                codec.parse_json_text(bad)
        for bad in (None, 5, {"a": 1}, ["x"]):
            with self.subTest(bad=bad), self.assertRaises(CodecError):
                codec.parse_json_text(bad)  # type: ignore[arg-type]

    def test_the_loads_helper_that_re_parsed_driver_values_is_gone(self) -> None:
        self.assertFalse(hasattr(codec, "loads_jsonb"))


class TimeoutContractTestCase(unittest.TestCase):
    """``NodeDefinition.timeout_s``: the existing domain contract, held at the codec boundary too.

    A plain int or float, finite, and strictly positive (``None`` means no per-node timeout).
    Booleans, NaN, +/-Infinity, zero, negatives and strings are rejected.
    """

    ACCEPTED = (1, 0.5, 300.25, 1e-9, 5e-324, 1e308, 10**30)
    REJECTED = (True, False, float("nan"), float("inf"), float("-inf"), 0, 0.0, -1, -0.5, "5", [5], {"s": 5})

    def _node(self, timeout):
        return {"nodes": [{"key": "a", "type": "t", "type_version": 1, "timeout_s": timeout}]}

    def test_the_domain_contract_itself(self) -> None:
        for value in self.ACCEPTED:
            with self.subTest(accepted=value):
                self.assertEqual(NodeDefinition(key="a", type="t", type_version=1, timeout_s=value).timeout_s, value)
        self.assertIsNone(NodeDefinition(key="a", type="t", type_version=1).timeout_s)
        for value in (True, False, "5", [5]):
            with self.subTest(rejected=value), self.assertRaises(TypeError):
                NodeDefinition(key="a", type="t", type_version=1, timeout_s=value)
        for value in (float("nan"), float("inf"), float("-inf"), 0, 0.0, -1, -0.5):
            with self.subTest(rejected=value), self.assertRaises(ValueError):
                NodeDefinition(key="a", type="t", type_version=1, timeout_s=value)

    def test_the_codec_accepts_exactly_what_the_domain_accepts(self) -> None:
        for value in self.ACCEPTED:
            with self.subTest(accepted=value):
                self.assertEqual(codec.definition_from_json(self._node(value), 1).nodes[0].timeout_s, value)
        self.assertIsNone(codec.definition_from_json(self._node(None), 1).nodes[0].timeout_s)
        absent = {"nodes": [{"key": "a", "type": "t", "type_version": 1}]}
        self.assertIsNone(codec.definition_from_json(absent, 1).nodes[0].timeout_s)

    def test_the_codec_rejects_everything_the_domain_rejects_as_a_codec_error(self) -> None:
        for value in self.REJECTED:
            with self.subTest(rejected=value), self.assertRaises(CodecError):
                codec.definition_from_json(self._node(value), 1)

    def test_non_finite_numbers_in_json_text_are_rejected(self) -> None:
        # Python's json accepts the non-standard NaN/Infinity tokens; the boundary must not (the
        # text parser refuses them outright, the domain rules refuse the other values).
        template = '{"nodes": [{"key": "a", "type": "t", "type_version": 1, "timeout_s": %s}]}'
        for token in ("NaN", "Infinity", "-Infinity", "true", "false", "0", "-3", '"5"'):
            with self.subTest(token=token), self.assertRaises(CodecError):
                codec.definition_from_json(codec.parse_json_text(template % token), 1)

    def test_a_valid_timeout_survives_a_full_encode_and_decode(self) -> None:
        for value in (1, 0.5, 300.25, 12.5, 1e-9):
            definition = WorkflowDefinition(
                schema_version=1, nodes=(NodeDefinition(key="a", type="t", type_version=1, timeout_s=value),), edges=()
            )
            text = codec.dumps_jsonb(codec.definition_to_json(definition))
            self.assertEqual(codec.definition_from_json(codec.parse_json_text(text), 1), definition)

    def test_a_non_finite_timeout_can_never_be_encoded(self) -> None:
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                codec.dumps_jsonb({"timeout_s": value})


class JsonbStorabilityTestCase(unittest.TestCase):
    """Valid JSON that PostgreSQL jsonb cannot store must be refused BEFORE any SQL.

    Confirmed against PostgreSQL 17.12 (tests/integration/test_codec_jsonb.py): a NUL
    raises ``DataError`` and a lone surrogate raises ``UnicodeEncodeError`` at bind time.
    """

    NUL = "a\x00b"
    LONE_HIGH = "a\ud800b"
    LONE_LOW = "a\udfffb"

    def test_nul_and_lone_surrogates_are_rejected_in_values(self) -> None:
        for bad in (self.NUL, self.LONE_HIGH, self.LONE_LOW, "\x00", "\ud800"):
            for shape in (lambda s: s, lambda s: [s], lambda s: {"k": s}, lambda s: {"k": [{"deep": [s]}]}):
                with self.subTest(bad=bad, shape=shape(bad)), self.assertRaises(CodecError):
                    codec.dumps_jsonb(shape(bad))

    def test_nul_and_lone_surrogates_are_rejected_in_object_keys_too(self) -> None:
        for bad in (self.NUL, self.LONE_HIGH):
            for shape in (lambda s: {s: 1}, lambda s: {"a": {s: 1}}, lambda s: [{s: None}]):
                with self.subTest(bad=bad), self.assertRaises(CodecError):
                    codec.dumps_jsonb(shape(bad))

    def test_the_error_names_where_the_value_is(self) -> None:
        with self.assertRaisesRegex(CodecError, r"\$\.k\[1\]\.deep"):
            codec.to_json_value({"k": [0, {"deep": self.NUL}]})
        with self.assertRaisesRegex(CodecError, "object key"):
            codec.to_json_value({"outer": {self.NUL: 1}})
        with self.assertRaisesRegex(CodecError, "NUL"):
            codec.to_json_value(self.NUL)
        with self.assertRaisesRegex(CodecError, "surrogate"):
            codec.to_json_value(self.LONE_HIGH)

    def test_storable_text_is_untouched(self) -> None:
        # Only U+0000 and unpaired surrogates are unstorable; everything else round-trips as text.
        for good in ("", "plain", "Zoë ✓ 日本語", "a😀b", "\x01\x1f\x7f\x80 ﻿", "\\u0000 as literal backslash text", "�"):
            with self.subTest(good=good):
                self.assertEqual(json.loads(codec.dumps_jsonb({good: [good]})), {good: [good]})

    def test_every_codec_path_that_feeds_jsonb_applies_the_rule(self) -> None:
        with self.assertRaises(CodecError):
            codec.items_to_json(items_from({"v": self.NUL}))
        with self.assertRaises(CodecError):
            codec.dumps_jsonb(codec.output_to_json({"main": items_from({"v": self.LONE_HIGH})}))
        with self.assertRaises(CodecError):
            codec.dumps_jsonb(codec.error_to_json(NodeError("c", f"bad {self.NUL}")))
        definition = WorkflowDefinition(
            schema_version=1,
            nodes=(NodeDefinition(key="a", type="t", type_version=1, params={"url": self.NUL}),),
            edges=(),
        )
        with self.assertRaises(CodecError):
            codec.definition_to_json(definition)

    def test_non_json_values_still_fail_with_the_domains_own_errors(self) -> None:
        with self.assertRaises(ValueError):
            codec.dumps_jsonb(float("nan"))
        with self.assertRaises(TypeError):
            codec.dumps_jsonb({1: "int key"})


class JsonbStringScalarIsNeverReparsedTestCase(unittest.TestCase):
    """The driver decodes jsonb once. A ``str`` handed to a decoder is a JSON STRING SCALAR.

    Where an object or array is required it is rejected, however much it looks like JSON text:
    parsing it again would silently accept a malformed ``"{...}"`` string as an object (for
    ``error`` that could even carry a ``cancel`` cause that exempts a failed/unknown row).
    """

    LOOKS_LIKE_JSON = (
        '{"code": "x", "message": "m", "cause": "cancel"}',
        '{"main": []}',
        '{"nodes": [], "edges": []}',
        '[]',
        '[{"index": 0, "status": "succeeded"}]',
        '{}',
        "null",
        "true",
        "123",
    )  # fmt: skip
    PLAIN_STRINGS = ("boom", "", " ", '"quoted"', "{not json")
    EVERY_STRING = LOOKS_LIKE_JSON + PLAIN_STRINGS

    def test_an_error_that_is_a_string_scalar_is_rejected(self) -> None:
        for raw in self.EVERY_STRING:
            with self.subTest(raw=raw), self.assertRaisesRegex(CodecError, "JSON object"):
                codec.error_from_json(raw)

    def test_an_output_that_is_a_string_scalar_is_rejected_even_when_it_reads_null_or_empty(self) -> None:
        for raw in self.EVERY_STRING:  # "null" and "{}" are not None / an empty mapping: they are text
            with self.subTest(raw=raw), self.assertRaisesRegex(CodecError, "JSON object"):
                codec.output_from_json(raw)

    def test_item_results_that_are_a_string_scalar_are_rejected(self) -> None:
        for raw in self.EVERY_STRING:
            with self.subTest(raw=raw), self.assertRaisesRegex(CodecError, "JSON array"):
                codec.item_results_from_json(raw)

    def test_a_definition_that_is_a_string_scalar_is_rejected(self) -> None:
        for raw in self.EVERY_STRING:
            with self.subTest(raw=raw), self.assertRaisesRegex(CodecError, "JSON object"):
                codec.definition_from_json(raw, 1)

    def test_bytes_are_not_decoded_either(self) -> None:
        for raw in (b'{"code": "x"}', bytearray(b'{"main": []}')):
            for decode in (codec.error_from_json, codec.output_from_json, codec.item_results_from_json):
                with self.subTest(raw=raw, decode=decode.__name__), self.assertRaises(CodecError):
                    decode(raw)
            with self.assertRaises(CodecError):
                codec.definition_from_json(raw, 1)

    def test_absent_values_keep_their_meaning(self) -> None:
        self.assertIsNone(codec.error_from_json(None))
        self.assertEqual(codec.output_from_json(None), {})
        self.assertIsNone(codec.item_results_from_json(None))
        with self.assertRaises(CodecError):
            codec.definition_from_json(None, 1)  # a definition is required

    def test_other_wrong_shapes_are_still_rejected(self) -> None:
        for raw in (5, 1.5, True, [1], ["a"]):
            with self.subTest(raw=raw):
                with self.assertRaises(CodecError):
                    codec.error_from_json(raw)
                with self.assertRaises(CodecError):
                    codec.output_from_json(raw)
                with self.assertRaises(CodecError):
                    codec.definition_from_json(raw, 1)
        for raw in (5, 1.5, True, {"a": 1}, "x"):
            with self.subTest(raw=raw), self.assertRaises(CodecError):
                codec.item_results_from_json(raw)

    def test_text_that_is_parsed_explicitly_decodes_normally(self) -> None:
        error = codec.error_from_json(codec.parse_json_text('{"code": "x", "message": "m", "cause": "crash"}'))
        self.assertEqual(error, NodeError("x", "m", "crash"))
        self.assertEqual(codec.output_from_json(codec.parse_json_text('{"main": [{"json": {"a": 1}}]}')), {"main": items_from({"a": 1})})
        self.assertEqual(codec.item_results_from_json(codec.parse_json_text("[]")), ())
        self.assertEqual(codec.definition_from_json(codec.parse_json_text("{}"), 1), WorkflowDefinition(schema_version=1, nodes=(), edges=()))

    def test_strings_that_look_like_json_inside_valid_structures_stay_strings(self) -> None:
        # Nothing nested is ever parsed: a string VALUE is data, wherever a string is allowed.
        text = '{"a": [1, 2], "b": null}'
        items = codec.items_from_json([{"json": {"body": text}}])
        self.assertEqual(items[0].json["body"], text)
        output = codec.output_from_json({"main": [{"json": {"body": text}}]})
        self.assertEqual(output["main"][0].json["body"], text)
        error = codec.error_from_json({"code": "x", "message": text, "cause": None})
        self.assertEqual(error.message, text)
        definition = codec.definition_from_json({"nodes": [{"key": "a", "type": "t", "type_version": 1, "params": {"body": text}}]}, 1)
        self.assertEqual(definition.nodes[0].params["body"], text)
        row = codec.node_run_from_row(_row(output={"main": [{"json": {"body": text}}]}, error={"code": "x", "message": text}))
        self.assertEqual(row.output["main"][0].json["body"], text)
        self.assertEqual(row.error.message, text)

    def test_a_node_row_whose_error_or_output_is_a_string_scalar_is_rejected(self) -> None:
        for column in ("error", "output"):
            with self.subTest(column=column), self.assertRaises(CodecError):
                codec.node_run_from_row(_row(**{column: '{"code": "x", "message": "m", "cause": "cancel"}'}))
        with self.assertRaises(CodecError):
            codec.node_run_from_row(_row(item_results="[]"))


class TimestampTestCase(unittest.TestCase):
    def test_microsecond_values_round_trip_exactly(self) -> None:
        for dt in (
            datetime(2026, 11, 14, 2, 9, 54, 123456, tzinfo=timezone.utc),
            datetime(2038, 1, 19, 3, 14, 7, 999999, tzinfo=timezone.utc),
            datetime(2000, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 12, 0, 0, 500000, tzinfo=timezone(timedelta(hours=-3))),
        ):
            with self.subTest(dt=dt):
                back = codec.epoch_to_datetime(codec.datetime_to_epoch(dt))
                self.assertEqual(back, dt)
                self.assertEqual(back.utcoffset(), timedelta(0))

    def test_naive_datetimes_are_rejected(self) -> None:
        with self.assertRaises(CodecError):
            codec.datetime_to_epoch(datetime(2026, 1, 1))
        with self.assertRaises(CodecError):
            codec.datetime_to_epoch("2026-01-01T00:00:00Z")  # type: ignore[arg-type]


class ItemsErrorsOutputsTestCase(unittest.TestCase):
    def test_items_round_trip_in_order_with_nested_and_unicode_payloads(self) -> None:
        items = items_from(UNICODE_JSON, {}, {"k": [3, 2, 1]})
        encoded = codec.items_to_json(items)
        self.assertEqual(encoded[0], {"json": UNICODE_JSON})
        self.assertEqual(codec.items_from_json(json.loads(codec.dumps_jsonb(encoded))), items)

    def test_malformed_items_are_codec_errors(self) -> None:
        # [{}] (no "json" key) is malformed, not an empty item.
        for bad in ("x", [1], [{}], [{"json": "scalar"}], [{"json": None}], [{"json": {}, "extra": 1}], {"not": "a list"}):
            with self.subTest(bad=bad), self.assertRaises(CodecError):
                codec.items_from_json(bad)
        self.assertEqual(codec.items_from_json([{"json": {}}]), items_from({}))  # an explicit empty payload is fine

    def test_empty_output_is_null_but_an_empty_port_is_kept(self) -> None:
        self.assertIsNone(codec.output_to_json({}))
        self.assertEqual(codec.output_from_json(None), {})
        encoded = codec.output_to_json({"main": ()})
        self.assertEqual(encoded, {"main": []})  # emitted on the port, zero items
        decoded = codec.output_from_json(encoded)
        self.assertIn("main", decoded)
        self.assertEqual(decoded["main"], ())

    def test_multi_port_output_round_trips(self) -> None:
        output = {"true": items_from({"a": 1}), "false": (), "other": items_from({"b": [None]})}
        self.assertEqual(codec.output_from_json(codec.output_to_json(output)), output)

    def test_errors_round_trip_including_none_and_null_cause(self) -> None:
        self.assertIsNone(codec.error_to_json(None))
        self.assertIsNone(codec.error_from_json(None))
        for error in (NodeError("boom"), NodeError("outcome_unknown", "dispatch unprovable", "crash")):
            with self.subTest(error=error):
                self.assertEqual(codec.error_from_json(codec.error_to_json(error)), error)
        self.assertEqual(codec.error_to_json(NodeError("c")), {"code": "c", "message": "", "cause": None})

    def test_sql_built_errors_and_extra_keys_are_read_tolerantly(self) -> None:
        sql_style = {"code": "outcome_unknown", "cause": "deadline", "message": "orphaned"}  # jsonb_build_object
        self.assertEqual(codec.error_from_json(sql_style), NodeError("outcome_unknown", "orphaned", "deadline"))
        self.assertEqual(codec.error_from_json({"code": "x", "items_dispatched": [1]}), NodeError("x"))

    def test_malformed_errors_are_codec_errors(self) -> None:
        # An explicit null message is malformed (no writer produces one); it is not coerced to "".
        for bad in ({"message": "no code"}, {"code": ""}, {"code": 5}, {"code": "x", "message": 3}, {"code": "x", "message": None}, {"code": "x", "cause": 3}, "str"):
            with self.subTest(bad=bad), self.assertRaises(CodecError):
                codec.error_from_json(bad)

    def test_item_results_round_trip(self) -> None:
        results = (
            ItemResult(index=0, status="succeeded", output=items_from({"a": 1})),
            ItemResult(index=1, status="failed", error=NodeError("bad", "item failed")),
            ItemResult(index=2, status="succeeded", output=()),
        )
        self.assertIsNone(codec.item_results_to_json(None))
        self.assertIsNone(codec.item_results_from_json(None))
        self.assertEqual(codec.item_results_from_json(codec.item_results_to_json(results)), results)
        self.assertEqual(codec.item_results_from_json(codec.item_results_to_json(())), ())

    def test_malformed_item_results_are_codec_errors(self) -> None:
        for bad in (
            [{"index": -1, "status": "succeeded"}], [{"index": 0, "status": "weird"}], [{"status": "failed"}],
            [{"index": 0, "status": "failed", "x": 1}], "no",
            [{"index": 1.0, "status": "succeeded"}], [{"index": True, "status": "succeeded"}], [{"index": "0", "status": "succeeded"}],
            [{"index": 0, "status": 5}], [{"index": 0}],
        ):
            with self.subTest(bad=bad), self.assertRaises(CodecError):
                codec.item_results_from_json(bad)


class NodeRunRowTestCase(unittest.TestCase):
    def test_every_status_effect_class_and_outcome_round_trips(self) -> None:
        for status, effect, outcome in itertools.product(NodeRunStatus, EffectClass, Outcome):
            with self.subTest(status=status, effect=effect, outcome=outcome):
                run = NodeRun(
                    run_id="r1",
                    node_key="n1",
                    status=status,
                    attempt=3,
                    failures=2,
                    effect_class=effect,
                    outcome=outcome,
                    retry_at=1_800_000_000.25 if status is NodeRunStatus.RETRY_WAIT else None,
                    started_epoch=7,
                )
                self.assertEqual(codec.node_run_from_row(codec.node_run_to_row(run)), run)

    def test_the_complete_domain_state_round_trips_through_jsonb_text(self) -> None:
        run = NodeRun(
            run_id="00000000-0000-7000-8000-000000000001",
            node_key="fetch",
            status=NodeRunStatus.RETRY_WAIT,
            attempt=4,
            failures=3,
            effect_class=EffectClass.IDEMPOTENT_WRITE,
            outcome=Outcome.UNKNOWN,
            retry_at=datetime(2026, 11, 14, 2, 9, 54, 123456, tzinfo=timezone.utc).timestamp(),
            output={"main": items_from(UNICODE_JSON), "empty": ()},
            error=NodeError("node_timeout", "attempt exceeded its timeout", "timeout"),
            item_results=(ItemResult(index=0, status="succeeded", output=items_from({"a": 1})),),
            started_epoch=12,
        )
        row = codec.node_run_to_row(run)
        for column in ("output", "error", "item_results"):  # what the driver hands back for jsonb is decoded JSON
            row[column] = json.loads(codec.dumps_jsonb(row[column]))
        self.assertEqual(codec.node_run_from_row(row), run)

    def test_the_run_id_may_be_a_uuid_or_text_and_comes_back_as_text(self) -> None:
        a = codec.node_run_from_row(_row())
        b = codec.node_run_from_row(_row(run_id="00000000-0000-7000-8000-000000000001"))
        self.assertEqual((a.run_id, b.run_id), ("00000000-0000-7000-8000-000000000001",) * 2)

    def test_persistence_only_columns_are_not_read(self) -> None:
        row = _row(started_at=datetime.now(timezone.utc), resolved_params={"x": 1})
        self.assertEqual(codec.node_run_from_row(row), codec.node_run_from_row(_row()))

    def test_started_epoch_and_absent_effect_class_survive(self) -> None:
        skipped = codec.node_run_from_row(_row(status="skipped", effect_class=None, started_epoch=None, attempt=0))
        self.assertEqual((skipped.status, skipped.effect_class, skipped.started_epoch), (NodeRunStatus.SKIPPED, None, None))
        self.assertIsNone(codec.node_run_to_row(skipped)["effect_class"])

    def test_invalid_rows_are_codec_errors_never_silently_repaired(self) -> None:
        cases = {
            "unknown status": _row(status="paused"),
            "unknown outcome": _row(outcome="maybe"),
            "unknown effect": _row(effect_class="dangerous"),
            "negative attempt": _row(attempt=-1),
            "negative epoch": _row(started_epoch=-1),
            "bool epoch": _row(started_epoch=True),
            "naive retry_at": _row(retry_at=datetime(2026, 1, 1)),
            "bad output": _row(output={"main": "not a list"}),
            "bad error": _row(error={"code": ""}),
            "empty node key": _row(node_key=""),
            "float attempt": _row(attempt=1.5),
            "bool attempt": _row(attempt=True),
            "string attempt": _row(attempt="1"),
            "float failures": _row(failures=2.0),
            "bool failures": _row(failures=False),
            "integer node key": _row(node_key=5),
            "integer run id": _row(run_id=7),
            "float epoch": _row(started_epoch=1.5),
        }
        for label, row in cases.items():
            with self.subTest(case=label), self.assertRaises(CodecError):
                codec.node_run_from_row(row)
        incomplete = _row()
        del incomplete["failures"]
        with self.assertRaisesRegex(CodecError, "failures"):
            codec.node_run_from_row(incomplete)

    def test_retry_at_is_rendered_as_a_datetime_for_fixtures_only(self) -> None:
        run = NodeRun(run_id="r", node_key="n", status=NodeRunStatus.RETRY_WAIT, effect_class=EffectClass.PURE, retry_at=1_800_000_000.5)
        rendered = codec.node_run_to_row(run)["retry_at"]
        self.assertEqual(rendered, datetime(2027, 1, 15, 8, 0, 0, 500000, tzinfo=timezone.utc))


def _definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=2,
        nodes=(
            NodeDefinition(key="trigger", type="manual_trigger", type_version=1),
            NodeDefinition(
                key="call",
                type="http",
                type_version=3,
                name="Call API",
                params={"url": "https://example.test", "headers": {"a": ["b", 1]}, "ok": True},
                credential_id="cred-1",
                retry=RetryPolicy(max_attempts=5, backoff_base_s=0.5, backoff_multiplier=3, backoff_max_s=30),
                timeout_s=12.5,
                on_error=OnErrorPolicy.CONTINUE,
                execution_mode=ExecutionMode.PER_ITEM,
                continue_on_item_error=True,
            ),
        ),
        edges=(Edge("e1", "trigger", "main", "call", "main"),),
    )


class DefinitionTestCase(unittest.TestCase):
    def test_a_full_definition_round_trips_through_jsonb_text(self) -> None:
        definition = _definition()
        text = codec.dumps_jsonb(codec.definition_to_json(definition))
        self.assertEqual(codec.definition_from_json(json.loads(text), definition.schema_version), definition)
        self.assertEqual(codec.definition_from_json(codec.parse_json_text(text), 2), definition)

    def test_node_and_edge_order_is_preserved(self) -> None:
        definition = _definition()
        decoded = codec.definition_from_json(codec.definition_to_json(definition), 2)
        self.assertEqual([n.key for n in decoded.nodes], ["trigger", "call"])

    def test_the_architecture_shape_with_defaults_and_ignored_ui(self) -> None:
        raw = {
            "nodes": [{"key": "a", "type": "t", "type_version": 1}, {"key": "b", "type": "t", "type_version": 1}],
            "edges": [{"source_key": "a", "source_port": "main", "target_key": "b", "target_port": "main"}],
            "ui": {"positions": {"a": [0, 0]}, "viewport": {}},
        }
        definition = codec.definition_from_json(raw, 1)
        self.assertEqual(definition.nodes[0], NodeDefinition(key="a", type="t", type_version=1))
        self.assertEqual(definition.edges[0].id, codec.derive_edge_id("a", "main", "b", "main"))
        self.assertEqual(definition.edges[0].id, "a.main->b.main")

    def test_an_empty_definition_is_decodable(self) -> None:
        self.assertEqual(codec.definition_from_json({}, 1), WorkflowDefinition(schema_version=1, nodes=(), edges=()))

    def test_unknown_fields_are_rejected_not_silently_dropped(self) -> None:
        for bad in (
            {"nodes": [{"key": "a", "type": "t", "type_version": 1, "surprise": 1}]},
            {"nodes": [], "edges": [{"source_key": "a", "source_port": "p", "target_key": "b", "target_port": "p", "x": 1}]},
            {"nodes": [{"key": "a", "type": "t", "type_version": 1, "retry": {"max_attempts": 2, "jitter": 1}}]},
            {"nodes": [], "extra": 1},
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(CodecError, "unknown field"):
                codec.definition_from_json(bad, 1)

    def test_domain_validation_is_reused_and_wrapped(self) -> None:
        cases = {
            "duplicate node keys": {"nodes": [{"key": "a", "type": "t", "type_version": 1}] * 2},
            "empty key": {"nodes": [{"key": "", "type": "t", "type_version": 1}]},
            "bad type_version": {"nodes": [{"key": "a", "type": "t", "type_version": 0}]},
            "bad timeout": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "timeout_s": -1}]},
            "bool timeout": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "timeout_s": True}]},
            "bad retry": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "retry": {"max_attempts": 0}}]},
            "bad on_error": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "on_error": "explode"}]},
            "bad execution mode": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "execution_mode": "x"}]},
            "bad params": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "params": {"x": float("nan")}}]},
            "params not an object": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "params": [1]}]},
            "edge missing port": {"nodes": [], "edges": [{"source_key": "a", "source_port": "p", "target_key": "b"}]},
            "empty port": {"nodes": [], "edges": [{"source_key": "a", "source_port": "", "target_key": "b", "target_port": "p"}]},
            "duplicate edge ids": {"nodes": [], "edges": [{"id": "e", "source_key": "a", "source_port": "p", "target_key": "b", "target_port": "p"}] * 2},
            "nodes not a list": {"nodes": {"a": 1}},
            # strict types at the JSON boundary (the M1 domain only checks truthiness and range)
            "integer key": {"nodes": [{"key": 5, "type": "t", "type_version": 1}]},
            "integer type": {"nodes": [{"key": "a", "type": 5, "type_version": 1}]},
            "bool type_version": {"nodes": [{"key": "a", "type": "t", "type_version": True}]},
            "float type_version": {"nodes": [{"key": "a", "type": "t", "type_version": 1.0}]},
            "string type_version": {"nodes": [{"key": "a", "type": "t", "type_version": "1"}]},
            "integer name": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "name": 5}]},
            "null name": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "name": None}]},
            "integer credential": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "credential_id": 5}]},
            "string continue flag": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "continue_on_item_error": "yes"}]},
            "integer continue flag": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "continue_on_item_error": 1}]},
            "string timeout": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "timeout_s": "5"}]},
            "missing key": {"nodes": [{"type": "t", "type_version": 1}]},
            "missing type": {"nodes": [{"key": "a", "type_version": 1}]},
            "missing type_version": {"nodes": [{"key": "a", "type": "t"}]},
            "unhashable on_error": {"nodes": [{"key": "a", "type": "t", "type_version": 1, "on_error": ["fail_run"]}]},
            "integer edge id": {"nodes": [], "edges": [{"id": 5, "source_key": "a", "source_port": "p", "target_key": "b", "target_port": "p"}]},
            "empty edge id": {"nodes": [], "edges": [{"id": "", "source_key": "a", "source_port": "p", "target_key": "b", "target_port": "p"}]},
            "integer edge port": {"nodes": [], "edges": [{"source_key": "a", "source_port": 1, "target_key": "b", "target_port": "p"}]},
            "bool schema_version in json": {"schema_version": True, "nodes": []},
            "float schema_version in json": {"schema_version": 1.0, "nodes": []},
        }
        for label, raw in cases.items():
            with self.subTest(case=label), self.assertRaises(CodecError):
                codec.definition_from_json(raw, 1)
        for bad_version in (0, True, 1.0, "1", None):  # the column value is held to the same strictness
            with self.subTest(schema_version=bad_version), self.assertRaises(CodecError):
                codec.definition_from_json({}, bad_version)  # type: ignore[arg-type]

    def test_a_schema_version_inside_the_json_must_agree_with_the_column(self) -> None:
        self.assertEqual(codec.definition_from_json({"schema_version": 2}, 2).schema_version, 2)
        with self.assertRaisesRegex(CodecError, "disagrees"):
            codec.definition_from_json({"schema_version": 3}, 2)

    def test_json_output_is_a_pure_function_of_the_definition(self) -> None:
        self.assertEqual(
            codec.dumps_jsonb(codec.definition_to_json(_definition())),
            codec.dumps_jsonb(codec.definition_to_json(_definition())),
        )


class PurityTestCase(unittest.TestCase):
    def test_the_codec_has_no_database_imports(self) -> None:
        source = (SRC / "codec.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(?:from|import)\s+(?:sqlalchemy|psycopg|alembic)\b", source, re.MULTILINE))

    def test_the_codec_only_depends_on_the_pure_domain(self) -> None:
        source = (SRC / "codec.py").read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*from\s+(flowforge\.[\w.]+)\s+import", source, re.MULTILINE))
        self.assertTrue(imported)
        self.assertTrue(all(name.startswith("flowforge.domain") for name in imported), imported)

    def test_a_rendered_row_never_aliases_the_frozen_domain_state(self) -> None:
        run = NodeRun(run_id="r", node_key="n", status=NodeRunStatus.SUCCEEDED, effect_class=EffectClass.PURE, output={"main": items_from({"a": [1]})})
        row = codec.node_run_to_row(run)
        row["output"]["main"][0]["json"]["a"].append(2)  # mutating the rendered row ...
        self.assertEqual(run.output["main"][0].json["a"], (1,))  # ... never reaches the domain object


if __name__ == "__main__":
    unittest.main()
