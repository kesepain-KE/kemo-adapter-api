from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.domain import Catalog, ConfigurationError, Credential, Deployment, ExtensionState, PublicModel, RuntimeOptions, Upstream
from gateway.extensions import install, read_manifest, extension_digest
from gateway.runtime import BUILTINS, adapter_modalities
from gateway.model_probe import ModelProbe
from gateway.model_names import TEMPLATE_VENDORS, public_model_name
from gateway.token_counting import TOKENIZER_CATALOG
from gateway.keys import KeyPolicyError
from gateway.security import preview
from gateway.store import RevisionConflict, dump, revision
from gateway.transport import validate_url
from web.backend.auth_routes import require_admin, require_owner, require_write_csrf
from api.middleware import control_plane_auth_required


from fastapi.routing import APIRoute
from jsonschema.exceptions import ValidationError as SchemaValidationError
from core.provider_contract import ProviderException


class ManagementRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                return await original(request)
            except ConfigurationError as exc:
                return JSONResponse(status_code=422, content={"detail": {
                    "code": exc.code, "field": exc.field, "message": str(exc), "details": exc.details,
                }})
            except ValidationError as exc:
                fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": {
                    "code": "INVALID_CONFIGURATION", "field": fields[0] if fields else "",
                    "message": "配置字段无效", "fields": fields,
                }})
            except SchemaValidationError as exc:
                field = "profile." + ".".join(str(part) for part in exc.absolute_path)
                return JSONResponse(status_code=422, content={"detail": {
                    "code": "INVALID_PROFILE", "field": field.rstrip("."),
                    "message": "适配器 Profile 不符合清单中的 schema",
                }})
            except (ValueError, KeyError, TypeError):
                # Pydantic/jsonschema exceptions may contain submitted secrets.
                raise HTTPException(422, "管理请求字段无效，请检查配置和实体引用") from None
            except LookupError:
                raise HTTPException(404, "对象不存在") from None
            except ProviderException as exc:
                message = "上游鉴权失败，请检查密钥与鉴权方式" if exc.error.provider_status in {401, 403} else exc.error.message
                return JSONResponse(status_code=502, content={"detail": {
                    "code": exc.error.code, "message": message, "provider_status": exc.error.provider_status,
                }})
            except TimeoutError:
                raise HTTPException(504, "上游操作超时") from None
        return handle


router = APIRouter(prefix="/admin/api", tags=["management"], route_class=ManagementRoute,
                   dependencies=[Depends(require_admin)])


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str
    value: dict[str, Any] = Field(default_factory=dict)


def runtime(request):
    return request.app.state.registry


def catalog(request):
    return runtime(request).store.catalog()


def key_revision(registry):
    return hashlib.sha256(dump(registry.keys.list()).encode()).hexdigest()


def check_keys(registry, expected):
    if expected != key_revision(registry):
        raise HTTPException(409, "密钥列表已更新，请刷新后重试")


def redact_upstream(upstream, vault=None):
    document = upstream.model_dump(mode="json")
    document["credentials"] = [{
        "id": c.id, "preview": preview(vault.decrypt(c.encrypted, f"{upstream.id}:{c.id}")) if vault else c.preview,
        "enabled": c.enabled, "priority": c.priority,
    } for c in upstream.credentials]
    return document


async def publish(request, body, candidate, *, model_renames=None):
    registry = runtime(request)
    try:
        result = await registry.publish(candidate, body.expected_revision, "web-console", model_renames=model_renames)
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (ConfigurationError, SchemaValidationError, ValidationError):
        raise
    except (ValueError, KeyError, OSError):
        raise HTTPException(422, "候选目录校验失败，请检查实体引用、能力、适配器 Profile 或密钥配置") from None
    return {"revision": result}


def patch_collection(current, name, identity, value, *, create=False, delete=False):
    items = current.model_dump(mode="json")[name]
    old = next((item for item in items if item["id"] == identity), None)
    if create and old is not None:
        raise HTTPException(409, "ID 已存在")
    if not create and old is None:
        raise HTTPException(404, "对象不存在")
    updated = [item for item in items if item["id"] != identity]
    if not delete:
        if "id" in value and value["id"] != identity:
            raise HTTPException(422, "修改对象时不能更换 ID")
        updated.append({**(old or {}), **value, "id": identity})
    return Catalog.model_validate({**current.model_dump(mode="json"), name: updated})


def extension_catalog(registry):
    result = {"protocol_adapters": [], "provider_templates": []}
    state = {(s.kind, s.id): s.enabled for s in registry.store.catalog().extensions}
    for kind in result:
        root = registry.project_root / "extensions" / kind
        if not root.exists():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name.startswith(".") or path.name.startswith("tmp"):
                continue
            try:
                manifest = read_manifest(path, kind)
                document = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else manifest
                result[kind].append({**document, "enabled": state.get((kind, path.name), False), "valid": True})
            except (ValueError, OSError):
                result[kind].append({"id": path.name, "enabled": False, "valid": False})
    return result


@router.get("/console")
async def console(request: Request, principal=Depends(require_admin)):
    registry = runtime(request)
    current = registry.store.catalog()
    attempts = registry.store.attempts(100)
    extensions = extension_catalog(registry)
    builtins = [{**cls.manifest.model_dump(mode="json"), "modalities": adapter_modalities(cls)} for cls in BUILTINS]
    templates = []
    for value in registry.current.templates.values():
        template = json.loads(value)
        if template.get("builtin"):
            templates.append({**template, "enabled": True, "valid": True})
    version_path = registry.project_root / "version.json"
    version = json.loads(version_path.read_text(encoding="utf-8")) if version_path.exists() else {"version": request.app.version}
    return {
        "revision": registry.current.revision, "key_revision": key_revision(registry),
        "version": version, "protocol_version": "2.0",
        "upstreams": [redact_upstream(u, registry.vault) for u in current.upstreams],
        "models": [{**m.model_dump(mode="json"), "declaration": m.declaration().model_dump(mode="json")} for m in current.models],
        "deployments": [d.model_dump(mode="json") for d in current.deployments],
        "settings": current.settings.model_dump(mode="json"),
        "keys": registry.keys.list(), "adapters": builtins + extensions["protocol_adapters"],
        "templates": templates + extensions["provider_templates"],
        "model_vendors": TEMPLATE_VENDORS,
        "tokenizers": [{"id": name, "label": config["label"]} for name, config in TOKENIZER_CATALOG.items()],
        "runtime": request.app.state.runtime_state.snapshot(), "health": registry.router.health(),
        "credential_health": registry.router.credential_health(),
        "retired_generations": len(registry._retired), "attempts": attempts,
        "authentication": {"required": control_plane_auth_required(request)},
        "permissions": {"can_restart": "owner" in principal.scopes, "can_manage_keys": "owner" in principal.scopes,
                        "can_manage_extensions": "owner" in principal.scopes},
        "startup": {
            "host": request.app.state.settings.host, "port": request.app.state.settings.port,
            "base_url": request.app.state.settings.base_url,
            "execution_timeout": request.app.state.settings.model_execution_timeout_seconds,
            "concurrency": request.app.state.settings.max_concurrent_executions,
            "heartbeat": request.app.state.settings.sse_heartbeat_seconds,
            "timezone": request.app.state.settings.statistics_timezone,
            "retention_days": request.app.state.settings.log_retention_days,
        },
    }


async def connection_value(request, value, old=None):
    allowed = set(Upstream.model_fields) - {"credentials", "discovered_models", "discovered_at"}
    if set(value) - allowed - {"keys"}:
        raise HTTPException(422, "上游包含未知字段；密钥请通过 keys 字段写入")
    document = old.model_dump(mode="json") if old else {}
    if old is None and value.get("template_id"):
        template = runtime(request).current.templates.get(value["template_id"])
        if template is None:
            raise HTTPException(422, "请选择已启用的模板")
        defaults = json.loads(template)["template"]
        document.update({k: v for k, v in defaults.items() if k != "catalog" and v is not None})
    document.update({k: v for k, v in value.items() if k != "keys"})
    if not document.get("id"):
        document["id"] = "up_" + uuid4().hex[:12]
    if "keys" in value:
        credentials, seen = [], set()
        existing = {c.id: c for c in old.credentials} if old else {}
        for item in value["keys"]:
            if not isinstance(item, dict) or set(item) - {"id", "value", "enabled", "priority"}:
                raise HTTPException(422, "密钥字段无效")
            identity = item.get("id") or "cred_" + uuid4().hex[:12]
            if identity in seen:
                raise HTTPException(422, "密钥 ID 重复")
            seen.add(identity)
            if item.get("value"):
                raw = item["value"]
                if not isinstance(raw, str) or len(raw) > 8192:
                    raise HTTPException(422, "密钥格式无效")
                encrypted = runtime(request).vault.encrypt(raw, f'{document["id"]}:{identity}')
                credentials.append(Credential(id=identity, encrypted=encrypted, preview=preview(raw),
                                              enabled=item.get("enabled", True), priority=item.get("priority", 0)))
            elif identity in existing:
                credentials.append(existing[identity].model_copy(update={
                    "enabled": item.get("enabled", existing[identity].enabled),
                    "priority": item.get("priority", existing[identity].priority),
                }))
            else:
                raise HTTPException(422, "新增密钥不能为空")
        document["credentials"] = [c.model_dump(mode="json") for c in credentials]
    candidate = Upstream.model_validate(document)
    validate_url(candidate.base_url, candidate.allow_private_network)
    return candidate


@router.post("/upstreams", dependencies=[Depends(require_write_csrf)])
async def create_upstream(body: Mutation, request: Request):
    current = catalog(request)
    value = await connection_value(request, body.value)
    candidate = patch_collection(current, "upstreams", value.id, value.model_dump(mode="json"), create=True)
    return {**await publish(request, body, candidate), "id": value.id, "upstream": redact_upstream(value, runtime(request).vault)}


@router.patch("/upstreams/{identity}", dependencies=[Depends(require_write_csrf)])
async def patch_upstream(identity: str, body: Mutation, request: Request):
    current = catalog(request)
    old = next((u for u in current.upstreams if u.id == identity), None)
    if old is None:
        raise HTTPException(404, "上游不存在")
    value = await connection_value(request, body.value, old)
    result = await publish(request, body, patch_collection(current, "upstreams", identity, value.model_dump(mode="json")))
    return {**result, "upstream": redact_upstream(value, runtime(request).vault)}


@router.delete("/upstreams/{identity}", dependencies=[Depends(require_write_csrf)])
async def delete_upstream(identity: str, body: Mutation, request: Request):
    current = catalog(request)
    # Referenced upstreams must first be removed from routing explicitly.
    if any(d.upstream_id == identity for d in current.deployments):
        raise HTTPException(409, "上游仍有 Deployment，请先删除对应路由")
    return await publish(request, body, patch_collection(current, "upstreams", identity, {}, delete=True))


def discovery_reference(registry, generation, identity):
    configured = next(upstream for upstream in registry.store.catalog().upstreams if upstream.id == identity)
    template = generation.templates.get(configured.template_id)
    models = json.loads(template)["template"]["catalog"] if template else []
    return {"models": models, "source": "template" if template else "manual", "verification": "unverified",
            "message": "上游未提供模型目录；模板内容仅供参考，请核对模型 ID"}


@router.post("/upstreams/{identity}/discover", dependencies=[Depends(require_write_csrf)])
async def discover(identity: str, request: Request):
    registry = runtime(request)
    generation = registry.current
    upstream = generation.upstreams.get(identity)
    if upstream is None:
        raise HTTPException(404, "上游不存在")
    adapter = generation.adapters[upstream.adapter_id]
    if not adapter.manifest.discovery:
        return discovery_reference(registry, generation, identity)
    credential = next(iter(registry.router.credentials(upstream)), None)
    if upstream.auth_scheme != "none" and credential is None:
        raise HTTPException(422, "上游缺少启用的密钥")
    # Discovery also leases the generation, so concurrent publishing cannot close it.
    registry._references[id(generation)] = registry._references.get(id(generation), 0) + 1
    try:
        import asyncio
        async with asyncio.timeout(upstream.timeout):
            from gateway.extensions import WorkerAdapter
            source = json.loads(upstream.worker_document) if isinstance(adapter, WorkerAdapter) else upstream
            models = await adapter.list_models(source, registry.router.headers(generation, upstream, credential))
        registry.router.record_credential(generation, upstream, credential)
        return {"models": models, "source": "upstream", "verification": "unknown"} if models else discovery_reference(registry, generation, identity)
    except ProviderException as exc:
        registry.router.record_credential(generation, upstream, credential, exc.error)
        if exc.error.provider_status == 404:
            return discovery_reference(registry, generation, identity)
        raise
    finally:
        registry._references[id(generation)] -= 1
        await registry._reap(generation)


@router.post("/upstreams/{identity}/register", dependencies=[Depends(require_write_csrf)])
async def register_models(identity: str, body: Mutation, request: Request):
    registry = runtime(request)
    generation = registry.current
    current = catalog(request)
    if identity not in {u.id for u in current.upstreams}:
        raise HTTPException(404, "上游不存在")
    if set(body.value) != {"models"}:
        raise HTTPException(422, "登记请求必须提供 models 数组")
    if body.expected_revision != generation.revision:
        raise HTTPException(409, "目录已更新，请刷新后重试")
    upstream = generation.upstreams[identity]
    configured = next(u for u in current.upstreams if u.id == identity)
    adapter = generation.adapters[upstream.adapter_id]
    value = current.model_dump(mode="json")
    pending = []
    registered = {(d.public_model, d.upstream_model) for d in current.deployments if d.upstream_id == identity}
    selected = set()
    for item in body.value["models"]:
        if set(item) - {"upstream_model", "public_model", "task", "capabilities"}:
            raise HTTPException(422, "模型登记字段无效")
        model_id = item.get("public_model") or public_model_name(upstream.name, item["upstream_model"], configured.template_id)
        if model_id in selected or (model_id, item["upstream_model"]) in registered:
            raise HTTPException(422, "模型已登记或公开模型名重复")
        selected.add(model_id)
        task = item.get("task", adapter.manifest.operations[0])
        if task not in adapter.manifest.operations:
            raise HTTPException(422, "任务类型超出适配器能力")
        deployment = Deployment(id="dep_" + uuid4().hex[:16], upstream_id=identity,
                                upstream_model=item["upstream_model"], public_model=model_id)
        if model_id not in {m["id"] for m in value["models"]}:
            model = PublicModel(id=model_id, task=task, capabilities=item.get("capabilities", {}))
            value["models"].append(model.model_dump(mode="json"))
        if "capabilities" not in item:
            pending.append((deployment, task))
        value["deployments"].append(deployment.model_dump(mode="json"))
    detection = []
    if pending:
        credential = next(iter(registry.router.credentials(upstream)), None)
        if upstream.auth_scheme != "none" and credential is None:
            raise HTTPException(422, "上游缺少启用的密钥")
        semaphore = asyncio.Semaphore(3)
        async def probe(deployment, task):
            async with semaphore:
                return await ModelProbe(
                    registry, generation, upstream, credential, deployment, task, assets=request.app.state.assets,
                ).run()
        # Hold the generation for the complete batch, including concurrent publication.
        registry._references[id(generation)] = registry._references.get(id(generation), 0) + 1
        try:
            results = await asyncio.gather(*(probe(deployment, task) for deployment, task in pending),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            existing = {m.id for m in current.models}
            for model, report in results:
                detection.append(report)
                if model.id not in existing:
                    value["models"] = [model.model_dump(mode="json") if m["id"] == model.id else m for m in value["models"]]
                else:
                    required = next(m for m in current.models if m.id == model.id).declaration()
                    detected = model.declaration()
                    if (required.task != detected.task
                            or required.streaming and not detected.streaming
                            or set(required.input_modalities) - set(detected.input_modalities)
                            or required.tools.function_calling and not detected.tools.function_calling
                            or required.structured_output.supported and not detected.structured_output.supported
                            or set(required.reasoning.efforts) - set(detected.reasoning.efforts)):
                        raise HTTPException(422, "检测结果无法满足已有公开模型的能力，请使用其他公开模型名")
        finally:
            registry._references[id(generation)] -= 1
            await registry._reap(generation)
    return {**await publish(request, body, Catalog.model_validate(value)), "detection": detection}


@router.post("/models", dependencies=[Depends(require_write_csrf)])
async def create_model(body: Mutation, request: Request):
    if set(body.value) & {"verification", "verified_at"}:
        raise HTTPException(422, "验证状态由真实探测结果产生")
    value = PublicModel.model_validate(body.value)
    return await publish(request, body, patch_collection(catalog(request), "models", value.id, value.model_dump(mode="json"), create=True))


@router.patch("/models/{identity:path}", dependencies=[Depends(require_write_csrf)])
async def patch_model(identity: str, body: Mutation, request: Request):
    # Verification is evidence, not a user-editable success switch.
    if set(body.value) & {"verification", "verified_at"}:
        raise HTTPException(422, "验证状态由真实探测结果产生")
    value = body.value
    if "capabilities" in value:
        value = {**value, "verification": "declared", "verified_at": None, "evidence": "operator declaration"}
    if "id" in value and value["id"] != identity:
        current = catalog(request)
        if any(model.id == value["id"] for model in current.models):
            raise HTTPException(409, "公开模型名已存在")
        old = next((model for model in current.models if model.id == identity), None)
        if old is None:
            raise HTTPException(404, "模型不存在")
        renamed = PublicModel.model_validate({**old.model_dump(mode="json"), **value})
        candidate = current.model_dump(mode="json")
        candidate["models"] = [renamed.model_dump(mode="json") if m["id"] == identity else m for m in candidate["models"]]
        for deployment in candidate["deployments"]:
            if deployment["public_model"] == identity:
                deployment["public_model"] = renamed.id
        return await publish(request, body, Catalog.model_validate(candidate), model_renames={identity: renamed.id})
    return await publish(request, body, patch_collection(catalog(request), "models", identity, value))


@router.delete("/models/{identity:path}", dependencies=[Depends(require_write_csrf)])
async def delete_model(identity: str, body: Mutation, request: Request):
    current = catalog(request)
    if not any(model.id == identity for model in current.models):
        raise HTTPException(404, "模型不存在")
    candidate = current.model_dump(mode="json")
    candidate["models"] = [model for model in candidate["models"] if model["id"] != identity]
    candidate["deployments"] = [deployment for deployment in candidate["deployments"]
                                if deployment["public_model"] != identity]
    return await publish(request, body, Catalog.model_validate(candidate))


@router.post("/deployments", dependencies=[Depends(require_write_csrf)])
async def create_deployment(body: Mutation, request: Request):
    value = Deployment.model_validate({**body.value, "id": body.value.get("id") or "dep_" + uuid4().hex[:16]})
    return await publish(request, body, patch_collection(catalog(request), "deployments", value.id, value.model_dump(mode="json"), create=True))


@router.post("/deployments/{identity}/test", dependencies=[Depends(require_write_csrf)])
@router.post("/deployments/{identity}/probe", dependencies=[Depends(require_write_csrf)])
async def inspect_deployment(identity: str, body: Mutation, request: Request, first_token: bool = False):
    registry = runtime(request)
    generation = registry.current
    if body.expected_revision != generation.revision:
        raise HTTPException(409, "目录已更新，请刷新后重试")
    if body.value:
        raise HTTPException(422, "测试和探测使用已保存的部署配置")
    deployment = next((d for d in generation.deployments if d.id == identity), None)
    if deployment is None:
        raise HTTPException(404, "部署不存在")
    upstream = generation.upstreams[deployment.upstream_id]
    if not deployment.enabled or not upstream.enabled:
        raise HTTPException(422, "请先启用部署和上游")
    credential = next(iter(registry.router.credentials(upstream)), None)
    if upstream.auth_scheme != "none" and credential is None:
        raise HTTPException(422, "上游缺少启用的密钥")
    testing = request.url.path.endswith("/test")
    probe = ModelProbe(
        registry, generation, upstream, credential, deployment, generation.models[deployment.public_model].task,
        assets=request.app.state.assets,
        subject="model-test" if testing else "capability-probe",
    )
    registry._references[id(generation)] = registry._references.get(id(generation), 0) + 1
    try:
        if testing:
            result = await probe.test(first_token=first_token)
        else:
            model, report = await probe.run()
            result = {**report, "capabilities": model.capabilities}
        return {**result, "deployment_id": deployment.id, "model": deployment.public_model, "revision": generation.revision}
    finally:
        registry._references[id(generation)] -= 1
        if registry._references[id(generation)] == 0:
            registry._references.pop(id(generation))
        await registry._reap(generation)


@router.patch("/deployments/{identity}", dependencies=[Depends(require_write_csrf)])
async def patch_deployment(identity: str, body: Mutation, request: Request):
    return await publish(request, body, patch_collection(catalog(request), "deployments", identity, body.value))


@router.delete("/deployments/{identity}", dependencies=[Depends(require_write_csrf)])
async def delete_deployment(identity: str, body: Mutation, request: Request, remove_orphan_model: bool = False):
    current = catalog(request)
    target = next((d for d in current.deployments if d.id == identity), None)
    candidate = patch_collection(current, "deployments", identity, {}, delete=True)
    if remove_orphan_model and not any(d.public_model == target.public_model for d in candidate.deployments):
        candidate = patch_collection(candidate, "models", target.public_model, {}, delete=True)
    return await publish(request, body, candidate)


@router.patch("/settings", dependencies=[Depends(require_write_csrf)])
async def settings(body: Mutation, request: Request):
    current = catalog(request)
    document = current.model_dump(mode="json")
    document["settings"].update(body.value)
    return await publish(request, body, Catalog.model_validate(document))


@router.post("/keys", dependencies=[Depends(require_owner), Depends(require_write_csrf)])
async def create_key(body: Mutation, request: Request):
    registry = runtime(request)
    async with registry._lock:
        check_keys(registry, body.expected_revision)
        policy = dict(body.value)
        token = policy.pop("token", None)
        identity, raw = registry.keys.create(policy, token)
        registry.store.audit("web-console", "create-key", identity)
        return {"id": identity, "token": raw, "key_revision": key_revision(registry)}


@router.patch("/keys/{identity}", dependencies=[Depends(require_owner), Depends(require_write_csrf)])
async def patch_key(identity: str, body: Mutation, request: Request):
    registry = runtime(request)
    async with registry._lock:
        check_keys(registry, body.expected_revision)
        registry.keys.patch(identity, body.value)
        registry.store.audit("web-console", "patch-key", identity)
        return {"key_revision": key_revision(registry)}


@router.get("/logs")
async def logs(request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
               model: str | None = None, start: datetime | None = None, end: datetime | None = None,
               status: Literal["running", "completed", "failed", "cancelled"] | None = None,
               task: Literal["llm", "embedding", "rerank"] | None = None,
               log_type: Literal["request", "model-test", "capability-probe"] | None = None):
    if any(value is not None and value.tzinfo is None for value in (start, end)):
        raise HTTPException(422, "日志时间必须包含时区")
    if start and end and start > end:
        raise HTTPException(422, "结束时间不能早于开始时间")
    return {"items": runtime(request).store.attempts(
        limit, offset, model, start=start.astimezone(timezone.utc).isoformat() if start else None,
        end=end.astimezone(timezone.utc).isoformat() if end else None, status=status, task=task, log_type=log_type,
    )}


@router.get("/logs/{request_id}/{attempt_no}")
async def log_detail(request_id: str, attempt_no: int, request: Request):
    metering = runtime(request).router.metering
    body = metering.detail(request_id, attempt_no)
    # A retried execution's final response is not an earlier attempt's response.
    # New attempt traces also redact credential echoes before persistence.
    record = None
    if body is None and metering.log_detail_mode == "full":
        record = await request.app.state.executions.get_by_response_id("gateway", request_id)
    response = (record.batch or record.response) if record else None
    return JSONResponse({
        "detail": body, "gateway_response": response.model_dump(mode="json", by_alias=True) if response else None,
    }, headers={"Cache-Control": "no-store"})


@router.post("/keys/{identity}/reveal", dependencies=[Depends(require_owner), Depends(require_write_csrf)])
async def reveal_key(identity: str, body: Mutation, request: Request):
    registry = runtime(request)
    async with registry._lock:
        check_keys(registry, body.expected_revision)
        try:
            token = registry.keys.reveal(identity)
        except KeyPolicyError as exc:
            raise HTTPException(409, str(exc)) from None
        registry.store.audit("web-console", "copy-key", identity)
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})


@router.delete("/keys/{identity}", dependencies=[Depends(require_owner), Depends(require_write_csrf)])
async def delete_key(identity: str, body: Mutation, request: Request):
    registry = runtime(request)
    async with registry._lock:
        check_keys(registry, body.expected_revision)
        registry.keys.delete(identity)
        registry.store.audit("web-console", "delete-key", identity)
        return {"key_revision": key_revision(registry)}


@router.post("/extensions/{kind}/install", dependencies=[Depends(require_owner), Depends(require_write_csrf)])
async def install_extension(kind: str, request: Request, expected_revision: str = Form(...), file: UploadFile = File(...)):
    registry = runtime(request)
    async with registry._lock:
        if registry.current.revision != expected_revision:
            raise HTTPException(409, "目录已更新")
        content = await file.read(16 * 1024 * 1024 + 1)
        identity = install(registry.project_root, kind, content)
        registry.store.audit("web-console", "install-extension", f"{kind}/{identity}")
    # Installation is intentionally inactive; enabling runs full candidate validation.
    return {"id": identity, "enabled": False}


@router.patch("/extensions/{kind}/{identity}", dependencies=[Depends(require_owner), Depends(require_write_csrf)])
async def patch_extension(kind: str, identity: str, body: Mutation, request: Request):
    if set(body.value) != {"enabled"}:
        raise HTTPException(422, "扩展只允许修改 enabled")
    state = ExtensionState(id=identity, kind=kind, **body.value)
    if state.enabled:
        path = runtime(request).project_root / "extensions" / kind / identity
        read_manifest(path, kind)
        state = state.model_copy(update={"digest": extension_digest(path, kind)})
    current = catalog(request)
    value = current.model_dump(mode="json")
    value["extensions"] = [e for e in value["extensions"] if (e["kind"], e["id"]) != (kind, identity)]
    value["extensions"].append(state.model_dump(mode="json"))
    return await publish(request, body, Catalog.model_validate(value))


@router.get("/extensions/protocol_adapters/{identity}/logs")
async def worker_logs(identity: str, request: Request):
    from gateway.extensions import WorkerAdapter
    adapter = runtime(request).current.adapters.get(identity)
    if not isinstance(adapter, WorkerAdapter):
        raise HTTPException(404, "此扩展 worker 当前未运行")
    return {"adapter_id": identity, "lines": [adapter._redact(line) for line in adapter.logs]}


@router.get("/catalog.yaml", response_class=PlainTextResponse)
async def export_yaml(request: Request):
    value = catalog(request).model_dump(mode="json")
    value["upstreams"] = [redact_upstream(u) for u in catalog(request).upstreams]
    return PlainTextResponse(dump(value), media_type="application/yaml",
                             headers={"Content-Disposition": 'attachment; filename="catalog.yaml"'})
