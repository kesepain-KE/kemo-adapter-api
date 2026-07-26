"""可信调用主体解析。正文 metadata.user 不参与资源授权。"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    subject_id: str
    scopes: frozenset[str]


async def authenticated_principal(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    return await _resolve_principal(request, authorization, enforce_gateway_enabled=True)


async def control_plane_principal(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    """管理面鉴权不受对外 LLM API 启停影响，避免关闭后无法重新开启。"""
    return await _resolve_principal(
        request,
        authorization,
        enforce_gateway_enabled=False,
        allow_unconfigured_control_plane=True,
    )


def control_plane_auth_required(request: Request) -> bool:
    """任一 Token 或 Web 用户认证字段有值时，管理面必须显式认证。"""
    settings = request.app.state.settings
    snapshot = request.app.state.live_config.current
    return bool(
        settings.api_keys
        or snapshot.api_keys
        or settings.web_username.strip()
        or settings.web_password.strip()
    )


async def _resolve_principal(
    request: Request,
    authorization: str | None,
    *,
    enforce_gateway_enabled: bool,
    allow_unconfigured_control_plane: bool = False,
) -> Principal:
    settings = request.app.state.settings
    snapshot = await request.app.state.live_config.refresh()
    await request.app.state.registry.apply_live_config(snapshot)
    if enforce_gateway_enabled and snapshot.gateway_api.get("enabled", True) is not True:
        raise HTTPException(status_code=503, detail="Gateway API 已停用")

    if not authorization:
        if allow_unconfigured_control_plane and not control_plane_auth_required(request):
            return Principal(
                tenant_id="local",
                subject_id="unauthenticated-web-console",
                scopes=frozenset({"owner"}),
            )
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要 Bearer Token")
    candidate = authorization[7:]

    # .env 是重启生效的启动配置；api/keys.json 是无需重启的运行时密钥配置。
    available_keys = dict(settings.api_keys)
    available_keys.update(snapshot.api_keys)
    for token, config in available_keys.items():
        if hmac.compare_digest(candidate, token):
            return Principal(config.tenant_id, config.subject_id, config.scopes)
    raise HTTPException(status_code=401, detail="Bearer Token 无效")
