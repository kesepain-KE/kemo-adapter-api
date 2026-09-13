"""只在调用者提供的临时目录构造配置，不读取实际 .env 或密钥。"""
from __future__ import annotations
import json
from pathlib import Path

def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def project(tmp_path: Path) -> Path:
    write_json(tmp_path / "api" / "runtime.json", {"gateway_api": {"enabled": True}})
    write_json(
        tmp_path / "api" / "keys.json",
        {
            "keys": {
                "live-token": {
                    "tenant_id": "tenant-live",
                    "subject_id": "agent-live",
                    "scopes": ["model:invoke"],
                }
            }
        },
    )
    write_json(
        tmp_path / "core" / "live_control.json",
        {
            "highest_priority_system_prompt": "policy-v1",
            "disabled_providers": [],
            "disabled_models": [],
        },
    )
    write_json(tmp_path / "providers" / "fake" / "config.json", {"base_url": "v1"})
    write_json(tmp_path / "providers" / "fake" / "secrets.json", {"api_key": "secret"})
    return tmp_path
