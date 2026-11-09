from __future__ import annotations

from hashlib import sha256
from typing import get_args

import pytest
import yaml

from gateway.domain import TokenizerName
from gateway.store import Store, dump
from gateway.token_counting import ROOT, TOKENIZER_CATALOG, count_text, select_tokenizer
from tests.support.gateway import add_upstream, instance, mutate, register, state


@pytest.mark.parametrize(("name", "expected"), [
    ("glm", 7), ("minimax", 7), ("mistral", 12), ("hunyuan", 8), ("ernie", 7),
    ("doubao", 8), ("stepfun", 8), ("yi", 8), ("mimo", 8), ("sensenova", 8),
    ("baichuan", 8), ("internlm", 7), ("cohere", 8), ("gemini", 7), ("grok", 14),
    ("ai21-j2", 12), ("llama", 8), ("claude-legacy", 11), ("kimi", 8),
])
def test_bundled_tokenizer_families_run_offline(name, expected):
    assert select_tokenizer(name, "custom-model") == name
    assert count_text("Hello 世界，模型计量测试！", name) == expected


def test_tokenizer_catalog_resources_and_model_selection():
    assert set(get_args(TokenizerName)) == {"auto", *TOKENIZER_CATALOG}
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    for path, source in sources.items():
        content = (ROOT / path).read_bytes()
        assert len(content) == source["bytes"], path
        assert sha256(content).hexdigest() == source["sha256"], path
    for model, expected in {
        "z-ai/GLM-4.5": "glm", "moonshotai/Kimi-K2": "kimi", "google/gemini-2.5-pro": "gemini",
        "MiniMax-M2": "minimax", "meta-llama/Llama-3.2-1B": "llama", "XiaomiMiMo/MiMo-V2-Flash": "mimo",
        "unknown-proprietary-model": "deepseek",
    }.items():
        assert select_tokenizer("auto", model) == expected
    assert select_tokenizer("openai", "custom-model") == "o200k_base"


@pytest.mark.parametrize(("template", "remote", "expected"), [
    (None, "vendor/model-v1", "local-vendor-model-v1"),
    ("command-code", "deepseek/deepseek-v4.1-flash", "command-code-deepseek-deepseek-v4.1-flash"),
    ("zhipu", "glm-4", "zhipu-z-ai-glm-4"),
])
def test_default_public_names_and_invalid_explicit_names_do_not_call_upstream(
    tmp_path, monkeypatch, template, remote, expected,
):
    calls = []
    with instance(tmp_path, monkeypatch, lambda request: calls.append(request)) as (client, _):
        add_upstream(client)
        if template:
            response = mutate(client, "/upstreams/local", {
                "template_id": template, "name": "User Display Name",
            }, method="PATCH")
            assert response.status_code == 200, response.text
        upstream = state(client)["upstreams"][0]
        response = mutate(client, f"/upstreams/{upstream['id']}/register", {
            "models": [{"upstream_model": remote, "capabilities": {}}],
        })
        assert response.status_code == 200, response.text
        assert state(client)["models"][0]["id"] == expected
        assert state(client)["deployments"][0]["upstream_model"] == remote
        response = mutate(client, f"/upstreams/{upstream['id']}/register", {
            "models": [{"upstream_model": remote, "public_model": "my-custom-alias", "capabilities": {}}],
        })
        assert response.status_code == 200, response.text
        assert {model["id"] for model in state(client)["models"]} == {expected, "my-custom-alias"}
        before = state(client)
        for name in ("bad/name", "bad\\name", "bad name"):
            assert mutate(client, "/models", {"id": name}).status_code == 422
            assert mutate(client, f"/upstreams/{upstream['id']}/register", {
                "models": [{"upstream_model": "remote", "public_model": name, "capabilities": {}}],
            }).status_code == 422
        assert state(client)["revision"] == before["revision"] and calls == []


def test_rename_moves_deployments_and_key_scopes_atomically(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        for upstream in ("first", "second"):
            add_upstream(client, identity=upstream)
            register(client, identity=upstream, model="shared-model")
        keys = []
        for allowed in (None, [], ["shared-model", "other-model"]):
            response = mutate(client, "/keys", {"name": "scope", "allowed_models": allowed},
                              key=True)
            assert response.status_code == 200, response.text
            keys.append(response.json()["id"])
        before = state(client)
        response = mutate(client, "/models/shared-model", {"id": "custom-model"}, method="PATCH")
        assert response.status_code == 200, response.text
        after = state(client)
        assert {model["id"] for model in after["models"]} == {"custom-model"}
        assert {deployment["public_model"] for deployment in after["deployments"]} == {"custom-model"}
        policies = {key["id"]: key["allowed_models"] for key in after["keys"]}
        assert [policies[key] for key in keys] == [None, [], ["custom-model", "other-model"]]
        assert after["key_revision"] != before["key_revision"]
        assert set(app.state.registry.current.routes) == {"custom-model"}
        assert after["attempts"] == before["attempts"]
        assert mutate(client, "/models/custom-model", {"id": "bad/name"}, method="PATCH").status_code == 422
        assert state(client)["revision"] == after["revision"]
        assert state(client)["keys"] == after["keys"]
        assert mutate(client, "/models/custom-model", {"id": "another"}, method="PATCH",
                      expected=before["revision"]).status_code == 409


def test_registered_model_removal_keeps_shared_model_until_last_route(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        for upstream in ("first", "second"):
            add_upstream(client, identity=upstream)
            register(client, identity=upstream)
        deployments = state(client)["deployments"]
        for index, deployment in enumerate(deployments):
            response = mutate(client, f"/deployments/{deployment['id']}?remove_orphan_model=true", method="DELETE")
            assert response.status_code == 200, response.text
            assert len(state(client)["deployments"]) == 1 - index
            assert len(state(client)["models"]) == 1 - index


def test_legacy_name_migration_preserves_scopes_secrets_and_upstream_ids(tmp_path):
    store = Store(tmp_path)
    with store.transaction() as db:
        upstream = {"id": "provider", "name": "My Provider", "adapter_id": "openai-chat",
                    "base_url": "https://example.com/v1", "auth_scheme": "none"}
        db.execute("INSERT INTO upstreams VALUES(?,?)", ("provider", dump(upstream)))
        db.execute("INSERT INTO upstreams VALUES(?,?)", ("provider2", dump({**upstream, "id": "provider2"})))
        for identity in ("vendor/model", "My-Provider-vendor-model"):
            db.execute("INSERT INTO models VALUES(?,?)", (identity, dump({"id": identity})))
        for identity in ("first", "second"):
            db.execute("INSERT INTO deployments VALUES(?,?)", (identity, dump({
                "id": identity, "upstream_id": "provider" if identity == "first" else "provider2",
                "public_model": "vendor/model", "upstream_model": "vendor/model",
            })))
        db.execute("INSERT INTO downstream_keys VALUES(?,?,?,?,?,?)", (
            "key", "prefix", "unchanged-hash", dump({"allowed_models": ["vendor/model"], "enabled": True}),
            "2026-01-01", "unchanged-encrypted",
        ))
    store._db.close()
    migrated = Store(tmp_path)
    catalog = migrated.catalog()
    renamed = next(model.id for model in catalog.models if model.id != "My-Provider-vendor-model")
    assert renamed.startswith("My-Provider-vendor-model-") and "/" not in renamed
    assert {d.public_model for d in catalog.deployments} == {renamed}
    assert {d.upstream_model for d in catalog.deployments} == {"vendor/model"}
    key = migrated._db.execute("SELECT * FROM downstream_keys").fetchone()
    assert yaml.safe_load(key["body"])["allowed_models"] == [renamed]
    assert key["encrypted"] == "unchanged-encrypted" and key["key_hash"] == "unchanged-hash"
    original = catalog.model_dump()
    migrated._db.close()
    reopened = Store(tmp_path)
    assert reopened.catalog().model_dump() == original
    reopened._db.close()
