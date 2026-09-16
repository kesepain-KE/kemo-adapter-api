"""Git 操作：远程检测、同步状态、暂存与精确提交应用。"""

from __future__ import annotations

import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import NamedTuple

from update._utils import redact_text
from update.constants import PROTECTED_EXCEPTIONS, PROTECTED_PATTERNS


class GitDiff(NamedTuple):
    files: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.files)


class GitSyncState(NamedTuple):
    relation: str
    ahead: int
    behind: int


# 兼容已有调用方；这些路径不只是从差异展示中排除，也会阻止更新执行。
EXCLUDED_PATTERNS = PROTECTED_PATTERNS

# 按优先级自动尝试的镜像源（空字符串 = 直连，不包装饰）
_MIRROR_CHAINS = [
    "",                                          # 1. 直连
    "https://ghproxy.net/",                      # 2. ghproxy 镜像
    "https://mirror.ghproxy.com/",               # 3. 另一个 ghproxy 节点
]


def _iter_mirrors(project_root: Path):
    """迭代镜像源列表，环境变量 GIT_MIRROR 优先级最高。"""
    env_mirror = os.environ.get("GIT_MIRROR", "").strip()
    if env_mirror:
        yield env_mirror
        return  # 环境变量指定后不再尝试其他
    for m in _MIRROR_CHAINS:
        yield m


def _git_environment() -> dict[str, str]:
    """Return a deterministic environment for machine-readable Git output.

    Git for Windows can emit UTF-8 repository data while Python otherwise
    decodes ``text=True`` streams with the active ANSI code page (for example
    cp936). A stable C locale also keeps diagnostics parseable on Linux.
    """
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def run_git(
    args: list[str], project_root: Path, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """Run Git with one UTF-8 text boundary on Windows and Linux."""
    command = [
        "git",
        "-c",
        "i18n.logOutputEncoding=UTF-8",
        "-c",
        "core.quotePath=false",
        *args,
    ]
    try:
        return subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout="",
            stderr=f"Git 命令超时（{timeout} 秒）: {exc}",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            command,
            127,
            stdout="",
            stderr=f"无法启动 Git: {exc}",
        )


def _git(
    args: list[str], project_root: Path, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """Backward-compatible internal alias for existing update operations."""
    return run_git(args, project_root, timeout)


def _resolve_remote_url(project_root: Path) -> str | None:
    """获取 origin 的远程仓库 URL（HTTPS 格式）。"""
    r = _git(["remote", "get-url", "origin"], project_root)
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    # SSH 格式转为 HTTPS（方便镜像源包装）
    if url.startswith("git@"):
        url = url.replace(":", "/").replace("git@", "https://")
    return url


def _mirror_url(remote_url: str, mirror_prefix: str) -> str:
    """用镜像源前缀包装远程 URL。mirror_prefix 为空时返回原 URL。"""
    if not mirror_prefix:
        return remote_url
    prefix = mirror_prefix.rstrip("/")
    return f"{prefix}/{remote_url}"


def fetch(
    project_root: Path,
    *,
    branch: str = "main",
    remote_url: str | None = None,
) -> tuple[bool, str]:
    """拉取指定分支，并返回 ``(成功, 使用的源描述)``。

    默认仍从本地 ``origin`` 读取仓库地址；测试、镜像和私有部署可以传入
    临时 URL，而不会改写仓库的 origin 配置。非 HTTP(S) 地址（例如本地
    bare 仓库）只尝试直连，避免无意义地拼接公共镜像前缀。
    """

    branch = branch.strip() or "main"
    if (
        branch.startswith("-")
        or branch.endswith("/")
        or ".." in branch
        or not re.fullmatch(r"[A-Za-z0-9._/@-]+", branch)
    ):
        return False, "远程分支名称无效"
    remote_url = remote_url or _resolve_remote_url(project_root)
    if not remote_url:
        return False, "无法获取远程仓库地址"

    last_error = ""

    mirror_candidates = list(_iter_mirrors(project_root))
    if not remote_url.lower().startswith(("http://", "https://")):
        mirror_candidates = [""]

    for mirror_prefix in mirror_candidates:
        url = _mirror_url(remote_url, mirror_prefix)
        label = "直连" if not mirror_prefix else f"镜像源({mirror_prefix})"

        # 使用临时 remote 来 fetch（不影响 origin 配置）
        r = _git(
            ["fetch", url, branch],
            project_root,
            timeout=60,
        )
        if r.returncode == 0:
            # 更新 origin/<branch> 引用；不改写 origin URL。
            _git(
                ["update-ref", f"refs/remotes/origin/{branch}", "FETCH_HEAD"],
                project_root,
            )
            return True, label

        last_error = redact_text(r.stderr.strip() or f"exit code {r.returncode}")
        # 如果是环境变量指定的镜像源失败，不再降级尝试
        if os.environ.get("GIT_MIRROR", "").strip():
            break

    return False, f"所有源均失败，最后错误: {last_error[:200]}"


def get_remote_files(project_root: Path) -> list[str]:
    """获取 FETCH_HEAD 相比 HEAD 新增、修改或删除的完整文件列表。"""
    r = _git(
        ["diff", "--name-only", "HEAD..FETCH_HEAD"],
        project_root,
    )
    if r.returncode != 0:
        # 可能没有本地 commit，尝试从 FETCH_HEAD 的父级 diff
        r = _git(
            ["diff", "--name-only", "4b825dc642cb6eb9a060e54bf899d153036d5e4d..FETCH_HEAD"],
            project_root,
        )
    if r.returncode != 0:
        return []

    return [
        f.replace("\\", "/")
        for f in r.stdout.strip().split("\n")
        if f.strip()
    ]


def get_remote_diff(project_root: Path) -> GitDiff:
    """获取可安全更新的远端文件列表。"""
    filtered = [f for f in get_remote_files(project_root) if not _is_protected(f)]
    return GitDiff(filtered)


def get_protected_remote_diff(project_root: Path) -> GitDiff:
    """获取会触碰本地持久化数据或私有目录的远端变更。"""
    protected = [f for f in get_remote_files(project_root) if _is_protected(f)]
    return GitDiff(protected)


def has_remote_commits(project_root: Path) -> bool:
    """检查 fetch 后是否有新提交。"""
    return get_sync_state(project_root).behind > 0


def get_sync_state(project_root: Path) -> GitSyncState:
    """Classify HEAD relative to the exact commit stored in FETCH_HEAD."""
    r = _git(
        ["rev-list", "--left-right", "--count", "HEAD...FETCH_HEAD"],
        project_root,
    )
    if r.returncode != 0:
        return GitSyncState("unknown", 0, 0)
    try:
        ahead_text, behind_text = r.stdout.split()
        ahead = int(ahead_text)
        behind = int(behind_text)
    except (TypeError, ValueError):
        return GitSyncState("unknown", 0, 0)
    if ahead and behind:
        relation = "diverged"
    elif ahead:
        relation = "ahead"
    elif behind:
        relation = "behind"
    else:
        relation = "up_to_date"
    return GitSyncState(relation, ahead, behind)


def get_commit_log(project_root: Path, max_count: int = 10) -> list[str]:
    """获取 FETCH_HEAD 相比 HEAD 的提交日志。"""
    r = _git(
        ["log", f"--max-count={max_count}", "--oneline", "HEAD..FETCH_HEAD"],
        project_root,
    )
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.strip().split("\n") if line.strip()]


def get_stash_label() -> str:
    import datetime
    return f"kemo-update-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"


def stash_local(project_root: Path, label: str) -> bool:
    """暂存已跟踪和未跟踪源码；Git 忽略的持久化数据保持原位。"""
    r = _git(["stash", "push", "--include-untracked", "-m", label], project_root)
    return r.returncode == 0 and "Saved working directory" in r.stdout


def create_stash(project_root: Path, label: str) -> str | None:
    """创建 stash 并返回不可变提交 ID，避免恢复错误的 stash@{0}。"""

    if not stash_local(project_root, label):
        return None
    result = _git(["rev-parse", "--verify", "refs/stash^{commit}"], project_root)
    commit = result.stdout.strip() if result.returncode == 0 else ""
    return commit or None


def stash_pop(project_root: Path) -> bool:
    r = _git(["stash", "pop"], project_root)
    return r.returncode == 0


def apply_stash(project_root: Path, stash_commit: str) -> bool:
    """应用指定 stash 但先不删除；只有完整更新成功后才会 drop。"""

    if not stash_commit:
        return True
    result = _git(["stash", "apply", "--index", stash_commit], project_root, timeout=60)
    return result.returncode == 0 and not get_unmerged_files(project_root)


def drop_stash(project_root: Path, stash_commit: str) -> bool:
    """只删除与给定提交 ID 完全匹配的 stash。"""

    result = _git(["stash", "list", "--format=%H"], project_root)
    if result.returncode != 0:
        return False
    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        index = commits.index(stash_commit)
    except ValueError:
        return False
    dropped = _git(["stash", "drop", f"stash@{{{index}}}"], project_root)
    return dropped.returncode == 0


def get_fetch_commit(project_root: Path) -> str:
    """Return the immutable commit id selected by the last successful fetch."""
    r = _git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], project_root)
    return r.stdout.strip() if r.returncode == 0 else ""


def fast_forward_to_fetch_head(
    project_root: Path,
    expected_commit: str | None = None,
) -> bool:
    """Fast-forward HEAD to the already inspected fetched commit."""
    target = expected_commit or "FETCH_HEAD"
    r = _git(["merge", "--ff-only", target], project_root, timeout=60)
    return r.returncode == 0


def hard_reset_to_fetch_head(
    project_root: Path,
    expected_commit: str | None = None,
) -> bool:
    """Reset tracked source to the inspected fetched commit in explicit repair mode."""
    target = expected_commit or "FETCH_HEAD"
    r = _git(["reset", "--hard", target], project_root, timeout=60)
    return r.returncode == 0


def reset_to_commit(project_root: Path, commit: str) -> bool:
    """事务失败时仅回到本事务记录的更新前提交。"""

    if not commit:
        return False
    result = _git(["reset", "--hard", commit], project_root, timeout=60)
    return result.returncode == 0


def get_unmerged_files(project_root: Path) -> list[str]:
    """列出 index 中所有 UU/AA/DD 等未解决文件。"""

    result = _git(["diff", "--name-only", "--diff-filter=U"], project_root)
    if result.returncode != 0:
        return []
    return [
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _git_path(project_root: Path, name: str) -> Path | None:
    result = _git(["rev-parse", "--git-path", name], project_root)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else project_root / path


def get_operation_state(project_root: Path) -> str | None:
    """检测尚未完成的 merge/rebase/cherry-pick/revert。"""

    candidates = (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    )
    for marker, label in candidates:
        path = _git_path(project_root, marker)
        if path is not None and path.exists():
            return label
    return None


def abort_in_progress_operations(project_root: Path) -> None:
    """尽力退出未完成 Git 操作；随后 reset 会保证 index 回到确定状态。"""

    operation = get_operation_state(project_root)
    commands = {
        "merge": ["merge", "--abort"],
        "rebase": ["rebase", "--abort"],
        "cherry-pick": ["cherry-pick", "--abort"],
        "revert": ["revert", "--abort"],
    }
    if operation in commands:
        _git(commands[operation], project_root, timeout=60)


def clean_update_paths(project_root: Path, paths: list[str]) -> bool:
    """只清理远端更新涉及的非保护未跟踪路径，绝不扩大到整个仓库。"""

    safe_paths = sorted({path for path in paths if path and not _is_protected(path)})
    if not safe_paths:
        return True
    success = True
    for start in range(0, len(safe_paths), 100):
        chunk = safe_paths[start : start + 100]
        result = _git(["clean", "-fd", "--", *chunk], project_root, timeout=60)
        success = success and result.returncode == 0
    return success


def create_recovery_ref(
    project_root: Path,
    *,
    label: str,
    commit: str,
) -> str | None:
    """Keep local commits reachable before a destructive repair reset."""
    safe_label = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in label
    ).strip("-")
    if not safe_label or not commit:
        return None
    reference = f"refs/kemo-update/recovery/{safe_label}"
    r = _git(["update-ref", reference, commit], project_root)
    return reference if r.returncode == 0 else None


def get_current_commit(project_root: Path) -> str:
    r = _git(["rev-parse", "HEAD"], project_root)
    return r.stdout.strip() if r.returncode == 0 else ""


def get_current_branch(project_root: Path) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def has_local_changes(project_root: Path) -> bool:
    """检查是否有未提交的本地修改。"""
    r = _git(["status", "--porcelain"], project_root)
    return bool(r.stdout.strip()) if r.returncode == 0 else False


def _is_excluded(path: str) -> bool:
    """向后兼容旧名称。"""
    return _is_protected(path)


def _is_protected(path: str) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in PROTECTED_EXCEPTIONS:
        return False
    for pattern in PROTECTED_PATTERNS:
        if pattern.endswith("/"):
            if normalized.startswith(pattern):
                return True
            # tests/ 属于发布测试源码；嵌套目录匹配只对部署数据目录生效，
            # 避免 tests/providers/ 被误判为部署者私有的 providers/。
            if not normalized.startswith("tests/") and f"/{pattern}" in normalized:
                return True
        elif "*" in pattern:
            basename = normalized.rsplit("/", 1)[-1]
            if fnmatch(normalized, pattern) or fnmatch(basename, pattern):
                return True
        elif normalized == pattern or normalized.endswith(f"/{pattern}"):
            return True
    return False
