from __future__ import annotations

from tests.support.gateway import extension_zip, install_extension, instance, mutate, state


def test_sources_are_explicit_and_user_packages_cannot_shadow_builtins(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        before = state(client)
        assert all(item["source"] == "builtin" for item in before["adapters"] + before["templates"])
        for kind, identity in (("protocol_adapters", "openai-chat"), ("provider_templates", "openai")):
            package = extension_zip(kind, identity=identity, code="",
                                    template={"adapter_id": "openai-chat", "name": "Local"})
            assert install_extension(client, kind, package).status_code == 422
            assert not (tmp_path / "extensions" / kind / identity).exists()
        package = extension_zip("provider_templates", identity="my-provider", template={
            "adapter_id": "openai-chat", "name": "My provider", "base_url": "https://example.invalid",
        })
        assert install_extension(client, "provider_templates", package).status_code == 200
        template = next(item for item in state(client)["templates"] if item["id"] == "my-provider")
        assert template["source"] == "user" and template["builtin"] is False
        assert mutate(client, "/extensions/provider_templates/my-provider", {"enabled": True}, method="PATCH").status_code == 200
        before = (tmp_path / "extensions/provider_templates/my-provider/template.yaml").read_bytes()
        response = client.post("/admin/api/extensions/provider_templates/install",
                               data={"expected_revision": state(client)["revision"], "replace": "true"},
                               files={"file": ("provider.zip", package, "application/zip")})
        assert response.status_code == 422
        assert (tmp_path / "extensions/provider_templates/my-provider/template.yaml").read_bytes() == before
        assert mutate(client, "/extensions/provider_templates/my-provider", {"enabled": False}, method="PATCH").status_code == 200
        response = client.post("/admin/api/extensions/provider_templates/install",
                               data={"expected_revision": state(client)["revision"], "replace": "true"},
                               files={"file": ("provider.zip", package, "application/zip")})
        assert response.status_code == 200, response.text


def test_resource_apply_requires_an_inspected_plan_and_current_catalog(tmp_path, monkeypatch):
    from update import resources
    checked = {"target_commit": "a" * 40, "resource_revision": "resource-one",
               "available": True, "groups": {}, "branch": "kemo-preview"}
    calls = []
    monkeypatch.setattr(resources, "check", lambda *_args, **_kwargs: dict(checked))
    def apply(root, plan):
        calls.append(plan)
        return {"restart_required": True, "updated": ["protocol_adapters"]}
    monkeypatch.setattr(resources, "apply", apply)
    with instance(tmp_path, monkeypatch) as (client, _):
        value = {key: checked[key] for key in ("target_commit", "resource_revision")}
        assert mutate(client, "/resources/apply", value).status_code == 409
        assert mutate(client, "/resources/check", {"branch": "kemo-preview"}).status_code == 200
        assert mutate(client, "/resources/apply", {**value, "target_commit": "b" * 40}).status_code == 409
        before = state(client)
        assert mutate(client, "/resources/apply", value, expected="stale").status_code == 409
        response = mutate(client, "/resources/apply", value)
        assert response.status_code == 200 and response.json()["restart_required"] is True
        assert len(calls) == 1
        assert state(client)["revision"] == before["revision"]
        assert state(client)["key_revision"] == before["key_revision"]
        assert mutate(client, "/resources/apply", value).status_code == 409
