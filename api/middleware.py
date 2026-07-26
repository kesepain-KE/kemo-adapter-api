"""可信调用主体解析。正文 metadata.user 不参与资源授权。"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from core.config import safe_key_id


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    subject_id: str
    scopes: frozenset[str]
    key_id: str | None = None


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
    """存在管理 Token 或 Web 用户认证字段时，管理面必须显式认证。"""
    settings = request.app.state.settings
    snapshot = request.app.state.live_config.current
    principals = (*settings.api_keys.values(), *snapshot.api_keys.values())
    has_management_token = any(
        "admin:web" in principal.scopes or "owner" in principal.scopes
        for principal in principals
    )
    return bool(
        has_management_token
        or settings.web_username.strip()
        or settings.web_password.strip()
        or settings.web_token.strip()
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
                key_id="local-web-console",
            )
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要 Bearer Token")
    candidate = authorization[7:]

    # 完整 Web 会话只属于管理面，不能作为模型调用密钥。
    web_session = (
        request.app.state.web_auth.resolve(candidate, stage="complete")
        if allow_unconfigured_control_plane
        else None
    )
    if web_session is not None:
        return Principal(
            tenant_id="local",
            subject_id="web-session-console",
            scopes=frozenset({"owner"}),
            key_id="web-console-session",
        )

    # .env 是重启生效的启动配置；api/keys.json 是无需重启的运行时密钥配置。
    available_keys = dict(settings.api_keys)
    available_keys.update(snapshot.api_keys)
    for token, config in available_keys.items():
        if hmac.compare_digest(candidate, token):
            return Principal(
                config.tenant_id,
                config.subject_id,
                config.scopes,
                safe_key_id(candidate, config.key_id),
            )
    raise HTTPException(status_code=401, detail="Bearer Token 无效")
