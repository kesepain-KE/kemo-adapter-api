"""依赖检测与安装。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def requirements_changed(diff_files: list[str]) -> bool:
    """检查更新是否涉及 requirements.txt。"""
    return "requirements.txt" in diff_files


def install_requirements(project_root: Path) -> bool:
    """pip install -r requirements.txt。"""
    req = project_root / "requirements.txt"
    if not req.is_file():
        return True  # 没有 requirements.txt 不算失败
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            cwd=project_root,
            capture_output=True,
            timeout=120,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
