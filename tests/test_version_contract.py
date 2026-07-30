"""发布版本契约：防止版本文件、前端包和文档徽章漂移。"""

from __future__ import annotations

import json
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _version_data() -> dict[str, str]:
    value = json.loads((PROJECT_ROOT / "version.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_release_version_is_valid_and_shared_with_frontend() -> None:
    data = _version_data()
    version = data.get("version")
    protocol = data.get("protocol_version")
    assert isinstance(version, str) and SEMVER.fullmatch(version)
    assert isinstance(protocol, str) and re.fullmatch(r"\d+\.\d+", protocol)

    package = json.loads(
        (PROJECT_ROOT / "web" / "frontend" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    assert package["version"] == version


def test_readme_release_badges_match_version_json() -> None:
    version = _version_data()["version"]
    for name in ("README.md", "README.en.md"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert f"gateway-{version}-blue" in text
        assert f"Gateway version {version}" in text


def test_protocol_contract_remains_kemo_10_until_core_models_upgrade() -> None:
    data = _version_data()
    assert data["protocol_version"] == "1.0"
    config = (PROJECT_ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert 'protocol_version: str = "1.0"' in config
