"""Kemo 网关部署入口。

无参数运行时执行完整部署：安装 Python 依赖、重新构建 Web 管理端、缺失时创建 .env，
最后检查部署结果。显式参数可只执行指定步骤；--check 只检查且不修改环境。
脚本永远不会覆盖已有 .env。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = PROJECT_ROOT / "web" / "frontend"
FRONTEND_RUNTIME_ROOT = FRONTEND_ROOT / ".runtime"
NODE_RELEASE_INDEX_URL = "https://nodejs.org/dist/index.json"
NODE_DOWNLOAD_ROOT = "https://nodejs.org/dist"
PNPM_VERSION = "11.9.0"
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
    "ADD_DIY/README.md",
    "ADD_DIY/verification.md",
    "ADD_DIY/provider-package.md",
    "ADD_DIY/keys-and-secrets.md",
    "ADD_DIY/architecture.md",
    "api",
    "api.md",
    "api/runtime.json",
    "core",
    "core/live_control.json",
    "providers",
    "template/provider/probe.py",
    "template/provider/test_contract.py",
    "tests",
    "web",
    "web/frontend/dist/index.html",
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
    pip_command = [sys.executable, "-m", "pip"]
    probe = subprocess.run(
        [*pip_command, "--version"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        print("[KEMO] 正在初始化 Python 包管理器", flush=True)
        subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            cwd=PROJECT_ROOT,
            check=True,
        )
    print("[KEMO] 正在安装或更新 Python 依赖", flush=True)
    subprocess.run(
        [*pip_command, "install", "--disable-pip-version-check", *DEPENDENCIES],
        cwd=PROJECT_ROOT,
        check=True,
    )


def _download_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def _download_file(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def _node_archive_name(version: str) -> str:
    machine = platform.machine().strip().lower()
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if architecture is None:
        raise RuntimeError(f"Node.js 自动安装不支持当前 CPU 架构: {machine}")

    system = platform.system().strip().lower()
    if system == "windows":
        return f"node-{version}-win-{architecture}.zip"
    if system == "linux":
        return f"node-{version}-linux-{architecture}.tar.xz"
    raise RuntimeError(f"Node.js 自动安装仅支持 Windows 和 Linux，当前系统: {system}")


def _latest_node_lts_version() -> str:
    releases = json.loads(_download_text(NODE_RELEASE_INDEX_URL))
    if not isinstance(releases, list):
        raise RuntimeError("Node.js 发布索引格式无效")
    for release in releases:
        if isinstance(release, dict) and release.get("lts") and release.get("version"):
            return str(release["version"])
    raise RuntimeError("Node.js 发布索引中没有可用的 LTS 版本")


def _expected_sha256(version: str, archive_name: str) -> str:
    checksums = _download_text(f"{NODE_DOWNLOAD_ROOT}/{version}/SHASUMS256.txt")
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == archive_name:
            return parts[0].lower()
    raise RuntimeError(f"Node.js 官方校验清单缺少 {archive_name}")


def _validate_archive_path(name: str) -> None:
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if (
        normalized.startswith("/")
        or ".." in parts
        or (parts and ":" in parts[0])
    ):
        raise RuntimeError("Node.js 安装包包含不安全路径")


def _extract_node_archive(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                _validate_archive_path(item.filename)
            archive.extractall(destination)
    else:
        with tarfile.open(archive_path, mode="r:xz") as archive:
            for item in archive.getmembers():
                _validate_archive_path(item.name)
                if item.isdev():
                    raise RuntimeError("Node.js 安装包包含不支持的设备文件")
                if item.issym():
                    link_target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(item.name), item.linkname)
                    )
                    _validate_archive_path(link_target)
                elif item.islnk():
                    _validate_archive_path(posixpath.normpath(item.linkname))
            archive.extractall(destination)

    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("Node.js 安装包目录结构无效")
    return roots[0]


def _install_local_node_runtime() -> tuple[str, str]:
    version = _latest_node_lts_version()
    archive_name = _node_archive_name(version)
    expected_hash = _expected_sha256(version, archive_name)
    FRONTEND_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

    print(
        f"[KEMO] 正在下载并安装 Node.js LTS {version}（项目本地工具链）",
        flush=True,
    )
    with tempfile.TemporaryDirectory(
        prefix="node-install-", dir=FRONTEND_RUNTIME_ROOT
    ) as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / archive_name
        _download_file(
            f"{NODE_DOWNLOAD_ROOT}/{version}/{archive_name}", archive_path
        )
        actual_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest().lower()
        if actual_hash != expected_hash:
            raise RuntimeError("Node.js 安装包 SHA-256 校验失败")
        extracted = _extract_node_archive(archive_path, temporary_root / "extracted")
        target = FRONTEND_RUNTIME_ROOT / "node"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), str(target))

    if platform.system().strip().lower() == "windows":
        executable_root = target
        node = target / "node.exe"
        npm = target / "npm.cmd"
    else:
        executable_root = target / "bin"
        node = executable_root / "node"
        npm = executable_root / "npm"
    if not node.is_file() or not npm.is_file():
        raise RuntimeError("Node.js 本地工具链安装不完整")
    os.environ["PATH"] = os.pathsep.join(
        [str(executable_root), os.environ.get("PATH", "")]
    )
    return str(node), str(npm)


def _ensure_node_runtime() -> tuple[str, str]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is not None and npm is not None:
        return node, npm
    return _install_local_node_runtime()


def _frontend_package_command() -> list[str]:
    pnpm = shutil.which("pnpm")
    if pnpm is not None:
        return [pnpm]

    _, npm = _ensure_node_runtime()
    print(
        f"[KEMO] pnpm 未安装，将通过 npm 自动使用 pnpm@{PNPM_VERSION}",
        flush=True,
    )
    return [
        npm,
        "exec",
        "--yes",
        f"--package=pnpm@{PNPM_VERSION}",
        "--",
        "pnpm",
    ]


def build_frontend() -> None:
    package_manager = _frontend_package_command()
    print("[KEMO] 正在安装锁定的前端依赖", flush=True)
    subprocess.run(
        [*package_manager, "install", "--frozen-lockfile"],
        cwd=FRONTEND_ROOT,
        check=True,
    )
    print("[KEMO] 正在重新构建 Web 管理端", flush=True)
    subprocess.run(
        [*package_manager, "run", "build"],
        cwd=FRONTEND_ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Kemo 网关一站式部署")
    parser.add_argument("--check", action="store_true", help="仅检查 Python、依赖和目录")
    parser.add_argument("--init-env", action="store_true", help="缺失时创建本地 .env")
    parser.add_argument("--install-dependencies", action="store_true", help="安装网关依赖")
    parser.add_argument("--build-frontend", action="store_true", help="安装并重新构建 Web 管理端")
    args = parser.parse_args()

    requested_action = any(
        (args.init_env, args.install_dependencies, args.build_frontend)
    )
    if args.check and requested_action:
        parser.error("--check 不能与部署步骤参数同时使用")
    default_deployment = not args.check and not requested_action
    install = default_deployment or args.install_dependencies
    build = default_deployment or args.build_frontend
    init_env = default_deployment or args.init_env

    try:
        if default_deployment:
            print(
                "[KEMO] 开始完整部署：安装依赖、重新构建网页并初始化环境配置",
                flush=True,
            )
        if install:
            install_dependencies()
        if build:
            build_frontend()
        if init_env:
            initialize_env()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] 首次部署步骤失败: {exc}", file=sys.stderr)
        return 2
    if not check_environment():
        return 1
    if not args.check:
        print("[OK] Kemo 网关部署完成；运行 python start_web.py 启动网关")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
