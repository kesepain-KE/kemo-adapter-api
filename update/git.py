"""Git 操作：远程检测、多镜像源、暂存、拉取、回滚。"""

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


PROTECTED_PATTERNS = (
    ".env",
    "providers/",
    "api/keys.json",
    "storage/daily/",
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


def _git(args: list[str], project_root: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=project_root,
        capture_output=True, text=True, timeout=timeout,
    )


def _resolve_remote_url(project_root: Path) -> str | None:
    """获取 origin 的远程仓库 URL（HTTPS 格式）。"""
    r = _git(["remote", "get-url", "origin"], project_root)
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    # SSH 格式转为 HTTPS（方便镜像源包装）
    if url.startswith("git@"):
        url = url.replace(":", "/").replace("git@", "https://")
    if url.endswith(".git"):
        url = url[:-4]
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
    r = _git(["rev-list", "--count", "HEAD..FETCH_HEAD"], project_root)
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


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
    """暂存本地未提交修改。返回 True 表示有修改被暂存。"""
    r = _git(["stash", "push", "-m", label], project_root)
    return r.returncode == 0 and "Saved working directory" in r.stdout


def stash_pop(project_root: Path) -> bool:
    r = _git(["stash", "pop"], project_root)
    return r.returncode == 0


def pull(project_root: Path, remote_url: str | None = None) -> bool:
    """git pull --ff-only，支持通过镜像 URL 拉取。"""
    if remote_url:
        r = _git(["pull", "--ff-only", remote_url, "main"], project_root, timeout=60)
    else:
        # 先尝试直连 origin
        r = _git(["pull", "--ff-only", "origin", "main"], project_root, timeout=60)
        if r.returncode != 0 and not os.environ.get("GIT_MIRROR"):
            # 直连失败且没有镜像配置，快速重试——用镜像包装后的 URL
            origin_url = _resolve_remote_url(project_root)
            if origin_url:
                for mirror_prefix in _MIRROR_CHAINS:
                    if not mirror_prefix:
                        continue
                    url = _mirror_url(origin_url, mirror_prefix)
                    r = _git(["pull", "--ff-only", url, "main"], project_root, timeout=60)
                    if r.returncode == 0:
                        return True
    return r.returncode == 0


def get_current_commit(project_root: Path) -> str:
    r = _git(["rev-parse", "HEAD"], project_root)
    return r.stdout.strip() if r.returncode == 0 else ""


def get_current_branch(project_root: Path) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def rollback(project_root: Path, target: str = "HEAD@{1}") -> bool:
    r = _git(["reset", "--hard", target], project_root)
    return r.returncode == 0


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
