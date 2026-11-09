"""Isolated extension process. Subprocess isolation is not an OS sandbox."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
import importlib.util
import json
from pathlib import Path
import sys
import traceback


async def main():
    root, directory = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(root))
    from pydantic import BaseModel
    from core.models import KemoRequest, EmbeddingRequest, RerankRequest
    from core.provider_contract import RequestContext, ProviderException
    from core.models import ErrorObject
    from gateway.auth import RequestHeaders
    from gateway.domain import Upstream
    from gateway.transport import Transport

    output = sys.stdout
    # Extension prints cannot inject RPC frames or expose credentials in logs.
    sys.stdout = sys.stderr
    spec = importlib.util.spec_from_file_location("kemo_extension", directory / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transport = Transport()
    adapter = module.create_adapter(transport)
    def encode(value):
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json", by_alias=True)
        if is_dataclass(value):
            return asdict(value)
        raise TypeError(type(value).__name__)
    def send(identity, **body):
        output.write(json.dumps({"jsonrpc": "2.0", "id": identity, **body}, default=encode) + "\n")
        output.flush()
    tasks = {}

    async def handle(command):
        identity, method, params = command["id"], command["method"], command["params"]
        try:
            if method == "initialize":
                from gateway.extensions import read_manifest
                manifest = read_manifest(directory, "protocol_adapters")
                methods = [{"llm": "execute", "embedding": "embed", "rerank": "rerank"}[op]
                           for op in manifest.operations]
                if manifest.streaming:
                    methods.append("stream")
                if manifest.discovery:
                    methods.append("discover")
                if any(not callable(getattr(adapter, name, None)) for name in methods):
                    raise ValueError("adapter does not implement its manifest")
                send(identity, result={"contract_version": "1.0"})
            else:
                if method not in {"execute", "stream", "embed", "rerank", "discover", "describe_model"}:
                    raise ValueError("unsupported method")
                upstream = Upstream.model_validate(params["upstream"])
                headers = RequestHeaders(params["headers"], query=params.get("credential_query"),
                                         secrets=params.get("credential_secrets", ()))
                if method == "describe_model":
                    describe = getattr(adapter, "describe_model", None)
                    result = await describe(upstream, params["model"], headers) if describe else {}
                    send(identity, result=result)
                elif method == "discover":
                    send(identity, result=await adapter.discover(upstream, headers))
                else:
                    cls = {"execute": KemoRequest, "stream": KemoRequest,
                           "embed": EmbeddingRequest, "rerank": RerankRequest}[method]
                    request = cls.model_validate(params["request"])
                    context = RequestContext(**params["context"])
                    args = (request, context, params["model"], upstream, headers)
                    if method == "stream":
                        async for event in adapter.stream(*args):
                            send(identity, result=event)
                    else:
                        send(identity, result=await getattr(adapter, method)(*args))
            send(identity, done=True)
        except ProviderException as exc:
            traceback.print_exc(file=sys.stderr)
            send(identity, error=exc.error)
        except asyncio.CancelledError:
            send(identity, done=True)
            raise
        except Exception:
            traceback.print_exc(file=sys.stderr)
            send(identity, error=ErrorObject(
                type="upstream_error", code="PROVIDER_BAD_RESPONSE", message="协议扩展执行失败",
                retryable=False, details={"reason": "ADAPTER_PROGRAMMING_ERROR"},
            ))
        finally:
            tasks.pop(identity, None)

    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            if len(line) > 2 * 1024 * 1024:
                raise ValueError("request too large")
            command = json.loads(line)
            if command.get("method") == "$/cancelRequest":
                task = tasks.get(command["params"]["id"])
                if task is not None:
                    task.cancel()
                continue
            identity = command["id"]
            tasks[identity] = asyncio.create_task(handle(command))
    finally:
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await transport.close()


if __name__ == "__main__":
    asyncio.run(main())
