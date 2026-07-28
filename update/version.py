"""版本号读取、解析、协议兼容检测与远程比对。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

from update.git import run_git


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
    """从 FETCH_HEAD 读取远程 version.json。"""
    try:
        result = run_git(
            ["show", "FETCH_HEAD:version.json"],
            project_root,
            timeout=10,
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
        local_part = lv[i] if i < len(lv) else 0
        remote_part = rv[i] if i < len(rv) else 0
        if local_part < remote_part:
            return 1
        if local_part > remote_part:
            return -1
    return 0


def check_protocol_compatibility(local: VersionInfo, remote: VersionInfo) -> tuple[bool, str]:
    """检测协议版本兼容性。返回 (兼容?, 说明)。"""
    try:
        lp = tuple(int(x) for x in local.protocol_version.split("."))
        rp = tuple(int(x) for x in remote.protocol_version.split("."))
    except ValueError:
        return True, ""

    if rp[0] > lp[0]:
        return (
            False,
            f"协议版本不兼容：本地 {local.protocol_version} → 远程 {remote.protocol_version}，"
            f"主版本号变更可能导致厂商包接口不匹配，请确认后再更新。",
        )
    if rp[0] < lp[0]:
        return (
            False,
            f"远程协议版本 {remote.protocol_version} 低于本地 {local.protocol_version}，"
            f"远程可能是旧版本，请确认更新方向是否正确。",
        )
    if rp[1] > lp[1]:
        return (
            True,
            f"协议次版本更新：{local.protocol_version} → {remote.protocol_version}，"
            f"向后兼容。",
        )
    return True, ""
