import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import subprocess
import signal

from gateway.plugins.sdk import Plugin, text_capabilities, text_result


class NeedsAuth(Exception):
    code = "PLUGIN_NEEDS_AUTH"


class Unavailable(Exception):
    code = "PLUGIN_RUNTIME_UNAVAILABLE"


class CodexPlugin(Plugin):
    def environment(self):
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
        }}
        environment["CODEX_HOME"] = self.config["auth_home"]
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    async def launch(self, *args, stdin=False):
        executable = Path(self.config["executable"])
        if not executable.is_absolute() or not executable.is_file() or (
            os.name == "nt" and executable.suffix.lower() != ".exe"
        ):
            raise Unavailable()
        return await asyncio.create_subprocess_exec(
            str(executable), *args, cwd=self.data_root, env=self.environment(),
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2 * 1024 * 1024,
            **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True}),
        )

    async def stop(self, process):
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill.exe", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await killer.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    async def command(self, *args):
        process = await self.launch(*args)
        try:
            async with asyncio.timeout(10):
                output = await process.stdout.read(65536)
                await process.wait()
            return process.returncode, output.decode("utf-8", "replace")
        finally:
            if process.returncode is None:
                await self.stop(process)

    async def diagnostics(self, params):
        try:
            code, version = await self.command("--version")
            logged_in, _ = await self.command("login", "status")
            match = re.search(r"\d+\.\d+\.\d+", version)
            return {"installed": code == 0, "authenticated": logged_in == 0,
                    "version": match.group() if match else "unknown", "quota": "unavailable"}
        except (OSError, Unavailable, TimeoutError):
            return {"installed": False, "authenticated": False, "quota": "unavailable"}

    async def models_list(self, params):
        status = await self.diagnostics({})
        marker = self.data_root / "verified-model.json"
        verified = json.loads(marker.read_text()) if marker.exists() else {}
        if not status["authenticated"] or verified.get("model") != self.config["model"] or (
            verified.get("auth_home") != self.config["auth_home"] or verified.get("version") != status.get("version")
        ):
            return []
        return [text_capabilities("codex-cli", self.config["model"])]

    async def models_probe(self, params):
        status = await self.diagnostics({})
        if not status["installed"]:
            raise Unavailable()
        if not status["authenticated"]:
            raise NeedsAuth()
        await self.infer("Reply with exactly: Kemo CLI ready")
        (self.data_root / "verified-model.json").write_text(json.dumps({
            "model": self.config["model"], "auth_home": self.config["auth_home"], "version": status["version"],
        }))
        return {"available": True}

    async def infer(self, prompt):
        process = await self.launch(
            "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--sandbox", "read-only",
            "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "multi_agent",
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", "mcp_servers={}", "-c", 'shell_environment_policy.inherit="none"',
            "--model", self.config["model"], "-", stdin=True,
        )
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            text, usage, total, completed = [], {}, 0, False
            while line := await process.stdout.readline():
                total += len(line)
                if total > 4 * 1024 * 1024:
                    raise ValueError("CLI output limit")
                event = json.loads(line)
                if event.get("type") in {"error", "turn.failed"}:
                    raise Unavailable()
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                    text.append(event["item"].get("text", ""))
                if event.get("type") == "turn.completed":
                    completed = True
                    raw = event.get("usage", {})
                    known = {key: raw[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens")
                             if isinstance(raw.get(key), int) and raw[key] >= 0}
                    if "input_tokens" in known and "output_tokens" in known:
                        known["total_tokens"] = known["input_tokens"] + known["output_tokens"]
                        usage = {**known, "measurement": {"mode": "provider", "exact": True, "exact_fields": list(known)}}
            await process.wait()
            if process.returncode != 0 or not completed or not text:
                raise Unavailable()
            return text_result("\n".join(text), usage=usage)
        finally:
            if process.returncode is None:
                await self.stop(process)

    async def models_execute(self, params):
        request = params["request"]
        prompt = "\n".join(filter(None, [
            params["context"].get("gateway_system_prompt", ""), request.get("system_prompt", ""),
            *[item.get("role", "user") + ": " + block["text"] for item in request["input"]
              for block in item.get("content", []) if block.get("type") == "text"],
        ]))
        return await self.infer(prompt)

    async def models_stream(self, params):
        # Codex's JSONL provides completed agent messages, not token deltas.
        # Keep its private progress/reasoning/tool events out of the client stream.
        result = await self.models_execute(params)
        yield {"kind": "output_text.delta", "item_id": "msg_plugin", "content_index": 0,
               "delta": result["output"][0]["content"][0]["text"]}
        yield {"kind": "provider.completed", "result": result}


def create_plugin():
    return CodexPlugin()
