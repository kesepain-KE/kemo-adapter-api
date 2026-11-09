"""只在调用者提供的临时目录构造配置，不读取实际 gateway.yaml 或密钥。"""
from __future__ import annotations
from core.yaml_config import dump_yaml
from pathlib import Path

def write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(value), encoding="utf-8")


def project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return tmp_path
