"""Git 操作：远程检测、同步状态、暂存与精确提交应用。"""

from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import NamedTuple


class GitDiff(NamedTuple):
    files: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.files)


class GitSyncState(NamedTuple):
    relation: str
    ahead: int
    behind: int


PROTECTED_PATTERNS = (
    ".env",
    "providers/",
    "api/keys.json",
    "storage/daily/",
    "storage/assets/",
    "storage/executions/",
    "core/runtime/",
    ".backup/",
    "开发目录/",
    "*.bak",
    "*.bak.*",
    "*.log",
    "*.pid",
)

PROTECTED_EXCEPTIONS = frozenset({"providers/__init__.py"})

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
    return subprocess.run(
        [
            "git",
            "-c",
            "i18n.logOutputEncoding=UTF-8",
            "-c",
            "core.quotePath=false",
            *args,
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_git_environment(),
        timeout=timeout,
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


def fetch(project_root: Path) -> tuple[bool, str]:
    """git fetch，自动尝试多镜像源。返回 (成功, 使用的镜像描述)。"""
    remote_url = _resolve_remote_url(project_root)
    if not remote_url:
        return False, "无法获取远程仓库地址"

    last_error = ""

    for mirror_prefix in _iter_mirrors(project_root):
        url = _mirror_url(remote_url, mirror_prefix)
        label = "直连" if not mirror_prefix else f"镜像源({mirror_prefix})"

        # 使用临时 remote 来 fetch（不影响 origin 配置）
        r = _git(
            ["fetch", url, "main"],
            project_root,
            timeout=60,
        )
        if r.returncode == 0:
            # 更新 origin/HEAD 引用
            _git(["update-ref", "refs/remotes/origin/main", "FETCH_HEAD"], project_root)
            return True, label

        last_error = r.stderr.strip() or f"exit code {r.returncode}"
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


def stash_pop(project_root: Path) -> bool:
    r = _git(["stash", "pop"], project_root)
    return r.returncode == 0


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
            if normalized.startswith(pattern) or f"/{pattern}" in normalized:
                return True
        elif "*" in pattern:
            basename = normalized.rsplit("/", 1)[-1]
            if fnmatch(normalized, pattern) or fnmatch(basename, pattern):
                return True
        elif normalized == pattern or normalized.endswith(f"/{pattern}"):
            return True
    return False
