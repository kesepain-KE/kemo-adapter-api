"""版本号读取、解析、协议兼容检测与远程比对。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple
from urllib.request import Request, urlopen

from update._utils import UpdateError, parse_version, redact_text
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
        return _from_mapping(data)
    except (UpdateError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
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
        return _from_mapping(data)
    except (UpdateError, TypeError, ValueError, KeyError, OSError, json.JSONDecodeError):
        return None


def read_remote_url(url: str, *, timeout: float = 30.0) -> VersionInfo | None:
    """从远程 URL 读取 version.json，失败时返回 None。

    这是 Git 不可用或远程仓库暂时不可达时的只读诊断通道；正式写入仍然
    必须回到精确的 Git 提交，避免版本文件与源码不是同一快照。
    """

    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "kemo-gateway-updater"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return _from_mapping(data)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _from_mapping(data: object) -> VersionInfo:
    if not isinstance(data, dict):
        raise ValueError("version.json 必须是 JSON 对象")
    version = str(data.get("version", "0.0.0")).strip()
    protocol = str(data.get("protocol_version", "0.0")).strip()
    parse_version(version)
    parse_version(protocol)
    notes_value = data.get("notes", "")
    if isinstance(notes_value, list):
        notes = "；".join(str(item) for item in notes_value)
    else:
        notes = str(notes_value or "")
    return VersionInfo(version, protocol, notes)


def validate(info: VersionInfo, *, label: str = "版本") -> VersionInfo:
    """严格验证版本对象；旧调用方仍可继续使用宽松的 read_local。"""

    try:
        parse_version(info.version)
        parse_version(info.protocol_version)
    except UpdateError as exc:
        raise UpdateError(f"{label}信息无效：{redact_text(exc)}") from exc
    return info


def compare(local: VersionInfo, remote: VersionInfo) -> int:
    """比较版本号。返回 -1=本地更新, 0=相同, 1=远程有新版本。"""
    try:
        lv = parse_version(local.version)
        rv = parse_version(remote.version)
    except UpdateError:
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
        lp_raw = tuple(int(x) for x in local.protocol_version.split("."))
        rp_raw = tuple(int(x) for x in remote.protocol_version.split("."))
    except (TypeError, ValueError):
        return True, ""

    # ``1``、``1.0`` 和 ``1.0.0`` 都是合法的写法；统一补齐后再比较，
    # 避免旧 version.json 因访问不存在的次版本下标而崩溃。
    width = max(2, len(lp_raw), len(rp_raw))
    lp = lp_raw + (0,) * (width - len(lp_raw))
    rp = rp_raw + (0,) * (width - len(rp_raw))

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
