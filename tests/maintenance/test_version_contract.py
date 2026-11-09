"""发布版本契约：防止版本文件、前端包和文档徽章漂移。"""

from __future__ import annotations

import json
from pathlib import Path
import re

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from tests.support.paths import PROJECT_ROOT
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
    assert version == "2.0.0"
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


def test_protocol_contract_is_kemo_20_after_core_models_upgrade() -> None:
    data = _version_data()
    assert data["protocol_version"] == "2.0"
    config = (PROJECT_ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert 'protocol_version: str = "2.0"' in config


def test_running_app_and_console_report_the_release_version(tmp_path) -> None:
    data = _version_data()
    (tmp_path / "version.json").write_text(json.dumps(data), encoding="utf-8")
    app = create_app(Settings(), live_config_root=tmp_path)
    with TestClient(app) as client:
        response = client.get("/admin/api/console")
        assert response.status_code == 200
        assert response.json()["version"] == data
        assert app.version == data["version"]
        assert response.json()["protocol_version"] == data["protocol_version"]
