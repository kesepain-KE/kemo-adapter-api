from __future__ import annotations

import asyncio
from datetime import datetime
import hmac
import json
import time
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from api.middleware import Principal, control_plane_auth_required, control_plane_principal
from core.config import safe_key_id
from core.provider_contract import ProviderException
from core.runtime_state import GatewayDrainingError
from web.backend.schemas import (
    GatewayRuntimeUpdate,
    KeyModelPolicyUpdate,
    LiveControlUpdate,
    ProviderApiUpdate,
    RestartRequestBody,
    WebPasswordAuth,
    WebTokenAuth,
)
from web.backend.auth_service import WEB_PREAUTH_COOKIE, WEB_SESSION_COOKIE
from web.backend.restart_service import RestartAlreadyRunning
from web.backend.service import RevisionConflict, RuntimeConfigWriter


router = APIRouter(
    prefix="/admin/api", tags=["admin-internal"], include_in_schema=False
)


def _password_auth_state(request: Request) -> tuple[bool, bool]:
    settings = request.app.state.settings
    username = bool(settings.web_username.strip())
    password = bool(settings.web_password.strip())
    return username or password, username == password


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def _masked_secret(value: str) -> str:
    """Return an operator hint without exposing a reusable credential."""
    return f"{'•' * 12}{value[-4:]}" if len(value) > 4 else "•" * 16


def _client_id(request: Request, stage: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{stage}:{host}"


def _enforce_login_limit(request: Request, stage: str) -> str:
    client_id = _client_id(request, stage)
    retry_after = request.app.state.web_auth.login_retry_after(client_id)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
    return client_id


def _secure_cookie(request: Request) -> bool:
    configured = request.app.state.settings.web_cookie_secure
    if configured is not None:
        return configured
    return request.app.state.settings.base_url.lower().startswith("https://")


def _set_session_cookie(
    response: Response, request: Request, *, name: str, token: str
) -> None:
    response.set_cookie(
        name,
        token,
        max_age=request.app.state.web_auth.ttl_seconds,
        path="/admin",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="strict",
    )


def _delete_session_cookie(response: Response, *, name: str) -> None:
    response.delete_cookie(name, path="/admin", httponly=True, samesite="strict")


def _require_same_origin(request: Request) -> None:
    """Reject cross-site browser requests while allowing non-browser API clients."""
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise HTTPException(status_code=403, detail="拒绝跨站管理请求")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    parsed = urlsplit(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=403, detail="管理请求来源无效")
    allowed = {request.headers.get("host", "").lower()}
    configured_base = request.app.state.settings.base_url.strip()
    if configured_base:
        allowed.add(urlsplit(configured_base).netloc.lower())
    if parsed.netloc.lower() not in allowed:
        raise HTTPException(status_code=403, detail="拒绝跨站管理请求")


def require_write_csrf(
    request: Request,
    authorization: str | None = Header(default=None),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """Require a per-session CSRF token for cookie-authenticated mutations."""
    if _bearer_token(authorization) is not None:
        return
    if not control_plane_auth_required(request):
        # Direct-login mode has no Web session/CSRF token. Still reject cross-site
        # browser writes, and ignore obsolete cookies left by an earlier setup.
        _require_same_origin(request)
        return
    session_token = request.cookies.get(WEB_SESSION_COOKIE, "")
    if not session_token:
        return
    _require_same_origin(request)
    session = request.app.state.web_auth.resolve(session_token, stage="complete")
    if session is None or csrf_token is None or not hmac.compare_digest(
        csrf_token, session.csrf_token
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


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


def statistics_day(request: Request, value: str | None) -> str:
    if value:
        return value
    store = request.app.state.statistics
    return datetime.now(ZoneInfo(store.timezone_name)).date().isoformat()


def invalid_statistics_query(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/auth/methods")
async def web_auth_methods(request: Request, response: Response) -> dict[str, object]:
    _no_store(response)
    password_required, password_valid = _password_auth_state(request)
    return {
        "token_required": bool(request.app.state.settings.web_token.strip()),
        "password_required": password_required,
        "configuration_valid": password_valid,
        "session_ttl_seconds": request.app.state.web_auth.ttl_seconds,
    }


@router.post("/auth/token")
async def authenticate_web_token(
    body: WebTokenAuth,
    request: Request,
    response: Response,
) -> dict[str, object]:
    _no_store(response)
    _require_same_origin(request)
    client_id = _enforce_login_limit(request, "token")
    configured = request.app.state.settings.web_token.strip()
    if not configured:
        raise HTTPException(status_code=404, detail="Web Token 鉴权未启用")
    if not hmac.compare_digest(body.token, configured):
        request.app.state.web_auth.record_login_failure(client_id)
        raise HTTPException(status_code=401, detail="Web Token 无效")
    request.app.state.web_auth.clear_login_failures(client_id)
    password_required, password_valid = _password_auth_state(request)
    if not password_valid:
        raise HTTPException(status_code=503, detail="WEB_USERNAME 与 WEB_PASSWORD 必须同时配置")
    session = request.app.state.web_auth.issue(
        stage="password" if password_required else "complete"
    )
    cookie_name = WEB_PREAUTH_COOKIE if password_required else WEB_SESSION_COOKIE
    _set_session_cookie(response, request, name=cookie_name, token=session.token)
    return {
        "expires_at": session.expires_at,
        "expires_in": request.app.state.web_auth.ttl_seconds,
        "next_step": "password" if password_required else "complete",
        "csrf_token": session.csrf_token if not password_required else None,
    }


@router.post("/auth/password")
async def authenticate_web_password(
    body: WebPasswordAuth,
    request: Request,
    response: Response,
) -> dict[str, object]:
    _no_store(response)
    _require_same_origin(request)
    client_id = _enforce_login_limit(request, "password")
    settings = request.app.state.settings
    password_required, password_valid = _password_auth_state(request)
    if not password_valid:
        raise HTTPException(status_code=503, detail="WEB_USERNAME 与 WEB_PASSWORD 必须同时配置")
    if not password_required:
        raise HTTPException(status_code=404, detail="用户名和密码鉴权未启用")

    token_required = bool(settings.web_token.strip())
    preauth_token = request.cookies.get(WEB_PREAUTH_COOKIE)
    if token_required and (
        preauth_token is None
        or request.app.state.web_auth.resolve(preauth_token, stage="password") is None
    ):
        raise HTTPException(status_code=401, detail="请先完成 Web Token 鉴权")

    username_ok = hmac.compare_digest(body.username, settings.web_username)
    password_ok = hmac.compare_digest(body.password, settings.web_password)
    if not (username_ok and password_ok):
        request.app.state.web_auth.record_login_failure(client_id)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    request.app.state.web_auth.clear_login_failures(client_id)
    session = request.app.state.web_auth.issue(stage="complete")
    _set_session_cookie(
        response, request, name=WEB_SESSION_COOKIE, token=session.token
    )
    if preauth_token is not None:
        request.app.state.web_auth.revoke(preauth_token)
        _delete_session_cookie(response, name=WEB_PREAUTH_COOKIE)
    return {
        "expires_at": session.expires_at,
        "expires_in": request.app.state.web_auth.ttl_seconds,
        "next_step": "complete",
        "csrf_token": session.csrf_token,
    }


@router.get("/auth/session")
async def web_auth_session(request: Request, response: Response) -> dict[str, object]:
    _no_store(response)
    token = request.cookies.get(WEB_SESSION_COOKIE, "")
    session = request.app.state.web_auth.resolve(token, stage="complete")
    if session is None:
        raise HTTPException(status_code=401, detail="Web 会话无效或已过期")
    return {
        "authenticated": True,
        "expires_at": session.expires_at,
        "csrf_token": session.csrf_token,
    }


@router.post("/auth/logout")
async def web_auth_logout(request: Request, response: Response) -> dict[str, str]:
    _no_store(response)
    _require_same_origin(request)
    for cookie_name in (WEB_SESSION_COOKIE, WEB_PREAUTH_COOKIE):
        token = request.cookies.get(cookie_name)
        if token:
            request.app.state.web_auth.revoke(token)
        _delete_session_cookie(response, name=cookie_name)
    return {"status": "logged_out"}


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
        "base_url": request.app.state.settings.base_url,
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


@router.get("/keys")
async def gateway_api_keys(
    request: Request,
    response: Response,
    _: Principal = Depends(require_owner),
) -> dict[str, object]:
    """Return configured gateway keys and their real all-time usage to owners."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"

    metadata: dict[str, dict[str, object]] = {}
    keys_path = request.app.state.runtime_config_writer.project_root / "api" / "keys.json"
    try:
        parsed = json.loads(keys_path.read_text(encoding="utf-8"))
        raw_keys = parsed.get("keys", {}) if isinstance(parsed, dict) else {}
        if isinstance(raw_keys, dict):
            metadata = {
                token: value
                for token, value in raw_keys.items()
                if isinstance(token, str) and isinstance(value, dict)
            }
    except (OSError, UnicodeError, json.JSONDecodeError):
        # The live snapshot remains authoritative when an invalid edit was rejected.
        metadata = {}

    configured: dict[str, dict[str, object]] = {}
    for token, config in request.app.state.settings.api_keys.items():
        stable_id = safe_key_id(token, config.key_id)
        configured[token] = {
            "id": stable_id,
            "name": config.subject_id.strip() or stable_id,
            "masked_token": _masked_secret(token),
            "source": "environment",
            "created_at": None,
            "allowed_models": (
                sorted(config.allowed_models) if config.allowed_models is not None else None
            ),
            "writable": False,
        }
    for token, config in request.app.state.live_config.current.api_keys.items():
        raw = metadata.get(token, {})
        created_at = raw.get("created_at")
        configured_name = raw.get("key_id")
        stable_id = safe_key_id(token, config.key_id)
        configured[token] = {
            "id": stable_id,
            "name": (
                configured_name.strip()
                if isinstance(configured_name, str) and configured_name.strip()
                else config.subject_id.strip() or stable_id
            ),
            "masked_token": _masked_secret(token),
            "source": "runtime",
            "created_at": created_at if isinstance(created_at, str) and created_at.strip() else None,
            "allowed_models": (
                sorted(config.allowed_models) if config.allowed_models is not None else None
            ),
            "writable": True,
        }

    usage = await request.app.state.statistics.gateway_key_usage()
    items: list[dict[str, object]] = []
    for item in configured.values():
        key_usage = usage.get(str(item["id"]), {})
        items.append(
            {
                **item,
                "usage": {
                    "calls": int(key_usage.get("calls", 0)),
                    "successes": int(key_usage.get("successes", 0)),
                    "total_tokens": key_usage.get("total_tokens"),
                },
                "last_used_at": key_usage.get("last_used_at"),
            }
        )
    items.sort(key=lambda item: str(item["name"]).lower())
    models: list[dict[str, object]] = []
    registry = request.app.state.registry
    for provider_id, package in sorted(registry.providers.items()):
        prefix = f"{provider_id}-"
        for model in sorted(package.models):
            try:
                registry.resolve(model)
                enabled = True
            except LookupError:
                enabled = False
            models.append(
                {
                    "id": model,
                    "provider_id": provider_id,
                    "provider_model": model[len(prefix):] if model.startswith(prefix) else model,
                    "enabled": enabled,
                }
            )
    for item in items:
        allowed_models = item["allowed_models"]
        item["model_policy"] = (
            "allow_all"
            if allowed_models is None
            else "deny_all"
            if not allowed_models
            else "allow_list"
        )
    return {
        "revision": request.app.state.live_config.current.revision,
        "items": items,
        "models": models,
    }


@router.post("/keys/{key_id}/reveal")
async def reveal_gateway_api_key(
    key_id: str,
    request: Request,
    response: Response,
    _: Principal = Depends(require_owner),
    _csrf: None = Depends(require_write_csrf),
) -> dict[str, str]:
    """Return one gateway token only after an explicit owner action."""
    _no_store(response)

    configured = dict(request.app.state.settings.api_keys)
    # Match the precedence used by gateway authentication: hot config wins.
    configured.update(request.app.state.live_config.current.api_keys)
    matches = [
        token
        for token, config in configured.items()
        if safe_key_id(token, config.key_id) == key_id
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="API 密钥不存在")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="API 密钥标识重复，无法安全读取")
    return {"token": matches[0]}


@router.put("/keys/{key_id}/model-policy")
async def update_key_model_policy(
    key_id: str,
    body: KeyModelPolicyUpdate,
    request: Request,
    response: Response,
    _: Principal = Depends(require_owner),
    _csrf: None = Depends(require_write_csrf),
    config_writer: RuntimeConfigWriter = Depends(writer),
) -> dict[str, object]:
    _no_store(response)
    if body.allowed_models is not None:
        registered_models = {
            model
            for package in request.app.state.registry.providers.values()
            for model in package.models
        }
        unknown = sorted(set(body.allowed_models) - registered_models)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"包含未注册模型: {', '.join(unknown)}",
            )
    try:
        async with config_writer.lock:
            config_writer.assert_revision(
                body.expected_revision,
                request.app.state.live_config.current.revision,
            )
            config_writer.update_key_model_policy(
                key_id,
                allowed_models=body.allowed_models,
            )
            snapshot = await refresh_after_write(request)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized = (
        None if body.allowed_models is None else sorted(set(body.allowed_models))
    )
    return {
        "revision": snapshot.revision,
        "status": "updated",
        "allowed_models": normalized,
        "model_policy": (
            "allow_all"
            if normalized is None
            else "deny_all"
            if not normalized
            else "allow_list"
        ),
    }


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


@router.get("/providers/{provider_id}/capabilities")
async def provider_capabilities(
    provider_id: str,
    request: Request,
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    package = request.app.state.registry.providers.get(provider_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Provider 不存在: {provider_id}")
    models: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for model in sorted(package.models):
        try:
            declaration = await package.capabilities(model)
            models.append(declaration.model_dump(mode="json"))
        except Exception as exc:
            errors.append({"model": model, "error": type(exc).__name__})
    return {
        "provider_id": provider_id,
        "models": models,
        "errors": errors,
    }


@router.post("/models/{model}/probe")
async def probe_model(
    model: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    _csrf: None = Depends(require_write_csrf),
) -> dict[str, object]:
    try:
        package = request.app.state.registry.resolve(model)
        declaration = await package.capabilities(model)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"模型不存在或当前已禁用: {model}") from exc

    request_id = f"probe_{uuid4().hex}"
    started_ns = time.perf_counter_ns()
    context = request.app.state.executor.make_context(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        request_id=request_id,
        gateway_key_id=principal.key_id,
    )
    lease = None
    try:
        lease = await request.app.state.runtime_state.admit_execution()
        result = await package.probe(model, context)
        reachable = result.reachable
        result_status = result.status
        error_code = result.error.code if result.error else None
    except ProviderException as exc:
        reachable = False
        result_status = "failed"
        error_code = exc.error.code
    except GatewayDrainingError:
        reachable = False
        result_status = "failed"
        error_code = "GATEWAY_DRAINING"
    except Exception:
        reachable = False
        result_status = "failed"
        error_code = "PROBE_FAILED"
    finally:
        if lease is not None:
            await lease.release()

    return {
        "model": model,
        "task": declaration.task,
        "reachable": reachable,
        "status": result_status,
        "latency_ms": round((time.perf_counter_ns() - started_ns) / 1_000_000, 2),
        "error_code": error_code,
        "tested_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }


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


@router.put("/runtime/gateway")
async def update_gateway(
    body: GatewayRuntimeUpdate,
    request: Request,
    _: Principal = Depends(require_admin),
    _csrf: None = Depends(require_write_csrf),
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
    _csrf: None = Depends(require_write_csrf),
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
    _csrf: None = Depends(require_write_csrf),
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
