import re

import pytest
from jsonschema import Draft202012Validator

from connectorgen.validate import Synthesizer
from connectorgen.validate.synth import regex_example


def valid(schema, value, defs=None):
    full = {**schema, "$defs": defs} if defs else schema
    return not list(Draft202012Validator(full).iter_errors(value))


@pytest.mark.parametrize("schema", [
    {"type": "string", "format": "email"},
    {"type": "string", "format": "date-time"},
    {"type": "string", "minLength": 10},
    {"type": "string", "maxLength": 2},
    {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$"},
    {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
    {"type": "integer", "minimum": 5, "maximum": 10},
    {"type": "integer", "exclusiveMinimum": 0},
    {"type": "number", "maximum": 0.1},
    {"type": "integer", "minimum": 3, "multipleOf": 5},
    {"enum": [None, "a", "b"]},
    {"type": ["null", "boolean"]},
    {"type": "array", "items": {"type": "integer"}, "minItems": 2},
    {"type": "object", "minProperties": 1, "properties": {"a": {"type": "string"}}},
    {"type": "object", "required": ["x"], "properties": {"x": {"const": 7}}},
    {"allOf": [{"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
               {"required": ["b"], "properties": {"b": {"type": "integer"}}}]},
    {"type": "object", "required": ["kind"], "properties": {"kind": {"type": "string"}},
     "oneOf": [{"required": ["a"], "properties": {"a": {"type": "string"}}}]},
])
def test_examples_are_valid(schema):
    value = Synthesizer().example(schema)
    assert valid(schema, value), value


def test_required_only_vs_full():
    schema = {"type": "object", "required": ["id"],
              "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}
    assert Synthesizer().example(schema) == {"id": 1}
    assert Synthesizer(full=True).example(schema) == {"id": 1, "name": "name"}


def test_recursive_schema_terminates_and_is_valid():
    defs = {"Node": {"type": "object", "required": ["children"], "properties": {
        "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
        "parent": {"$ref": "#/$defs/Node"}}}}
    schema = {"$ref": "#/$defs/Node"}
    value = Synthesizer(defs, full=True).example(schema)
    assert valid(schema, value, defs)
    assert value["children"] == [{"children": []}] or value["children"] == []


def test_anyof_prefers_scalar_branch():
    # Stripe's expandable fields: either an id string or the full object
    schema = {"anyOf": [{"type": "object", "properties": {"id": {"type": "string"}}},
                        {"type": "string"}]}
    assert Synthesizer().example(schema) == "example"


def test_nullable_ref_picks_non_null():
    defs = {"A": {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}}
    schema = {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/A"}]}
    assert Synthesizer(defs).example(schema) == {"x": 1}


@pytest.mark.parametrize("pattern", [
    r"^sha256:[a-f0-9]{64}$",
    r"^refs/(heads|tags)/.+$",
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)$",
    r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$",
    r"^/v1/charges",
    r"^[A-Z]{3}$",
])
def test_regex_example_matches(pattern):
    value = regex_example(pattern)
    assert value is not None and re.search(pattern, value)


def test_regex_example_gives_up_cleanly():
    assert regex_example(r"(a)\1") is None  # backreference: unsupported
