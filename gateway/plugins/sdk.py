"""Public process SDK v1. This module intentionally depends only on the standard library."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from queue import Queue
import sys
import threading

PROTOCOL = "kemo-plugin-rpc-v1"
MAX_FRAME = 2 * 1024 * 1024


class Unsupported(Exception):
    code = "PLUGIN_UNSUPPORTED"


class Plugin:
    config: dict
    data_root: Path

    async def initialize(self, params):
        self.config = params["config"]
        self.data_root = Path(params["data_root"])
        return {"protocol": PROTOCOL}

    async def dispatch(self, method, params):
        if method == "initialize":
            return await self.initialize(params)
        handler = getattr(self, method.replace(".", "_"), None)
        if handler is not None:
            return await handler(params)
        if method == "health":
            return {"ready": True}
        if method == "diagnostics":
            return {"status": "ready", "quota": "unavailable"}
        if method == "shutdown":
            return {"closed": True}
        raise Unsupported()


def text_result(text, *, usage=None):
    return {
        "status": "completed",
        "output": [{"type": "message", "id": "msg_plugin", "role": "assistant", "status": "completed",
                    "phase": "final_answer", "content": [{"type": "text", "text": text}]}],
        "usage": usage or {},
    }


def text_capabilities(provider, model, *, streaming=True):
    return {
        "protocol_version": "2.0", "model": provider + "-" + model,
        "provider_id": provider, "provider_model": model, "task": "llm",
        "streaming": streaming, "input_modalities": ["text"], "output_modalities": ["text"],
        "limits": {"context_window": 32768, "max_output_tokens": 8192},
    }


async def serve(plugin):
    incoming = Queue(maxsize=128)
    tasks = {}
    write_lock = asyncio.Lock()

    def reader():
        while raw := sys.stdin.buffer.readline(MAX_FRAME + 1):
            if len(raw) > MAX_FRAME:
                incoming.put(None)
                return
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                value = None
            incoming.put(value)
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()

    async def send(value):
        payload = json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            raise ValueError("RPC output exceeds limit")
        async with write_lock:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()

    async def handle(value):
        identity = value["id"]
        try:
            method, params = value["method"], value.get("params", {})
            if method == "models.stream":
                stream = getattr(plugin, "models_stream", None)
                if stream is None:
                    raise Unsupported()
                async for event in stream(params):
                    await send({"id": identity, "result": event})
            else:
                await send({"id": identity, "result": await plugin.dispatch(method, params)})
        except asyncio.CancelledError:
            # Nested subprocess coroutines must release/terminate in their finally blocks.
            pass
        except Exception as exc:
            await send({"id": identity, "error": {"code": getattr(exc, "code", "PLUGIN_EXECUTION_FAILED")}})
        finally:
            await send({"id": identity, "done": True})
            tasks.pop(identity, None)

    try:
        while True:
            value = await asyncio.to_thread(incoming.get)
            if not isinstance(value, dict):
                break
            if value.get("method") == "$/cancelRequest":
                task = tasks.get(value.get("params", {}).get("id"))
                if task:
                    task.cancel()
                continue
            if value.get("method") == "shutdown":
                await handle(value)
                break
            if len(tasks) >= 24 or value.get("id") in tasks:
                await send({"id": value.get("id"), "error": {"code": "PLUGIN_BUSY"}})
                continue
            tasks[value["id"]] = asyncio.create_task(handle(value))
    finally:
        for task in list(tasks.values()):
            task.cancel()
        await asyncio.gather(*list(tasks.values()), return_exceptions=True)


def run(plugin):
    asyncio.run(serve(plugin))
