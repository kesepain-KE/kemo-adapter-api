"""Git 操作：远程检测、暂存本地修改、拉取、回滚。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple


class GitDiff(NamedTuple):
    """远程相比本地新增/修改的文件清单。"""
    files: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.files)


# 更新时排除的路径模式（不覆盖本地部署配置）
EXCLUDED_PATTERNS = (
    "providers/",
    "prompt.md",
    "api/keys.json",
    "*.bak",
)


def _git(args: list[str], project_root: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=project_root,
        capture_output=True, text=True, timeout=timeout,
    )


def fetch(project_root: Path) -> bool:
    """git fetch origin。返回是否成功。"""
    r = _git(["fetch", "origin"], project_root)
    return r.returncode == 0


def get_remote_diff(project_root: Path) -> GitDiff:
    """获取 origin/main 相比 HEAD 新增/修改的文件列表（排除更新保护路径）。"""
    r = _git(
        ["diff", "--name-only", "HEAD..origin/main"],
        project_root,
    )
    if r.returncode != 0:
        return GitDiff([])

    files = [f.replace("\\", "/") for f in r.stdout.strip().split("\n") if f.strip()]
    filtered = [f for f in files if not _is_excluded(f)]
    return GitDiff(filtered)


def has_remote_commits(project_root: Path) -> bool:
    """检查远程是否有新提交。"""
    r = _git(["rev-list", "--count", "HEAD..origin/main"], project_root)
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


def get_stash_label() -> str:
    """生成 stash 标记名。"""
    import datetime
    return f"kemo-update-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"


def stash_local(project_root: Path, label: str) -> bool:
    """暂存本地未提交的修改。返回是否有修改被暂存。"""
    r = _git(["stash", "push", "-m", label], project_root)
    if r.returncode != 0:
        return False
    return "Saved working directory" in r.stdout


def stash_pop(project_root: Path) -> bool:
    """恢复暂存的本地修改。"""
    r = _git(["stash", "pop"], project_root)
    return r.returncode == 0


def pull(project_root: Path) -> bool:
    """git pull --ff-only origin main。返回是否成功。"""
    r = _git(["pull", "--ff-only", "origin", "main"], project_root, timeout=60)
    return r.returncode == 0


def get_current_commit(project_root: Path) -> str:
    """获取当前 HEAD commit hash。"""
    r = _git(["rev-parse", "HEAD"], project_root)
    return r.stdout.strip() if r.returncode == 0 else ""


def rollback(project_root: Path, target: str = "HEAD@{1}") -> bool:
    """回滚到指定引用（默认上一个 HEAD）。"""
    r = _git(["reset", "--hard", target], project_root)
    return r.returncode == 0


def _is_excluded(path: str) -> bool:
    """判断路径是否属于更新排除项。"""
    for pattern in EXCLUDED_PATTERNS:
        if pattern.endswith("/"):
            if path.startswith(pattern) or f"/{pattern}" in path:
                return True
        elif pattern.startswith("*"):
            if path.endswith(pattern[1:]):
                return True
        elif path == pattern or path.endswith(f"/{pattern}"):
            return True
    return False
