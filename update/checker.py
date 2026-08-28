"""远端检查、版本报告与精确更新目标规划。"""

from __future__ import annotations

from pathlib import Path

from update import git, version
from update._utils import redact_text
from update.constants import DEFAULT_BRANCH


def check(
    project_root: Path,
    local: version.VersionInfo | None = None,
    *,
    branch: str = DEFAULT_BRANCH,
    remote_url: str | None = None,
    remote_version_url: str | None = None,
) -> tuple[int, version.VersionInfo | None, version.VersionInfo | None]:
    """只读检查远端，并报告 HEAD 与精确 FETCH_HEAD 的关系。

    ``remote_url`` 和 ``branch`` 只用于本次 fetch，不会改写 origin；
    ``remote_version_url`` 是无 Git 环境下的只读版本检查备用通道，不能
    单独授权写入源码。
    """

    local = local or version.read_local(project_root)
    print(f"[KEMO] 本地版本: {local.version}  (protocol {local.protocol_version})")
    if local.notes:
        print(f"[KEMO] 本地说明: {local.notes}")
    print("[KEMO] 正在检测远程更新...")
    if branch == DEFAULT_BRANCH and remote_url is None:
        ok, mirror_label = git.fetch(project_root)
    else:
        ok, mirror_label = git.fetch(
            project_root,
            branch=branch,
            remote_url=remote_url,
        )
    if not ok:
        if remote_version_url:
            fallback = version.read_remote_url(remote_version_url)
            if fallback is not None:
                print(f"[KEMO] 远程版本（只读）：{fallback.version}  (protocol {fallback.protocol_version})")
                if fallback.notes:
                    print(f"[KEMO] 更新说明：{redact_text(fallback.notes)}")
                print("[WARN] 当前无法完成 Git fetch；该结果只能用于查看，不能执行源码更新。")
                return 0, local, fallback
        print(f"[ERROR] 无法连接远程仓库: {mirror_label}")
        return 1, local, None
    print(f"[KEMO] 远程源: {mirror_label}")

    target_commit = git.get_fetch_commit(project_root)
    state = git.get_sync_state(project_root)
    if not target_commit or state.relation == "unknown":
        print("[ERROR] 无法确认 FETCH_HEAD 与本地提交关系。")
        return 1, local, None
    remote = version.read_remote(project_root)
    if remote is None:
        print("[ERROR] 无法读取远程版本信息。")
        return 1, local, None
    print(f"[KEMO] 远程版本: {remote.version}  (protocol {remote.protocol_version})")
    if remote.notes:
        print(f"[KEMO] 更新说明: {remote.notes}")

    if state.relation == "up_to_date":
        print("[KEMO] 当前已是最新版本。")
    elif state.relation == "behind":
        print(f"[KEMO] 远端新增 {state.behind} 个提交。")
    elif state.relation == "ahead":
        print(f"[WARN] 本地领先远端 {state.ahead} 个提交；普通更新不会覆盖本地提交。")
    else:
        print(
            f"[WARN] 本地与远端已经分叉：本地独有 {state.ahead} 个提交，"
            f"远端独有 {state.behind} 个提交。"
        )

    if state.behind:
        logs = git.get_commit_log(project_root)
        if logs:
            print(f"[KEMO] 更新内容 ({len(logs)} 个提交):")
            for log in logs:
                print(f"      {log}")
        diff = git.get_remote_diff(project_root)
        protected_diff = git.get_protected_remote_diff(project_root)
        if diff.has_changes:
            print(f"[KEMO] 可更新源码 ({len(diff.files)} 个):")
            for file_name in diff.files:
                print(f"      - {file_name}")
        if protected_diff.has_changes:
            print(f"[WARN] 受保护路径变更 ({len(protected_diff.files)} 个):")
            for file_name in protected_diff.files:
                print(f"      - {file_name}")
            print("[WARN] 普通更新和修复都会拒绝该远端提交。")

    comparison = version.compare(local, remote)
    if comparison > 0:
        print(f"[KEMO] 版本差异: {local.version} → {remote.version}")
    elif comparison < 0:
        print(f"[WARN] 本地版本 {local.version} 高于远端版本 {remote.version}。")
    elif state.behind:
        print("[KEMO] 版本号相同，但远端包含补丁提交。")
    compatible, message = version.check_protocol_compatibility(local, remote)
    if message:
        print(f"[KEMO] {message}" if compatible else f"[WARN] {message}")
    return 0, local, remote


def reject_protected_remote_changes(project_root: Path) -> bool:
    protected = git.get_protected_remote_diff(project_root)
    if not protected.has_changes:
        return False
    print("[ERROR] 远端差异涉及受保护路径，已拒绝操作：")
    for file_name in protected.files:
        print(f"      - {file_name}")
    print("[ERROR] 本地配置、Provider、密钥、统计和开发目录均未改动。")
    return True

