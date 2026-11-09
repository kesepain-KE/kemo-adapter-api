from tests.support.gateway import add_upstream, instance, mutate, register, state


def test_delete_public_model_cascades_routes_and_scopes_but_preserves_history(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        for upstream in ("first", "second"):
            add_upstream(client, identity=upstream)
            register(client, identity=upstream, model="stable")
        register(client, model="keep", identity="first")
        policies = [None, [], ["stable"], ["stable", "keep", "unknown-model"]]
        keys = []
        for allowed in policies:
            response = mutate(client, "/keys", {"name": "scope", "allowed_models": allowed}, key=True)
            assert response.status_code == 200, response.text
            keys.append(response.json())
        response = client.post("/model/responses", headers={
            "Authorization": f"Bearer {keys[0]['token']}", "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "req_before_delete",
        }, json={
            "protocol_version": "2.0", "request_id": "req_before_delete", "model": "stable", "stream": False,
            "input": [{"type": "message", "id": "msg_user", "role": "user",
                       "content": [{"type": "text", "text": "hello"}]}],
        })
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        stale = state(client)["revision"]
        assert mutate(client, "/settings", {"max_attempts": 2}, method="PATCH").status_code == 200
        before = state(client)
        assert mutate(client, "/models/stable", method="DELETE", expected=stale).status_code == 409
        unchanged = state(client)
        for name in ("revision", "key_revision", "models", "deployments", "keys", "attempts"):
            assert unchanged[name] == before[name]

        response = mutate(client, "/models/stable", method="DELETE")
        assert response.status_code == 200, response.text
        after = state(client)
        assert [model["id"] for model in after["models"]] == ["keep"]
        assert [deployment["public_model"] for deployment in after["deployments"]] == ["keep"]
        assert after["upstreams"] == before["upstreams"]
        scopes = {key["id"]: key["allowed_models"] for key in after["keys"]}
        assert [scopes[key["id"]] for key in keys] == [None, [], [], ["keep", "unknown-model"]]
        assert after["key_revision"] != before["key_revision"]
        assert after["attempts"] == before["attempts"]
        assert after["attempts"][0]["model"] == "stable"
        assert app.state.registry.store.attempt_detail(
            after["attempts"][0]["request_id"], 1)["request"]["model"] == "stable"
        assert set(app.state.registry.current.routes) == {"keep"}
        assert mutate(client, "/models/stable", method="DELETE").status_code == 404
        assert state(client)["revision"] == after["revision"]
