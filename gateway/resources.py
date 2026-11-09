"""Official resource inventories. User resources live exclusively in extensions/."""
from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path
from typing import Literal

from pydantic import Field

from core.yaml_config import read_yaml
from gateway.domain import Document
from gateway.extensions import read_manifest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_PATHS = {
    "protocol_adapters": "gateway/adapters/builtin",
    "provider_templates": "templates/provider_presets",
}
VERSION = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"


class ResourcePack(Document):
    contract_version: Literal["1.0"]
    version: str = Field(pattern=VERSION)


class AdapterEntry(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    version: str = Field(pattern=VERSION)
    module: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    class_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")


class AdapterPack(ResourcePack):
    adapters: tuple[AdapterEntry, ...] = Field(min_length=1)


def adapter_pack(root=SOURCE_ROOT):
    pack = AdapterPack.model_validate(read_yaml(root / BUILTIN_PATHS["protocol_adapters"] / "manifest.yaml"))
    if len({entry.id for entry in pack.adapters}) != len(pack.adapters):
        raise ValueError("内置适配器 ID 重复")
    return pack


def load_builtin_adapters():
    result = []
    for entry in adapter_pack().adapters:
        cls = getattr(import_module(f"gateway.adapters.builtin.{entry.module}"), entry.class_name)
        manifest = cls.manifest
        if (manifest.id, manifest.version, manifest.contract_version, manifest.builtin) != (
            entry.id, entry.version, "1.0", True,
        ):
            raise ValueError(f"内置适配器清单与实现不一致：{entry.id}")
        result.append(cls)
    return tuple(result)


def builtin_templates(root=SOURCE_ROOT):
    path = root / BUILTIN_PATHS["provider_templates"]
    ResourcePack.model_validate(read_yaml(path / "manifest.yaml"))
    result = {}
    for directory in sorted(path.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        manifest = read_manifest(directory, "provider_templates")
        if manifest["id"] != directory.name:
            raise ValueError("内置模板清单 ID 与目录不一致")
        result[directory.name] = {**manifest, "builtin": True, "source": "builtin"}
    return result


def inventory(root: Path):
    """Read versions without executing downloaded Python code."""
    adapters = adapter_pack(root)
    templates = builtin_templates(root)
    template_pack = ResourcePack.model_validate(
        read_yaml(root / BUILTIN_PATHS["provider_templates"] / "manifest.yaml"),
    )
    return {
        "protocol_adapters": {
            "version": adapters.version,
            "items": {entry.id: entry.version for entry in adapters.adapters},
        },
        "provider_templates": {
            "version": template_pack.version,
            "items": {identity: value["version"] for identity, value in templates.items()},
        },
    }


def resource_revision(root: Path):
    digest = hashlib.sha256()
    for relative in BUILTIN_PATHS.values():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("内置资源目录不能链接到项目外")
        for file in sorted(path.rglob("*")):
            if "__pycache__" in file.parts:
                continue
            if file.is_symlink() or not file.resolve().is_relative_to(path.resolve()):
                raise ValueError("内置资源不能包含符号链接")
            if file.is_file():
                digest.update(file.relative_to(root).as_posix().encode())
                digest.update(file.read_bytes())
    return digest.hexdigest()


def resource_stamp(root: Path):
    """Official updates replace these manifests; console polling need not hash Python files."""
    return tuple((path.stat().st_mtime_ns, path.stat().st_size)
                 for path in (root / relative / "manifest.yaml" for relative in BUILTIN_PATHS.values()))
