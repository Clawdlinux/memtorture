"""Test-only evaluator for the JSON Schema keywords our wire schemas use."""

import json
import math

KEYWORDS = {
    "$schema",
    "$defs",
    "$ref",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "items",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "minimum",
    "minItems",
    "maxItems",
    "minLength",
}


def equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def matches(schema, value, path):
    assert not schema.keys() - KEYWORDS, "Extend the evaluator for new schema keywords"
    if "$ref" in schema:
        filename, pointer = schema["$ref"].split("#", 1)
        target_path = path.parent / filename if filename else path
        target = json.loads(target_path.read_text())
        for part in pointer.lstrip("/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        if not matches(target, value, target_path):
            return False
    for keyword, predicate in (
        ("anyOf", any),
        ("allOf", all),
        ("oneOf", lambda results: sum(results) == 1),
    ):
        if keyword in schema and not predicate(
            matches(child, value, path) for child in schema[keyword]
        ):
            return False
    if "not" in schema and matches(schema["not"], value, path):
        return False
    number = type(value) in (int, float) and math.isfinite(value)
    types = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": type(value) is bool,
        "null": value is None,
        "number": number,
        "integer": number and int(value) == value,
    }
    if "type" in schema and not types[schema["type"]]:
        return False
    if "const" in schema and not equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(equal(value, item) for item in schema["enum"]):
        return False
    if "minimum" in schema and number and value < schema["minimum"]:
        return False
    if isinstance(value, dict):
        if not set(schema.get("required", ())) <= value.keys():
            return False
        if not all(
            matches(child, value[key], path)
            for key, child in schema.get("properties", {}).items()
            if key in value
        ):
            return False
    if isinstance(value, list):
        if (
            not schema.get("minItems", 0)
            <= len(value)
            <= schema.get("maxItems", math.inf)
        ):
            return False
        if "items" in schema and not all(
            matches(schema["items"], v, path) for v in value
        ):
            return False
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        return False
    return True
