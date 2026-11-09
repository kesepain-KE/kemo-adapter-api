from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing, suppress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from gateway.plugins.schema import MAX_FRAME, PROTOCOL, PluginError, safe_file
from gateway.tracing import redact_secrets


class PluginProcess:
    """Bounded multiplexed JSONL RPC; only stdout contains protocol frames."""

    def __init__(self, process, manifest, secrets):
        self.process, self.manifest = process, manifest
        self.secrets = tuple(value for value in secrets if value)
        self.pending, self.counter = {}, 0
        self.logs = deque(maxlen=200)
        self._send_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(manifest.runtime.concurrency)
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._read_stderr())

    @classmethod
    async def start(cls, root, manifest, data_root, config):
        data_root.mkdir(parents=True, exist_ok=True)
        # Do not inherit Gateway credentials, proxy secrets or arbitrary PYTHONPATH.
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG",
        }}
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", str(Path(__file__).with_name("worker.py")),
            str(Path(__file__).resolve().parents[2]), str(safe_file(root, manifest.runtime.entry)),
            cwd=data_root, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME, **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                              if os.name == "nt" else {"start_new_session": True}),
        )
        secrets = [value for key, value in config.items()
                   if isinstance(value, str) and manifest.config_schema.get("properties", {}).get(key, {}).get("writeOnly")]
        host = cls(process, manifest, secrets)
        try:
            result = await host.call("initialize", {
                "protocol": PROTOCOL, "plugin_id": manifest.id, "config": config,
                "data_root": str(data_root), "permissions": manifest.permissions.requested,
            }, timeout=15)
            if not isinstance(result, dict) or result.get("protocol") != PROTOCOL:
                raise PluginError("PLUGIN_PROTOCOL_ERROR", "插件握手协议不兼容")
            return host
        except BaseException:
            await host.close()
            raise

    async def _read_stderr(self):
        while data := await self.process.stderr.read(4096):
            # Unstructured stderr can contain arbitrary credentials. Expose bounded
            # byte counts, never raw process text; structured diagnostics is the public path.
            self.logs.append({"event": "stderr", "bytes": len(data)})

    async def _read(self):
        error = PluginError("PLUGIN_RUNTIME_UNAVAILABLE", "插件进程已退出", 503)
        try:
            while raw := await self.process.stdout.readline():
                value = json.loads(raw)
                if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                        or type(value.get("id")) is not int
                        or sum(key in value for key in ("result", "error", "done")) != 1
                        or "done" in value and value["done"] is not True
                        or "error" in value and (not isinstance(value["error"], dict)
                                               or not isinstance(value["error"].get("code"), str))):
                    raise ValueError("invalid frame")
                queue = self.pending.get(value.get("id"))
                if queue is None:
                    continue
                queue.put_nowait(value)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError, asyncio.QueueFull):
            error = PluginError("PLUGIN_PROTOCOL_ERROR", "插件协议帧无效或输出缓冲已满", 502)
            await self.kill()
        finally:
            for queue in list(self.pending.values()):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(error)

    async def send(self, value):
        payload = json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            raise PluginError("PLUGIN_PROTOCOL_ERROR", "插件消息超过大小限制")
        async with self._send_lock:
            try:
                self.process.stdin.write(payload)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError):
                raise PluginError("PLUGIN_RUNTIME_UNAVAILABLE", "插件进程连接已断开", 503) from None

    async def frames(self, method, params, *, timeout=None):
        if len(self.pending) >= self.manifest.runtime.concurrency + 8:
            raise PluginError("PLUGIN_BUSY", "插件请求队列已满", 503)
        self.counter += 1
        identity = self.counter
        queue = self.pending[identity] = asyncio.Queue(maxsize=128)
        completed, started = False, False
        self.logs.append({"method": method, "phase": "started", "rpc_id": identity})
        try:
            async with asyncio.timeout(timeout or self.manifest.runtime.timeout), self._slots:
                if self._reader.done() or self.process.returncode is not None:
                    raise PluginError("PLUGIN_RUNTIME_UNAVAILABLE", "插件进程不可用", 503)
                started = True
                await self.send({"id": identity, "method": method, "params": params})
                while True:
                    value = await queue.get()
                    if isinstance(value, PluginError):
                        raise value
                    if "error" in value:
                        completed = True
                        code = value["error"].get("code", "PLUGIN_PROTOCOL_ERROR")
                        if code not in {
                            "PLUGIN_UNSUPPORTED", "PLUGIN_NEEDS_AUTH", "PLUGIN_NEEDS_CONFIG",
                            "PLUGIN_RUNTIME_UNAVAILABLE", "PLUGIN_MODEL_UNAVAILABLE", "PLUGIN_START_FAILED",
                            "PLUGIN_TIMEOUT", "PLUGIN_PROTOCOL_ERROR", "PLUGIN_EXECUTION_FAILED", "PLUGIN_BUSY",
                        }:
                            code = "PLUGIN_PROTOCOL_ERROR"
                        self.logs.append({"method": method, "phase": "failed", "code": code})
                        # A third-party exception message is not safe to display.
                        raise PluginError(code, "插件操作失败，请检查插件诊断", 502)
                    if value.get("done"):
                        completed = True
                        return
                    yield value.get("result")
        except TimeoutError:
            raise PluginError("PLUGIN_TIMEOUT", "插件操作超时", 504) from None
        finally:
            if started and not completed and self.process.returncode is None and not self._reader.done():
                try:
                    async with asyncio.timeout(4):
                        await self.send({"method": "$/cancelRequest", "params": {"id": identity}})
                        while True:
                            value = await queue.get()
                            if isinstance(value, PluginError) or value.get("done") or "error" in value:
                                break
                except (PluginError, TimeoutError):
                    await self.kill()
            self.pending.pop(identity, None)
            self.logs.append({"method": method, "phase": "completed" if completed else "cancelled", "rpc_id": identity})

    async def call(self, method, params=None, *, timeout=None):
        results = []
        async with aclosing(self.frames(method, params or {}, timeout=timeout)) as frames:
            async for value in frames:
                results.append(value)
                if len(results) > 1:
                    raise PluginError("PLUGIN_PROTOCOL_ERROR", "非流式方法返回了多个结果", 502)
        if len(results) != 1:
            raise PluginError("PLUGIN_PROTOCOL_ERROR", "插件未返回结果", 502)
        return results[0]

    def redact(self, value):
        return json.loads(redact_secrets(json.dumps(value, ensure_ascii=False), self.secrets))

    async def close(self):
        if self.process.returncode is None:
            with suppress(PluginError, TimeoutError, ConnectionError):
                await self.call("shutdown", timeout=2)
            if self.process.returncode is None:
                await self.kill()
            await self.process.wait()
        for task in (self._reader, self._stderr):
            task.cancel()
        await asyncio.gather(self._reader, self._stderr, return_exceptions=True)

    async def kill(self):
        if self.process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(self.process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()
