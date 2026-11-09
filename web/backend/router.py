"""Authenticated operational APIs, independent of catalog publication."""
from __future__ import annotations
import asyncio
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from api.middleware import Principal
from core.restart_control import RestartAlreadyRunning
from web.backend.auth_routes import require_admin, require_owner, require_write_csrf
from web.backend.schemas import RestartRequestBody
from web.backend.gateway_config import gateway_config_view
from web.backend.startup_config import StartupConflict, config_view, save_config
from core.restart_control import read_json
from core.terminal_logs import terminal_logs
from gateway.diagnostics import request_activity, usage_overview
router = APIRouter(prefix="/admin/api", tags=["operations"])


class StartupMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str
    yaml: str
    secrets: dict[str, str] = Field(default_factory=dict)


def startup_overrides(request):
    root = request.app.state.registry.project_root
    metadata = read_json(root / "core/runtime/gateway.pid.json") or {}
    return metadata.get("environment_override_names", [])


@router.get("/system/startup-config")
async def startup_config(request: Request, _: Principal = Depends(require_owner)):
    try:
        return await asyncio.to_thread(config_view, request.app.state.registry.project_root, startup_overrides(request))
    except (ValueError, OSError):
        raise HTTPException(422, "无法读取启动配置，请检查 gateway.yaml") from None


@router.put("/system/startup-config", dependencies=[Depends(require_write_csrf)])
async def update_startup_config(body: StartupMutation, request: Request, _: Principal = Depends(require_owner)):
    root = request.app.state.registry.project_root
    try:
        await asyncio.to_thread(save_config, root, body.expected_revision, body.yaml, body.secrets)
    except StartupConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        raise HTTPException(503, "启动配置未能保存，请检查文件权限或稍后重试") from None
    request.app.state.registry.store.audit("web-console", "save-startup-config", "gateway.yaml")
    return {**config_view(root, startup_overrides(request)), "restart_required": True}


@router.get("/diagnostics/usage")
async def diagnostic_usage(request: Request, period: Literal["today", "24h", "7d", "30d", "all"] = "today",
                           _: Principal = Depends(require_admin)):
    return await asyncio.to_thread(usage_overview, request.app.state.registry.store, period,
                                   request.app.state.settings.statistics_timezone)


@router.get("/diagnostics/activity")
async def diagnostic_activity(request: Request, period: Literal["week", "month", "26w"] = "month",
                              granularity: Literal["day", "hour"] = "day",
                              day: date | None = Query(default=None, alias="date"),
                              _: Principal = Depends(require_admin)):
    return await asyncio.to_thread(request_activity, request.app.state.registry.store, period,
                                   request.app.state.settings.statistics_timezone, granularity=granularity, day=day)


@router.get("/system/terminal-logs")
async def terminal_log_snapshot(limit: int = Query(default=300, ge=1, le=1000),
                                stream: Literal["all", "out", "err"] = "all",
                                _: Principal = Depends(require_admin)):
    return terminal_logs.snapshot(limit, stream)


@router.get("/system/gateway-config")
async def gateway_config(request: Request, principal: Principal = Depends(require_admin)):
    return gateway_config_view(request.app.state.settings)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


def statistics_day(request: Request, value: str | None) -> str:
    if value:
        return value
    store = request.app.state.statistics
    return datetime.now(ZoneInfo(store.timezone_name)).date().isoformat()


def invalid_statistics_query(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/statistics/daily")
async def daily_statistics(
    request: Request,
    day: str | None = Query(default=None, alias="date"),
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    try:
        return await request.app.state.statistics.daily(statistics_day(request, day))
    except ValueError as exc:
        raise invalid_statistics_query(exc) from exc


@router.get("/statistics/hourly")
async def hourly_statistics(
    request: Request,
    day: str | None = Query(default=None, alias="date"),
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    try:
        return await request.app.state.statistics.hourly(statistics_day(request, day))
    except ValueError as exc:
        raise invalid_statistics_query(exc) from exc


@router.get("/statistics/rankings")
async def statistics_rankings(
    request: Request,
    dimension: str = Query(pattern="^(provider|model|gateway_key)$"),
    day: str | None = Query(default=None, alias="date"),
    limit: int = Query(default=20, ge=1, le=100),
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    try:
        return await request.app.state.statistics.rankings(
            statistics_day(request, day), dimension, limit=limit
        )
    except ValueError as exc:
        raise invalid_statistics_query(exc) from exc


@router.get("/statistics/invocations")
async def statistics_invocations(
    request: Request,
    response: Response,
    outcome: str = Query(default="all", pattern="^(all|success|failure)$"),
    day: str | None = Query(default=None, alias="date"),
    hour: int | None = Query(default=None, ge=0, le=23),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    legacy_limit: int | None = Query(default=None, alias="limit", ge=1, le=1000),
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    """Return a database-backed page of secret-safe invocation logs."""
    _no_store(response)
    effective_page = 1 if legacy_limit is not None else page
    effective_page_size = legacy_limit or page_size
    try:
        result = await request.app.state.statistics.recent_invocations(
            outcome,
            limit=effective_page_size,
            day=day,
            hour=hour,
            offset=(effective_page - 1) * effective_page_size,
        )
    except ValueError as exc:
        raise invalid_statistics_query(exc) from exc
    total = int(result.get("total", 0))
    result["page"] = effective_page
    result["page_size"] = effective_page_size
    result["pages"] = max(1, (total + effective_page_size - 1) // effective_page_size)
    return result


@router.get("/statistics/series")
async def statistics_series(
    request: Request,
    start: str = Query(alias="from"),
    end: str = Query(alias="to"),
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    try:
        return await request.app.state.statistics.series(start, end)
    except ValueError as exc:
        raise invalid_statistics_query(exc) from exc


@router.get("/system/restart")
async def restart_status(
    request: Request, _: Principal = Depends(require_owner)
) -> dict[str, object]:
    return {
        "gateway": request.app.state.runtime_state.snapshot(),
        "restart": request.app.state.restart_service.status(),
    }


@router.get("/system/restart-required")
async def restart_required(
    request: Request, _: Principal = Depends(require_admin)
) -> dict[str, object]:
    return await asyncio.to_thread(request.app.state.system_inspector.restart_required)


@router.get("/system/version-check")
async def version_check(
    request: Request, _: Principal = Depends(require_admin)
) -> dict[str, object]:
    return await asyncio.to_thread(request.app.state.system_inspector.version_check)


@router.post("/system/restart", status_code=status.HTTP_202_ACCEPTED)
async def request_restart(
    body: RestartRequestBody,
    request: Request,
    principal: Principal = Depends(require_owner),
    _csrf: None = Depends(require_write_csrf),
) -> dict[str, object]:
    try:
        restart_request = request.app.state.restart_service.enqueue(
            reason=body.reason,
            force=body.force,
            requested_by=principal.subject_id,
        )
    except RestartAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "request_id": restart_request.request_id,
        "status": "queued",
        "force": restart_request.force,
    }
