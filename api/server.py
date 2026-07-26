"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import mimetypes
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.errors import install_exception_handlers
from api.routes import capabilities_router, responses_router, retrieval_router
from core.config import Settings
from core.executor import GatewayExecutor
from core.live_config import LiveConfigManager
from core.registry import ProviderRegistry
from core.retrieval_executor import RetrievalExecutor
from core.runtime_state import GatewayRuntimeState
from core.stores import InMemoryExecutionStore
from web.backend.router import router as admin_router
from web.backend.restart_service import RestartService
from web.backend.service import RuntimeConfigWriter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def create_app(
    settings: Settings | None = None,
    *,
    live_config_root: Path | None = None,
    discover_providers: bool = True,
) -> FastAPI:
    load_dotenv()
    resolved_settings = settings or Settings.from_env()
    registry = ProviderRegistry()
    live_config = LiveConfigManager(live_config_root or PROJECT_ROOT)
    runtime_state = GatewayRuntimeState()
    restart_service = RestartService(live_config.project_root, runtime_state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        snapshot = await live_config.refresh()
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
        await registry.close()

    app = FastAPI(title="Kemo Provider Gateway", version="0.4.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.registry = registry
    app.state.live_config = live_config
    app.state.runtime_state = runtime_state
    app.state.restart_service = restart_service
    app.state.uvicorn_server = None
    app.state.runtime_config_writer = RuntimeConfigWriter(live_config.project_root)
    app.state.executor = GatewayExecutor(
        registry,
        InMemoryExecutionStore(),
        live_config,
        runtime_state,
    )
    app.state.retrieval_executor = RetrievalExecutor(registry, live_config, runtime_state)
    app.include_router(responses_router)
    app.include_router(retrieval_router)
    app.include_router(capabilities_router)
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
