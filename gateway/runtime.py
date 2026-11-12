from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import random
import time
from types import MappingProxyType
from typing import Any

import jsonschema

from core.models import ModelCapabilities, Usage
from core.provider_contract import ProviderEventKind as Kind, ProviderException
from gateway.adapters.builtin.common import unsupported
from gateway.domain import Catalog, Upstream
from gateway.auth import RequestHeaders
from gateway.connection import validate_connection
from gateway.federation import validate_self_target
from gateway.extensions import WorkerAdapter, read_manifest, extension_digest
from gateway.keys import KeyPolicyError, KeyService
from gateway.metering import Metering
from gateway.security import SecretVault
from gateway.store import Store, RevisionConflict, revision
from gateway.transport import Transport, upstream_error
from gateway.tracing import current_trace, document
from gateway.store import utc_now
from gateway.resources import SOURCE_ROOT, builtin_templates, inventory, load_builtin_adapters, resource_stamp


BUILTINS = load_builtin_adapters()


def adapter_modalities(adapter):
    """Media that the adapter can actually encode/decode, independent of a model."""
    return adapter.manifest.media.model_dump(mode="json")


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class Generation:
    revision: str
    catalog_yaml: str
    adapters: Any
    upstreams: Any
    deployments: Any
    declarations: Any
    models: Any
    options: Any
    transport: Transport
    templates: Any = field(default_factory=dict)
    routes: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class RuntimeUpstream:
    id: str
    adapter_id: str
    base_url: str
    timeout: float
    enabled: bool
    allow_private_network: bool
    auth_scheme: str
    headers: Any
    profile: Any
    credentials: tuple
    worker_document: str
    name: str = ""
    tokenizer: str = "auto"
    proxy: str | None = None


class ModelRoute:
    """An admitted public model in one runtime generation."""
    provider_id = "gateway"

    def __init__(self, registry, generation, model):
        self.registry, self.generation, self.model = registry, generation, model
        self.models = frozenset({model})

    async def capabilities(self, model):
        return ModelCapabilities.model_validate_json(self.generation.declarations[model])

    async def execute(self, request, context):
        return await self.registry.router.call(self, "execute", request, context)

    def stream(self, request, context):
        return self.registry.router.stream(self, request, context)

    async def embed(self, request, context):
        return await self.registry.router.call(self, "embed", request, context)

    async def rerank(self, request, context):
        return await self.registry.router.call(self, "rerank", request, context)

    async def cancel(self, provider_response_id, context):
        # The execution owner cancels the producer task. httpx closes that exact
        # generation's response/connection through the async context manager.
        return None


class Router:
    def __init__(self, registry, keys, metering):
        self.registry, self.keys, self.metering = registry, keys, metering
        self._key_cursor = {}
        self._cooldown = {}
        self._rpm = {}
        self._credential_health = {}

    def credentials(self, upstream):
        now = time.monotonic()
        configured = [c for c in upstream.credentials if c.enabled]
        if len(configured) == 1:
            credential = configured[0]
            health = self._credential_health.get((upstream.id, credential.id), {})
            if health.get("encrypted") == credential.encrypted:
                health["until"] = 0
            return configured
        enabled = [c for c in configured if (
            (health := self._credential_health.get((upstream.id, c.id), {})).get("encrypted") != c.encrypted
            or health.get("until", 0) <= now
        )]
        ordered = []
        for priority in sorted({c.priority for c in enabled}, reverse=True):
            group = [c for c in enabled if c.priority == priority]
            identity = (upstream.id, priority)
            start = self._key_cursor.get(identity, 0) % len(group)
            self._key_cursor[identity] = start + 1
            ordered.extend(group[start:] + group[:start])
        return ordered

    def record_credential(self, generation, upstream, credential, error=None):
        if credential is None:
            return
        status = error.provider_status if error else None
        # A transport failure says nothing about whether this credential works.
        if error and status not in {401, 403, 429, 500, 502, 503, 504}:
            return
        cooldown = generation.options.cooldown_seconds if error and sum(c.enabled for c in upstream.credentials) > 1 else 0
        self._credential_health[(upstream.id, credential.id)] = {
            "encrypted": credential.encrypted,
            "status": "healthy" if error is None else "invalid" if status in {401, 403}
                      else "cooldown" if status == 429 and cooldown else "unavailable",
            "checked_at": utc_now(), "error_code": error.code if error else None,
            "provider_status": status,
            "until": time.monotonic() + cooldown if cooldown else 0,
        }

    def credential_health(self):
        result, now = {}, time.monotonic()
        for upstream in self.registry.current.upstreams.values():
            result[upstream.id] = {}
            for credential in upstream.credentials:
                health = self._credential_health.get((upstream.id, credential.id), {})
                if health.get("encrypted") != credential.encrypted:
                    health = {}
                remaining = max(0, round(health.get("until", 0) - now))
                result[upstream.id][credential.id] = {
                    "status": "disabled" if not credential.enabled else health.get("status", "unknown"),
                    "checked_at": health.get("checked_at"), "error_code": health.get("error_code"),
                    "provider_status": health.get("provider_status"), "cooldown_seconds": remaining,
                }
        return result

    def _unavailable_error(self, generation, candidates, reason):
        now = time.monotonic()
        ready_at = []
        for deployment in candidates:
            upstream = generation.upstreams[deployment.upstream_id]
            deployment_until = self._cooldown.get(deployment.id, 0)
            if upstream.auth_scheme == "none":
                ready_at.append(deployment_until)
                continue
            for credential in upstream.credentials:
                if not credential.enabled:
                    continue
                health = self._credential_health.get((upstream.id, credential.id), {})
                credential_until = health.get("until", 0) if health.get("encrypted") == credential.encrypted else 0
                ready_at.append(max(deployment_until, credential_until))
        error = upstream_error(code=reason, retryable=bool(ready_at))
        if ready_at:
            error.error.retry_after_ms = math.ceil(max(0, min(ready_at) - now) * 1000)
            error.error.message = ("上游凭据冷却中，请稍后重试" if reason == "NO_UPSTREAM_CREDENTIAL"
                                   else "模型路由冷却中，请稍后重试")
        return error

    def _revive_earliest_credential(self, generation, candidates):
        now, cooling = time.monotonic(), []
        for deployment in candidates:
            upstream = generation.upstreams[deployment.upstream_id]
            if upstream.auth_scheme == "none":
                continue
            for credential in upstream.credentials:
                if not credential.enabled:
                    continue
                health = self._credential_health.get((upstream.id, credential.id), {})
                if health.get("encrypted") != credential.encrypted or health.get("until", 0) <= now:
                    return None
                cooling.append((health["until"], deployment, upstream, credential))
        if not cooling:
            return None
        _, deployment, upstream, credential = min(cooling, key=lambda item: item[0])
        # Release one path for a real attempt; do not mark an untested key healthy.
        self._credential_health[(upstream.id, credential.id)]["until"] = 0
        self._cooldown.pop(deployment.id, None)
        return deployment, upstream, credential

    def plan(self, route, context):
        generation = route.generation
        candidates = [d for d in generation.deployments if d.public_model == route.model and d.enabled
                      and generation.upstreams[d.upstream_id].enabled]
        for deployment in candidates:
            upstream = generation.upstreams[deployment.upstream_id]
            if upstream.auth_scheme != "none" and sum(c.enabled for c in upstream.credentials) == 1:
                self._cooldown.pop(deployment.id, None)
        now = time.monotonic()
        available = [d for d in candidates if self._cooldown.get(d.id, 0) <= now]
        plan = []
        for priority in sorted({d.priority for d in available}, reverse=True):
            remaining = [d for d in available if d.priority == priority]
            while remaining:
                selected = random.choices(remaining, weights=[d.weight for d in remaining], k=1)[0]
                remaining.remove(selected)
                upstream = generation.upstreams[selected.upstream_id]
                credentials = self.credentials(upstream)
                if upstream.auth_scheme == "none":
                    credentials = [None]
                plan.extend((selected, upstream, c) for c in credentials)
        if not plan:
            revived = self._revive_earliest_credential(generation, candidates)
            if revived is None:
                reason = "NO_UPSTREAM_CREDENTIAL" if available else "NO_HEALTHY_DEPLOYMENT"
                raise self._unavailable_error(generation, candidates, reason)
            plan = [revived]
            available = [revived[0]]
        try:
            self.keys.admit(context, generation.models[route.model], available, generation.options.max_attempts)
        except KeyPolicyError as exc:
            from core.models import ErrorObject
            raise ProviderException(ErrorObject(type="rate_limit_error", code="RATE_LIMITED",
                                                message=str(exc), retryable=False)) from None
        return plan[:generation.options.max_attempts]

    def headers(self, generation, upstream, credential):
        manifest = generation.adapters[upstream.adapter_id].manifest
        headers = RequestHeaders({**upstream.headers, **manifest.fixed_headers})
        if credential is None:
            return headers
        value = self.registry.vault.decrypt(credential.encrypted, f"{upstream.id}:{credential.id}")
        trace = current_trace.get()
        if trace is not None:
            trace.secrets = (value,)
        scheme = next(scheme for scheme in manifest.auth_schemes if scheme.id == upstream.auth_scheme)
        if scheme.location == "header":
            headers[scheme.name] = scheme.prefix + value
        elif scheme.location == "query":
            headers.query[scheme.name] = scheme.prefix + value
        headers.secrets = (value,)
        return headers

    def reserve_deployment(self, deployment):
        now = time.monotonic()
        started, count = self._rpm.get(deployment.id, (now, 0))
        if now - started >= 60:
            started, count = now, 0
        if deployment.rpm and count >= deployment.rpm:
            return False
        self._rpm[deployment.id] = (started, count + 1)
        return True

    def failed(self, generation, deployment, error, credential=None):
        if error.provider_status in {500, 502, 503, 504} or error.provider_status == 429 and credential is None:
            upstream = generation.upstreams[deployment.upstream_id]
            if credential is None or sum(c.enabled for c in upstream.credentials) > 1:
                self._cooldown[deployment.id] = time.monotonic() + generation.options.cooldown_seconds

    async def call(self, route, method, request, context):
        generation = route.generation
        last = None
        no = 0
        try:
            for deployment, upstream, credential in self.plan(route, context):
                if not self.reserve_deployment(deployment):
                    continue
                no += 1
                usage, error, cancelled = Usage(), None, False
                task = generation.models[route.model].task
                started = self.metering.start(
                    context, no, deployment, upstream, request=request, method=method, credential=credential, task=task,
                )
                try:
                    with started.capture():
                        headers = self.headers(generation, upstream, credential)
                        adapter = generation.adapters[upstream.adapter_id]
                        async with asyncio.timeout(upstream.timeout):
                            if isinstance(adapter, WorkerAdapter):
                                result = await getattr(adapter, method)(request, context, deployment.upstream_model,
                                                                         json.loads(upstream.worker_document), headers)
                            else:
                                result = await getattr(adapter, method)(request, context, deployment.upstream_model, upstream, headers)
                    usage = result.usage
                    started.response = document(result)
                    error = getattr(result, "error", None)
                    self.record_credential(generation, upstream, credential, error)
                    return result
                except ProviderException as exc:
                    error, last = exc.error, exc
                    if (method == "execute" and set(request.output.modalities) - {"text"}
                            and error.provider_status is None):
                        error.retryable = False
                    self.failed(generation, deployment, error, credential)
                    self.record_credential(generation, upstream, credential, error)
                    if not error.retryable and error.provider_status not in {401, 403}:
                        raise
                except TimeoutError:
                    media_output = method == "execute" and bool(set(request.output.modalities) - {"text"})
                    error = upstream_error(code="UPSTREAM_TIMEOUT", retryable=not media_output).error
                    error.details.update(timeout_scope="total", timeout_seconds=upstream.timeout)
                    last = ProviderException(error)
                    self.record_credential(generation, upstream, credential, error)
                    if media_output:
                        raise last from None
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except Exception:
                    error = upstream_error(code="ADAPTER_BAD_RESULT", retryable=False).error
                    raise ProviderException(error) from None
                finally:
                    self.metering.finish(context, no, started, deployment, usage, error=error, cancelled=cancelled,
                                         upstream=upstream, task=task)
            raise last or upstream_error(code="NO_HEALTHY_DEPLOYMENT")
        finally:
            self.keys.settle(context)

    async def stream(self, route, request, context):
        generation = route.generation
        usage, error, cancelled = Usage(), None, False
        finished = False
        try:
            plan = self.plan(route, context)
            selected = next((p for p in plan if self.reserve_deployment(p[0])), None)
            if selected is None:
                raise upstream_error(code="DEPLOYMENT_RATE_LIMIT")
            deployment, upstream, credential = selected
            started = self.metering.start(context, 1, deployment, upstream, request=request,
                                          method="stream", credential=credential)
            try:
                with started.capture():
                    headers = self.headers(generation, upstream, credential)
                adapter = generation.adapters[upstream.adapter_id]
                source = json.loads(upstream.worker_document) if isinstance(adapter, WorkerAdapter) else upstream
                async with asyncio.timeout(upstream.timeout), aclosing(started.events(
                    adapter.stream(request, context, deployment.upstream_model, source, headers),
                )) as events:
                    async for event in events:
                        if event.kind == Kind.TEXT_DELTA:
                            started.text(event.delta)
                        if event.usage is not None:
                            usage = event.usage
                        if event.result is not None:
                            started.response = document(event.result)
                            usage = event.result.usage
                            error = event.result.error
                        if event.error is not None:
                            error = event.error
                        if event.kind in {Kind.COMPLETED, Kind.INCOMPLETE, Kind.FAILED, Kind.CANCELLED, Kind.ERROR}:
                            cancelled = event.kind == Kind.CANCELLED
                            self.record_credential(generation, upstream, credential, error)
                            self.metering.finish(context, 1, started, deployment, usage, error=error,
                                                 cancelled=cancelled, upstream=upstream)
                            finished = True
                        yield event
                self.record_credential(generation, upstream, credential, error)
            except ProviderException as exc:
                error = exc.error
                self.failed(generation, deployment, error, credential)
                self.record_credential(generation, upstream, credential, error)
                raise
            except TimeoutError:
                error = upstream_error(code="UPSTREAM_TIMEOUT").error
                error.details.update(timeout_scope="total", timeout_seconds=upstream.timeout)
                self.record_credential(generation, upstream, credential, error)
                raise ProviderException(error) from None
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception:
                error = upstream_error(code="ADAPTER_BAD_RESULT").error
                raise ProviderException(error) from None
            finally:
                if not finished:
                    # Cancellation can arrive while the executor consumes a yielded event.
                    cancelled = cancelled or asyncio.current_task().cancelling() > 0
                    if error is None and not cancelled:
                        error = upstream_error(code="ADAPTER_BAD_RESULT").error
                    self.metering.finish(context, 1, started, deployment, usage, error=error, cancelled=cancelled,
                                         upstream=upstream)
        finally:
            self.keys.settle(context)

    def health(self):
        now = time.monotonic()
        return {identity: max(0, round(until - now)) for identity, until in self._cooldown.items() if until > now}


class RuntimeRegistry:
    def __init__(self, project_root: Path, store: Store, vault: SecretVault, *, transport_factory=Transport,
                 log_detail_max_bytes=4 * 1024 * 1024, log_detail_mode="full", gateway_urls=()):
        self.project_root, self.store, self.vault = project_root, store, vault
        self.transport_factory = transport_factory
        self.gateway_urls = gateway_urls
        self.keys = KeyService(store, vault)
        self.router = Router(self, self.keys, Metering(
            store, log_detail_max_bytes=log_detail_max_bytes, log_detail_mode=log_detail_mode,
        ))
        self.current: Generation | None = None
        self._lock = asyncio.Lock()
        self._references = {}
        self._retired = {}
        self._executions = {}
        self._external = {}
        self.last_error = None
        # Resource updates take effect on restart. Publications in this process
        # retain the same official templates and imported adapter classes.
        self.builtin_templates = builtin_templates()
        self.resource_inventory = inventory(SOURCE_ROOT)
        self.resource_stamp = resource_stamp(SOURCE_ROOT)
        self.resource_update_plan = None

    async def build(self, catalog: Catalog):
        transport = self.transport_factory()
        adapters = {cls.manifest.id: cls(transport) for cls in BUILTINS}
        templates = {}
        try:
            templates.update({identity: json.dumps(value) for identity, value in self.builtin_templates.items()})
            for state in catalog.extensions:
                if not state.enabled:
                    continue
                path = self.project_root / "extensions" / state.kind / state.id
                manifest = read_manifest(path, state.kind)
                identity = manifest.id if hasattr(manifest, "id") else manifest["id"]
                if identity != state.id:
                    raise ValueError("扩展清单 ID 与安装目录不一致")
                if state.digest and extension_digest(path, state.kind) != state.digest:
                    raise ValueError("扩展文件已改变，请重新启用以发布新的内容版本")
                if state.kind == "protocol_adapters":
                    if manifest.id in adapters:
                        raise ValueError("扩展不能覆盖内置适配器")
                    adapters[state.id] = await WorkerAdapter.start(path, manifest)
                else:
                    templates[state.id] = json.dumps(manifest)
            for template in templates.values():
                definition = json.loads(template)["template"]
                adapter = adapters.get(definition["adapter_id"])
                if adapter is None or definition["auth_scheme"] not in {scheme.id for scheme in adapter.manifest.auth_schemes}:
                    raise ValueError("模板引用了不可用的适配器或鉴权方式")
                jsonschema.validate({key: value for key, value in definition["profile"].items()
                                     if key not in {"extra_body", "extra_query"}}, adapter.manifest.config_schema)
            upstreams = {}
            for upstream in catalog.upstreams:
                if upstream.adapter_id not in adapters:
                    raise ValueError(f"上游 {upstream.id} 引用了不可用的适配器")
                manifest = adapters[upstream.adapter_id].manifest
                if upstream.template_id is not None and upstream.template_id not in templates:
                    raise ValueError("上游引用的模板未启用")
                if upstream.template_id is not None:
                    definition = json.loads(templates[upstream.template_id])["template"]
                    if definition["adapter_id"] != upstream.adapter_id:
                        raise ValueError("上游协议与所选模板不一致")
                validate_connection(manifest, upstream)
                validate_self_target(upstream.base_url, self.gateway_urls)
                for key in upstream.credentials:
                    self.vault.decrypt(key.encrypted, f"{upstream.id}:{key.id}")
                document = upstream.model_dump(mode="json")
                worker_document = {**document, "credentials": []}
                upstreams[upstream.id] = RuntimeUpstream(
                    upstream.id, upstream.adapter_id, upstream.base_url, upstream.timeout,
                    upstream.enabled, upstream.allow_private_network, upstream.auth_scheme,
                    freeze(document["headers"]), freeze(document["profile"]), upstream.credentials,
                    json.dumps(worker_document),
                    upstream.name, upstream.tokenizer, upstream.proxy,
                )
            declarations = {}
            models = {m.id: m for m in catalog.models}
            for model in catalog.models:
                caps = model.declaration()
                declarations[model.id] = caps.model_dump_json()
                for d in catalog.deployments:
                    if d.public_model != model.id:
                        continue
                    adapter = adapters[upstreams[d.upstream_id].adapter_id]
                    manifest = adapter.manifest
                    if model.task not in manifest.operations:
                        raise ValueError(f"Deployment {d.id} 的协议不支持 {model.task}")
                    if caps.streaming and not manifest.streaming:
                        raise ValueError("模型声明的流式能力超出适配器合同")
                    self.validate_declaration(adapter, caps, upstreams[d.upstream_id])
            from gateway.store import dump
            generation = Generation(
                revision(catalog), dump(catalog.model_dump(mode="json")),
                MappingProxyType(adapters), MappingProxyType(upstreams), catalog.deployments,
                MappingProxyType(declarations), MappingProxyType(models), catalog.settings, transport, MappingProxyType(templates),
            )
            routes = {m.id: ModelRoute(self, generation, m.id) for m in catalog.models if m.enabled}
            object.__setattr__(generation, "routes", MappingProxyType(routes))
            return generation
        except BaseException:
            for adapter in adapters.values():
                if isinstance(adapter, WorkerAdapter):
                    await adapter.close()
            await transport.close()
            raise

    @staticmethod
    def validate_declaration(adapter, caps, upstream):
        if caps.task != "llm":
            return
        manifest = adapter.manifest
        modalities = adapter_modalities(adapter)
        if set(caps.input_modalities) - set(modalities["input"]) or set(caps.output_modalities) - set(modalities["output"]):
            raise ValueError("模型声明的输入或输出模态超出适配器已实现的范围")
        operations = caps.extensions.get("operations", {})
        supported = {"conversation"}
        for modality, operation in (("image", "vision"), ("audio", "audio_transcription"), ("video", "video_understanding")):
            if modality in modalities["input"] and "text" in modalities["output"]:
                supported.add(operation)
        for modality, operation in (("image", "image_generation"), ("audio", "speech_generation"), ("video", "video_generation")):
            if modality in modalities["output"]:
                supported.add(operation)
        if "image" in modalities["input"] and "image" in modalities["output"]:
            supported.add("image_edit")
        if "audio" in modalities["input"] and "audio" in modalities["output"]:
            supported.add("speech_to_speech")
        if any(value is True or isinstance(value, dict) and value.get("supported") is True
               for key, value in operations.items() if key not in supported):
            raise ValueError("当前适配器未实现声明的媒体操作")
        if (caps.supports_prediction and not manifest.prediction
                or caps.service_tiers and not manifest.service_tiers
                or caps.decoding.logprobs and not manifest.logprobs
                or caps.decoding.verbosity and not manifest.verbosity):
            raise ValueError("当前适配器未实现 prediction、service tier、logprobs 或 verbosity")
        if caps.supports_multiple_choices and not manifest.multiple_choices:
            raise ValueError("当前协议适配器未开放多候选")
        if caps.streaming and not manifest.streaming:
            raise ValueError("当前适配器未实现流式响应")
        if caps.tools.function_calling and not manifest.tools:
            raise ValueError("当前适配器未实现工具调用")
        if caps.reasoning.supported:
            if not manifest.reasoning_returns:
                raise ValueError("当前适配器未实现推理参数映射")
            levels = upstream.profile.get("reasoning_effort_map", manifest.reasoning_levels)
            if not manifest.dynamic_reasoning and set(caps.reasoning.efforts) - set(levels):
                raise ValueError("推理档位超出适配器 Profile 映射")
            if (set(caps.reasoning.returns) - set(manifest.reasoning_returns)
                    or caps.reasoning.summary and "summary" not in manifest.reasoning_returns):
                raise ValueError("推理返回形态超出适配器实现范围")
        if (caps.reasoning.persisted_state and not manifest.persisted_reasoning
                or caps.tools.multimodal_results and not manifest.multimodal_tool_results):
            raise ValueError("当前内置适配器未实现持久推理或多模态工具结果")
        if caps.structured_output.supported and not manifest.structured_output:
            raise ValueError("当前适配器未开放结构化输出")

    async def initialize(self):
        self.store._migrate_auth_schemes()
        self.current = await self.build(self.store.catalog())

    async def publish(self, catalog: Catalog, expected: str, actor: str, *, model_renames=None):
        async with self._lock:
            if self.current.revision != expected:
                raise RevisionConflict("目录已更新，请刷新后重试")
            candidate = await self.build(catalog)
            try:
                reserved = {identity for package in self._external.values() for identity in (*package.models, package.provider_id)}
                if set(candidate.routes).intersection(reserved):
                    raise ValueError("公开模型 ID 与高级插件模型冲突")
                self.store.save(catalog, expected, actor, model_renames=model_renames)
            except BaseException:
                await self._close_generation(candidate)
                raise
            previous, self.current = self.current, candidate
            self._retired[id(previous)] = previous
            await self._reap(previous)
            return candidate.revision

    @property
    def providers(self):
        return {**(self.current.routes if self.current else {}),
                **{package.provider_id: package for package in self._external.values() if package.available}}

    async def publish_external(self, identity, package, commit):
        """Validate and atomically switch one independently owned process generation."""
        async with self._lock:
            if package is not None:
                occupied = set(self.current.routes)
                provider_ids = {"gateway", *self.current.routes}
                for key, other in self._external.items():
                    if key != identity:
                        occupied.update(other.models)
                        provider_ids.add(other.provider_id)
                if occupied.intersection(package.models) or package.provider_id in provider_ids:
                    raise ValueError("插件 Provider 或模型 ID 与现有目录冲突")
            commit()
            previous = self._external.pop(identity, None)
            if package is not None:
                self._external[identity] = package
            if previous is not None:
                self._retired[id(previous)] = previous
                await self._reap(previous)

    def resolve(self, model):
        for package in self._external.values():
            if model in package.models and package.available:
                return package
        route = self.current.routes.get(model) if self.current else None
        if route is None or not any(d.public_model == model and d.enabled and self.current.upstreams[d.upstream_id].enabled
                                    for d in self.current.deployments):
            raise LookupError(f"没有可用 Deployment: {model}")
        return route

    def resolve_registered(self, model, *, response_id=None):
        return self._executions.get(response_id) or self.resolve(model)

    def acquire_active(self, model):
        route = self.resolve(model)
        self._references[id(route.generation)] = self._references.get(id(route.generation), 0) + 1
        return route

    def acquire_registered(self, model, *, response_id=None):
        route = self.resolve_registered(model, response_id=response_id)
        self._references[id(route.generation)] = self._references.get(id(route.generation), 0) + 1
        return route

    def bind_execution(self, response_id, route):
        self._executions[response_id] = route

    def unbind_execution(self, response_id, route):
        if self._executions.get(response_id) is route:
            self._executions.pop(response_id)

    async def release_registered(self, route):
        identity = id(route.generation)
        self._references[identity] = self._references.get(identity, 1) - 1
        if self._references[identity] <= 0:
            self._references.pop(identity, None)
            await self._reap(route.generation)

    async def _reap(self, generation):
        identity = id(generation)
        if identity in self._retired and not self._references.get(identity):
            self._retired.pop(identity)
            await self._close_generation(generation)

    async def _close_generation(self, generation):
        if not isinstance(generation, Generation):
            await generation.close()
            return
        for adapter in generation.adapters.values():
            if isinstance(adapter, WorkerAdapter):
                await adapter.close()
        await generation.transport.close()

    async def close(self):
        generations = {**self._retired}
        if self.current:
            generations[id(self.current)] = self.current
        generations.update({id(package): package for package in self._external.values()})
        for generation in generations.values():
            await self._close_generation(generation)
        self._retired.clear()
        self._external.clear()


class ControlView:
    """Executor control state, derived only from the published generation."""
    def __init__(self, registry):
        self.registry, self.project_root = registry, registry.project_root

    @property
    def last_error(self):
        return self.registry.last_error

    @property
    def current(self):
        generation = self.registry.current
        if generation is None:
            return ControlSnapshot()
        return ControlSnapshot(
            revision=generation.revision,
            gateway_api={"enabled": generation.options.enabled},
            gateway_system_prompt=generation.options.system_prompt,
        )


@dataclass(frozen=True)
class ControlSnapshot:
    revision: str = "empty"
    gateway_api: dict = field(default_factory=dict)
    gateway_system_prompt: str = ""
