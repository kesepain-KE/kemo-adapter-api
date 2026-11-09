from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import fields
from types import SimpleNamespace

from core.models import ErrorObject, ModelCapabilities, Usage
from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderException, ProviderResult
from gateway.domain import Deployment, PublicModel
from gateway.keys import KeyPolicyError
from gateway.plugins.schema import PluginError


def provider_error(exc: PluginError) -> ProviderException:
    # ErrorObject uses the pinned Kemo code set. Plugin codes live in details.
    return ProviderException(ErrorObject(
        type="upstream_error", code="PROVIDER_TIMEOUT" if exc.status == 504 else "PROVIDER_UNAVAILABLE",
        message=exc.message, retryable=False, details={"reason": exc.code},
    ))


def result(value) -> ProviderResult:
    value = dict(value)
    value["usage"] = Usage.model_validate(value.get("usage", {}))
    if value.get("error"):
        value["error"] = ErrorObject.model_validate(value["error"])
    return ProviderResult(**value)


class PluginProvider:
    """A process generation published through the existing executor/registry."""

    def __init__(self, registry, manifest, host, capabilities):
        self.registry, self.manifest, self.host = registry, manifest, host
        self.provider_id = manifest.provider.provider_id
        self.declarations = {caps.model: caps for caps in capabilities}
        self.models = frozenset(self.declarations)
        self.generation = self

    @property
    def available(self):
        return self.host.process.returncode is None and not self.host._reader.done()

    async def capabilities(self, model):
        return self.declarations[model].model_copy(deep=True)

    def params(self, request, context):
        return {
            "request": request.model_dump(mode="json", by_alias=True),
            "context": {field.name: getattr(context, field.name) for field in fields(context) if field.name != "assets"},
        }

    def begin(self, request, context, method):
        caps = self.declarations[request.model]
        model = PublicModel(id=request.model, task=caps.task, capabilities=caps.model_dump(mode="json"))
        deployment = Deployment(
            id="plugin_" + self.manifest.id, upstream_id="plugin_" + self.manifest.id,
            upstream_model=caps.provider_model, public_model=request.model,
        )
        try:
            self.registry.keys.admit(context, model, [deployment])
        except KeyPolicyError as exc:
            raise ProviderException(ErrorObject(
                type="rate_limit_error", code="RATE_LIMITED", message=str(exc), retryable=False,
            )) from None
        trace = self.registry.router.metering.start(
            context, 1, deployment, SimpleNamespace(id=deployment.upstream_id, name=self.manifest.name),
            request=request, method=method, task=caps.task,
        )
        return deployment, trace

    async def execute(self, request, context):
        deployment, trace = self.begin(request, context, "execute")
        usage, error, cancelled = Usage(), None, False
        try:
            response = result(await self.host.call("models.execute", self.params(request, context)))
            usage, error = response.usage, response.error
            trace.response = {"output": response.output, "usage": usage.model_dump(mode="json")}
            return response
        except PluginError as exc:
            mapped = provider_error(exc)
            error = mapped.error
            raise mapped from None
        except (TypeError, ValueError, KeyError):
            error = provider_error(PluginError("PLUGIN_PROTOCOL_ERROR", "插件返回的结果无效", 502)).error
            raise ProviderException(error) from None
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # No tokenizer estimate is invented for an external CLI.
            self.registry.router.metering.finish(context, 1, trace, deployment, usage, error=error, cancelled=cancelled)
            self.registry.keys.settle(context)

    async def stream(self, request, context):
        deployment, trace = self.begin(request, context, "stream")
        usage, error, cancelled, terminal = Usage(), None, False, False
        try:
            async with aclosing(self.host.frames("models.stream", self.params(request, context))) as frames:
                async for value in frames:
                    value = dict(value)
                    value["kind"] = ProviderEventKind(value["kind"])
                    if value.get("usage") is not None:
                        value["usage"] = Usage.model_validate(value["usage"])
                    if value.get("result") is not None:
                        value["result"] = result(value["result"])
                    if value.get("error") is not None:
                        value["error"] = ErrorObject.model_validate(value["error"])
                    event = ProviderEvent(**value)
                    if event.usage:
                        usage = event.usage
                    if event.result:
                        usage, error = event.result.usage, event.result.error
                        trace.response = {"output": event.result.output, "usage": usage.model_dump(mode="json")}
                    if event.error:
                        error = event.error
                    if event.kind == ProviderEventKind.TEXT_DELTA:
                        trace.text(event.delta)
                    if event.kind in {ProviderEventKind.COMPLETED, ProviderEventKind.INCOMPLETE,
                                      ProviderEventKind.FAILED, ProviderEventKind.CANCELLED, ProviderEventKind.ERROR}:
                        if terminal:
                            raise ValueError("duplicate terminal")
                        terminal = True
                        cancelled = event.kind == ProviderEventKind.CANCELLED
                    yield event
                if not terminal:
                    raise ValueError("missing terminal")
        except PluginError as exc:
            mapped = provider_error(exc)
            error = mapped.error
            raise mapped from None
        except (TypeError, ValueError, KeyError):
            error = provider_error(PluginError("PLUGIN_PROTOCOL_ERROR", "插件流式结果无效", 502)).error
            raise ProviderException(error) from None
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self.registry.router.metering.finish(context, 1, trace, deployment, usage, error=error, cancelled=cancelled)
            self.registry.keys.settle(context)

    async def cancel(self, provider_response_id, context):
        # The executor cancels the owning task. frames() forwards cancellation
        # to that exact process generation, including nested CLI work.
        return None

    async def close(self):
        await self.host.close()


async def discover(registry, manifest, host):
    models = await host.call("models.list")
    if not isinstance(models, list) or len(models) > 128:
        raise PluginError("PLUGIN_PROTOCOL_ERROR", "插件模型列表无效", 502)
    declarations = []
    seen = set()
    try:
        for value in models:
            caps = ModelCapabilities.model_validate(value)
            if (caps.provider_id != manifest.provider.provider_id
                    or not caps.model.startswith(caps.provider_id + "-") or caps.model in seen
                    or len(caps.model) > 200 or any(char in caps.model for char in "/\\ \t\r\n")
                    or caps.task != "llm" or set(caps.input_modalities) != {"text"}
                    or set(caps.output_modalities) != {"text"} or caps.supports_multiple_choices):
                raise ValueError("unsupported model contract")
            seen.add(caps.model)
            declarations.append(caps)
    except (ValueError, TypeError):
        raise PluginError("PLUGIN_PROTOCOL_ERROR", "插件能力声明或模型命名不符合 SDK v1 合同", 502) from None
    return PluginProvider(registry, manifest, host, declarations)
