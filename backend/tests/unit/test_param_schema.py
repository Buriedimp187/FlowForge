"""JSON Schema Draft 2020-12 node-parameter validation tests.

M1 final-correction finding 3: the previous `ParamSpec` custom
validator did not satisfy docs/architecture.md section 5's "JSON
Schema" requirement. These tests exercise the real
`jsonschema.Draft202012Validator` through
`flowforge.engine.param_schema`, not merely import it.
"""

from __future__ import annotations

import unittest

from flowforge.engine.param_schema import check_node_type_schema, validate_params


class SchemaWellFormednessTestCase(unittest.TestCase):
    def test_a_valid_schema_passes_check(self) -> None:
        check_node_type_schema({"type": "object", "properties": {"x": {"type": "string"}}})  # no raise

    def test_an_invalid_schema_is_rejected(self) -> None:
        # "type" must be a string or array of strings, not an integer --
        # this is a structurally invalid Draft 2020-12 schema, not an
        # instance-validation failure.
        with self.assertRaises(ValueError):
            check_node_type_schema({"type": 12345})

    def test_rejection_error_is_a_plain_value_error_not_the_library_exception(self) -> None:
        try:
            check_node_type_schema({"type": 12345})
            self.fail("expected ValueError")
        except ValueError as exc:
            # Only a plain ValueError with a readable message leaks out;
            # the library's own SchemaError type must not be exposed.
            self.assertNotIn("SchemaError", type(exc).__name__)
            self.assertIsInstance(str(exc), str)
            self.assertTrue(len(str(exc)) > 0)

    def test_frozen_mapping_schema_is_accepted(self) -> None:
        # check_node_type_schema must work on a deep_freeze()d schema
        # (MappingProxyType-based), not only a plain dict -- this is
        # exactly how NodeTypeSpec.params_schema is stored.
        from flowforge.domain._immutable import deep_freeze

        frozen_schema = deep_freeze({"type": "object", "properties": {"x": {"type": "string"}}})
        check_node_type_schema(frozen_schema)  # no raise


class InstanceValidationTestCase(unittest.TestCase):
    def test_no_schema_accepts_any_params(self) -> None:
        self.assertEqual(validate_params(None, {"anything": "goes", "x": 1}), [])

    def test_required_property_missing(self) -> None:
        schema = {"type": "object", "required": ["url"]}
        errors = validate_params(schema, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("url", errors[0])

    def test_additional_properties_rejected(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False}
        errors = validate_params(schema, {"x": "ok", "bogus": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("bogus", errors[0])

    def test_nested_object_validation(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "filters": {
                    "type": "object",
                    "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                    "required": ["tags"],
                }
            },
            "required": ["filters"],
        }
        self.assertEqual(validate_params(schema, {"filters": {"tags": ["a", "b"]}}), [])
        errors = validate_params(schema, {"filters": {"tags": [1, 2]}})
        self.assertTrue(len(errors) >= 1)

    def test_array_items_validation(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertEqual(validate_params(schema, [1, 2, 3]), [])
        self.assertTrue(len(validate_params(schema, [1, "two", 3])) >= 1)

    def test_enum_constraint(self) -> None:
        schema = {"type": "object", "properties": {"method": {"enum": ["GET", "POST"]}}}
        self.assertEqual(validate_params(schema, {"method": "GET"}), [])
        self.assertTrue(len(validate_params(schema, {"method": "DELETE"})) >= 1)

    def test_numeric_constraints(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 10}}}
        self.assertEqual(validate_params(schema, {"n": 5}), [])
        self.assertTrue(len(validate_params(schema, {"n": 0})) >= 1)
        self.assertTrue(len(validate_params(schema, {"n": 11})) >= 1)

    def test_string_constraints(self) -> None:
        schema = {"type": "object", "properties": {"s": {"type": "string", "minLength": 2, "pattern": "^[a-z]+$"}}}
        self.assertEqual(validate_params(schema, {"s": "ok"}), [])
        self.assertTrue(len(validate_params(schema, {"s": "A"})) >= 1)  # too short AND wrong case
        self.assertTrue(len(validate_params(schema, {"s": "ABC"})) >= 1)  # pattern mismatch

    def test_composition_oneOf(self) -> None:
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(validate_params(schema, "ok"), [])
        self.assertEqual(validate_params(schema, 5), [])
        self.assertTrue(len(validate_params(schema, 5.5)) >= 1)

    def test_composition_anyOf(self) -> None:
        schema = {"type": "object", "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
        self.assertEqual(validate_params(schema, {"v": "x"}), [])
        self.assertEqual(validate_params(schema, {"v": None}), [])
        self.assertTrue(len(validate_params(schema, {"v": 5})) >= 1)

    def test_composition_allOf(self) -> None:
        schema = {"allOf": [{"type": "string"}, {"minLength": 3}]}
        self.assertEqual(validate_params(schema, "abcd"), [])
        self.assertTrue(len(validate_params(schema, "ab")) >= 1)

    def test_conditional_if_then_else(self) -> None:
        schema = {
            "type": "object",
            "properties": {"kind": {"enum": ["a", "b"]}},
            "if": {"properties": {"kind": {"const": "a"}}},
            "then": {"required": ["a_only_field"]},
            "else": {"required": ["b_only_field"]},
        }
        self.assertEqual(validate_params(schema, {"kind": "a", "a_only_field": 1}), [])
        self.assertTrue(len(validate_params(schema, {"kind": "a"})) >= 1)
        self.assertEqual(validate_params(schema, {"kind": "b", "b_only_field": 1}), [])

    def test_ref_within_defs(self) -> None:
        schema = {
            "$defs": {"posint": {"type": "integer", "minimum": 0}},
            "type": "object",
            "properties": {"n": {"$ref": "#/$defs/posint"}},
        }
        self.assertEqual(validate_params(schema, {"n": 5}), [])
        self.assertTrue(len(validate_params(schema, {"n": -1})) >= 1)

    def test_error_ordering_is_deterministic(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"],
        }
        first = validate_params(schema, {})
        second = validate_params(schema, {})
        self.assertEqual(first, second)

    def test_error_strings_do_not_expose_unstable_exception_repr(self) -> None:
        schema = {"type": "object", "required": ["x"]}
        errors = validate_params(schema, {})
        self.assertEqual(len(errors), 1)
        self.assertNotIn("ValidationError(", errors[0])
        self.assertNotIn("<jsonschema", errors[0])

    def test_frozen_mapping_params_are_accepted(self) -> None:
        # NodeDefinition.params is a MappingProxyType by the time it
        # reaches this function in real use -- it must work the same as
        # a plain dict.
        from flowforge.domain._immutable import deep_freeze

        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        frozen_params = deep_freeze({"x": "ok"})
        self.assertEqual(validate_params(schema, frozen_params), [])
        self.assertTrue(len(validate_params(schema, deep_freeze({}))) >= 1)


if __name__ == "__main__":
    unittest.main()
