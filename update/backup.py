"""更新前冷备份：复制项目到 .backup/ 目录。不依赖 git，纯文件拷贝。"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path


BACKUP_ROOT_NAME = ".backup"
KEEP_BACKUPS = 10

# 备份时排除的目录和文件
_EXCLUDE_DIRS = frozenset({
    BACKUP_ROOT_NAME,
    ".git",
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    "htmlcov",
    ".coverage",
})

_EXCLUDE_PREFIXES = frozenset({
    ".backup",
    ".git",
})

_EXCLUDE_SUFFIXES = frozenset({
    ".pyc",
    ".bak",
    ".log",
    ".pid",
})


def _should_exclude(path: Path, project_root: Path) -> bool:
    """判断路径是否应排除在备份之外。"""
    try:
        rel = path.relative_to(project_root).as_posix()
    except ValueError:
        return True  # 超出项目根

    # 检查每层父目录
    parts = rel.split("/")
    for part in parts:
        if part in _EXCLUDE_DIRS:
            return True
        if part.startswith(tuple(_EXCLUDE_PREFIXES)):
            return True
    if path.suffix in _EXCLUDE_SUFFIXES:
        return True
    if ".bak" in path.suffixes:
        return True
    return False


def _backup_dir(project_root: Path) -> Path:
    return project_root / BACKUP_ROOT_NAME


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def create(project_root: Path) -> tuple[bool, str]:
    """冷备份整个项目（排除无关文件）到 .backup/<timestamp>/。"""
    ts = _timestamp()
    dst = _backup_dir(project_root) / ts

    # 检查磁盘空间（粗略）
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 计算需要复制的文件数
    count = 0
    try:
        for src in project_root.rglob("*"):
            if src.is_file() and not _should_exclude(src, project_root):
                rel = src.relative_to(project_root)
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                count += 1
    except Exception as e:
        return False, f"备份失败: {e}"

    # 清理旧备份
    _cleanup_old(project_root)

    return True, f"冷备份完成: .backup/{ts}/ ({count} 个文件)"


def list_backups(project_root: Path) -> list[str]:
    """按时间倒序列出所有备份。"""
    backup_dir = _backup_dir(project_root)
    if not backup_dir.is_dir():
        return []
    dirs = []
    for d in backup_dir.iterdir():
        if d.is_dir():
            dirs.append(d.name)
    dirs.sort(reverse=True)
    return dirs


def restore(project_root: Path, backup_id: str) -> tuple[bool, str]:
    """从 .backup/<backup_id>/ 恢复。

    backup_id 可以是具体时间戳或 'latest'。
    跳过排除目录和文件。
    """
    if backup_id == "latest":
        backups = list_backups(project_root)
        if not backups:
            return False, "没有可用的备份"
        backup_id = backups[0]

    src = _backup_dir(project_root) / backup_id
    if not src.is_dir():
        return False, f"备份不存在: .backup/{backup_id}/"

    # 恢复：从备份目录复制文件回项目根
    count = 0
    try:
        for src_file in src.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(src)
                target = project_root / rel

                # 检查目标是否在排除范围
                if _should_exclude(target, project_root):
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, target)
                count += 1
    except Exception as e:
        return False, f"恢复失败: {e}"

    return True, f"已从 .backup/{backup_id}/ 恢复 ({count} 个文件)"


def _cleanup_old(project_root: Path) -> None:
    """保留最近 KEEP_BACKUPS 份备份，删除旧的。"""
    backups = list_backups(project_root)
    if len(backups) <= KEEP_BACKUPS:
        return

    backup_dir = _backup_dir(project_root)
    for old in backups[KEEP_BACKUPS:]:
        path = backup_dir / old
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
