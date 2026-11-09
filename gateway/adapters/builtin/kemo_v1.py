"""Archived Kemo 1.0 wire conversion, confined to the upstream boundary."""
from __future__ import annotations

from copy import deepcopy

from core.models import ErrorObject, KemoRequest, ProviderStreamEvent
from gateway.adapters.builtin.common import unsupported
from gateway.adapters.builtin.kemo_v2 import KemoV2, bad_response


ITEM_PREFIXES = {"tool_call": ("tool_call_", "call_"),
                 "tool_result": ("tool_result_", "result_"), "reasoning": ("reasoning_", "rs_")}


def identifier(value, old, new):
    if not isinstance(value, str) or not value.startswith(old):
        raise bad_response("KEMO_V1_INVALID_IDENTIFIER")
    result = new + value[len(old):]
    if len(result) > 128:
        raise unsupported("Kemo 1.0 标识符转换后超过 128 字符")
    return result


def item(value, *, outbound=False):
    value = deepcopy(value)
    prefixes = ITEM_PREFIXES.get(value["type"])
    if prefixes:
        old, new = reversed(prefixes) if outbound else prefixes
        value["id"] = identifier(value["id"], old, new)
    if value["type"] in {"tool_call", "tool_result"}:
        old, new = ("callid_", "call_") if outbound else ("call_", "callid_")
        value["call_id"] = identifier(value["call_id"], old, new)
    if outbound:
        value.update(metadata={}, extensions={})
    else:
        value.pop("metadata", None)
        value.pop("extensions", None)
    return value


class LegacySequenceGuard:
    """1.0 has sequence/identity guarantees but no item-added/text-done frames."""
    def __init__(self):
        self.identity, self.sequence, self.terminal = None, -1, False
        self.events = {}

    def accept(self, event):
        identity = event.request_id, event.response_id
        payload = event.model_dump_json()
        if self.identity is not None and identity != self.identity:
            raise bad_response("KEMO_RESPONSE_IDENTITY_MISMATCH")
        if event.event_id in self.events:
            if self.events[event.event_id] != payload:
                raise bad_response("KEMO_EVENT_REPLAY_CONFLICT")
            return False
        if self.terminal or event.sequence != self.sequence + 1:
            raise bad_response("KEMO_INVALID_STREAM_SEQUENCE")
        self.identity, self.sequence, self.terminal = identity, event.sequence, event.terminal
        self.events[event.event_id] = payload
        return True


class KemoV1(KemoV2):
    version = "1.0"
    manifest = KemoV2.manifest.model_copy(update={
        "id": "kemo-v1", "name": "Kemo Protocol 1.0",
        "multiple_choices": False, "structured_output": False, "prediction": False,
        "service_tiers": False, "logprobs": False, "verbosity": False,
        "reasoning_returns": ("none", "auto", "summary"),
        "usage_semantics": "Kemo 1.0 usage; unknown values remain unknown",
    })

    def encode(self, request, context, model, upstream):
        if isinstance(request, KemoRequest):
            if (request.structured_output is not None or request.prediction is not None
                    or any(value is not None for value in request.service.model_dump().values())
                    or request.generation.n != 1 or request.generation.logprobs is not None
                    or request.generation.verbosity is not None
                    or request.tool_choice.mode not in {"auto", "none"}
                    or request.tool_choice.mode == "none" and request.tools
                    or request.reasoning is not None and request.reasoning.return_mode == "content"):
                raise unsupported("Kemo 1.0 无法表达所请求的工具选择、结构化输出或高级生成选项")
        value = super().encode(request, context, model, upstream)
        if isinstance(request, KemoRequest):
            for field in ("tool_choice", "structured_output", "prediction", "service"):
                value.pop(field, None)
            generation = value["generation"]
            generation["parallel_tool_calls"] = value.pop("parallel_tool_calls")
            for field in ("n", "logprobs", "verbosity"):
                generation.pop(field, None)
            value["input"] = [item(entry, outbound=True) for entry in value["input"]]
        return value

    def error(self, value):
        value = deepcopy(value)
        codes = {entry.value for entry in ErrorObject.model_fields["code"].annotation}
        if value.get("code") not in codes:
            original = value["code"]
            value.update(code="UNKNOWN_ERROR", details={**value.get("details", {}), "original_code": original})
        return ErrorObject.model_validate(value)

    def decode(self, value):
        value = deepcopy(value)
        capability = "task" in value and "model" in value
        if value.get("protocol_version", "1.0" if capability else None) != "1.0":
            raise bad_response("KEMO_PROTOCOL_VERSION_MISMATCH")
        value["protocol_version"] = "2.0"
        if "output" in value:
            value["output"] = [item(entry) for entry in value["output"]]
        if value.get("error"):
            value["error"] = self.error(value["error"]).model_dump(mode="json")
        if value.get("object") == "kemo.model_list":
            value["supported_protocol_versions"] = ["2.0"]
        if capability:
            metadata = value.get("metadata", {})
            value["provider_id"] = metadata["provider_id"]
            value["provider_model"] = metadata["upstream_model"]
            reasoning = value.get("reasoning", {})
            reasoning["returns"] = ["none", "auto"] + (["summary"] if reasoning.get("summary") else [])
            value["reasoning"] = reasoning
            # An upstream claim is exposed only when this converter can express it.
            value.update(structured_output={"supported": False}, supports_multiple_choices=False,
                         supports_prediction=False, service_tiers=[])
            value.setdefault("tools", {})["tool_choice_modes"] = ["auto"]
            value.setdefault("decoding", {}).update(logprobs=False, verbosity=False, top_logprobs_max=0)
        return value

    def event(self, value):
        value = deepcopy(value)
        if value.pop("data", {}) != {}:
            raise bad_response("KEMO_V1_UNSUPPORTED_EVENT_DATA")
        if value.get("item"):
            value["item"] = item(value["item"])
        if value.get("item_id"):
            for old, new in ITEM_PREFIXES.values():
                if value["item_id"].startswith(old):
                    value["item_id"] = identifier(value["item_id"], old, new)
                    break
        if value.get("call_id"):
            value["call_id"] = identifier(value["call_id"], "call_", "callid_")
        if value.get("response"):
            value["response"] = self.decode(value["response"])
        if value.get("error"):
            value["error"] = self.error(value["error"]).model_dump(mode="json")
        value.setdefault("previous_sequence", value["sequence"] - 1 if value["sequence"] else None)
        return ProviderStreamEvent.model_validate(value)

    def stream_guard(self):
        return LegacySequenceGuard()
