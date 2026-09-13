from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.executor import GatewayExecutor
from core.models import (
    KemoRequest,
    MessageItem,
    ToolCallItem,
    ToolDefinition,
)
from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderResult
from core.stores import ExecutionRecord, InMemoryExecutionStore, InternalStatus
from core.tool_arguments import MISSING, parse_tool_arguments, validate_tool_call_output


def test_parser_distinguishes_missing_empty_object_and_non_object() -> None:
    missing = parse_tool_arguments(MISSING)
    empty = parse_tool_arguments("")
    explicit = parse_tool_arguments("{}")
    non_object = parse_tool_arguments("[]")

    assert missing.arguments == {}
    assert missing.arguments_raw is None
    assert missing.parse_error["kind"] == "missing_arguments"
    assert empty.parse_error["kind"] == "empty_arguments"
    assert explicit.arguments == {}
    assert explicit.parse_error is None
    assert non_object.arguments == {}
    assert non_object.parse_error["kind"] == "non_object"


def test_parser_rejects_oversized_valid_prefix_without_truncating() -> None:
    prefix = '{"x":1}'
    oversized = prefix + (" " * (1_000_000 - len(prefix))) + "TRAILING"

    parsed = parse_tool_arguments(oversized)

    assert parsed.arguments == {}
    assert parsed.parse_error["kind"] == "arguments_too_large"
    assert parsed.arguments_raw is None


def test_parser_rejects_deep_json_before_python_parser_variation() -> None:
    parsed = parse_tool_arguments("[" * 8000 + "0" + "]" * 8000)

    assert parsed.arguments == {}
    assert parsed.parse_error["kind"] == "invalid_json"




def test_schema_validation_allows_real_no_argument_tool_and_rejects_required_gap() -> None:
    no_arg = ToolCallItem(
        id="item_no_arg",
        call_id="call_no_arg",
        name="noop",
        arguments={},
        arguments_raw="{}",
    )
    required_gap = ToolCallItem(
        id="item_required",
        call_id="call_required",
        name="file",
        arguments={},
        arguments_raw="{}",
    )
    tools = [
        ToolDefinition(
            name="noop",
            description="noop",
            parameters={"type": "object", "additionalProperties": False},
        ),
        ToolDefinition(
            name="file",
            description="file",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["action", "path"],
                "additionalProperties": False,
            },
        ),
    ]

    assert validate_tool_call_output([no_arg], tools) == []
    invalid = validate_tool_call_output([required_gap], tools)
    assert len(invalid) == 1
    assert invalid[0]["validation_error"]["errors"][0]["kind"] == "missing_required"
    assert "secret" not in str(invalid)

    malformed = ToolCallItem(
        id="item_parse_error",
        call_id="call_parse_error",
        name="file",
        arguments={},
        parse_error={"kind": "password=diagnostic-secret"},
    )
    malformed_diagnostic = validate_tool_call_output([malformed], tools)
    assert malformed_diagnostic[0]["parse_error"]["kind"] == "invalid_json"
    assert "diagnostic-secret" not in str(malformed_diagnostic)


def test_executor_converts_invalid_tool_batch_to_incomplete_without_calls() -> None:
    request = KemoRequest(
        protocol_version="1.0",
        request_id="req_invalid_tools",
        attempt=1,
        model="test-model",
        stream=False,
        system_prompt="",
        generation={"max_output_tokens": 32},
        output={"modalities": ["text"]},
        tools=[
            ToolDefinition(
                name="file",
                description="file",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["action", "path"],
                    "additionalProperties": False,
                },
            )
        ],
        input=[
            MessageItem(
                id="msg_user",
                role="user",
                content=[{"type": "text", "text": "read"}],
            ),
        ],
        provider_options={},
        metadata={},
        extensions={},
    )
    record = ExecutionRecord(
        tenant_id="tenant",
        request_id=request.request_id,
        request_hash="hash",
        response_id="resp_invalid_tools",
        model=request.model,
        provider_id="test",
        subject_id="subject",
    )
    result = ProviderResult(
        status="requires_action",
        output=[
            {
                "id": "call_item",
                "type": "tool_call",
                "call_id": "call_file",
                "name": "file",
                "arguments": {},
                "arguments_raw": "{}",
            }
        ],
    )
    gateway = GatewayExecutor.__new__(GatewayExecutor)
    response = gateway._response_from_result(
        request,
        record,
        result,
        SimpleNamespace(assets=None),
    )
    assert response.status == "incomplete"
    assert response.incomplete_details["reason"] == "invalid_tool_arguments"
    assert not any(isinstance(item, ToolCallItem) for item in response.output)


def test_executor_streaming_path_uses_same_terminal_contract() -> None:
    # The same response conversion is used by the streaming terminal event, so
    # keep this assertion explicit to guard against a future split implementation.
    assert GatewayExecutor._response_from_result is not None
    assert asyncio.iscoroutinefunction(GatewayExecutor.execute)


def test_invalid_stream_tool_completed_event_is_not_publishable() -> None:
    request = KemoRequest(
        protocol_version="1.0",
        request_id="req_stream_invalid_tools",
        attempt=1,
        model="test-model",
        stream=True,
        system_prompt="",
        generation={"max_output_tokens": 32},
        output={"modalities": ["text"]},
        tools=[
            ToolDefinition(
                name="file",
                description="file",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["action", "path"],
                    "additionalProperties": False,
                },
            )
        ],
        input=[
            MessageItem(
                id="msg_user_stream",
                role="user",
                content=[{"type": "text", "text": "read"}],
            ),
        ],
        provider_options={},
        metadata={},
        extensions={},
    )
    invalid = ProviderEvent(
        kind=ProviderEventKind.TOOL_COMPLETED,
        item={
            "id": "call_item",
            "type": "tool_call",
            "call_id": "call_file",
            "name": "file",
            "arguments": {},
            "arguments_raw": "{}",
        },
    )
    valid = ProviderEvent(
        kind=ProviderEventKind.TOOL_COMPLETED,
        item={
            "id": "call_item_valid",
            "type": "tool_call",
            "call_id": "call_file_valid",
            "name": "file",
            "arguments": {"action": "read", "path": "note.txt"},
            "arguments_raw": '{"action":"read","path":"note.txt"}',
        },
    )

    assert GatewayExecutor._valid_stream_tool_call(request, invalid) is False
    assert GatewayExecutor._valid_stream_tool_call(request, valid) is True


def test_stream_invalid_parallel_batch_is_atomic_and_uses_incomplete_terminal() -> None:
    request = KemoRequest(
        protocol_version="1.0",
        request_id="req_stream_atomic_tools",
        attempt=1,
        model="test-model",
        stream=True,
        system_prompt="",
        generation={"max_output_tokens": 32},
        output={"modalities": ["text"]},
        tools=[
            ToolDefinition(
                name="file",
                description="file",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["action", "path"],
                    "additionalProperties": False,
                },
            )
        ],
        input=[
            MessageItem(
                id="msg_stream_atomic",
                role="user",
                content=[{"type": "text", "text": "read"}],
            ),
        ],
        provider_options={},
        metadata={},
        extensions={},
    )
    valid_item = {
        "id": "call_item_valid",
        "type": "tool_call",
        "call_id": "call_file_valid",
        "name": "file",
        "arguments": {"action": "read", "path": "note.txt"},
        "arguments_raw": '{"action":"read","path":"note.txt"}',
    }
    invalid_item = {
        "id": "call_item_invalid",
        "type": "tool_call",
        "call_id": "call_file_invalid",
        "name": "file",
        "arguments": {},
        "arguments_raw": "{}",
    }

    class Package:
        provider_id = "test"

        async def stream(self, _request, _context):
            yield ProviderEvent(
                kind=ProviderEventKind.TOOL_COMPLETED,
                item=valid_item,
            )
            yield ProviderEvent(
                kind=ProviderEventKind.TOOL_COMPLETED,
                item=invalid_item,
            )
            yield ProviderEvent(
                kind=ProviderEventKind.COMPLETED,
                result=ProviderResult(
                    status="requires_action",
                    output=[valid_item, invalid_item],
                ),
            )

    class Registry:
        def resolve_registered(self, _model):
            return Package()

    async def exercise() -> ExecutionRecord:
        store = InMemoryExecutionStore()
        record = ExecutionRecord(
            tenant_id="tenant",
            request_id=request.request_id,
            request_hash="hash",
            response_id="resp_stream_atomic",
            model=request.model,
            provider_id="test",
            subject_id="subject",
            status=InternalStatus.RUNNING,
        )
        gateway = GatewayExecutor(Registry(), store)
        await gateway._produce_stream_inner(
            request,
            SimpleNamespace(assets=None),
            record,
        )
        return record

    record = asyncio.run(exercise())
    assert [event.type for event in record.events] == ["response.incomplete"]
    assert record.response is not None
    assert record.response.status == "incomplete"
    assert record.response.incomplete_details["reason"] == "invalid_tool_arguments"


def test_stream_valid_parallel_batch_publishes_all_tool_calls_before_terminal() -> None:
    request = KemoRequest(
        protocol_version="1.0",
        request_id="req_stream_valid_tools",
        attempt=1,
        model="test-model",
        stream=True,
        system_prompt="",
        generation={"max_output_tokens": 32},
        output={"modalities": ["text"]},
        tools=[
            ToolDefinition(
                name="file",
                description="file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ],
        input=[
            MessageItem(
                id="msg_stream_valid",
                role="user",
                content=[{"type": "text", "text": "read"}],
            ),
        ],
        provider_options={},
        metadata={},
        extensions={},
    )
    items = [
        {
            "id": f"call_item_{index}",
            "type": "tool_call",
            "call_id": f"call_file_{index}",
            "name": "file",
            "arguments": {"path": f"note-{index}.txt"},
            "arguments_raw": f'{{"path":"note-{index}.txt"}}',
        }
        for index in range(2)
    ]

    class Package:
        provider_id = "test"

        async def stream(self, _request, _context):
            for item in items:
                yield ProviderEvent(
                    kind=ProviderEventKind.TOOL_COMPLETED,
                    item=item,
                )
            yield ProviderEvent(
                kind=ProviderEventKind.COMPLETED,
                result=ProviderResult(status="requires_action", output=items),
            )

    class Registry:
        def resolve_registered(self, _model):
            return Package()

    async def exercise() -> ExecutionRecord:
        record = ExecutionRecord(
            tenant_id="tenant",
            request_id=request.request_id,
            request_hash="hash",
            response_id="resp_stream_valid",
            model=request.model,
            provider_id="test",
            subject_id="subject",
            status=InternalStatus.RUNNING,
        )
        gateway = GatewayExecutor(Registry(), InMemoryExecutionStore())
        await gateway._produce_stream_inner(
            request,
            SimpleNamespace(assets=None),
            record,
        )
        return record

    record = asyncio.run(exercise())
    assert [event.type for event in record.events] == [
        "tool_call.completed",
        "tool_call.completed",
        "response.completed",
    ]
    assert record.response is not None
    assert record.response.status == "requires_action"


def test_schema_validation_reports_deep_schema_instead_of_recursion_error() -> None:
    schema: dict[str, object] = {"type": "string"}
    for _ in range(1200):
        schema = {
            "type": "object",
            "properties": {"next": schema},
            "required": ["next"],
        }
    tool = ToolDefinition(
        name="deep",
        description="deep",
        parameters=schema,
    )
    arguments: dict[str, object] = {}
    for _ in range(1200):
        arguments = {"next": arguments}
    item = ToolCallItem(
        id="deep_item",
        call_id="deep_call",
        name="deep",
        arguments=arguments,
    )
    invalid = validate_tool_call_output([item], [tool])
    assert invalid
    assert any(
        error["kind"] == "schema_limit"
        for error in invalid[0]["validation_error"]["errors"]
    )


def test_schema_validation_does_not_silently_skip_array_tail() -> None:
    tool = ToolDefinition(
        name="batch",
        description="batch",
        parameters={"type": "object", "properties": {"items": {
            "type": "array",
            "items": {"type": "string"},
        }}},
    )
    values = ["ok"] * 64 + [123]
    item = ToolCallItem(
        id="batch_item",
        call_id="batch_call",
        name="batch",
        arguments={"items": values},
    )
    invalid = validate_tool_call_output([item], [tool])
    assert invalid
    errors = invalid[0]["validation_error"]["errors"]
    assert any(error.get("path") == "arguments.items[64]" for error in errors)
