from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.middleware import Principal, control_plane_auth_required, control_plane_principal
from web.backend.schemas import (
    GatewayRuntimeUpdate,
    LiveControlUpdate,
    ProviderApiUpdate,
    RestartRequestBody,
)
from web.backend.restart_service import RestartAlreadyRunning
from web.backend.service import RevisionConflict, RuntimeConfigWriter


router = APIRouter(prefix="/admin/api", tags=["admin-internal"])


def require_admin(principal: Principal = Depends(control_plane_principal)) -> Principal:
    if "admin:web" not in principal.scopes and "owner" not in principal.scopes:
        raise HTTPException(status_code=403, detail="需要 admin:web 权限")
    return principal


def require_owner(principal: Principal = Depends(control_plane_principal)) -> Principal:
    if "owner" not in principal.scopes:
        raise HTTPException(status_code=403, detail="重启网关需要 owner 权限")
    return principal


def writer(request: Request) -> RuntimeConfigWriter:
    return request.app.state.runtime_config_writer


async def refresh_after_write(request: Request):
    snapshot = await request.app.state.live_config.refresh()
    await request.app.state.registry.apply_live_config(snapshot)
    return snapshot


@router.get("/console")
async def console_data(
    request: Request, principal: Principal = Depends(require_admin)
) -> dict[str, object]:
    snapshot = request.app.state.live_config.current
    registry = request.app.state.registry
    return {
        "version": request.app.version,
        "protocol_version": request.app.state.settings.protocol_version,
        "authentication": {"required": control_plane_auth_required(request)},
        "permissions": {"can_restart": "owner" in principal.scopes},
        "revision": snapshot.revision,
        "gateway_api": snapshot.gateway_api,
        "highest_priority_system_prompt": snapshot.gateway_system_prompt,
        "disabled_providers": sorted(snapshot.disabled_providers),
        "disabled_models": sorted(snapshot.disabled_models),
        "providers": [package.diagnostics() for package in registry.providers.values()],
        "provider_configs": request.app.state.runtime_config_writer.provider_configs(),
        "live_config_error": request.app.state.live_config.last_error,
        "runtime": request.app.state.runtime_state.snapshot(),
    }


@router.get("/system/restart")
async def restart_status(
    request: Request, _: Principal = Depends(require_owner)
) -> dict[str, object]:
    return {
        "gateway": request.app.state.runtime_state.snapshot(),
        "restart": request.app.state.restart_service.status(),
    }


@router.post("/system/restart", status_code=status.HTTP_202_ACCEPTED)
async def request_restart(
    body: RestartRequestBody,
    request: Request,
    principal: Principal = Depends(require_owner),
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


@router.put("/runtime/gateway")
async def update_gateway(
    body: GatewayRuntimeUpdate,
    request: Request,
    _: Principal = Depends(require_admin),
    config_writer: RuntimeConfigWriter = Depends(writer),
) -> dict[str, str]:
    try:
        async with config_writer.lock:
            config_writer.assert_revision(body.expected_revision, request.app.state.live_config.current.revision)
            config_writer.update_gateway(enabled=body.enabled)
            snapshot = await refresh_after_write(request)
            return {"revision": snapshot.revision, "status": "updated"}
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/runtime/control")
async def update_control(
    body: LiveControlUpdate,
    request: Request,
    _: Principal = Depends(require_admin),
    config_writer: RuntimeConfigWriter = Depends(writer),
) -> dict[str, str]:
    try:
        async with config_writer.lock:
            config_writer.assert_revision(body.expected_revision, request.app.state.live_config.current.revision)
            config_writer.update_control(
                prompt=body.highest_priority_system_prompt,
                disabled_providers=body.disabled_providers,
                disabled_models=body.disabled_models,
            )
            snapshot = await refresh_after_write(request)
            return {"revision": snapshot.revision, "status": "updated"}
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/runtime/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    body: ProviderApiUpdate,
    request: Request,
    _: Principal = Depends(require_admin),
    config_writer: RuntimeConfigWriter = Depends(writer),
) -> dict[str, str]:
    try:
        async with config_writer.lock:
            config_writer.assert_revision(body.expected_revision, request.app.state.live_config.current.revision)
            config_writer.update_provider(provider_id, config=body.config, api_key=body.api_key)
            snapshot = await refresh_after_write(request)
            return {"revision": snapshot.revision, "status": "updated"}
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
