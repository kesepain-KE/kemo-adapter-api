"""Kemo 网关一站式部署。

流程：
  1. 环境依赖检查（Python / Git / Node.js），Node.js 缺失时自动安装 LTS
  2. 创建 Python 虚拟环境 .venv
  3. 初始化 .env（从 .env.example 复制，不覆盖已有）
  4. .venv pip install -r requirements.txt
  5. 构建 Web 前端（pnpm install + build）
  6. 输出启动指令

用法：
  python setup.py                    完整部署（步骤 1→6）
  python setup.py --check            仅步骤 1，不做任何修改
  python setup.py --build-frontend   仅步骤 1 + 5（供更新系统调用）
  python setup.py --init-env         仅步骤 3
"""

from __future__ import annotations

import argparse
import hashlib
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

# ── 早期：确保 stdout/stderr 使用 UTF-8 ──────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── 项目路径 ──────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
FRONTEND_ROOT = PROJECT_ROOT / "web" / "frontend"
FRONTEND_RUNTIME_ROOT = FRONTEND_ROOT / ".runtime"

# ── Node.js 自举常量 ──────────────────────────────────────────────
NODE_RELEASE_INDEX_URL = "https://nodejs.org/dist/index.json"
NODE_DOWNLOAD_ROOT = "https://nodejs.org/dist"
PNPM_VERSION = "11.9.0"

# ── 源文件路径（不含构建产物）─────────────────────────────────────
SOURCE_PATHS = (
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
    "web/frontend/package.json",
    ".env.example",
    "version.json",
    "agent_control.md",
    "start_web.py",
    "requirements.txt",
)

# ── 平台工具 ──────────────────────────────────────────────────────

def _venv_python() -> str:
    """返回 .venv 中 python 可执行文件的路径。"""
    if platform.system().strip().lower() == "windows":
        return str(VENV_DIR / "Scripts" / "python.exe")
    return str(VENV_DIR / "bin" / "python")


def _venv_pip() -> list[str]:
    """返回 [venv_python, "-m", "pip"] 列表。"""
    return [_venv_python(), "-m", "pip"]


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 300) -> bool:
    """执行命令；成功返回 True，失败打印错误并返回 False。"""
    try:
        r = subprocess.run(cmd, cwd=cwd or PROJECT_ROOT, check=False, timeout=timeout)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[ERROR] 命令超时: {' '.join(cmd)}", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"[ERROR] 无法执行命令: {exc}", file=sys.stderr)
        return False


# ═══════════════════════════════════════════════════════════════════
# 步骤 1：环境依赖检查 + Node.js 自动安装
# ═══════════════════════════════════════════════════════════════════

def _check_python() -> bool:
    if sys.version_info >= (3, 11):
        print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        return True
    print(f"[ERROR] Python 版本必须 ≥ 3.11，当前 {sys.version_info.major}.{sys.version_info.minor}", file=sys.stderr)
    return False


def _check_git() -> bool:
    git = shutil.which("git")
    if git is not None:
        print(f"[OK] Git: {git}")
        return True
    print("[WARN] Git 未安装——网关可正常运行，但 update.py 更新系统不可用")
    return True  # 非硬性错误


def _check_node() -> tuple[str, str] | None:
    """检查 Node.js + npm；都找到返回 (node, npm)，否则返回 None。"""
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is not None and npm is not None:
        print(f"[OK] Node.js: {node}")
        print(f"[OK] npm: {npm}")
        return (node, npm)
    return None


def _local_node_runtime(*, activate: bool) -> tuple[str, str] | None:
    """发现并按需激活已经安装的项目本地 Node.js 工具链。"""
    is_windows = platform.system().strip().lower() == "windows"
    target = FRONTEND_RUNTIME_ROOT / "node"
    executable_root = target if is_windows else target / "bin"
    node = executable_root / ("node.exe" if is_windows else "node")
    npm = executable_root / ("npm.cmd" if is_windows else "npm")
    if not node.is_file() or not npm.is_file():
        return None
    if activate:
        current_path = os.environ.get("PATH", "")
        path_parts = current_path.split(os.pathsep) if current_path else []
        if str(executable_root) not in path_parts:
            os.environ["PATH"] = os.pathsep.join([str(executable_root), current_path])
    print(f"[OK] Node.js（项目本地）: {node}")
    print(f"[OK] npm（项目本地）: {npm}")
    return str(node), str(npm)


def _download_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def _download_file(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def _node_archive_name(version: str) -> str:
    machine = platform.machine().strip().lower()
    arch_map = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}
    architecture = arch_map.get(machine)
    if architecture is None:
        raise RuntimeError(f"Node.js 自动安装不支持当前 CPU 架构: {machine}")

    system_name = platform.system().strip().lower()
    if system_name == "windows":
        return f"node-{version}-win-{architecture}.zip"
    if system_name == "linux":
        return f"node-{version}-linux-{architecture}.tar.xz"
    raise RuntimeError(f"Node.js 自动安装仅支持 Windows 和 Linux，当前系统: {system_name}")


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
    if normalized.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
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

    roots = [p for p in destination.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("Node.js 安装包目录结构无效")
    return roots[0]


def _install_local_node_runtime() -> tuple[str, str]:
    """下载并安装 Node.js LTS 到 web/frontend/.runtime/node/。返回 (node_path, npm_path)。"""
    version = _latest_node_lts_version()
    archive_name = _node_archive_name(version)
    expected_hash = _expected_sha256(version, archive_name)
    FRONTEND_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"[KEMO] 正在下载 Node.js LTS {version}（项目本地工具链）", flush=True)
    with tempfile.TemporaryDirectory(prefix="node-install-", dir=FRONTEND_RUNTIME_ROOT) as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / archive_name
        _download_file(f"{NODE_DOWNLOAD_ROOT}/{version}/{archive_name}", archive_path)

        actual_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest().lower()
        if actual_hash != expected_hash:
            raise RuntimeError("Node.js 安装包 SHA-256 校验失败")

        extracted = _extract_node_archive(archive_path, temporary_root / "extracted")
        target = FRONTEND_RUNTIME_ROOT / "node"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), str(target))

    is_windows = platform.system().strip().lower() == "windows"
    executable_root = target if is_windows else target / "bin"
    node = executable_root / ("node.exe" if is_windows else "node")
    npm = executable_root / ("npm.cmd" if is_windows else "npm")

    if not node.is_file() or not npm.is_file():
        raise RuntimeError("Node.js 本地工具链安装不完整")

    # 将本地 Node 加入当前进程 PATH，后续 pnpm 命令可用
    os.environ["PATH"] = os.pathsep.join([str(executable_root), os.environ.get("PATH", "")])
    return str(node), str(npm)


def _ensure_node() -> tuple[str, str]:
    """确保 Node.js + npm 可用；优先系统安装，否则自动安装本地工具链。"""
    found = _check_node()
    if found is not None:
        return found
    local = _local_node_runtime(activate=True)
    if local is not None:
        return local
    print("[KEMO] 系统未检测到 Node.js，正在自动安装本地工具链…", flush=True)
    return _install_local_node_runtime()


def check_environment(*, bootstrap_node: bool = True) -> bool:
    """步骤 1：检查环境依赖。返回 True 表示可以继续部署。"""
    print("\n[1/5] 环境依赖检查", flush=True)
    ok = True
    if not _check_python():
        ok = False
    _check_git()  # 非致命
    if bootstrap_node:
        try:
            _ensure_node()
        except (OSError, RuntimeError) as exc:
            print(f"[ERROR] Node.js 环境配置失败: {exc}", file=sys.stderr)
            ok = False
    elif _check_node() is None and _local_node_runtime(activate=False) is None:
        try:
            _node_archive_name("v0.0.0")
            print("[WARN] Node.js 未安装；完整部署时会自动安装项目本地 LTS")
        except RuntimeError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            ok = False

    for relative_path in SOURCE_PATHS:
        if not (PROJECT_ROOT / relative_path).exists():
            print(f"[ERROR] 缺少项目路径: {relative_path}", file=sys.stderr)
            ok = False

    if ok:
        print("[OK] 环境检查通过")
    return ok


# ═══════════════════════════════════════════════════════════════════
# 步骤 2：创建 Python 虚拟环境
# ═══════════════════════════════════════════════════════════════════

def setup_venv() -> bool:
    """创建 .venv，已存在则跳过。"""
    print("\n[2/5] Python 虚拟环境", flush=True)
    if VENV_DIR.is_dir():
        print("[SKIP] .venv 已存在")
        return True

    print("[KEMO] 正在创建 .venv …", flush=True)
    if not _run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        timeout=120,
    ):
        print("[ERROR] 虚拟环境创建失败", file=sys.stderr)
        return False
    print("[OK] .venv 创建完成")
    return True


# ═══════════════════════════════════════════════════════════════════
# 步骤 3：初始化 .env
# ═══════════════════════════════════════════════════════════════════

def init_env() -> None:
    """从 .env.example 复制，永不覆盖已有 .env。"""
    print("\n[3/5] 环境变量配置", flush=True)
    source = PROJECT_ROOT / ".env.example"
    target = PROJECT_ROOT / ".env"
    if not source.is_file():
        print("[SKIP] .env.example 不存在")
        return
    if target.exists():
        print("[SKIP] .env 已存在，未覆盖")
        return
    shutil.copyfile(source, target)
    print("[OK] 已从 .env.example 创建 .env；请按需修改后重启网关")


# ═══════════════════════════════════════════════════════════════════
# 步骤 4：安装 Python 依赖
# ═══════════════════════════════════════════════════════════════════

def install_dependencies() -> bool:
    """在 .venv 中执行 pip install -r requirements.txt。"""
    print("\n[4/5] Python 依赖安装", flush=True)

    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.is_file():
        print("[SKIP] requirements.txt 不存在")
        return True

    # 先确保 venv 里 pip 本身是最新的
    _run(_venv_pip() + ["install", "--upgrade", "pip"], timeout=60)

    print("[KEMO] 正在安装依赖…", flush=True)
    ok = _run(
        _venv_pip() + ["install", "-r", str(req_file)],
        timeout=300,
    )
    if ok:
        print("[OK] 依赖安装完成")
    else:
        print("[ERROR] 依赖安装失败", file=sys.stderr)
    return ok


# ═══════════════════════════════════════════════════════════════════
# 步骤 5：构建前端
# ═══════════════════════════════════════════════════════════════════

def _pnpm_command() -> list[str]:
    """返回 pnpm 可执行命令。优先用系统安装的 pnpm，否则通过 npm exec 获取。"""
    pnpm = shutil.which("pnpm")
    if pnpm is not None:
        return [pnpm]

    _, npm = _ensure_node()
    return [npm, "exec", "--yes", f"--package=pnpm@{PNPM_VERSION}", "--", "pnpm"]


def build_frontend() -> bool:
    """安装前端依赖并执行生产构建。"""
    print("\n[5/5] Web 前端构建", flush=True)

    pkg_json = FRONTEND_ROOT / "package.json"
    if not pkg_json.is_file():
        print("[SKIP] web/frontend/package.json 不存在，跳过前端构建")
        return True

    try:
        # 确保 Node.js 可用
        _ensure_node()
        pm = _pnpm_command()
    except (OSError, RuntimeError) as exc:
        print(f"[ERROR] 前端工具链不可用: {exc}", file=sys.stderr)
        return False
    print("[KEMO] 安装前端依赖…", flush=True)
    if not _run(pm + ["install", "--frozen-lockfile"], cwd=FRONTEND_ROOT, timeout=300):
        print("[ERROR] 前端依赖安装失败", file=sys.stderr)
        return False

    print("[KEMO] 构建生产包…", flush=True)
    if not _run(pm + ["run", "build"], cwd=FRONTEND_ROOT, timeout=300):
        print("[ERROR] 前端构建失败", file=sys.stderr)
        return False

    dist_index = FRONTEND_ROOT / "dist" / "index.html"
    if not dist_index.is_file():
        print("[ERROR] 构建完成但缺少 dist/index.html", file=sys.stderr)
        return False

    print("[OK] 前端构建完成")
    return True


# ═══════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════

def _print_done() -> None:
    """部署完成提示。"""
    venv_py = _venv_python()
    print()
    print("=" * 60)
    print("[KEMO] 部署完毕！", flush=True)
    print()
    print("  启动网关：")
    print(f"    {venv_py} start_web.py")
    print()
    print("  检查更新：")
    print(f"    {venv_py} update.py --check")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kemo 网关一站式部署")
    parser.add_argument("--check", action="store_true", help="仅检查环境，不做任何修改")
    parser.add_argument("--build-frontend", action="store_true", help="仅执行环境检查 + 前端构建")
    parser.add_argument("--init-env", action="store_true", help="仅创建 .env")
    args = parser.parse_args()

    # --check 不能与任何部署操作并存
    if args.check:
        if args.build_frontend or args.init_env:
            parser.error("--check 不能与 --build-frontend / --init-env 同时使用")
        ok = check_environment(bootstrap_node=False)
        return 0 if ok else 1

    # 单步模式
    if args.init_env and not args.build_frontend:
        init_env()
        return 0

    if args.build_frontend:
        # 更新系统入口：只做环境检查 + 前端构建
        if not check_environment():
            return 1
        if not build_frontend():
            return 2
        print("[OK] 前端构建完成")
        return 0

    # ── 无参数 = 完整部署 ──────────────────────────────────────
    print("[KEMO] 开始完整部署", flush=True)

    try:
        if not check_environment():
            return 1
        if not setup_venv():
            return 2
        init_env()
        if not install_dependencies():
            return 2
        if not build_frontend():
            return 2
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] 部署失败: {exc}", file=sys.stderr)
        return 2

    _print_done()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
