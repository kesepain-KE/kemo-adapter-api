from __future__ import annotations

import base64
from pathlib import Path
import shutil

import pytest
import yaml

from gateway.domain import Catalog, Credential, Deployment, ExtensionState, PublicModel, Upstream
from gateway.extensions import extension_digest, install
from gateway.keys import KeyService
from gateway.resources import BUILTIN_PATHS, inventory, resource_revision
from gateway.security import SecretVault
from gateway.store import Store, revision, utc_now
from tests.support.update_git import GIT, git as _git, repository as _repository
from tests.support.gateway import extension_zip
from tests.support.paths import PROJECT_ROOT
from update import integrity, resources, transaction
from update._utils import UpdateError
from update.version import VersionInfo


pytestmark = pytest.mark.skipif(GIT is None, reason="git executable is required")


def _project(tmp_path, monkeypatch):
    seed, local, _ = _repository(tmp_path)
    for name in ("gateway", "core", "vendor", "templates", "update"):
        # These tests build catalogs, not tokenizers. Preserve tokenizer
        # manifests but avoid copying vocabularies into every temporary Git repo.
        shutil.copytree(PROJECT_ROOT / name, seed / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.gz", "*.model", "*.tiktoken"))
    shutil.copyfile(PROJECT_ROOT / ".gitignore", seed / ".gitignore")
    shutil.copyfile(PROJECT_ROOT / "version.json", seed / "version.json")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "资源基线")
    _git(seed, "push", "origin", "main")
    _git(local, "pull", "--ff-only")
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"r" * 32).decode())
    package = extension_zip("provider_templates", identity="user-example", template={
        "adapter_id": "openai-chat", "name": "User", "base_url": "https://user.invalid",
    })
    install(local, "provider_templates", package)
    # Preserve the formerly supported same-ID user template and the existing
    # upstream's template_id, rather than migrating user configuration.
    install(local, "provider_templates", extension_zip(
        "provider_templates", identity="openai",
        template={"adapter_id": "openai-chat", "name": "User override", "base_url": "https://user.invalid"},
    ))
    package = extension_zip("protocol_adapters", identity="user-native",
                            code=(PROJECT_ROOT / "templates/protocol_adapter/adapter.py").read_text(encoding="utf-8"))
    install(local, "protocol_adapters", package)
    store = Store(local / "data")
    vault = SecretVault(local / "data")
    credential = Credential(id="primary", encrypted=vault.encrypt("private-test-value", "local:primary"), preview="***")
    catalog = Catalog(
        upstreams=(Upstream(id="local", name="Local", adapter_id="openai-chat",
                            base_url="https://provider.invalid", credentials=(credential,), template_id="openai"),),
        models=(PublicModel(id="public", capabilities={"streaming": True}),),
        deployments=(Deployment(id="dep_local", upstream_id="local", upstream_model="remote",
                                public_model="public", priority=7, weight=3),),
        extensions=tuple(ExtensionState(
            id=identity, kind=kind, enabled=True,
            digest=extension_digest(local / "extensions" / kind / identity, kind),
        ) for kind, identity in (("provider_templates", "user-example"), ("provider_templates", "openai"),
                                ("protocol_adapters", "user-native"))),
    )
    store.save(catalog, revision(Catalog()), "test")
    KeyService(store, vault).create({"name": "Client", "allowed_models": ["public"]})
    store.audit("test", "private-call-log", "req_saved")
    with store.transaction() as db:
        db.execute("""INSERT INTO attempts(
            request_id,attempt_no,response_id,deployment_id,upstream_id,model,started_at,status,price)
            VALUES(?,?,?,?,?,?,?,?,?)""", (
                "req_saved", 1, "resp_saved", "dep_local", "local", "public", utc_now(), "completed", "{}",
            ))
    store.close()
    for relative in ("gateway.yaml", ".env", "data/master.key", "data/executions/saved",
                     "data/statistics/saved", "data/assets/saved"):
        path = local / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-test-content", encoding="utf-8")
    return seed, local


def _private_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for relative in ("data", "extensions", "gateway.yaml", ".env")
            for path in ([root / relative] if (root / relative).is_file() else (root / relative).rglob("*"))
            # SQLite read-only WAL connections create shared-memory bookkeeping.
            # The persisted database and all application records must stay exact.
            if path.is_file() and not path.name.endswith(("-shm", "-wal"))}


def _release(seed, *, broken=False, conflict=False):
    path = seed / BUILTIN_PATHS["protocol_adapters"] / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["version"] = "1.2.0"
    manifest["adapters"].append({
        "id": "new-native", "version": "1.0.0", "module": "new_native", "class_name": "NewNative",
    })
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    source = "this is invalid python!" if broken else (
        "from gateway.adapters.builtin.openai_chat import OpenAIChat\n"
        "class NewNative(OpenAIChat):\n"
        "    manifest = OpenAIChat.manifest.model_copy(update={'id': 'new-native', 'name': 'New native'})\n"
    )
    (path.parent / "new_native.py").write_text(source, encoding="utf-8")
    path = seed / BUILTIN_PATHS["provider_templates"] / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["version"] = "1.2.0"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    identity = "user-example" if conflict else "new-provider"
    directory = path.parent / identity
    directory.mkdir()
    (directory / "manifest.yaml").write_text(yaml.safe_dump({
        "id": identity, "name": "New", "version": "1.0.0", "contract_version": "1.0",
    }), encoding="utf-8")
    (directory / "template.yaml").write_text(yaml.safe_dump({
        "adapter_id": "new-native", "name": "New", "base_url": "https://new.invalid",
    }), encoding="utf-8")
    (seed / "README.txt").write_text("new framework content must not be updated independently", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "发布独立资源")
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def test_independent_update_loads_new_adapter_and_preserves_framework_and_private_data(tmp_path, monkeypatch):
    seed, local = _project(tmp_path, monkeypatch)
    target = _release(seed)
    head = _git(local, "rev-parse", "HEAD")
    version = (local / "version.json").read_bytes()
    private = _private_files(local)
    unrelated = (local / "README.txt").read_bytes()
    plan = resources.check(local, remote_url=seed.as_uri())
    assert plan["available"] and plan["target_commit"] == target
    assert plan["groups"]["protocol_adapters"]["changes"][-1]["id"] == "new-native"
    result = resources.apply(local, plan)
    assert result["updated"] == ["protocol_adapters", "provider_templates"]
    assert result["restart_required"] is True
    assert inventory(local)["protocol_adapters"]["items"]["new-native"] == "1.0.0"
    assert _git(local, "rev-parse", "HEAD") == head
    assert (local / "version.json").read_bytes() == version
    assert (local / "README.txt").read_bytes() == unrelated
    assert _private_files(local) == private
    assert resources.check(local, remote_url=seed.as_uri())["available"] is False


@pytest.mark.parametrize("broken,conflict", [(True, False), (False, True)])
def test_resource_update_failure_restores_both_directories_without_touching_user_data(
    tmp_path, monkeypatch, broken, conflict,
):
    seed, local = _project(tmp_path, monkeypatch)
    _release(seed, broken=broken, conflict=conflict)
    before, private = resource_revision(local), _private_files(local)
    plan = resources.check(local, remote_url=seed.as_uri())
    with pytest.raises(UpdateError, match="不兼容|用户资源"):
        resources.apply(local, plan)
    assert resource_revision(local) == before
    assert _private_files(local) == private


@pytest.mark.parametrize("repair,valid", [(False, True), (True, True), (False, False)])
def test_full_update_replaces_official_resources_and_keeps_private_data(tmp_path, monkeypatch, repair, valid):
    seed, local = _project(tmp_path, monkeypatch)
    # A prior independent update, plus a local file in the replaceable area.
    local_file = local / BUILTIN_PATHS["protocol_adapters"] / "local_only.py"
    local_file.write_text("local builtin customization", encoding="utf-8")
    manifest = local / BUILTIN_PATHS["protocol_adapters"] / "manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("1.1.0", "1.1.1"), encoding="utf-8")
    original, private = resource_revision(local), _private_files(local)
    head = _git(local, "rev-parse", "HEAD")
    target = _release(seed)
    assert resources.git.fetch(local, remote_url=seed.as_uri())[0]
    monkeypatch.setattr(transaction, "_install_deps", lambda _: True)
    monkeypatch.setattr(transaction, "_build_frontend", lambda _: True)
    monkeypatch.setattr(integrity, "validate_installed_source", lambda *_args, **_kwargs: integrity.IntegrityResult(valid, "test gate"))
    result = transaction.perform_update(
        local, VersionInfo("2.0.0", "2.0", ""), VersionInfo("2.0.0", "2.0", ""),
        is_repair=repair, yes=True, diff=resources.git.get_remote_diff(local), target_commit=target,
    )
    assert result == (0 if valid else 6)
    assert _private_files(local) == private
    if valid:
        assert _git(local, "rev-parse", "HEAD") == target
        assert not local_file.exists()
        assert inventory(local)["protocol_adapters"]["version"] == "1.2.0"
        assert _git(local, "status", "--porcelain") == ""
    else:
        assert _git(local, "rev-parse", "HEAD") == head
        assert resource_revision(local) == original
        assert local_file.read_text(encoding="utf-8") == "local builtin customization"


def test_resource_update_rejects_stale_files_and_version_downgrade(tmp_path, monkeypatch):
    seed, local = _project(tmp_path, monkeypatch)
    _release(seed)
    plan = resources.check(local, remote_url=seed.as_uri())
    path = local / BUILTIN_PATHS["protocol_adapters"] / "common.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
    before, private = resource_revision(local), _private_files(local)
    with pytest.raises(UpdateError, match="本地资源已改变"):
        resources.apply(local, plan)
    assert resource_revision(local) == before and _private_files(local) == private
    path = seed / BUILTIN_PATHS["protocol_adapters"] / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["version"] = "1.0.0"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "无效降级资源")
    _git(seed, "push", "origin", "main")
    with pytest.raises(UpdateError, match="低于本地"):
        resources.check(local, remote_url=seed.as_uri())
    assert resource_revision(local) == before and _private_files(local) == private
