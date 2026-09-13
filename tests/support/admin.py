"""管理端测试身份与临时项目构造器。"""
from __future__ import annotations
from pathlib import Path
from tests.support.project import project, write_json

ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}


OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


CALLER_HEADERS = {"Authorization": "Bearer caller-token"}


def admin_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_json(
        root / "api" / "keys.json",
        {
            "keys": {
                "admin-token": {
                    "tenant_id": "admin",
                    "subject_id": "console",
                    "scopes": ["admin:web"],
                },
                "owner-token": {
                    "tenant_id": "admin",
                    "subject_id": "owner-console",
                    "scopes": ["owner"],
                },
                "caller-token": {
                    "tenant_id": "tenant",
                    "subject_id": "agent",
                    "scopes": ["model:invoke"],
                },
            }
        },
    )
    return root
