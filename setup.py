"""Kemo 网关首次部署助手。

默认只检查环境；使用 --init-env 创建本地配置，使用 --install-dependencies 安装依赖。
脚本永远不会覆盖已有 .env。
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent
DEPENDENCIES = (
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "pycryptodome>=3.20.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "python-dotenv>=1.0.0",
    "python-multipart>=0.0.9",
    "pytest>=8.0.0",
)
IMPORT_CHECKS = ("fastapi", "uvicorn", "Crypto", "httpx", "pydantic", "dotenv", "pytest")
REQUIRED_PATHS = (
    "ADD_DIY",
    "api",
    "api.md",
    "api/runtime.json",
    "core",
    "core/live_control.json",
    "providers",
    "tests",
    "web",
    ".env.example",
    "version.json",
    "agent_control.md",
    "start_web.py",
)


def check_environment() -> bool:
    problems: list[str] = []
    if sys.version_info < (3, 11):
        problems.append("Python 版本必须为 3.11 或更高")
    for relative_path in REQUIRED_PATHS:
        if not (PROJECT_ROOT / relative_path).exists():
            problems.append(f"缺少项目路径: {relative_path}")
    missing_modules = [name for name in IMPORT_CHECKS if importlib.util.find_spec(name) is None]
    if missing_modules:
        problems.append(f"缺少 Python 模块: {', '.join(missing_modules)}")
    if problems:
        for problem in problems:
            print(f"[ERROR] {problem}")
        return False
    print("[OK] 首次部署环境检查通过")
    return True


def initialize_env() -> None:
    source = PROJECT_ROOT / ".env.example"
    target = PROJECT_ROOT / ".env"
    if target.exists():
        print("[SKIP] .env 已存在，未覆盖")
        return
    shutil.copyfile(source, target)
    print("[OK] 已创建启动环境变量文件 .env；修改该文件后必须重启")


def install_dependencies() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", *DEPENDENCIES],
        cwd=PROJECT_ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Kemo 网关首次部署")
    parser.add_argument("--check", action="store_true", help="检查 Python、依赖和目录")
    parser.add_argument("--init-env", action="store_true", help="缺失时创建本地 .env")
    parser.add_argument("--install-dependencies", action="store_true", help="安装网关依赖")
    args = parser.parse_args()

    if args.install_dependencies:
        install_dependencies()
    if args.init_env:
        initialize_env()
    # 无参数时也执行无副作用检查。
    return 0 if check_environment() else 1


if __name__ == "__main__":
    raise SystemExit(main())
