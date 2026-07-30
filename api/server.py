"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.errors import install_exception_handlers
from api.request_limits import RequestBodyLimitMiddleware
from api.routes import (
    assets_router,
    capabilities_router,
    responses_router,
    retrieval_router,
    status_router,
)
from core.assets import AssetLimits, AssetStore
from core.config import Settings
from core.executor import GatewayExecutor
from core.live_config import LiveConfigManager
from core.registry import ProviderRegistry
from core.retrieval_executor import RetrievalExecutor
from core.runtime_state import GatewayRuntimeState
from core.stores import SQLiteExecutionStore
from storage.statistics import StatisticsStore
from web.backend.router import router as admin_router
from web.backend.auth_service import WebAuthService
from web.backend.restart_service import RestartService
from web.backend.service import RuntimeConfigWriter
from web.backend.system_inspector import SystemInspector


PROJECT_ROOT = Path(__file__).resolve().parents[1]
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def _project_version() -> str:
    try:
        value = json.loads((PROJECT_ROOT / "version.json").read_text(encoding="utf-8"))
        version = value.get("version") if isinstance(value, dict) else None
        return version.strip() if isinstance(version, str) and version.strip() else "0.0.0"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "0.0.0"


def _web_auth_namespace(settings: Settings) -> str:
    """Bind persisted Web sessions to the active credential configuration."""

    payload = json.dumps(
        {
            "web_token": settings.web_token,
            "web_username": settings.web_username,
            "web_password": settings.web_password,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_web_exposure(settings: Settings) -> None:
    username = bool(settings.web_username.strip())
    password = bool(settings.web_password.strip())
    if username != password:
        raise RuntimeError("WEB_USERNAME 与 WEB_PASSWORD 必须同时配置")
    if settings.base_url:
        parsed_base = urlsplit(settings.base_url)
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.username is not None
            or parsed_base.password is not None
        ):
            raise RuntimeError(
                "GATEWAY_BASE_URL 必须是无内嵌凭据的 http/https 地址"
            )


def create_app(
    settings: Settings | None = None,
    *,
    live_config_root: Path | None = None,
    statistics_root: Path | None = None,
    asset_root: Path | None = None,
    discover_providers: bool = True,
) -> FastAPI:
    load_dotenv()
    resolved_settings = settings or Settings.from_env()
    _validate_web_exposure(resolved_settings)
    registry = ProviderRegistry()
    live_config = LiveConfigManager(live_config_root or PROJECT_ROOT)
    runtime_state = GatewayRuntimeState(
        max_concurrent_executions=resolved_settings.max_concurrent_executions
    )
    restart_service = RestartService(live_config.project_root, runtime_state)
    statistics = StatisticsStore(
        statistics_root or live_config.project_root / "storage",
        timezone_name=resolved_settings.statistics_timezone,
    )
    assets = AssetStore(
        asset_root or live_config.project_root / "storage" / "assets",
        limits=AssetLimits(
            image_bytes=resolved_settings.asset_image_max_bytes,
            audio_bytes=resolved_settings.asset_audio_max_bytes,
            video_bytes=resolved_settings.asset_video_max_bytes,
            file_bytes=resolved_settings.asset_file_max_bytes,
            retention_hours=resolved_settings.asset_retention_hours,
        ),
    )
    executions = SQLiteExecutionStore(
        live_config.project_root / "storage" / "executions",
        retention_hours=resolved_settings.execution_retention_hours,
        max_events_per_response=resolved_settings.max_sse_events_per_response,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        snapshot = await live_config.refresh()
        await statistics.initialize()
        await assets.initialize()
        await executions.initialize()
        if discover_providers:
            registry.discover(
                resolved_settings.provider_settings, snapshot.provider_settings
            )
        await registry.apply_live_config(snapshot)
        await runtime_state.mark_running()
        await restart_service.start_watcher()
        yield
        await restart_service.stop_watcher()
        await runtime_state.mark_stopping()
        await assets.close()
        await executions.close()
        await registry.close()

    app = FastAPI(
        title="Kemo Provider Gateway",
        version=_project_version(),
        lifespan=lifespan,
        docs_url="/docs" if resolved_settings.api_docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if resolved_settings.api_docs_enabled else None,
    )
    if resolved_settings.web_allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(resolved_settings.web_allowed_hosts),
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "style-src-attr 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        admin_document = request.url.path.startswith("/admin") and not (
            request.url.path.startswith("/admin/assets/")
            or request.url.path == "/admin/logo.png"
        )
        if request.url.path.startswith("/admin/api") or admin_document:
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=resolved_settings.request_json_max_bytes,
        paths=frozenset(
            {"/model/responses", "/model/embeddings", "/model/rerank"}
        ),
        path_prefixes=frozenset({"/admin/api/"}),
    )
    app.state.settings = resolved_settings
    app.state.registry = registry
    app.state.live_config = live_config
    app.state.runtime_state = runtime_state
    app.state.restart_service = restart_service
    app.state.system_inspector = SystemInspector(live_config.project_root)
    app.state.uvicorn_server = None
    app.state.runtime_config_writer = RuntimeConfigWriter(live_config.project_root)
    app.state.statistics = statistics
    app.state.assets = assets
    app.state.executions = executions
    app.state.web_auth = WebAuthService(
        persistence_path=live_config.project_root
        / "core"
        / "runtime"
        / "web-sessions.json",
        namespace=_web_auth_namespace(resolved_settings),
    )
    app.state.executor = GatewayExecutor(
        registry,
        executions,
        live_config,
        runtime_state,
        statistics,
        assets,
        execution_timeout_seconds=resolved_settings.model_execution_timeout_seconds,
    )
    app.state.retrieval_executor = RetrievalExecutor(
        registry,
        live_config,
        runtime_state,
        statistics,
        execution_timeout_seconds=resolved_settings.model_execution_timeout_seconds,
    )
    app.include_router(responses_router)
    app.include_router(assets_router)
    app.include_router(retrieval_router)
    app.include_router(capabilities_router)
    app.include_router(status_router)
    app.include_router(admin_router)

    frontend_dist = PROJECT_ROOT / "web" / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount(
            "/admin/assets",
            StaticFiles(directory=frontend_dist / "assets"),
            name="admin-assets",
        )

        @app.get("/admin/logo.png", include_in_schema=False)
        async def admin_logo() -> FileResponse:
            return FileResponse(PROJECT_ROOT / "kemo-adapter-api.png")

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/{path:path}", include_in_schema=False)
        async def admin_spa(path: str = "") -> FileResponse:
            del path
            return FileResponse(frontend_dist / "index.html")

        @app.get("/", include_in_schema=False)
        async def admin_entry() -> RedirectResponse:
            return RedirectResponse("/admin", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "protocol_version": resolved_settings.protocol_version,
            "providers": sorted(registry.providers),
            "live_config_revision": live_config.current.revision,
            "live_config_error": live_config.last_error,
            **runtime_state.snapshot(),
        }

    install_exception_handlers(app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=app.state.settings.host, port=app.state.settings.port)
