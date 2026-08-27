"""Shared parsing and schema checks for untrusted Provider tool arguments.

This module deliberately keeps argument values out of diagnostics.  Tool
arguments are still returned to the runtime when they are valid; only the
diagnostic metadata is allowlisted and length based.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


MISSING = object()
_MAX_RAW_ARGUMENTS = 1_000_000
_MAX_ARGUMENT_DEPTH = 64
_MAX_SCHEMA_DEPTH = 32
_MAX_SCHEMA_NODES = 4096
_MAX_SCHEMA_ARRAY_ITEMS = 4096
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_PARSE_ERROR_KINDS = frozenset(
    {
        "missing_arguments",
        "empty_arguments",
        "invalid_arguments_type",
        "non_object",
        "arguments_too_large",
        "invalid_json",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedToolArguments:
    arguments: dict[str, Any]
    arguments_raw: str | None
    parse_error: dict[str, Any] | None


def _error(kind: str, message: str, *, exc: json.JSONDecodeError | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "message": message}
    if exc is not None:
        result.update({"line": exc.lineno, "column": exc.colno, "position": exc.pos})
    return result


def _raw_json_nesting_exceeds(raw: str, *, limit: int) -> bool:
    """Detect excessive JSON container nesting without invoking the JSON parser."""

    depth = 0
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth = max(0, depth - 1)
    return False


def parse_tool_arguments(value: Any = MISSING) -> ParsedToolArguments:
    """Parse one Provider value without treating missing data as ``{}``.

    A real object (including an explicit empty object) is valid JSON.  Missing,
    empty, non-object, and malformed values remain distinguishable through the
    parse diagnostic while the executable value is always a safe empty object.
    """

    if value is MISSING:
        return ParsedToolArguments(
            arguments={},
            arguments_raw=None,
            parse_error=_error("missing_arguments", "工具参数字段缺失"),
        )
    if isinstance(value, Mapping):
        arguments = dict(value)
        try:
            arguments_raw = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError, RecursionError):
            return ParsedToolArguments(
                arguments={},
                arguments_raw=None,
                parse_error=_error(
                    "arguments_too_large", "工具参数对象无法在安全边界内序列化"
                ),
            )
        if len(arguments_raw) > _MAX_RAW_ARGUMENTS:
            return ParsedToolArguments(
                arguments={},
                arguments_raw=None,
                parse_error=_error("arguments_too_large", "工具参数原始内容超过大小上限"),
            )
        return ParsedToolArguments(
            arguments=arguments,
            arguments_raw=arguments_raw,
            parse_error=None,
        )
    if isinstance(value, str):
        if len(value) > _MAX_RAW_ARGUMENTS:
            return ParsedToolArguments(
                arguments={},
                arguments_raw=None,
                parse_error=_error("arguments_too_large", "工具参数原始内容超过大小上限"),
            )
        arguments_raw = value
        if not arguments_raw.strip():
            return ParsedToolArguments(
                arguments={},
                arguments_raw=arguments_raw,
                parse_error=_error("empty_arguments", "工具参数字段为空"),
            )
    elif value is None:
        return ParsedToolArguments(
            arguments={},
            arguments_raw=None,
            parse_error=_error("invalid_arguments_type", "工具参数字段类型无效"),
        )
    else:
        arguments_raw = str(value)
        if len(arguments_raw) > _MAX_RAW_ARGUMENTS:
            return ParsedToolArguments(
                arguments={},
                arguments_raw=None,
                parse_error=_error("arguments_too_large", "工具参数原始内容超过大小上限"),
            )

    if _raw_json_nesting_exceeds(arguments_raw, limit=_MAX_ARGUMENT_DEPTH):
        return ParsedToolArguments(
            arguments={},
            arguments_raw=arguments_raw,
            parse_error=_error("invalid_json", "工具参数 JSON 嵌套层级超过解析上限"),
        )

    try:
        parsed = json.loads(arguments_raw)
    except RecursionError:
        return ParsedToolArguments(
            arguments={},
            arguments_raw=arguments_raw,
            parse_error=_error("invalid_json", "工具参数 JSON 嵌套层级超过解析上限"),
        )
    except json.JSONDecodeError as exc:
        return ParsedToolArguments(
            arguments={},
            arguments_raw=arguments_raw,
            parse_error=_error("invalid_json", "工具参数不是有效 JSON", exc=exc),
        )
    if not isinstance(parsed, dict):
        return ParsedToolArguments(
            arguments={},
            arguments_raw=arguments_raw,
            parse_error=_error("non_object", "工具参数 JSON 根节点必须是对象"),
        )
    return ParsedToolArguments(
        arguments=parsed,
        arguments_raw=arguments_raw,
        parse_error=None,
    )


def _safe_identifier(value: Any) -> str:
    text = str(value or "").strip()[:160]
    return text if text and _SAFE_IDENTIFIER_RE.fullmatch(text) else ""


def _safe_parse_error(value: Any) -> dict[str, str]:
    kind = str(value.get("kind") or "").strip().casefold() if isinstance(value, Mapping) else ""
    return {
        "kind": kind if kind in _PARSE_ERROR_KINDS else "invalid_json",
        "message": "工具参数解析失败",
    }


def _matches_type(value: Any, expected: Any) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for item in expected_types:
        if item == "object" and isinstance(value, dict):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "null" and value is None:
            return True
    return expected is None


def _schema_errors(
    value: Any,
    schema: Mapping[str, Any],
    path: str = "arguments",
    *,
    _depth: int = 0,
    _state: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Return safe, field-oriented errors for the subset used by plugins."""

    state = _state if _state is not None else [0]
    state[0] += 1
    if _depth > _MAX_SCHEMA_DEPTH or state[0] > _MAX_SCHEMA_NODES:
        return [
            {
                "kind": "schema_limit",
                "path": path,
            }
        ]
    errors: list[dict[str, Any]] = []
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        if not any(
            isinstance(candidate, Mapping)
            and not _schema_errors(
                value,
                candidate,
                path,
                _depth=_depth + 1,
                _state=state,
            )
            for candidate in any_of
        ):
            errors.append({"kind": "schema", "path": path})
        return errors
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        matches = sum(
            1
            for candidate in one_of
            if isinstance(candidate, Mapping)
            and not _schema_errors(
                value,
                candidate,
                path,
                _depth=_depth + 1,
                _state=state,
            )
        )
        if matches != 1:
            errors.append({"kind": "schema", "path": path})
        return errors

    if not _matches_type(value, schema.get("type")):
        errors.append({"kind": "type", "path": path})
        return errors
    if "enum" in schema and isinstance(schema.get("enum"), list):
        if value not in schema["enum"]:
            errors.append({"kind": "enum", "path": path})
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append({"kind": "min_length", "path": path})
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append({"kind": "max_length", "path": path})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append({"kind": "minimum", "path": path})
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append({"kind": "maximum", "path": path})
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [
            str(name)
            for name in required
            if isinstance(name, str) and name not in value
        ]
        if missing:
            errors.append({"kind": "missing_required", "fields": missing[:64]})
        if schema.get("additionalProperties") is False:
            extras = [str(name) for name in value if name not in properties]
            if extras:
                errors.append({"kind": "additional_properties", "fields": extras[:64]})
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, Mapping):
                errors.extend(
                    _schema_errors(
                        child,
                        child_schema,
                        f"{path}.{name}",
                        _depth=_depth + 1,
                        _state=state,
                    )[:64]
                )
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        if len(value) > _MAX_SCHEMA_ARRAY_ITEMS:
            errors.append(
                {
                    "kind": "array_too_large",
                    "path": path,
                    "max_items": _MAX_SCHEMA_ARRAY_ITEMS,
                }
            )
        for index, item in enumerate(value[:_MAX_SCHEMA_ARRAY_ITEMS]):
            errors.extend(
                _schema_errors(
                    item,
                    schema["items"],
                    f"{path}[{index}]",
                    _depth=_depth + 1,
                    _state=state,
                )[:64]
            )
    for error in errors:
        if error.get("kind") == "schema_limit":
            return [error]
    return errors[:64]


def validate_tool_call_output(output: Sequence[Any], tools: Sequence[Any]) -> list[dict[str, Any]]:
    """Validate all output tool calls and return only safe diagnostics."""

    from core.models import ToolCallItem

    schemas: dict[str, Mapping[str, Any]] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        parameters = getattr(tool, "parameters", None)
        if name and isinstance(parameters, Mapping):
            schemas[name] = parameters

    invalid: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, ToolCallItem):
            continue
        safe_id = _safe_identifier(item.call_id)
        safe_name = _safe_identifier(item.name)
        diagnostic: dict[str, Any] = {
            **({"call_id": safe_id} if safe_id else {}),
            **({"name": safe_name} if safe_name else {}),
            "arguments_diagnostic": {
                "available": item.arguments_raw is not None,
                "length": len(item.arguments_raw or ""),
                "content_omitted": True,
                "json_root_expected": "object",
            },
        }
        if item.parse_error is not None:
            diagnostic["parse_error"] = _safe_parse_error(item.parse_error)
        else:
            schema = schemas.get(item.name)
            if schema is None:
                diagnostic["validation_error"] = {"kind": "unknown_tool"}
            else:
                errors = _schema_errors(item.arguments, schema)
                if errors:
                    diagnostic["validation_error"] = {"errors": errors}
        if "parse_error" in diagnostic or "validation_error" in diagnostic:
            invalid.append(diagnostic)
    return invalid


__all__ = ["MISSING", "ParsedToolArguments", "parse_tool_arguments", "validate_tool_call_output"]
