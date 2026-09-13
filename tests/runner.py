"""单一测试调度入口：解析参数、选套件、启动同一解释器的 pytest。"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

from tests.suites import SUITES, suite_files
from tests.support.paths import PROJECT_ROOT


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Kemo 测试入口；不带参数运行全部源码测试。",
        epilog="pytest 参数可直接追加，例如 --suite protocol -q -k tool；同一个 --suite 可以重复使用。",
    )
    result.add_argument("--suite", action="append", choices=tuple(SUITES), help="选择套件，默认 all")
    result.add_argument("--list", action="store_true", help="列出套件，不加载测试或厂商")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    options, pytest_args = parser().parse_known_args(argv)
    if options.list:
        for name, suite in SUITES.items():
            print(f"{name:16} {suite.description}")
        return 0
    names = options.suite or ["all"]
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if importlib.util.find_spec("pytest") is None:
        print("当前 Python 未安装 pytest，请在项目使用的 Python 环境中安装 requirements.txt。", file=sys.stderr)
        return 2
    try:
        files = suite_files(names)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    # 只有显式选择专用套件才允许真实进程替换；不继承意外残留的开关。
    environment["KEMO_RUN_RESTART_E2E"] = "1" if "restart-e2e" in names else "0"
    command = [sys.executable, "-m", "pytest", *(str(path) for path in files), *pytest_args]
    print(f"[KEMO] 测试套件：{', '.join(names)}；{len(files)} 个文件", flush=True)
    try:
        # 直接继承输出，不对 Windows 控制台输出进行 GBK/UTF-8 二次解码。
        return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, check=False).returncode
    except KeyboardInterrupt:
        return 130
    except OSError:
        print("无法启动测试进程，请检查当前 Python 解释器。", file=sys.stderr)
        return 2
