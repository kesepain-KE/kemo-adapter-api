"""Kemo 网关模块化更新系统。

根目录 ``update.py`` 和 ``python -m update`` 都只进入这里的同一套 CLI；
导入包本身不会读取环境变量、加载 Provider 或执行 Git。
"""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """延迟导入 CLI，避免 ``import update`` 触发应用初始化。"""

    from update.cli import main as cli_main

    return cli_main(list(argv) if argv is not None else None)


__all__ = ["main"]
