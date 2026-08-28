"""更新前检查结果的结构化表示。

交互界面、状态命令和实际更新都可以消费同一份计划，避免“检查时看到一套
提交、确认后又重新拉了另一套提交”的逻辑分叉。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git, integrity, version
from .git import GitDiff, GitSyncState


@dataclass(frozen=True)
class UpdatePlan:
    root: Path
    local: version.VersionInfo
    remote: version.VersionInfo | None
    state: GitSyncState
    target_commit: str
    source_diff: GitDiff
    protected_diff: GitDiff
    source_state: integrity.IntegrityResult
    mirror_label: str = ""
    protocol_compatible: bool | None = None
    protocol_message: str = ""

    @property
    def has_remote_target(self) -> bool:
        return bool(self.remote is not None and self.target_commit)

    @property
    def has_update(self) -> bool:
        return self.state.behind > 0 and bool(self.source_diff.files)

    @property
    def safe_to_apply(self) -> bool:
        return (
            self.has_remote_target
            and self.state.relation in {"behind", "up_to_date"}
            and not self.protected_diff.has_changes
            and self.source_state.ok
            and self.protocol_compatible is True
        )


def collect(
    root: Path,
    *,
    branch: str = "main",
    remote_url: str | None = None,
) -> tuple[int, UpdatePlan]:
    """拉取一次远端并构造计划；失败也返回可展示的本地部分结果。"""

    local = version.read_local(root)
    if branch == "main" and remote_url is None:
        ok, mirror_label = git.fetch(root)
    else:
        ok, mirror_label = git.fetch(root, branch=branch, remote_url=remote_url)
    if not ok:
        return 1, UpdatePlan(
            root,
            local,
            None,
            GitSyncState("unknown", 0, 0),
            "",
            GitDiff([]),
            GitDiff([]),
            integrity.check_source_state(root),
            mirror_label,
        )

    target_commit = git.get_fetch_commit(root)
    state = git.get_sync_state(root)
    remote = version.read_remote(root)
    source_diff = git.get_remote_diff(root)
    protected_diff = git.get_protected_remote_diff(root)
    code = 0 if target_commit and state.relation != "unknown" and remote else 1
    if remote is None:
        protocol_compatible, protocol_message = None, ""
    else:
        protocol_compatible, protocol_message = version.check_protocol_compatibility(
            local, remote
        )
    return code, UpdatePlan(
        root,
        local,
        remote,
        state,
        target_commit,
        source_diff,
        protected_diff,
        integrity.check_source_state(root),
        mirror_label,
        protocol_compatible,
        protocol_message,
    )


def render(plan: UpdatePlan) -> list[str]:
    """生成适合终端显示的稳定摘要；不包含任何密钥原文。"""

    lines = [
        f"本地版本：{plan.local.version}（Kemo 协议 {plan.local.protocol_version}）",
    ]
    if plan.remote is None:
        lines.append("远程版本：无法读取")
    else:
        lines.append(
            f"远程版本：{plan.remote.version}（Kemo 协议 {plan.remote.protocol_version}）"
        )
        if plan.protocol_compatible is False:
            detail = f"：{plan.protocol_message}" if plan.protocol_message else ""
            lines.append(f"协议状态：不兼容，实际更新将被拒绝{detail}")
        elif plan.protocol_compatible is True:
            detail = f"（{plan.protocol_message}）" if plan.protocol_message else ""
            lines.append(f"协议状态：兼容{detail}")
        else:
            lines.append("协议状态：无法判断，实际更新前会再次检查")
    lines.append(
        "Git 状态："
        f"{plan.state.relation}（本地领先 {plan.state.ahead}，远端领先 {plan.state.behind}）"
    )
    if plan.target_commit:
        lines.append(f"目标提交：{plan.target_commit[:12]}")
    if plan.mirror_label:
        lines.append(f"远程源：{plan.mirror_label}")
    lines.append(f"源码变更：{len(plan.source_diff.files)} 个文件")
    if plan.protected_diff.has_changes:
        lines.append(f"受保护变更：{len(plan.protected_diff.files)} 个文件（将拒绝更新）")
    if not plan.source_state.ok:
        lines.append(f"源码完整性：{plan.source_state.message}")
    else:
        lines.append("源码完整性：正常")
    return lines
