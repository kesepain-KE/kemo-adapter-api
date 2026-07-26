"""Dedicated read-only gateway perception API for external agents."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hmac
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status


router = APIRouter(tags=["agent-status"])


def _status_token(request: Request, authorization: str | None) -> None:
    configured = request.app.state.settings.status_token.strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="状态接口未启用",
        )
    snapshot = request.app.state.live_config.current
    if (
        configured == request.app.state.settings.web_token.strip()
        or configured in request.app.state.settings.api_keys
        or configured in snapshot.api_keys
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STATUS_TOKEN 必须独立于其他网关 Token",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要 STATUS_TOKEN Bearer 鉴权",
            headers={"WWW-Authenticate": "Bearer"},
        )
    candidate = authorization[7:].strip()
    if not candidate or not hmac.compare_digest(candidate, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="STATUS_TOKEN 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _statistics_day(request: Request, value: str | None) -> str:
    if value is not None:
        return value
    timezone = ZoneInfo(request.app.state.statistics.timezone_name)
    return datetime.now(timezone).date().isoformat()


@router.get("/status")
async def gateway_status(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    day: str | None = Query(default=None, alias="date"),
    ranking_limit: int = Query(default=100, ge=1, le=100),
    log_limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Return a deliberately allow-listed, secret-free operational snapshot."""
    _status_token(request, authorization)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"

    target_day = _statistics_day(request, day)
    store = request.app.state.statistics
    try:
        (
            daily,
            provider_ranking,
            model_ranking,
            key_ranking,
            recent_logs,
            success_logs,
            failure_logs,
            version,
        ) = await asyncio.gather(
            store.daily(target_day),
            store.rankings(target_day, "provider", limit=ranking_limit),
            store.rankings(target_day, "model", limit=ranking_limit),
            store.rankings(target_day, "gateway_key", limit=ranking_limit),
            store.recent_invocations("all", limit=log_limit),
            store.recent_invocations("success", limit=log_limit),
            store.recent_invocations("failure", limit=log_limit),
            asyncio.to_thread(request.app.state.system_inspector.cached_version_check),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="状态查询参数无效") from exc

    snapshot = request.app.state.live_config.current
    provider_items: list[dict[str, Any]] = []
    enabled_models: list[dict[str, str]] = []
    for provider_id, package in sorted(request.app.state.registry.providers.items()):
        provider_enabled = provider_id not in snapshot.disabled_providers
        models = sorted(package.models)
        provider_items.append(
            {
                "provider_id": provider_id,
                "enabled": provider_enabled,
                "registered_models": models,
            }
        )
        if provider_enabled:
            enabled_models.extend(
                {"model": model, "provider_id": provider_id}
                for model in models
                if model not in snapshot.disabled_models
            )

    recent_items = recent_logs["items"]
    return {
        "object": "kemo.gateway_status",
        "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "protocol_version": request.app.state.settings.protocol_version,
        "runtime": request.app.state.runtime_state.snapshot(),
        "version": version,
        "registry": {
            "providers": provider_items,
            "registered_provider_ids": [item["provider_id"] for item in provider_items],
            "enabled_models": enabled_models,
        },
        "control": {
            "highest_priority_system_prompt": snapshot.gateway_system_prompt,
            "disabled_providers": sorted(snapshot.disabled_providers),
            "disabled_models": sorted(snapshot.disabled_models),
        },
        "statistics": {
            "date": target_day,
            "timezone": store.timezone_name,
            "summary": daily,
            "token_cache_rate": daily["cache_hit_rate"],
            "rankings": {
                "providers": provider_ranking["items"],
                "models": model_ranking["items"],
                "gateway_keys": key_ranking["items"],
            },
        },
        "logs": {
            "recent": recent_items,
            "successful": success_logs["items"],
            "failed": failure_logs["items"],
            "last_invocation": recent_items[0] if recent_items else None,
        },
    }


__all__ = ["router"]
