from __future__ import annotations

import json
import mimetypes

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from gateway.plugins.schema import MAX_PACKAGE, PluginError, safe_file
from web.backend.auth_routes import require_admin, require_owner, require_write_csrf


class PluginRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except PluginError as exc:
                return JSONResponse(status_code=exc.status, content={"detail": {
                    "code": exc.code, "message": exc.message,
                }})
            except (ValueError, TypeError, KeyError):
                return JSONResponse(status_code=422, content={"detail": {
                    "code": "PLUGIN_INVALID_REQUEST", "message": "插件请求字段无效",
                }})
        return handle


router = APIRouter(prefix="/admin/api/advanced-plugins", tags=["advanced-plugins"],
                   route_class=PluginRoute, dependencies=[Depends(require_admin)])
ui_router = APIRouter(prefix="/admin/plugin-ui", route_class=PluginRoute, include_in_schema=False)


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str
    value: dict = Field(default_factory=dict)


def manager(request):
    return request.app.state.advanced_plugins


async def audit(request, principal, identity, action, operation, *, digest=None, version=None):
    entry = manager(request).state["plugins"].get(identity)
    metadata = {"plugin_id": identity, "version": version or (entry["manifest"]["version"] if entry else None)}
    if digest:
        metadata["sha256"] = digest
    try:
        result = await operation
    except PluginError as exc:
        manager(request).registry.store.audit(principal.subject_id, "plugin-" + action,
                                              json.dumps({**metadata, "result": exc.code}))
        raise
    if digest:
        installed = next(item for item in result["plugins"] if item["sha256"] == digest)
        metadata.update(plugin_id=installed["id"], version=installed["version"])
    else:
        current = manager(request).state["plugins"].get(identity)
        if current:
            metadata["version"] = current["manifest"]["version"]
    manager(request).registry.store.audit(principal.subject_id, "plugin-" + action,
                                          json.dumps({**metadata, "result": "success"}))
    return result


async def upload_content(file):
    content = await file.read(MAX_PACKAGE + 1)
    if len(content) > MAX_PACKAGE:
        raise PluginError("PLUGIN_INVALID_PACKAGE", "插件包超过 16 MiB")
    return content


@router.get("")
async def inventory(request: Request):
    return manager(request).snapshot()


@router.get("/catalog")
async def catalog(request: Request):
    return await manager(request).catalog()


@router.put("/sources", dependencies=[Depends(require_write_csrf)])
async def sources(body: Mutation, request: Request, principal=Depends(require_owner)):
    return await audit(request, principal, "sources", "sources",
                       manager(request).sources(body.expected_revision, body.value.get("sources", [])))


@router.post("/preview", dependencies=[Depends(require_write_csrf)])
async def preview(request: Request, file: UploadFile = File(...), _=Depends(require_owner)):
    return await manager(request).preview(await upload_content(file))


@router.post("/install", dependencies=[Depends(require_write_csrf)])
async def install(request: Request, file: UploadFile = File(...), expected_revision: str = Form(...),
                  sha256: str = Form(...), update: bool = Form(False), principal=Depends(require_owner)):
    content = await upload_content(file)
    return await audit(request, principal, "package", "update" if update else "install",
                       manager(request).install(content, expected_revision, sha256, update=update), digest=sha256)


@router.post("/catalog/install", dependencies=[Depends(require_write_csrf)])
async def install_catalog(body: Mutation, request: Request, principal=Depends(require_owner)):
    value = body.value
    return await audit(request, principal, value.get("id", ""), "catalog-install",
                       manager(request).market_install(body.expected_revision, value["source_id"],
                                                       value["id"], value["version"], update=bool(value.get("update"))),
                       version=value["version"])


@router.post("/catalog/preview", dependencies=[Depends(require_write_csrf)])
async def preview_catalog(body: Mutation, request: Request, _=Depends(require_owner)):
    value = body.value
    return await manager(request).market_preview(body.expected_revision, value["source_id"], value["id"], value["version"])


@router.get("/{identity}")
async def detail(identity: str, request: Request):
    manager(request).entry(identity)
    return next(item for item in manager(request).snapshot()["plugins"] if item["id"] == identity)


@router.get("/{identity}/models")
async def models(identity: str, request: Request):
    manager(request).entry(identity)
    package = manager(request).registry._external.get(identity)
    return {"models": [caps.model_dump(mode="json") for caps in package.declarations.values()]
            if package and package.available else []}


@router.get("/{identity}/diagnostics")
async def diagnostics(identity: str, request: Request):
    return await manager(request).diagnostics(identity)


@router.get("/{identity}/config")
async def config(identity: str, request: Request):
    return manager(request).config_view(identity)


@router.patch("/{identity}/config", dependencies=[Depends(require_write_csrf)])
async def save_config(identity: str, body: Mutation, request: Request, principal=Depends(require_owner)):
    return await audit(request, principal, identity, "config",
                       manager(request).change(identity, body.expected_revision, "config", body.value))


@router.post("/{identity}/actions/{action}", dependencies=[Depends(require_write_csrf)])
async def action(identity: str, action: str, body: Mutation, request: Request, principal=Depends(require_owner)):
    return await audit(request, principal, identity, action,
                       manager(request).action(identity, body.expected_revision, action, body.value))


@router.delete("/{identity}", dependencies=[Depends(require_write_csrf)])
async def uninstall(identity: str, body: Mutation, request: Request, principal=Depends(require_owner)):
    return await audit(request, principal, identity, "uninstall",
                       manager(request).change(identity, body.expected_revision, "uninstall"))


@router.post("/{identity}/{operation}", dependencies=[Depends(require_write_csrf)])
async def lifecycle(identity: str, operation: str, body: Mutation, request: Request, principal=Depends(require_owner)):
    if operation not in {"enable", "disable", "reload", "probe", "rollback", "update"}:
        raise HTTPException(404, "插件操作不存在")
    if operation == "update":
        return await audit(request, principal, identity, operation,
                           manager(request).market_install(body.expected_revision, body.value["source_id"],
                                                           identity, body.value["version"], update=True),
                           version=body.value["version"])
    return await audit(request, principal, identity, operation,
                       manager(request).change(identity, body.expected_revision, operation, body.value))


@router.get("/{identity}/ui/manifest")
async def ui_manifest(identity: str, request: Request):
    entry = manager(request).entry(identity)
    return {"ui": entry["manifest"]["ui"], "sha256": entry["sha256"], "plugin_id": identity,
            "asset_base": manager(request).ui_access(identity)}


def asset_response(entry, path, request):
    root = manager(request).root / entry["package"]
    if not path.startswith("ui/") or path.endswith((".py", ".yaml", ".map")):
        raise HTTPException(404, "插件 UI 资源不存在")
    candidate = safe_file(root, path)
    mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return FileResponse(candidate, media_type=mime)


@router.get("/{identity}/ui/assets/{path:path}")
async def ui_asset(identity: str, path: str, request: Request):
    entry = manager(request).entry(identity)
    return asset_response(entry, path, request)


@ui_router.get("/{token}/{path:path}")
async def sandbox_asset(token: str, path: str, request: Request):
    # A capability grants only this installed version's static ui/ subtree.
    # It never authenticates management APIs or grants model execution.
    return asset_response(manager(request).ui_entry(token), path, request)
