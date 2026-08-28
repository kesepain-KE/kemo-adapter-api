"""网关版本清单的加载、验证、比较和安全报告。

网关目前使用扁平的 ``version.json``（版本号、协议版本、构建号和说明），
与 kemo-agent 的板块清单不同，因此这里不虚构组件版本；模块化只体现在
职责拆分上，版本文件仍保持向后兼容。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

from ._utils import UpdateError, compare_versions, redact_text
from .version import VersionInfo


def _version_from_document(document: object, *, label: str) -> VersionInfo:
    if not isinstance(document, dict):
        raise UpdateError(f"{label} version.json 不是 JSON 对象")
    version = str(document.get("version", "")).strip()
    protocol = str(document.get("protocol_version", "")).strip()
    if not version:
        raise UpdateError(f"{label} version.json 缺少 version")
    if not protocol:
        raise UpdateError(f"{label} version.json 缺少 protocol_version")
    # 复用严格版本解析，避免将任意文本误认为可更新版本。
    from ._utils import parse_version

    parse_version(version)
    parse_version(protocol)
    notes = document.get("notes", "")
    if isinstance(notes, list):
        notes_text = "；".join(str(item) for item in notes)
    else:
        notes_text = str(notes or "")
    return VersionInfo(version, protocol, notes_text)


def load_local(*, root: Path) -> VersionInfo:
    path = root / "version.json"
    if not path.is_file():
        raise UpdateError(f"未找到本地版本文件：{path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"本地 version.json 无法读取：{redact_text(exc)}") from exc
    return _version_from_document(document, label="本地")


def load_from_git(*, root: Path) -> VersionInfo:
    from .git import run_git

    result = run_git(["show", "FETCH_HEAD:version.json"], root, timeout=10)
    if result.returncode != 0:
        raise UpdateError("FETCH_HEAD 中没有可读取的远程 version.json")
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise UpdateError(f"远程 version.json 格式无效：{redact_text(exc)}") from exc
    return _version_from_document(document, label="远程")


def load_from_url(url: str, *, timeout: float = 30.0) -> VersionInfo:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kemo-gateway-updater",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(
            f"远程版本文件读取失败：{redact_text(exc)}"
        ) from exc
    return _version_from_document(document, label="远程")


def compare(local: VersionInfo, remote: VersionInfo) -> int:
    """返回 -1（远程较旧）、0（相同）、1（远程较新）。"""

    return compare_versions(local.version, remote.version) * -1


def ensure_no_downgrade(local: VersionInfo, remote: VersionInfo) -> None:
    if compare_versions(remote.version, local.version) < 0:
        raise UpdateError(
            f"拒绝自动降级：本地 {local.version} 高于远程 {remote.version}。"
            "如需恢复旧版本，请使用经过确认的更新备份。"
        )


def protocol_compatibility(local: VersionInfo, remote: VersionInfo) -> tuple[bool, str]:
    """检查 Kemo 协议主版本是否发生不兼容变化。"""

    from .version import check_protocol_compatibility

    return check_protocol_compatibility(local, remote)


def report(local: VersionInfo, remote: VersionInfo) -> list[str]:
    lines = [
        f"本地版本    {local.version}（Kemo 协议 {local.protocol_version}）",
        f"远程版本    {remote.version}（Kemo 协议 {remote.protocol_version}）",
    ]
    comparison = compare(local, remote)
    if comparison > 0:
        lines.append("状态        有可用更新")
    elif comparison < 0:
        lines.append("状态        本地版本较新，已禁止自动降级")
    else:
        lines.append("状态        版本号相同；仍需结合 Git 提交判断补丁更新")
    if remote.notes:
        lines.append(f"远程说明    {remote.notes}")
    compatible, message = protocol_compatibility(local, remote)
    if message:
        lines.append(("协议提示    " if compatible else "协议警告    ") + message)
    return lines

