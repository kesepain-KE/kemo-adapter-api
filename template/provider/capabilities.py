"""模型目录必须反映本厂商和本路由流水线的真实能力。"""

from __future__ import annotations

from core.models import ModelCapabilities, ReasoningCapabilities, ToolCapabilities


MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    "example-model-name": ModelCapabilities(
        model="example-model-name",
        task="llm",
        input_modalities=["text"],
        output_modalities=["text"],
        streaming=True,
        reasoning=ReasoningCapabilities(supported=False),
        # 只声明已经用真实请求验证过的能力；模板默认不替厂商作乐观推断。
        tools=ToolCapabilities(function_calling=False, parallel_calls=False),
        structured_output=False,
        metadata={"source": "provider_package", "upstream_model": "model-name"},
        extensions={
            "limits": {"max_input_tokens": 128000, "max_output_tokens": 8192},
            "operations": {"conversation": {"supported": True}},
            "probe": {
                "supported": True,
                "mode": "minimal_inference",
                "billable": True,
            },
        },
    )
}
