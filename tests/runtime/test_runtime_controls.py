from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from tests.support.project import project
from tests.support.gateway import install_fixtures
from tests.support.llm import FakeProvider


def test_downstream_policy_and_runtime_controls_publish_without_restart(tmp_path: Path, monkeypatch) -> None:
    import base64
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    root = project(tmp_path)
    app = create_app(Settings(), live_config_root=root, )
    with TestClient(app) as client:
        state = client.get("/admin/api/console").json()
        created = client.post("/admin/api/keys", json={"expected_revision": state["key_revision"], "value": {"name": "agent"}}).json()
        headers = {"Authorization": "Bearer " + created["token"], "X-Kemo-Protocol-Version": "2.0"}
        assert client.get("/model/models", headers=headers).status_code == 200
        changed = client.patch("/admin/api/settings", json={"expected_revision": state["revision"], "value": {"enabled": False}})
        assert changed.status_code == 200
        assert client.get("/model/models", headers=headers).status_code == 503
        assert client.get("/model/responses/resp_missing", headers=headers).status_code == 404
        assert client.post("/model/responses/resp_missing/cancel", headers=headers).status_code == 404
        state = client.get("/admin/api/console").json()
        assert client.patch("/admin/api/keys/" + created["id"], json={"expected_revision": state["key_revision"], "value": {"enabled": False}}).status_code == 200
        assert client.get("/model/responses/resp_missing", headers=headers).status_code == 401


def test_key_model_whitelist_distinguishes_allow_all_and_deny_all(tmp_path: Path, monkeypatch) -> None:
    import base64
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    app = create_app(Settings(), live_config_root=project(tmp_path))
    install_fixtures(app.state.registry, FakeProvider())
    with TestClient(app) as client:
        state = client.get("/admin/api/console").json()
        created = client.post("/admin/api/keys", json={
            "expected_revision": state["key_revision"], "value": {"name": "agent"},
        }).json()
        headers = {"Authorization": "Bearer " + created["token"], "X-Kemo-Protocol-Version": "2.0"}
        assert client.get("/model/models/fake-model/capabilities", headers=headers).status_code == 200

        for models, expected_status in (([], 403), (["fake-model"], 200), (None, 200)):
            state = client.get("/admin/api/console").json()
            changed = client.patch("/admin/api/keys/" + created["id"], json={
                "expected_revision": state["key_revision"],
                "value": {"allowed_models": models},
            })
            assert changed.status_code == 200
            assert client.get("/model/models/fake-model/capabilities", headers=headers).status_code == expected_status
