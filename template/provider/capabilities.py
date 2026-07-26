"""模型目录必须反映本厂商和本路由流水线的真实能力。"""

from __future__ import annotations

from core.models import ModelCapabilities, ReasoningCapabilities, ToolCapabilities


MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    "example/model-name": ModelCapabilities(
        model="example/model-name",
        input_modalities=["text"],
        output_modalities=["text"],
        streaming=True,
        reasoning=ReasoningCapabilities(supported=False),
        tools=ToolCapabilities(function_calling=True),
        structured_output=True,
        metadata={"source": "provider_package"},
        extensions={
            "limits": {"max_input_tokens": 128000, "max_output_tokens": 8192},
            "operations": {"conversation": {"supported": True}},
        },
    )
}
