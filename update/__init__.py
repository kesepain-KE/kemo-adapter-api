"""Kemo 网关模块化更新系统。"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """延迟导入 CLI，避免 ``import update`` 触发应用初始化。"""

    from update.cli import main as cli_main

    return cli_main(argv)
