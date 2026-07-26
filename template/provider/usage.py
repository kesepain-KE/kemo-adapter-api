"""本文件是唯一了解该厂商 token/计费单位语义的位置。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models import Usage, UsageMeasurement


class ExampleUsageMapper:
    def from_response(self, raw_usage: Mapping[str, Any] | None) -> Usage:
        if not raw_usage:
            return Usage(measurement=UsageMeasurement(mode="unknown"))

        # TODO: 按厂商文档明确处理以下问题，禁止按名字猜测：
        # - cached tokens 是否已经包含在 input tokens；
        # - reasoning tokens 是否已经包含在 output tokens；
        # - total 是厂商原始总数还是可以安全求和；
        # - 图片、音频、视频的计量单位是 token、秒、张还是像素；
        # - 流式 usage 是累计快照还是本次增量。
        input_tokens = int(raw_usage["vendor_input_tokens"])
        output_tokens = int(raw_usage["vendor_output_tokens"])
        total_tokens = int(raw_usage.get("vendor_total_tokens", input_tokens + output_tokens))
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            measurement=UsageMeasurement(
                mode="provider",
                exact=True,
                exact_fields=["input_tokens", "output_tokens", "total_tokens"],
            ),
            # 生产默认不保留 raw；调试时也只能放经过字段白名单和大小限制的数据。
            provider_raw={},
        )
