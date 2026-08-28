"""支持 ``python -m update``，与根目录入口共用同一个 CLI。"""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
