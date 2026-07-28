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
        # 每个模型必须显式声明。默认不支持；确认支持后，面向 kemo-agent
        # 统一暴露 minimal/low/medium/high/max 五档。厂商档位较少时允许
        # 多个 Kemo 档位折叠到同一个真实值，但必须在 extensions 中公布映射。
        reasoning=ReasoningCapabilities(
            supported=False,
            efforts=[],
            summary=False,
            persisted_state=False,
        ),
        # 只声明已经用真实请求验证过的能力；模板默认不替厂商作乐观推断。
        tools=ToolCapabilities(function_calling=False, parallel_calls=False),
        structured_output=False,
        metadata={"source": "provider_package", "upstream_model": "model-name"},
        extensions={
            "limits": {"max_input_tokens": 128000, "max_output_tokens": 8192},
            "operations": {
                "conversation": {"supported": True},
                "vision": {"supported": False},
                "image_generation": {"supported": False},
                "image_edit": {"supported": False},
                "audio_transcription": {"supported": False},
                "speech_generation": {"supported": False},
                "speech_to_speech": {"supported": False},
                "video_understanding": {"supported": False},
                "video_generation": {"supported": False},
            },
            "asset_limits": {
                "image_bytes": 20 * 1024 * 1024,
                "audio_bytes": 100 * 1024 * 1024,
                "video_bytes": 1024 * 1024 * 1024,
                "file_bytes": 100 * 1024 * 1024,
            },
            "probe": {
                "supported": True,
                "mode": "minimal_inference",
                "billable": True,
            },
            # 推理模型必须逐项填写。原生五档用 identity 映射；档位较少时
            # 显式折叠；只有开关/默认推理时值可为 None，并把 mode 标为
            # provider_default。非推理模型保持空映射和 unsupported。
            "reasoning_effort_map": {},
            "reasoning_policy": {
                "mode": "unsupported",
                "logical_efforts": [],
                "upstream_parameter": None,
                "collapsed": False,
            },
        },
    )
}
