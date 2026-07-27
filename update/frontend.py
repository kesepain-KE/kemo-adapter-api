"""前端构建检测与执行。"""

from __future__ import annotations

import subprocess
import sys
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
    """复用 setup.py 的跨平台工具链安装并重建前端。"""
    frontend_dir = project_root / "web" / "frontend"
    if not (frontend_dir / "package.json").is_file():
        return False, "前端目录不存在 package.json"

    setup_script = project_root / "setup.py"
    if not setup_script.is_file():
        return False, "项目根目录不存在 setup.py，无法准备前端工具链"

    try:
        r = subprocess.run(
            [sys.executable, str(setup_script), "--build-frontend"],
            cwd=project_root,
            check=False,
            timeout=900,
        )
        if r.returncode != 0:
            return False, f"一站式前端部署失败（exit code {r.returncode}）"
        output = frontend_dir / "dist" / "index.html"
        if not output.is_file():
            return False, "前端部署返回成功，但缺少 dist/index.html"
        return True, "已通过 setup.py 自动准备工具链并完成生产构建"
    except subprocess.TimeoutExpired:
        return False, "前端工具链准备或构建超时（900秒）"
    except OSError as exc:
        return False, f"无法启动一站式前端部署: {exc}"
