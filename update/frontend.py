"""前端构建检测与执行。"""

from __future__ import annotations

import subprocess
from pathlib import Path


FRONTEND_PATTERNS = (
    "web/frontend/src/",
    "web/frontend/package.json",
    "web/frontend/pnpm-lock.yaml",
    "web/frontend/vite.config.ts",
    "web/frontend/index.html",
    "web/frontend/tsconfig.json",
    "web/frontend/tsconfig.app.json",
    "web/frontend/tsconfig.node.json",
)


def frontend_changed(diff_files: list[str]) -> bool:
    """检查更新是否涉及前端源码。"""
    for f in diff_files:
        for pattern in FRONTEND_PATTERNS:
            if f.startswith(pattern):
                return True
    return False


def build_frontend(project_root: Path) -> tuple[bool, str]:
    """执行 pnpm build。返回 (成功, 输出摘要)。"""
    frontend_dir = project_root / "web" / "frontend"
    if not (frontend_dir / "package.json").is_file():
        return False, "前端目录不存在 package.json"

    # 确认 pnpm 可用
    try:
        subprocess.run(
            ["pnpm", "--version"],
            cwd=frontend_dir,
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "pnpm 不可用，请确保已安装 Node.js 和 pnpm"

    try:
        r = subprocess.run(
            ["pnpm", "build"],
            cwd=frontend_dir,
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            # 提取最后一行构建结果
            lines = [l.strip() for l in r.stdout.split("\n") if l.strip()]
            summary = lines[-1] if lines else "构建完成"
            return True, summary
        else:
            errors = r.stderr[-500:] if r.stderr else "未知错误"
            return False, f"构建失败: {errors}"
    except subprocess.TimeoutExpired:
        return False, "前端构建超时（120秒）"
