"""Pinned copy of the Platform Model Router's response-schema subset and context estimate.

Source: bezalel-deploy commit 4bcf4ae99dfb0a44e6382c79ae6938ccc4474770,
services/platform-model-router/model_router/service.py (file last changed in
4ddd03e4abeceab229c7d8569b35609933eba47d). Lines 702-864 (`_SCHEMA_TYPES`,
`_schema_types`, `validate_schema`, `validate_instance`, `estimate_tokens`) are
copied verbatim between the PINNED markers; tests/test_router_fake.py checks
their SHA-256. `provider_messages` (lines 867-892) follows with `task.messages`
and `task.response_schema` passed as arguments. `RouterError` (service.py lines
17-21) and `canonical` (store.py line 18) are reproduced so the copy imports
nothing from the router. When the router's validator changes, re-copy the block,
update the commit, line numbers and hash, and never edit the copied bodies.
"""

from __future__ import annotations

import json
from typing import Any


class RouterError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- BEGIN PINNED bezalel-deploy 4bcf4ae services/platform-model-router/model_router/service.py:702-864 ---
_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


def _schema_types(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        types = [value]
    elif isinstance(value, list):
        types = value
    else:
        raise RouterError("MODEL_TASK_INVALID", "response_schema type is unsupported")
    if (
        not types
        or any(not isinstance(item, str) or item not in _SCHEMA_TYPES for item in types)
        or len(set(types)) != len(types)
    ):
        raise RouterError("MODEL_TASK_INVALID", "response_schema type is unsupported")
    return frozenset(types)


def validate_schema(schema: Any, *, depth: int = 0) -> None:
    if depth > 8 or not isinstance(schema, dict):
        raise RouterError("MODEL_TASK_INVALID", "response_schema is invalid or too deeply nested")
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "multipleOf",
        "description",
    }
    if set(schema) - allowed:
        raise RouterError("MODEL_TASK_INVALID", "response_schema contains unsupported keywords")
    schema_types = _schema_types(schema.get("type"))
    if "description" in schema and (
        not isinstance(schema["description"], str) or len(schema["description"]) > 4_096
    ):
        raise RouterError("MODEL_TASK_INVALID", "response_schema description is invalid")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"] or len(schema["enum"]) > 100):
        raise RouterError("MODEL_TASK_INVALID", "response_schema enum is invalid")
    if "multipleOf" in schema:
        multiple_of = schema["multipleOf"]
        if (
            not isinstance(multiple_of, (int, float))
            or isinstance(multiple_of, bool)
            or multiple_of <= 0
        ):
            raise RouterError("MODEL_TASK_INVALID", "response_schema multipleOf is invalid")
    if "object" in schema_types:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or len(properties) > 100
            or not isinstance(required, list)
            or len(required) > 100
            or any(not isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
        ):
            raise RouterError("MODEL_TASK_INVALID", "response_schema object contract is invalid")
        if any(not isinstance(key, str) or len(key) > 100 for key in properties) or any(item not in properties for item in required):
            raise RouterError("MODEL_TASK_INVALID", "response_schema required fields are invalid")
        additional_properties = schema.get("additionalProperties", False)
        if additional_properties is not False and not isinstance(additional_properties, dict):
            raise RouterError("MODEL_TASK_INVALID", "response_schema must reject extra object fields")
        for child in properties.values():
            validate_schema(child, depth=depth + 1)
        if isinstance(additional_properties, dict):
            validate_schema(additional_properties, depth=depth + 1)
    if "array" in schema_types:
        if "items" not in schema:
            raise RouterError("MODEL_TASK_INVALID", "response_schema arrays require items")
        for keyword in ("minItems", "maxItems"):
            if keyword in schema and (
                not isinstance(schema[keyword], int)
                or isinstance(schema[keyword], bool)
                or not 0 <= schema[keyword] <= 10_000
            ):
                raise RouterError(
                    "MODEL_TASK_INVALID", f"response_schema {keyword} is invalid"
                )
        if schema.get("minItems", 0) > schema.get("maxItems", 10_000):
            raise RouterError("MODEL_TASK_INVALID", "response_schema array bounds are invalid")
        validate_schema(schema["items"], depth=depth + 1)
    if "string" in schema_types:
        for keyword in ("minLength", "maxLength"):
            if keyword in schema and (
                not isinstance(schema[keyword], int)
                or isinstance(schema[keyword], bool)
                or not 0 <= schema[keyword] <= 1_000_000
            ):
                raise RouterError("MODEL_TASK_INVALID", f"response_schema {keyword} is invalid")
        if schema.get("minLength", 0) > schema.get("maxLength", 1_000_000):
            raise RouterError("MODEL_TASK_INVALID", "response_schema string bounds are invalid")
    if {"integer", "number"} & schema_types:
        for keyword in ("minimum", "maximum"):
            if keyword in schema and (
                not isinstance(schema[keyword], (int, float))
                or isinstance(schema[keyword], bool)
            ):
                raise RouterError("MODEL_TASK_INVALID", f"response_schema {keyword} is invalid")
        if schema.get("minimum", float("-inf")) > schema.get("maximum", float("inf")):
            raise RouterError("MODEL_TASK_INVALID", "response_schema numeric bounds are invalid")


def validate_instance(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    expected = _schema_types(schema["type"])
    matches = any(
        {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }[schema_type]
        for schema_type in expected
    )
    if not matches:
        raise RouterError("MODEL_OUTPUT_INVALID", f"{path} has the wrong JSON type", status=502)
    if "enum" in schema and value not in schema["enum"]:
        raise RouterError("MODEL_OUTPUT_INVALID", f"{path} is outside the allowed enum", status=502)
    if "object" in expected and isinstance(value, dict):
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})
        if not required <= set(value):
            raise RouterError("MODEL_OUTPUT_INVALID", f"{path} is missing required fields", status=502)
        additional_properties = schema.get("additionalProperties", False)
        if additional_properties is False and set(value) - set(properties):
            raise RouterError("MODEL_OUTPUT_INVALID", f"{path} contains extra fields", status=502)
        for key, child in value.items():
            if key in properties:
                validate_instance(child, properties[key], path=f"{path}.{key}")
            elif isinstance(additional_properties, dict):
                validate_instance(child, additional_properties, path=f"{path}.{key}")
    elif "array" in expected and isinstance(value, list):
        if (
            len(value) < schema.get("minItems", 0)
            or len(value) > schema.get("maxItems", 10_000)
        ):
            raise RouterError("MODEL_OUTPUT_INVALID", f"{path} has an invalid length", status=502)
        for index, child in enumerate(value):
            validate_instance(child, schema["items"], path=f"{path}[{index}]")
    elif "string" in expected and isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            raise RouterError("MODEL_OUTPUT_INVALID", f"{path} has an invalid length", status=502)
    elif {"integer", "number"} & expected and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise RouterError("MODEL_OUTPUT_INVALID", f"{path} is outside numeric bounds", status=502)


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    return max(1, sum(len(item["content"]) for item in messages) // 4 + len(messages) * 4)
# --- END PINNED ---


def provider_messages(messages: list[dict[str, str]], response_schema: Any) -> list[dict[str, str]]:
    """service.py provider_messages with the task's messages and schema as arguments."""
    if response_schema is None:
        return list(messages)
    schema = canonical(response_schema)
    schema_contract = (
        "Return exactly one JSON value that validates against the following "
        "JSON Schema. Do not include Markdown, code fences, comments, or "
        f"explanatory text. JSON Schema: {schema}"
    )
    messages = list(messages)
    if messages and messages[0]["role"] == "system":
        return [
            {
                "role": "system",
                "content": f"{schema_contract}\n\n{messages[0]['content']}",
            },
            *messages[1:],
        ]
    return [
        {
            "role": "system",
            "content": schema_contract,
        },
        *messages,
    ]
