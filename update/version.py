"""版本号读取、解析与远程比对。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple


class VersionInfo(NamedTuple):
    version: str
    protocol_version: str
    notes: str


def read_local(project_root: Path) -> VersionInfo:
    """读取本地 version.json。"""
    path = project_root / "version.json"
    if not path.is_file():
        return VersionInfo("0.0.0", "0.0", "")
    try:
        data = json.loads(path.read_text("utf-8"))
        return VersionInfo(
            version=str(data.get("version", "0.0.0")),
            protocol_version=str(data.get("protocol_version", "0.0")),
            notes=str(data.get("notes", "")),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return VersionInfo("0.0.0", "0.0", "")


def read_remote(project_root: Path) -> VersionInfo | None:
    """从 origin/main 读取远程 version.json。"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "show", "origin/main:version.json"],
            cwd=project_root,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return VersionInfo(
            version=str(data.get("version", "0.0.0")),
            protocol_version=str(data.get("protocol_version", "0.0")),
            notes=str(data.get("notes", "")),
        )
    except Exception:
        return None


def compare(local: VersionInfo, remote: VersionInfo) -> int:
    """比较版本号。返回 -1=本地更新, 0=相同, 1=远程有新版本。"""
    try:
        lv = tuple(int(x) for x in local.version.split("."))
        rv = tuple(int(x) for x in remote.version.split("."))
    except ValueError:
        return 0
    for i in range(max(len(lv), len(rv))):
        l = lv[i] if i < len(lv) else 0
        r = rv[i] if i < len(rv) else 0
        if l < r:
            return 1
        if l > r:
            return -1
    return 0
