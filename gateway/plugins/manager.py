from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import secrets
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import jsonschema
from packaging.version import Version

from core.restart_control import atomic_json
from gateway.plugins.packages import unpack
from gateway.plugins.process import PluginProcess
from gateway.plugins.provider import discover
from gateway.plugins.schema import CatalogEntry, Manifest, PluginError, MAX_PACKAGE, read_manifest, read_panel
from gateway.transport import Transport


class AdvancedPluginManager:
    def __init__(self, registry, version):
        self.registry, self.version = registry, version
        self.root = registry.project_root / "data/advanced_plugins"
        self.path = self.root / "inventory.json"
        self.state = {"revision": uuid4().hex, "plugins": {}, "sources": [], "retained": {}}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        self._lock = asyncio.Lock()
        self.errors = {}
        self.phases = {}
        self._catalog = []
        self._ui_access = {}

    def ui_access(self, identity):
        entry = self.entry(identity)
        now = time.monotonic()
        self._ui_access = {key: value for key, value in self._ui_access.items() if value[2] > now}
        if len(self._ui_access) >= 256:
            self._ui_access.pop(next(iter(self._ui_access)))
        token = secrets.token_urlsafe(32)
        self._ui_access[token] = (identity, entry["sha256"], now + 7200)
        return "/admin/plugin-ui/" + token + "/"

    def ui_entry(self, token):
        value = self._ui_access.get(token)
        if not value or value[2] <= time.monotonic():
            raise PluginError("PLUGIN_PERMISSION_DENIED", "插件静态资源会话已失效，请重新打开页面", 403)
        identity, digest, _ = value
        entry = self.entry(identity)
        if entry["sha256"] != digest:
            raise PluginError("PLUGIN_PERMISSION_DENIED", "插件版本已更新，请重新打开页面", 403)
        return entry

    def _save(self, state):
        state = {**state, "revision": uuid4().hex}
        atomic_json(self.path, state)
        self.state = state

    def check(self, expected):
        if self.state["revision"] != expected:
            raise PluginError("PLUGIN_CONFLICT", "插件库存已更新，请刷新后重试", 409)

    def entry(self, identity):
        try:
            return self.state["plugins"][identity]
        except KeyError:
            raise PluginError("PLUGIN_NOT_FOUND", "插件不存在", 404) from None

    def manifest(self, entry):
        return Manifest.model_validate(entry["manifest"])

    def config(self, entry):
        manifest = self.manifest(entry)
        result = deepcopy(entry.get("config", {}))
        for key, value in result.items():
            if manifest.config_schema.get("properties", {}).get(key, {}).get("writeOnly"):
                result[key] = self.registry.vault.decrypt(value, f"plugin:{manifest.id}:{key}")
        return result

    def config_view(self, identity):
        entry = self.entry(identity)
        manifest = self.manifest(entry)
        values, secrets = {}, {}
        for key, value in entry.get("config", {}).items():
            if manifest.config_schema.get("properties", {}).get(key, {}).get("writeOnly"):
                secrets[key] = bool(value)
            else:
                values[key] = value
        return {"revision": self.state["revision"], "schema": manifest.config_schema, "values": values,
                "secrets_set": secrets, "panel": self.panel(entry)}

    def panel(self, entry):
        manifest = self.manifest(entry)
        if manifest.ui.fallback_panel:
            return read_panel(self.root / entry["package"], manifest.ui.fallback_panel)
        return None

    def snapshot(self):
        items = []
        for identity, entry in self.state["plugins"].items():
            package = self.registry._external.get(identity)
            error = self.errors.get(identity)
            status = entry.get("state", "disabled")
            if entry.get("enabled"):
                status = "ready" if package and package.available and (package.models or not entry["manifest"]["provider"]["enabled"]) else (
                    "needs_config" if package and package.available else "failed")
            if error:
                status = "degraded" if package and package.available and package.models else {
                    "PLUGIN_NEEDS_AUTH": "needs_auth", "PLUGIN_NEEDS_CONFIG": "needs_config",
                }.get(error["code"], "failed")
            status = self.phases.get(identity, status)
            items.append({
                **entry["manifest"], "enabled": entry.get("enabled", False), "status": status,
                "sha256": entry["sha256"], "source": entry["source"], "grants": entry.get("grants", []),
                "model_count": len(package.models) if package and package.available else 0,
                "models": sorted(package.models) if package and package.available else [],
                "error": error, "can_rollback": bool(entry.get("previous")),
                "update": next((row for row in self._catalog if row["id"] == identity
                                and Version(row["version"]) > Version(entry["manifest"]["version"])), None),
            })
        return {"revision": self.state["revision"], "plugins": items, "sources": self.state["sources"]}

    async def initialize(self):
        for identity, entry in list(self.state["plugins"].items()):
            if entry.get("enabled"):
                candidate = None
                try:
                    candidate = await self._candidate(entry)
                    await self.registry.publish_external(identity, candidate, lambda: None)
                except (PluginError, ValueError, OSError) as exc:
                    if candidate:
                        await candidate.close()
                    self.errors[identity] = self._error(exc)

    def _error(self, exc):
        return {"code": exc.code if isinstance(exc, PluginError) else "PLUGIN_START_FAILED",
                "message": exc.message if isinstance(exc, PluginError) else "插件候选版本未通过校验",
                "stage": "activation"}

    async def _candidate(self, entry, *, probe=False, model=None):
        path = self.root / entry["package"]
        manifest = read_manifest(path, self.version)
        if manifest.model_dump(mode="json") != entry["manifest"]:
            raise PluginError("PLUGIN_INVALID_MANIFEST", "已安装插件清单发生变化，请重新安装")
        if not set(manifest.permissions.requested) <= set(entry.get("grants", [])):
            raise PluginError("PLUGIN_PERMISSION_DENIED", "请先明确授予插件请求的全部权限", 403)
        config = self.config(entry)
        try:
            jsonschema.validate(config, manifest.config_schema)
        except jsonschema.ValidationError:
            raise PluginError("PLUGIN_NEEDS_CONFIG", "请先完成插件所需配置") from None
        host = await PluginProcess.start(path, manifest, self.root / "runtime" / manifest.id, config)
        try:
            if probe:
                result = await host.call("models.probe", {"model": model})
                if not isinstance(result, dict) or result.get("available") is not True:
                    raise PluginError("PLUGIN_MODEL_UNAVAILABLE", "插件模型尚未通过可用性探测", 502)
            if not manifest.provider.enabled:
                from gateway.plugins.provider import PluginProvider
                return PluginProvider(self.registry, manifest, host, [])
            return await discover(self.registry, manifest, host)
        except BaseException:
            await host.close()
            raise

    async def preview(self, content):
        with unpack(self.root / "staging", content, self.version) as (_, manifest, digest):
            return {"manifest": manifest.model_dump(mode="json"), "sha256": digest, "status": "discovered",
                    "signature": "unsigned", "execution": "仅在 owner 授权并启用后执行"}

    async def install(self, content, expected, digest, *, update=False, source="local-upload"):
        async with self._lock:
            self.check(expected)
            with unpack(self.root / "staging", content, self.version) as (stage, manifest, checksum):
                if checksum != digest:
                    raise PluginError("PLUGIN_INVALID_PACKAGE", "插件包摘要与预览或索引不一致")
                previous = self.state["plugins"].get(manifest.id)
                if bool(previous) != update:
                    raise PluginError("PLUGIN_CONFLICT", "安装状态已改变，请选择正确的安装或更新操作", 409)
                if previous and Version(manifest.version) <= Version(previous["manifest"]["version"]):
                    raise PluginError("PLUGIN_CONFLICT", "更新版本必须高于当前版本", 409)
                destination = self.root / "packages" / manifest.id / checksum
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copytree(stage, destination)
                entry = {
                    "manifest": manifest.model_dump(mode="json"), "sha256": checksum,
                    "package": destination.relative_to(self.root).as_posix(), "source": source,
                    "enabled": previous.get("enabled", False) if previous else False,
                    "state": previous.get("state", "installed") if previous else "installed",
                    "grants": previous.get("grants", []) if previous else [],
                    "config": previous.get("config", {}) if previous else {
                        key: value["default"] for key, value in manifest.config_schema.get("properties", {}).items()
                        if "default" in value and not value.get("writeOnly")
                    },
                }
                retained = self.state.get("retained", {}).get(manifest.id)
                if previous is None and retained:
                    if retained["manifest"]["config_schema"] != manifest.config_schema:
                        raise PluginError("PLUGIN_NEEDS_CONFIG", "保留配置与新插件 Schema 不一致，请安装原版本后编辑配置")
                    entry["config"] = retained["config"]
                if previous:
                    entry["previous"] = {key: value for key, value in previous.items() if key != "previous"}
                    # Re-encode against the candidate schema; credentials stay
                    # encrypted even when a new version changes their declarations.
                    old_secrets = {key for key, field in self.manifest(previous).config_schema.get("properties", {}).items()
                                   if field.get("writeOnly") and key in previous.get("config", {})}
                    new_properties = manifest.config_schema.get("properties", {})
                    if any(not new_properties.get(key, {}).get("writeOnly") for key in old_secrets):
                        raise PluginError("PLUGIN_UPDATE_FAILED", "新版本不能将已有秘密字段改为公开字段或删除，请先显式清除凭据")
                await self._commit(manifest.id, entry)
            return self.snapshot()

    async def _commit(self, identity, entry, *, probe=False, model=None):
        candidate = None
        self.phases[identity] = "uninstalling" if entry is None else (
            "updating" if identity in self.registry._external else "starting")
        try:
            if entry and entry.get("enabled"):
                candidate = await self._candidate(entry, probe=probe, model=model)
            state = deepcopy(self.state)
            if entry is None:
                old = state["plugins"][identity]
                state.setdefault("retained", {})[identity] = {"config": old["config"], "manifest": old["manifest"]}
                state["plugins"].pop(identity)
            else:
                state["plugins"][identity] = entry
                state.setdefault("retained", {}).pop(identity, None)
            await self.registry.publish_external(identity, candidate, lambda: self._save(state))
            self.errors.pop(identity, None)
        except (PluginError, ValueError, OSError) as exc:
            if candidate:
                await candidate.close()
            self.errors[identity] = self._error(exc)
            if isinstance(exc, PluginError):
                raise
            raise PluginError("PLUGIN_CONFLICT", "插件候选版本或模型注册冲突，原版本继续服务", 409) from None
        finally:
            self.phases.pop(identity, None)

    async def change(self, identity, expected, operation, value=None):
        async with self._lock:
            self.check(expected)
            entry = deepcopy(self.entry(identity))
            value = value or {}
            if operation == "enable":
                requested = self.manifest(entry).permissions.requested
                if set(value.get("grants", [])) != set(requested):
                    raise PluginError("PLUGIN_PERMISSION_DENIED", "启用前须明确授予清单请求的权限", 403)
                entry.update(enabled=True, grants=requested)
            elif operation == "disable":
                entry.update(enabled=False, grants=[], state="disabled")
            elif operation == "uninstall":
                entry = None
            elif operation == "rollback":
                if not entry.get("previous"):
                    raise PluginError("PLUGIN_UPDATE_FAILED", "没有可恢复的旧版本")
                entry = entry["previous"]
            elif operation == "config":
                manifest = self.manifest(entry)
                if "plugin.config.write" not in manifest.permissions.requested:
                    raise PluginError("PLUGIN_PERMISSION_DENIED", "此插件未声明配置写入权限", 403)
                merged = {**self.config(entry), **value}
                # Explicit null clears secrets; omitted fields retain their values.
                merged = {key: item for key, item in merged.items() if item is not None}
                try:
                    jsonschema.validate(merged, manifest.config_schema)
                except jsonschema.ValidationError:
                    raise PluginError("PLUGIN_NEEDS_CONFIG", "配置不符合插件 Schema，请检查必填项及字段类型") from None
                for key, item in merged.items():
                    if manifest.config_schema.get("properties", {}).get(key, {}).get("writeOnly"):
                        if not isinstance(item, str):
                            raise PluginError("PLUGIN_NEEDS_CONFIG", "秘密配置字段必须为字符串")
                        merged[key] = self.registry.vault.encrypt(item, f"plugin:{identity}:{key}")
                entry["config"] = merged
            elif operation not in {"reload", "probe"}:
                raise PluginError("PLUGIN_UNSUPPORTED", "不支持的插件操作")
            if operation in {"probe", "reload"} and not entry.get("enabled"):
                raise PluginError("PLUGIN_PERMISSION_DENIED", "请先授权并启用插件", 403)
            await self._commit(identity, entry, probe=operation == "probe", model=value.get("model"))
            return self.snapshot()

    async def diagnostics(self, identity):
        entry = self.entry(identity)
        package = self.registry._external.get(identity)
        detail = {"process_available": bool(package and package.available), "error": self.errors.get(identity),
                  "usage": "以模型返回的实际 measurement 为准；缺失时 unavailable"}
        if package and package.available:
            try:
                detail["runtime"] = package.host.redact(await package.host.call("diagnostics", timeout=10))
            except PluginError as exc:
                detail["runtime_error"] = self._error(exc)
            detail["logs"] = list(package.host.logs)
        return detail

    async def action(self, identity, expected, name, value):
        async with self._lock:
            self.check(expected)
            entry = self.entry(identity)
            manifest = self.manifest(entry)
            permission = manifest.actions.get(name)
            if not permission or permission not in entry.get("grants", []):
                raise PluginError("PLUGIN_PERMISSION_DENIED", "此动作未声明或未获得授权", 403)
            package = self.registry._external.get(identity)
            if not package or not package.available:
                raise PluginError("PLUGIN_RUNTIME_UNAVAILABLE", "插件未运行", 503)
            response = await package.host.call("ui.actions.invoke", {"action": name, "value": value})
            return package.host.redact(response)

    async def fetch(self, url, *, allow_private=False, limit=MAX_PACKAGE):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise PluginError("PLUGIN_INVALID_SOURCE", "插件源须为无凭据、无查询参数的 HTTP(S) 地址")
        upstream = type("Source", (), {
            "base_url": urlunsplit((parsed.scheme, parsed.netloc, "", "", "")), "profile": {},
            "allow_private_network": allow_private, "timeout": 20, "proxy": None,
        })()
        transport = Transport()
        try:
            async with transport.response(upstream, parsed.path or "/", {}, method="GET") as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise PluginError("PLUGIN_INVALID_SOURCE", "插件源响应大小超限")
                return bytes(content)
        except PluginError:
            raise
        except Exception:
            raise PluginError("PLUGIN_SOURCE_UNAVAILABLE", "插件源请求失败，请检查地址及网络权限", 502) from None
        finally:
            await transport.close()

    async def catalog(self):
        items, errors = [], []
        for source in self.state["sources"]:
            if not source["enabled"]:
                continue
            try:
                value = json.loads(await self.fetch(source["url"], allow_private=source["allow_private_network"], limit=2 * 1024 * 1024))
                if value.get("schema_version") != 1 or not isinstance(value.get("plugins"), list) or len(value["plugins"]) > 500:
                    raise ValueError("invalid catalog")
                rows = []
                for row in value["plugins"]:
                    checked = CatalogEntry.model_validate(row)
                    Version(checked.version)
                    if any(not isinstance(item, str) or len(item) > 100 for item in checked.capabilities):
                        raise ValueError("invalid capability tag")
                    rows.append({**checked.model_dump(mode="json"), "source_id": source["id"], "source_name": source["name"]})
                items.extend(rows)
            except (PluginError, ValueError, TypeError, KeyError):
                errors.append({"source_id": source["id"], "message": "插件索引不可用或格式无效"})
        self._catalog = items
        return {"items": items, "errors": errors, "sources": self.state["sources"]}

    async def sources(self, expected, value):
        async with self._lock:
            self.check(expected)
            if len(value) > 16:
                raise PluginError("PLUGIN_INVALID_SOURCE", "最多配置 16 个插件源")
            clean = []
            for item in value:
                url = item["url"]
                parsed = urlsplit(url)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                        or parsed.password or parsed.query or parsed.fragment):
                    raise PluginError("PLUGIN_INVALID_SOURCE", "插件索引地址无效")
                clean.append({"id": str(item.get("id") or uuid4().hex), "name": str(item["name"])[:100],
                              "url": url, "enabled": bool(item.get("enabled", True)),
                              "allow_private_network": bool(item.get("allow_private_network", False))})
            if len({item["id"] for item in clean}) != len(clean):
                raise PluginError("PLUGIN_INVALID_SOURCE", "插件源 ID 重复")
            self._save({**self.state, "sources": clean})
            return self.snapshot()

    async def market_install(self, expected, source_id, identity, version, *, update=False):
        self.check(expected)
        catalog = await self.catalog()
        row = next((item for item in catalog["items"] if (item["source_id"], item["id"], item["version"])
                    == (source_id, identity, version)), None)
        if not row:
            raise PluginError("PLUGIN_NOT_FOUND", "插件索引中没有此版本", 404)
        source = next(item for item in self.state["sources"] if item["id"] == source_id)
        content = await self.fetch(row["url"], allow_private=source["allow_private_network"])
        preview = await self.preview(content)
        if preview["manifest"]["id"] != identity or preview["manifest"]["version"] != version:
            raise PluginError("PLUGIN_INVALID_PACKAGE", "插件包身份与索引不一致")
        return await self.install(content, expected, row["sha256"], update=update, source=source["name"])

    async def market_preview(self, expected, source_id, identity, version):
        self.check(expected)
        catalog = await self.catalog()
        row = next((item for item in catalog["items"] if (item["source_id"], item["id"], item["version"])
                    == (source_id, identity, version)), None)
        if not row:
            raise PluginError("PLUGIN_NOT_FOUND", "插件索引中没有此版本", 404)
        source = next(item for item in self.state["sources"] if item["id"] == source_id)
        content = await self.fetch(row["url"], allow_private=source["allow_private_network"])
        preview = await self.preview(content)
        if (preview["sha256"] != row["sha256"] or preview["manifest"]["id"] != identity
                or preview["manifest"]["version"] != version):
            raise PluginError("PLUGIN_INVALID_PACKAGE", "插件包身份或摘要与索引不一致")
        return preview
