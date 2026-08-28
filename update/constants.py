"""Kemo 网关更新器的稳定路径与安全边界。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

APP_NAME = "kemo-adapter-api"
DEFAULT_BRANCH = "main"
DEFAULT_REPO_URL = "https://github.com/kesepain-KE/kemo-adapter-api.git"
DEFAULT_REPOSITORY_SLUG = "kesepain-KE/kemo-adapter-api"
VERSION_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/kesepain-KE/kemo-adapter-api/"
    "{branch}/version.json"
)
BACKUP_KEEP = 10

# 这些路径属于部署者、运行时或私有 Provider，不允许远端更新覆盖。
PROTECTED_PATTERNS = (
    ".env",
    "providers/",
    "api/keys.json",
    "storage/daily/",
    "storage/assets/",
    "storage/executions/",
    "core/runtime/",
    ".backup/",
    "开发目录/",
    "*.bak",
    "*.bak.*",
    "*.log",
    "*.pid",
    ".update.lock",
    ".update.maintenance",
)
PROTECTED_EXCEPTIONS = frozenset({"providers/__init__.py"})

# 冲突标记只扫描发布源码和文档；运行数据、构建产物和备份不参与。
INTEGRITY_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".backup",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "dist",
        "storage",
        "开发目录",
    }
)
INTEGRITY_SUFFIXES = frozenset(
    {
        ".bat", ".cfg", ".cmd", ".css", ".html", ".ini", ".js",
        ".json", ".jsx", ".md", ".ps1", ".py", ".pyi", ".sh",
        ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
    }
)

PYTHON_COMPILE_TARGETS = (
    "api",
    "core",
    "providers",
    "template",
    "tests",
    "update",
    "web/backend",
)

