from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import fields
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

import jsonschema

from core.yaml_config import read_yaml
from core.models import ErrorObject, Usage
from core.provider_contract import (
    ProviderEmbedding, ProviderEmbeddingResult, ProviderEvent, ProviderEventKind,
    ProviderException, ProviderResult, ProviderRerankItem, ProviderRerankResult,
)
from gateway.adapters.contract import AdapterManifest
from gateway.auth import legacy_scheme
from gateway.domain import Document, TokenizerName
from pydantic import Field
from typing import Literal
from gateway.tracing import redact_secrets


KINDS = {"protocol_adapters", "provider_templates"}


class TemplateDefinition(Document):
    adapter_id: str
    base_url: str = ""
    auth_scheme: str = "bearer"
    name: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    profile: dict = Field(default_factory=dict)
    tokenizer: TokenizerName = "auto"
    catalog: tuple[str, ...] = ()
    timeout: float | None = Field(default=None, gt=0, le=7200)
    note: str = ""
    allow_private_network: bool = False


class TemplateManifest(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    name: str = Field(min_length=1)
    version: str
    contract_version: Literal["1.0"]
    description: str = ""
    category: Literal["provider", "coding_service"] = "provider"
    verification: Literal["declared", "verified"] = "declared"


def extension_digest(path: Path, kind: str) -> str:
    digest = hashlib.sha256()
    for name in ("manifest.yaml", "adapter.py" if kind == "protocol_adapters" else "template.yaml"):
        digest.update(name.encode())
        digest.update((path / name).read_bytes())
    return digest.hexdigest()


def read_manifest(path: Path, kind: str):
    if kind not in KINDS:
        raise ValueError("扩展类型无效")
    value = read_yaml(path / "manifest.yaml")
    if kind == "protocol_adapters":
        manifest = AdapterManifest.model_validate({**value, "builtin": False})
        if manifest.contract_version != "1.0":
            raise ValueError("扩展 Adapter 合同版本不兼容")
        if not manifest.operations or not set(manifest.operations) <= {"llm", "embedding", "rerank"}:
            raise ValueError("此版本扩展 worker 仅开放 llm、embedding 和 rerank 操作")
        if not manifest.auth_schemes or len({scheme.id for scheme in manifest.auth_schemes}) != len(manifest.auth_schemes):
            raise ValueError("适配器鉴权类型无效")
        if set(manifest.media.input + manifest.media.output) & {"image", "audio", "video", "file"}:
            raise ValueError("扩展 worker 尚未开放跨进程媒体资源访问，不能声明媒体输入或输出")
        try:
            jsonschema.Draft202012Validator.check_schema(manifest.config_schema)
        except jsonschema.SchemaError:
            raise ValueError("适配器 config_schema 无效") from None
        return manifest
    manifest = TemplateManifest.model_validate(value)
    definition = read_yaml(path / "template.yaml")
    if "auth_type" in definition:
        definition["auth_scheme"] = legacy_scheme(definition["adapter_id"], definition.pop("auth_type"))
    template = TemplateDefinition.model_validate(definition)
    return {**manifest.model_dump(mode="json"), "template": template.model_dump(mode="json")}


def install(root: Path, kind: str, content: bytes) -> str:
    if kind not in KINDS or len(content) > 16 * 1024 * 1024:
        raise ValueError("扩展类型或包大小无效")
    destination_root = root / "extensions" / kind
    destination_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination_root) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 100 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                raise ValueError("扩展展开大小或文件数超限")
            names = set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("扩展包含路径穿越或符号链接")
                if path.name not in {"manifest.yaml", "adapter.py", "template.yaml", "README.md"} or len(path.parts) != 1:
                    raise ValueError("扩展只能包含根目录清单、实现和说明文件；不执行安装脚本")
                if name in names:
                    raise ValueError("扩展包内存在重复路径")
                names.add(name)
                (staging / name).write_bytes(archive.read(entry))
        manifest = read_manifest(staging, kind)
        adapter_id = manifest.id if isinstance(manifest, AdapterManifest) else manifest["id"]
        if kind == "protocol_adapters" and not (staging / "adapter.py").is_file():
            raise ValueError("适配器缺少 adapter.py")
        target = destination_root / adapter_id
        if target.exists():
            raise ValueError("同名扩展已安装；请使用新的版本 ID 安装，再切换引用")
        shutil.copytree(staging, target)
    return adapter_id


class WorkerAdapter:
    def __init__(self, manifest, process):
        self.manifest, self.process = manifest, process
        self._lock = asyncio.Lock()
        self._counter = 0
        self._pending = {}
        self._secrets = set()
        self.logs = deque(maxlen=200)
        self._reader_task = asyncio.create_task(self._read())
        self._log_task = asyncio.create_task(self._read_logs())

    @classmethod
    async def start(cls, path, manifest):
        source_root = Path(__file__).resolve().parents[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("extension_worker.py")),
            str(source_root), str(path), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,
        )
        adapter = cls(manifest, process)
        try:
            async with asyncio.timeout(15):
                async for result in adapter._rpc("initialize", {}):
                    if result.get("contract_version") != "1.0":
                        raise ValueError("扩展握手失败")
        except BaseException:
            await adapter.close()
            raise
        return adapter

    def _redact(self, value):
        return redact_secrets(value, self._secrets)

    async def _read_logs(self):
        buffer = ""
        while chunk := await self.process.stderr.read(4096):
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                self.logs.append(self._redact(line)[:8192])
            if len(buffer) > 16384:
                self.logs.append(self._redact(buffer)[:8192])
                buffer = ""
        if buffer:
            self.logs.append(self._redact(buffer)[:8192])

    @staticmethod
    def _worker_error(reason, *, retryable=False):
        return ProviderException(ErrorObject(
            type="upstream_error", code="PROVIDER_BAD_RESPONSE",
            message="协议扩展执行失败", retryable=retryable, details={"reason": reason},
        ))

    async def _read(self):
        failure = self._worker_error("ADAPTER_WORKER_EXITED")
        try:
            while raw := await self.process.stdout.readline():
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise ValueError("invalid worker frame")
                queue = self._pending.get(value.get("id"))
                if queue is None:
                    continue  # A cancelled call can still have buffered frames.
                try:
                    queue.put_nowait(value)
                except asyncio.QueueFull:
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(self._worker_error("ADAPTER_STREAM_BUFFER_FULL"))
                    await self._send({"method": "$/cancelRequest", "params": {"id": value["id"]}})
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError):
            failure = self._worker_error("ADAPTER_WORKER_PROTOCOL_ERROR")
        finally:
            for queue in self._pending.values():
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(failure)

    async def _send(self, value):
        async with self._lock:
            payload = json.dumps({"jsonrpc": "2.0", **value}).encode() + b"\n"
            if len(payload) > 2 * 1024 * 1024:
                raise self._worker_error("ADAPTER_REQUEST_TOO_LARGE")
            try:
                self.process.stdin.write(payload)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError):
                raise self._worker_error("ADAPTER_WORKER_EXITED") from None

    async def _rpc(self, method, params):
        self._counter += 1
        identity = self._counter
        queue = self._pending[identity] = asyncio.Queue(maxsize=128)
        self._secrets.update(params.get("credential_secrets", ()))
        completed = False
        try:
            if self._reader_task.done():
                raise self._worker_error("ADAPTER_WORKER_EXITED")
            await self._send({"id": identity, "method": method, "params": params})
            while True:
                value = await queue.get()
                if isinstance(value, Exception):
                    raise value
                if "error" in value:
                    try:
                        error = ErrorObject.model_validate(json.loads(self._redact(json.dumps(value["error"]))))
                    except ValueError:
                        raise self._worker_error("ADAPTER_WORKER_INVALID_ERROR") from None
                    completed = True
                    raise ProviderException(error)
                if value.get("done"):
                    completed = True
                    return
                yield value["result"]
        finally:
            self._pending.pop(identity, None)
            if not completed and self.process.returncode is None:
                await self._send({"method": "$/cancelRequest", "params": {"id": identity}})

    def _params(self, request, context, model, upstream, headers):
        return {"request": request.model_dump(mode="json", by_alias=True),
                "context": {f.name: getattr(context, f.name) for f in fields(context) if f.name != "assets"},
                "model": model, "upstream": upstream, **self._auth_params(headers)}

    @staticmethod
    def _auth_params(headers):
        return {"headers": dict(headers), "credential_query": getattr(headers, "query", {}),
                "credential_secrets": getattr(headers, "secrets", ())}

    async def _call(self, method, params):
        values = [value async for value in self._rpc(method, params)]
        if len(values) != 1:
            raise self._worker_error("ADAPTER_WORKER_INVALID_RESULT")
        return values[0]

    @staticmethod
    def _result(value):
        value["usage"] = Usage.model_validate(value.get("usage", {}))
        if value.get("error") is not None:
            value["error"] = ErrorObject.model_validate(value["error"])
        return ProviderResult(**value)

    async def execute(self, request, context, model, upstream, headers):
        params = self._params(request, context, model, upstream, headers)
        return self._result(await self._call("execute", params))

    async def stream(self, request, context, model, upstream, headers):
        params = self._params(request, context, model, upstream, headers)
        async for value in self._rpc("stream", params):
            value["kind"] = ProviderEventKind(value["kind"])
            if value.get("result"):
                value["result"] = self._result(value["result"])
            if value.get("usage") is not None:
                value["usage"] = Usage.model_validate(value["usage"])
            if value.get("error") is not None:
                value["error"] = ErrorObject.model_validate(value["error"])
            yield ProviderEvent(**value)

    async def embed(self, request, context, model, upstream, headers):
        value = await self._call("embed", self._params(request, context, model, upstream, headers))
        value["embeddings"] = [ProviderEmbedding(**e) for e in value["embeddings"]]
        value["usage"] = Usage.model_validate(value.get("usage", {}))
        return ProviderEmbeddingResult(**value)

    async def rerank(self, request, context, model, upstream, headers):
        value = await self._call("rerank", self._params(request, context, model, upstream, headers))
        value["results"] = [ProviderRerankItem(**e) for e in value["results"]]
        value["usage"] = Usage.model_validate(value.get("usage", {}))
        return ProviderRerankResult(**value)

    async def discover(self, upstream, headers):
        return await self._call("discover", {"upstream": upstream, **self._auth_params(headers)})

    async def list_models(self, upstream, headers):
        return await self.discover(upstream, headers)

    async def describe_model(self, upstream, model, headers):
        return await self._call("describe_model", {"upstream": upstream, "model": model, **self._auth_params(headers)})

    async def close(self):
        if self.process.returncode is None:
            self.process.kill()
        await self.process.wait()
        self._reader_task.cancel()
        self._log_task.cancel()
        await asyncio.gather(self._reader_task, self._log_task, return_exceptions=True)
