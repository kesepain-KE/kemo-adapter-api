#!/usr/bin/env python3
"""Kemo 网关更新器兼容入口。

所有更新、检查、预览、备份恢复和错误处理都位于 ``update/`` 包内；本文件
只保留 ``python update.py`` 这一条长期兼容的启动路径。
"""

from __future__ import annotations

from update.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
