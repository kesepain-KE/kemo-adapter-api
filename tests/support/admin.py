"""管理端测试身份与临时项目构造器。"""
from __future__ import annotations
from pathlib import Path
from tests.support.project import project
from core.config import PrincipalConfig, Settings

ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}


OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


CALLER_HEADERS = {"Authorization": "Bearer caller-token"}


def admin_settings(**values) -> Settings:
    return Settings(api_keys={
        "admin-token": PrincipalConfig("admin", "console", frozenset({"admin:web"})),
        "owner-token": PrincipalConfig("admin", "owner-console", frozenset({"owner"})),
        "caller-token": PrincipalConfig("tenant", "agent", frozenset({"model:invoke"})),
    }, **values)


def admin_project(tmp_path: Path) -> Path:
    return project(tmp_path)
