"""可选：针对本地未发布厂商的适配器回归，不属于源码默认测试集。"""
from __future__ import annotations
import asyncio
from importlib import import_module
from types import SimpleNamespace
import pytest

def _provider_components(provider: str) -> SimpleNamespace:
    try:
        package = f"providers.{provider}"
        return SimpleNamespace(
            errors=import_module(f"{package}.errors"),
            protocol=import_module(f"{package}.protocol"),
            streaming=import_module(f"{package}.streaming"),
            usage=import_module(f"{package}.usage"),
        )
    except ModuleNotFoundError as exc:
        missing = str(exc.name or "")
        if missing == "providers" or missing.startswith("providers."):
            pytest.skip(f"部署端 Provider 包 {provider} 未随源码仓库提交")
        raise


@pytest.mark.parametrize("provider", ["codexmanager", "opencode", "deepseek"])
@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [(None, "missing_arguments"), ("", "empty_arguments"), ("{}", None), ("[]", "non_object")],
)
def test_provider_protocol_mappers_preserve_argument_state(
    provider: str,
    arguments: str | None,
    expected_error: str | None,
) -> None:
    components = _provider_components(provider)
    if provider == "codexmanager":
        mapper = components.protocol.CodexManagerProtocolMapper(
            components.usage.CodexManagerUsageMapper(),
            components.errors.CodexManagerErrorMapper(),
            provider_id=provider,
        )
        item: dict[str, object] = {
            "id": "item",
            "call_id": "call",
            "name": "file",
        }
        if arguments is not None:
            item["arguments"] = arguments
        result = mapper.build_tool_call_item(item, index=0)
    elif provider == "opencode":
        mapper = components.protocol.OpenCodeProtocolMapper(
            components.usage.OpenCodeUsageMapper(),
            components.errors.OpenCodeErrorMapper(),
            provider_id=provider,
        )
        function: dict[str, object] = {"name": "file"}
        if arguments is not None:
            function["arguments"] = arguments
        result = mapper.build_tool_call_item(
            {"id": "call", "function": function}, item_id="item"
        )
    else:
        mapper = components.protocol.DeepSeekProtocolMapper(
            components.usage.DeepSeekUsageMapper(), components.errors.DeepSeekErrorMapper()
        )
        function = {"name": "file"}
        if arguments is not None:
            function["arguments"] = arguments
        result = mapper.build_tool_call_item(
            {"id": "call", "function": function}, item_id="item"
        )

    assert result["arguments"] == {}
    if expected_error is None:
        assert result.get("parse_error") is None
    else:
        assert result["parse_error"]["kind"] == expected_error


@pytest.mark.parametrize("provider", ["codexmanager", "opencode", "deepseek"])
def test_provider_stream_mappers_mark_missing_arguments_without_cross_request_state(
    provider: str,
) -> None:
    components = _provider_components(provider)

    async def source() -> object:
        if provider == "codexmanager":
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "item",
                    "type": "function_call",
                    "call_id": "call",
                    "name": "file",
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "item",
                    "type": "function_call",
                    "call_id": "call",
                    "name": "file",
                },
            }
            yield {
                "type": "response.completed",
                "response": {"id": "resp", "status": "completed", "output": []},
            }
        else:
            yield {
                "id": "resp",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call", "function": {"name": "file"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield {
                "id": "resp",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            }

    async def collect() -> list[object]:
        if provider == "codexmanager":
            protocol = components.protocol.CodexManagerProtocolMapper(
                components.usage.CodexManagerUsageMapper(),
                components.errors.CodexManagerErrorMapper(),
                provider_id=provider,
            )
            mapper = components.streaming.CodexManagerStreamMapper(
                components.usage.CodexManagerUsageMapper(),
                protocol,
                components.errors.CodexManagerErrorMapper(),
            )
        elif provider == "opencode":
            usage = components.usage.OpenCodeUsageMapper()
            protocol = components.protocol.OpenCodeProtocolMapper(
                usage, components.errors.OpenCodeErrorMapper(), provider_id=provider
            )
            mapper = components.streaming.OpenCodeStreamMapper(
                usage, protocol, components.errors.OpenCodeErrorMapper()
            )
        else:
            usage = components.usage.DeepSeekUsageMapper()
            protocol = components.protocol.DeepSeekProtocolMapper(
                usage, components.errors.DeepSeekErrorMapper()
            )
            mapper = components.streaming.DeepSeekStreamMapper(
                usage, protocol, components.errors.DeepSeekErrorMapper()
            )
        return [event async for event in mapper.convert(source())]  # type: ignore[arg-type]

    events = asyncio.run(collect())
    completed = next(event for event in events if getattr(event, "item", None))
    assert completed.item["parse_error"]["kind"] == "missing_arguments"


def test_opencode_stream_argument_buffers_are_request_local() -> None:
    components = _provider_components("opencode")
    usage = components.usage.OpenCodeUsageMapper()
    protocol = components.protocol.OpenCodeProtocolMapper(
        usage, components.errors.OpenCodeErrorMapper(), provider_id="opencode"
    )
    mapper = components.streaming.OpenCodeStreamMapper(
        usage, protocol, components.errors.OpenCodeErrorMapper()
    )

    async def source(value: str):
        yield {
            "id": f"resp-{value}",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": f"call-{value}",
                        "function": {"name": "lookup", "arguments": '{"value":"'},
                    }]
                },
                "finish_reason": None,
            }],
        }
        yield {
            "id": f"resp-{value}",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": f"{value}" + '"}'},
                    }]
                },
                "finish_reason": "tool_calls",
            }],
        }

    async def collect(value: str):
        return [event async for event in mapper.convert(source(value))]

    async def scenario():
        return await asyncio.gather(collect("A"), collect("B"))

    first, second = asyncio.run(scenario())
    first_item = next(event.item for event in first if getattr(event, "item", None))
    second_item = next(event.item for event in second if getattr(event, "item", None))
    assert first_item["arguments"] == {"value": "A"}
    assert second_item["arguments"] == {"value": "B"}
