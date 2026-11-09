from __future__ import annotations
import hmac
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from api.middleware import Principal, control_plane_auth_required, control_plane_local_bypass, control_plane_principal
from web.backend.auth_service import WEB_PREAUTH_COOKIE, WEB_SESSION_COOKIE
from web.backend.schemas import WebPasswordAuth, WebTokenAuth
router = APIRouter(prefix="/admin/api", include_in_schema=False)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip() or None
def _password_auth_state(request: Request) -> tuple[bool, bool]:
    if control_plane_local_bypass(request):
        return False, True
    settings = request.app.state.settings
    username = bool(settings.web_username.strip())
    password = bool(settings.web_password.strip())
    return username or password, username == password


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"




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
    # ``auto`` follows the URL actually used by this request. This matters when
    # one instance is reached through public HTTPS and also through a LAN HTTP
    # address: a Secure cookie issued for the LAN flow would never be sent back.
    scheme = request.url.scheme.lower()
    if request.client is not None and request.client.host in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        if forwarded in {"http", "https"}:
            scheme = forwarded
    return scheme == "https"


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




@router.get("/auth/methods")
async def web_auth_methods(request: Request, response: Response) -> dict[str, object]:
    _no_store(response)
    local_bypass = control_plane_local_bypass(request)
    password_required, password_valid = _password_auth_state(request)
    return {
        "token_required": bool(request.app.state.settings.web_token.strip()) and not local_bypass,
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
