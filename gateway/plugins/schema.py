from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Literal

import jsonschema
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from core.yaml_config import read_yaml

PERMISSIONS = frozenset({
    "model.register", "model.invoke", "process.cli.execute", "account.status.read",
    "account.authorize", "plugin.config.write", "network.access",
})
PROTOCOL = "kemo-plugin-rpc-v1"
MAX_PACKAGE = 16 * 1024 * 1024
MAX_FRAME = 2 * 1024 * 1024


class PluginError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Runtime(Document):
    kind: Literal["process"] = "process"
    entry: str
    protocol: Literal["kemo-plugin-rpc-v1"] = PROTOCOL
    timeout: float = Field(default=120, ge=1, le=1200)
    concurrency: int = Field(default=2, ge=1, le=16)


class Provider(Document):
    enabled: bool = True
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    model_discovery: Literal["dynamic"] = "dynamic"


class UIPage(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    entry: str


class UI(Document):
    mode: Literal["sandbox"] = "sandbox"
    entry: str | None = None
    fallback_panel: str | None = None
    pages: list[UIPage] = Field(default_factory=list, max_length=12)


class Permissions(Document):
    requested: list[str] = Field(default_factory=list, max_length=16)


class Manifest(Document):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(max_length=60)
    author: str = Field(default="", max_length=200)
    category: Literal["model_account", "cli_provider", "provider_extension"] = "provider_extension"
    summary: str = Field(default="", max_length=1000)
    compatibility: dict[str, str] = Field(default_factory=lambda: {"gateway": ">=2.0.0,<3.0.0"})
    runtime: Runtime
    provider: Provider
    ui: UI = Field(default_factory=UI)
    permissions: Permissions = Field(default_factory=Permissions)
    config_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    actions: dict[str, str] = Field(default_factory=dict)


class CatalogEntry(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(max_length=60)
    url: str = Field(max_length=2000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(default="", max_length=1000)
    author: str = Field(default="", max_length=200)
    category: Literal["model_account", "cli_provider", "provider_extension"] = "provider_extension"
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    compatibility: str = Field(default=">=2.0.0,<3.0.0", max_length=100)


def safe_file(root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
            or any(part.startswith(".") for part in path.parts)):
        raise PluginError("PLUGIN_INVALID_MANIFEST", "插件文件路径无效")
    candidate = root / path
    if not candidate.resolve().is_relative_to(root.resolve()) or any(
        parent.is_symlink() for parent in (candidate, *candidate.parents) if parent.is_relative_to(root)
    ):
        raise PluginError("PLUGIN_INVALID_MANIFEST", "插件文件不能通过链接越过安装目录")
    if not candidate.is_file():
        raise PluginError("PLUGIN_INVALID_MANIFEST", "插件入口文件不存在")
    return candidate


def read_manifest(root: Path, gateway_version: str) -> Manifest:
    try:
        manifest = Manifest.model_validate(read_yaml(safe_file(root, "plugin.yaml")))
        Version(manifest.version)
        if set(manifest.compatibility) != {"gateway"}:
            raise ValueError("compatibility")
        if Version(gateway_version) not in SpecifierSet(manifest.compatibility["gateway"]):
            raise PluginError("PLUGIN_INCOMPATIBLE", "插件与当前网关版本不兼容")
        requested = manifest.permissions.requested
        if len(set(requested)) != len(requested) or set(requested) - PERMISSIONS:
            raise ValueError("permissions")
        if manifest.provider.enabled and not {"model.register", "model.invoke"} <= set(requested):
            raise ValueError("provider permissions")
        if any(permission not in requested for permission in manifest.actions.values()):
            raise ValueError("action permission")
        if manifest.runtime.entry.split(".")[-1] != "py":
            raise ValueError("python entry")
        safe_file(root, manifest.runtime.entry)
        jsonschema.Draft202012Validator.check_schema(manifest.config_schema)
        if manifest.config_schema.get("type") != "object":
            raise ValueError("config schema")
        nodes = [((), manifest.config_schema)]
        while nodes:
            location, node = nodes.pop()
            if isinstance(node, dict):
                if any(key in node and (not isinstance(node[key], str) or not node[key].startswith("#"))
                       for key in ("$ref", "$dynamicRef")):
                    raise ValueError("external schema references are not allowed")
                if node.get("writeOnly") and (
                    len(location) != 2 or location[0] != "properties" or node.get("type") != "string"
                    or any(key in node for key in ("default", "const", "enum", "examples"))
                ):
                    raise ValueError("secrets must be top-level strings without public values")
                nodes.extend(((*location, key), value) for key, value in node.items())
            elif isinstance(node, list):
                nodes.extend(((*location, index), value) for index, value in enumerate(node))
        for entry in [manifest.ui.entry, *(page.entry for page in manifest.ui.pages)]:
            if entry:
                if not entry.startswith("ui/") or not entry.endswith(".html"):
                    raise ValueError("UI entry must be ui/*.html")
                safe_file(root, entry)
        if len({page.id for page in manifest.ui.pages}) != len(manifest.ui.pages):
            raise ValueError("duplicate UI pages")
        if manifest.ui.fallback_panel:
            panel = read_panel(root, manifest.ui.fallback_panel)
            properties = manifest.config_schema.get("properties", {})
            for container in panel.get("containers", []):
                if container["kind"] == "config":
                    for field in container.get("fields", []):
                        if field["key"] not in properties or field.get("masked") and not properties[field["key"]].get("writeOnly"):
                            raise ValueError("panel configuration must reference its config schema")
                    for preset in container.get("presets", []):
                        if any(key not in properties or properties[key].get("writeOnly") for key in preset["values"]):
                            raise ValueError("panel presets cannot contain secrets or unknown fields")
                if any(control["command"] not in manifest.actions for control in container.get("controls", [])):
                    raise ValueError("panel action must be declared")
        return manifest
    except PluginError:
        raise
    except (ValueError, TypeError, KeyError, OSError, jsonschema.SchemaError):
        raise PluginError("PLUGIN_INVALID_MANIFEST", "插件清单、入口或配置 Schema 无效") from None


def read_panel(root: Path, entry: str) -> dict:
    panel = json.loads(safe_file(root, entry).read_text(encoding="utf-8"))
    if not isinstance(panel, dict) or panel.get("schema_version") != 1:
        raise ValueError("unsupported panel")
    containers = panel.get("containers", [])
    if not isinstance(containers, list) or len(containers) > 32:
        raise ValueError("invalid containers")
    for container in containers:
        if not isinstance(container, dict) or container.get("kind") not in {"status", "config", "action"}:
            raise ValueError("invalid container")
        if "title" in container and not isinstance(container["title"], str):
            raise ValueError("invalid container title")
        fields, controls, presets = (container.get(key, []) for key in ("fields", "controls", "presets"))
        if any(not isinstance(items, list) or len(items) > 100 for items in (fields, controls, presets)):
            raise ValueError("invalid panel lists")
        for control in controls:
            if not isinstance(control, dict) or control.get("type") not in {"button", "send"} or not isinstance(control.get("command"), str):
                raise ValueError("invalid panel control")
            if "label" in control and not isinstance(control["label"], str):
                raise ValueError("invalid control label")
            inputs = control.get("inputs", [])
            if not isinstance(inputs, list) or len(inputs) > 100:
                raise ValueError("invalid panel action inputs")
            fields = [*fields, *inputs]
        for field in fields:
            if (not isinstance(field, dict) or not isinstance(field.get("key"), str)
                    or field.get("type") not in {"string", "text", "enum", "number", "boolean", "badge", "markdown"}
                    or "label" in field and not isinstance(field["label"], str)):
                raise ValueError("invalid panel field")
            options = field.get("options", [])
            if not isinstance(options, list) or any(
                not isinstance(option, dict) or not isinstance(option.get("value"), (str, int, float, bool))
                or "label" in option and not isinstance(option["label"], str) for option in options
            ):
                raise ValueError("invalid panel options")
        if any(not isinstance(preset, dict) or not isinstance(preset.get("values"), dict)
               or not isinstance(preset.get("name"), str) for preset in presets):
            raise ValueError("invalid panel presets")
    return panel
