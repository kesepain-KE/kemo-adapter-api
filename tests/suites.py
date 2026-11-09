"""测试套件目录；只声明路径，不导入或执行任何测试模块。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.support.paths import PROJECT_ROOT


@dataclass(frozen=True)
class Suite:
    description: str
    paths: tuple[str, ...]


DEFAULT_SUITES = {
    "gateway": Suite("声明式目录、路由、密钥、扩展与真实适配器", ("tests/gateway",)),
    "kemo-contract": Suite("Kemo 2.0 固定协议制品", ("tests/contracts/kemo_v2",)),
    "api": Suite("公开 API", ("tests/api",)),
    "web": Suite("管理端 API", ("tests/web",)),
    "runtime": Suite("启动、热配置、重启控制", ("tests/runtime",)),
    "protocol": Suite("协议、工具、多模态和传输", ("tests/protocol",)),
    "storage": Suite("持久化与统计", ("tests/storage",)),
    "maintenance": Suite("安装、更新、版本", ("tests/maintenance",)),
    "architecture": Suite("测试架构与入口", ("tests/architecture",)),
}
SUITES = {
    "all": Suite("全部源码测试（默认，不加载本地厂商、不替换真实进程）",
                 tuple(path for suite in DEFAULT_SUITES.values() for path in suite.paths)),
    **DEFAULT_SUITES,
    "agent-control": Suite("智能体操作、目录/密钥、模型、鉴权与启动配置", (
        "tests/gateway/test_agent_control.py",
        "tests/gateway/test_console_operations.py",
        "tests/gateway/test_provider_console.py",
        "tests/gateway/test_model_actions.py",
        "tests/gateway/test_model_deletion.py",
        "tests/gateway/test_diagnostics.py",
        "tests/web/test_admin_api.py",
        "tests/web/test_startup_config.py",
        "tests/runtime/test_runtime_controls.py",
    )),
    "version": Suite("版本契约", ("tests/maintenance/test_version_contract.py",)),
    "update-unit": Suite("更新单元安全测试", ("tests/maintenance/test_update.py",)),
    "update": Suite("更新与临时 Git 仓库集成", ("tests/maintenance/test_update.py", "tests/maintenance/test_update_git_integration.py")),
    "update-git": Suite("更新临时 Git 仓库集成", ("tests/maintenance/test_update_git_integration.py",)),
    "restart-e2e": Suite("真实进程替换：仅在临时目录，需已构建前端", ("tests/integration",)),
}


def suite_files(names: list[str], root: Path = PROJECT_ROOT) -> list[Path]:
    """展开为文件后去重，all + version 等组合不会重复运行同一文件。"""
    files: set[Path] = set()
    for name in names:
        for relative in SUITES[name].paths:
            target = root / relative
            if not target.exists():
                raise ValueError(f"测试路径不存在：{relative}")
            if target.is_file():
                files.add(target)
            else:
                files.update(path for path in target.rglob("test_*.py") if "__pycache__" not in path.parts)
    if not files:
        raise ValueError("所选套件没有测试文件")
    return sorted(files)
