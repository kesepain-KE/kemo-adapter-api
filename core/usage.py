"""只聚合已经由 Provider 包标准化的 Usage。

厂商 token 字段解释、缓存 token 是否包含在 input、reasoning 是否包含在 output、
以及缺失字段估算均属于对应 Provider 包，禁止在这里用厂商名称分支判断。
"""

from __future__ import annotations

from core.models import StageUsage, Usage, UsageMeasurement


def aggregate_stages(stages: list[StageUsage]) -> Usage:
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    totals: dict[str, int | None] = {}
    for field_name in token_fields:
        values = [getattr(stage, field_name) for stage in stages]
        known = [value for value in values if value is not None]
        totals[field_name] = sum(known) if known else None

    modes = {stage.measurement.mode for stage in stages}
    mode = next(iter(modes)) if len(modes) == 1 else "mixed"
    exact_fields = [
        field_name
        for field_name in token_fields
        if totals[field_name] is not None
        and all(
            getattr(stage, field_name) is None
            or field_name in stage.measurement.exact_fields
            or stage.measurement.exact
            for stage in stages
        )
    ]
    estimated_fields = sorted(
        {
            field_name
            for stage in stages
            for field_name in stage.measurement.estimated_fields
        }
    )
    media_keys = {key for stage in stages for key in stage.media}
    media: dict[str, int | float | None] = {}
    for key in media_keys:
        values = [stage.media.get(key) for stage in stages]
        known = [value for value in values if value is not None]
        media[key] = sum(known) if known else None
    known_token_fields = [key for key, value in totals.items() if value is not None]
    return Usage(
        **totals,
        stages=stages,
        media=media,
        measurement=UsageMeasurement(
            mode=mode if stages else "unknown",
            exact=bool(known_token_fields) and len(exact_fields) == len(known_token_fields),
            exact_fields=exact_fields,
            estimated_fields=estimated_fields,
        ),
    )
