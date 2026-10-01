from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from core.models import KemoRequest, PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[3]
LOCK_PATH = ROOT / "vendor" / "kemo-protocol.lock.json"


def test_pinned_protocol_artifact_digest_and_version() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    artifact = LOCK_PATH.parent / lock["artifact"]
    assert lock["protocol_version"] == PROTOCOL_VERSION == "2.0"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == lock["sha256"]


def test_artifact_schema_and_fixture_digests_match_lock() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    artifact = LOCK_PATH.parent / lock["artifact"]
    with zipfile.ZipFile(artifact) as archive:
        schema = archive.read("provider/protocol/spec_artifacts/schema.json")
        freeze = json.loads(
            archive.read("provider/protocol/spec_artifacts/freeze.json")
        )
    assert hashlib.sha256(schema).hexdigest() == lock["schema_sha256"]
    assert freeze["fixture_sha256"] == lock["fixture_sha256"]


def test_runtime_model_is_the_artifact_model_and_requires_version() -> None:
    assert KemoRequest.__module__ == "provider.protocol.models"
    schema = KemoRequest.model_json_schema()
    assert "protocol_version" in schema["required"]
    assert schema["properties"]["protocol_version"]["title"] == "Protocol Version"
