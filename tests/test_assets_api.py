from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from tests.test_live_config import project


PNG = b"\x89PNG\r\n\x1a\n" + b"kemo-test-png"


def _settings() -> Settings:
    return Settings(
        api_keys={
            "asset-token": PrincipalConfig(
                "tenant-a",
                "agent-a",
                frozenset({"asset:read", "asset:write", "model:invoke"}),
            ),
            "other-token": PrincipalConfig(
                "tenant-a",
                "agent-b",
                frozenset({"asset:read", "asset:write"}),
            ),
            "model-only": PrincipalConfig(
                "tenant-a",
                "agent-a",
                frozenset({"model:invoke"}),
            ),
        }
    )


def _headers(token: str = "asset-token", *, key: str = "upload-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Kemo-Protocol-Version": "1.0",
        "Idempotency-Key": key,
        "X-Content-SHA256": hashlib.sha256(PNG).hexdigest(),
    }


def _upload(client: TestClient, *, data: bytes = PNG, key: str = "upload-1"):
    headers = _headers(key=key)
    headers["X-Content-SHA256"] = hashlib.sha256(data).hexdigest()
    return client.post(
        "/assets",
        headers=headers,
        data={
            "metadata": json.dumps(
                {
                    "user": "alice",
                    "session_id": "session-1",
                    "purpose": "input",
                    "capability": "vision",
                }
            )
        },
        files={"file": ("pixel.png", data, "image/png")},
    )


def test_asset_upload_query_range_idempotency_and_delete(tmp_path: Path) -> None:
    root = project(tmp_path)
    app = create_app(
        _settings(),
        live_config_root=root,
        statistics_root=root / "storage",
        asset_root=root / "storage" / "assets",
        discover_providers=False,
    )

    with TestClient(app) as client:
        created = _upload(client)
        assert created.status_code == 201
        descriptor = created.json()
        assert descriptor["object"] == "kemo.asset"
        assert descriptor["status"] == "ready"
        assert descriptor["purpose"] == "input"
        assert descriptor["mime_type"] == "image/png"
        assert descriptor["size"] == len(PNG)
        assert descriptor["checksum_sha256"] == hashlib.sha256(PNG).hexdigest()

        replay = _upload(client)
        assert replay.status_code == 201
        assert replay.json()["id"] == descriptor["id"]

        conflict = _upload(client, data=PNG + b"different")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        fetched = client.get(
            f"/assets/{descriptor['id']}", headers=_headers()
        )
        assert fetched.status_code == 200
        assert fetched.json()["id"] == descriptor["id"]

        partial = client.get(
            f"/assets/{descriptor['id']}/content",
            headers={**_headers(), "Range": "bytes=0-7"},
        )
        assert partial.status_code == 206
        assert partial.content == PNG[:8]
        assert partial.headers["content-range"] == f"bytes 0-7/{len(PNG)}"
        assert partial.headers["accept-ranges"] == "bytes"

        hidden = client.get(
            f"/assets/{descriptor['id']}", headers=_headers("other-token")
        )
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "ASSET_NOT_FOUND"

        deleted = client.delete(
            f"/assets/{descriptor['id']}", headers=_headers()
        )
        assert deleted.status_code == 204
        expired = client.get(
            f"/assets/{descriptor['id']}", headers=_headers()
        )
        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "ASSET_DELETED"

        revived = _upload(client)
        assert revived.status_code == 201
        assert revived.json()["id"] == descriptor["id"]
        assert revived.json()["status"] == "ready"


def test_asset_rejects_scope_and_media_signature_mismatch(tmp_path: Path) -> None:
    root = project(tmp_path)
    app = create_app(
        _settings(),
        live_config_root=root,
        statistics_root=root / "storage",
        asset_root=root / "storage" / "assets",
        discover_providers=False,
    )

    with TestClient(app) as client:
        denied = client.post(
            "/assets",
            headers=_headers("model-only"),
            data={"metadata": "{}"},
            files={"file": ("pixel.png", PNG, "image/png")},
        )
        assert denied.status_code == 403

        headers = _headers(key="bad-mime")
        mismatch = client.post(
            "/assets",
            headers=headers,
            data={"metadata": "{}"},
            files={"file": ("audio.wav", PNG, "audio/wav")},
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == "INVALID_MEDIA"


def test_asset_infers_detected_mime_from_generic_upload_and_encodes_filename(
    tmp_path: Path,
) -> None:
    root = project(tmp_path)
    app = create_app(
        _settings(),
        live_config_root=root,
        statistics_root=root / "storage",
        asset_root=root / "storage" / "assets",
        discover_providers=False,
    )
    body = b"%PDF-1.7\n% test\n"
    headers = _headers(key="generic-pdf")
    headers["X-Content-SHA256"] = hashlib.sha256(body).hexdigest()
    with TestClient(app) as client:
        created = client.post(
            "/assets",
            headers=headers,
            data={"metadata": "{}"},
            files={"file": ("资料.pdf", body, "application/octet-stream")},
        )
        assert created.status_code == 201, created.text
        assert created.json()["mime_type"] == "application/pdf"
        downloaded = client.get(
            f"/assets/{created.json()['id']}/content", headers=_headers()
        )
        assert downloaded.status_code == 200
        disposition = downloaded.headers["content-disposition"]
        assert "filename*=UTF-8''" in disposition
        assert "%E8%B5%84%E6%96%99.pdf" in disposition
