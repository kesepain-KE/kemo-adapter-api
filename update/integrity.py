"""更新前后完整性门禁：Git、冲突标记、编译、前端与启动预检。"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from update import git
from update.constants import (
    INTEGRITY_EXCLUDED_DIRS,
    INTEGRITY_SUFFIXES,
    PYTHON_COMPILE_TARGETS,
)


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool
    message: str
    files: tuple[str, ...] = ()


def _is_scannable(path: Path, project_root: Path) -> bool:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return False
    if any(part in INTEGRITY_EXCLUDED_DIRS for part in relative.parts[:-1]):
        return False
    return path.suffix.lower() in INTEGRITY_SUFFIXES


def find_conflict_markers(project_root: Path) -> list[str]:
    """返回仍包含真实 Git 冲突标记的发布文件。"""

    affected: list[str] = []
    for path in project_root.rglob("*"):
        if not path.is_file() or not _is_scannable(path, project_root):
            continue
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(
            line == "======="
            or line == "<<<<<<<"
            or line.startswith("<<<<<<< ")
            or line == ">>>>>>>"
            or line.startswith(">>>>>>> ")
            for line in lines
        ):
            affected.append(path.relative_to(project_root).as_posix())
    return sorted(affected)


def check_source_state(project_root: Path) -> IntegrityResult:
    """检查会让普通更新或启动处于不确定状态的问题。"""

    unmerged = git.get_unmerged_files(project_root)
    if unmerged:
        return IntegrityResult(False, "Git 存在未解决合并冲突", tuple(unmerged))
    operation = git.get_operation_state(project_root)
    if operation:
        return IntegrityResult(False, f"Git 正处于 {operation} 状态")
    markers = find_conflict_markers(project_root)
    if markers:
        return IntegrityResult(False, "源码中存在 Git 冲突标记", tuple(markers))
    return IntegrityResult(True, "源码状态正常")


def compile_python(project_root: Path, timeout: int = 180) -> IntegrityResult:
    targets = [
        str(project_root / name)
        for name in PYTHON_COMPILE_TARGETS
        if (project_root / name).exists()
    ]
    targets.extend(str(path) for path in project_root.glob("*.py") if path.is_file())
    if not targets:
        return IntegrityResult(False, "没有找到可编译的 Python 源码")
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", *targets],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return IntegrityResult(False, f"Python 编译检查无法完成: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return IntegrityResult(False, "Python 编译检查失败", tuple(detail[-8:]))
    return IntegrityResult(True, "Python 编译检查通过")


def run_startup_preflight(
    project_root: Path,
    timeout: int = 90,
) -> IntegrityResult:
    start_script = project_root / "start_web.py"
    if not start_script.is_file():
        return IntegrityResult(False, "缺少 start_web.py")
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, str(start_script), "--preflight"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return IntegrityResult(False, f"启动预检无法完成: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return IntegrityResult(
            False,
            f"start_web.py --preflight 失败（退出码 {result.returncode}）",
            tuple(detail[-8:]),
        )
    return IntegrityResult(True, "网关启动预检通过")


def validate_installed_source(
    project_root: Path,
    *,
    expected_commit: str,
) -> IntegrityResult:
    """所有更新完成后、宣布成功前必须通过的硬门槛。"""

    state = check_source_state(project_root)
    if not state.ok:
        return state
    current_commit = git.get_current_commit(project_root)
    if not current_commit or current_commit != expected_commit:
        return IntegrityResult(False, "更新后的 HEAD 与锁定目标提交不一致")
    frontend_index = project_root / "web" / "frontend" / "dist" / "index.html"
    if not frontend_index.is_file():
        return IntegrityResult(False, "前端产物缺少 web/frontend/dist/index.html")
    compiled = compile_python(project_root)
    if not compiled.ok:
        return compiled
    return run_startup_preflight(project_root)

