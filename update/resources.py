"""Versioned updates confined to the two official resource directories."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from uuid import uuid4

from gateway.resources import BUILTIN_PATHS, inventory, resource_revision
from update import git
from update._utils import UpdateError, compare_versions, redact_text
from update.constants import DEFAULT_BRANCH, DEFAULT_REPO_URL
from update.lock import UpdateLock


def _export(root: Path, commit: str, destination: Path):
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise UpdateError("资源更新提交无效")
    archive = destination / "resources.tar"
    result = git.run_git(
        ["archive", "--format=tar", f"--output={archive}", commit, *BUILTIN_PATHS.values()], root, timeout=60,
    )
    if result.returncode:
        raise UpdateError("远端没有兼容的独立资源包，请先更新网关框架")
    with tarfile.open(archive) as bundle:
        entries = bundle.getmembers()
        if len(entries) > 2000 or sum(item.size for item in entries) > 32 * 1024 * 1024:
            raise UpdateError("资源包展开大小或文件数超限")
        names = set()
        for entry in entries:
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.name or ":" in entry.name
                    or not (entry.isfile() or entry.isdir())):
                raise UpdateError("资源包包含不安全路径或链接")
            if entry.isdir() and any(PurePosixPath(value).is_relative_to(path) for value in BUILTIN_PATHS.values()):
                continue  # git archive also includes the parent directories.
            if not any(path.is_relative_to(PurePosixPath(value)) for value in BUILTIN_PATHS.values()):
                raise UpdateError("资源包试图写入非资源目录")
            if entry.name.casefold() in names:
                raise UpdateError("资源包存在重复路径")
            names.add(entry.name.casefold())
            target = destination.joinpath(*path.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    archive.unlink()


def _plan(root: Path, remote_root: Path, commit: str):
    local, remote = inventory(root), inventory(remote_root)
    groups = {}
    for kind, current in local.items():
        target = remote[kind]
        comparison = compare_versions(target["version"], current["version"])
        changes = []
        for identity in sorted(current["items"].keys() | target["items"].keys()):
            old, new = current["items"].get(identity), target["items"].get(identity)
            if old != new:
                if old is not None and new is not None and compare_versions(new, old) < 0:
                    raise UpdateError(f"拒绝降低资源版本：{kind}/{identity}")
                changes.append({"id": identity, "current": old, "target": new})
        if comparison < 0:
            raise UpdateError(f"远端 {kind} 资源包版本低于本地，请选择正确分支或更新框架")
        if comparison == 0 and changes:
            raise UpdateError(f"{kind} 内容版本已变化，但资源包版本未递增")
        groups[kind] = {"current": current["version"], "target": target["version"],
                        "available": comparison > 0, "changes": changes}
    return {"target_commit": commit, "resource_revision": resource_revision(root),
            "available": any(group["available"] for group in groups.values()), "groups": groups}


def check(root: Path, *, branch=DEFAULT_BRANCH, remote_url=None):
    with UpdateLock(root):
        ok, message = git.fetch(root, branch=branch, remote_url=remote_url or DEFAULT_REPO_URL)
        if not ok:
            raise UpdateError(message)
        commit = git.get_fetch_commit(root)
        with tempfile.TemporaryDirectory(prefix="kemo-resources-") as temporary:
            staging = Path(temporary)
            _export(root, commit, staging)
            return {**_plan(root, staging, commit), "branch": branch}


def _remove(root: Path, relative: str):
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise UpdateError("资源目录不能链接到项目外")
    if target.exists():
        shutil.rmtree(target)


def _validate(root: Path):
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    # An isolated interpreter prevents imports cached by the running gateway
    # from hiding a broken replacement or a new dependency.
    script = ("import runpy,sys; sys.path.insert(0,sys.argv[1]); "
              "runpy.run_module('gateway.resource_validation',run_name='__main__')")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(root.resolve())],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("新资源启动校验超时，已停止更新") from exc
    if result.returncode:
        raise UpdateError("新资源与当前框架或用户配置不兼容：" + redact_text(
            (result.stderr or result.stdout)[-4000:],
        ))


def apply(root: Path, plan: dict):
    with UpdateLock(root):
        if resource_revision(root) != plan["resource_revision"]:
            raise UpdateError("本地资源已改变，请重新检查更新")
        with tempfile.TemporaryDirectory(prefix="kemo-resources-") as temporary:
            staging = Path(temporary)
            _export(root, plan["target_commit"], staging)
            actual = _plan(root, staging, plan["target_commit"])
            kinds = [kind for kind, group in actual["groups"].items() if group["available"]]
            if not kinds:
                return {**actual, "updated": [], "restart_required": False}
            # New official IDs must never silently take over a user resource.
            local_inventory, remote_inventory = inventory(root), inventory(staging)
            for kind in kinds:
                for identity in remote_inventory[kind]["items"]:
                    existing_template = kind == "provider_templates" and identity in local_inventory[kind]["items"]
                    if (root / "extensions" / kind / identity).exists() and not existing_template:
                        raise UpdateError(f"内置资源与用户资源 ID 冲突，请先为用户资源换名：{kind}/{identity}")
            backup = root / ".backup" / "resources" / uuid4().hex
            backup.mkdir(parents=True)
            changed = []
            try:
                for kind in kinds:
                    relative = BUILTIN_PATHS[kind]
                    old = backup / relative
                    old.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(root / relative, old, ignore=shutil.ignore_patterns("__pycache__"))
                    changed.append(relative)
                    _remove(root, relative)
                    shutil.copytree(staging / relative, root / relative)
                _validate(root)
                new_revision = resource_revision(root)
                (backup / "update.json").write_text(json.dumps({
                    "target_commit": plan["target_commit"], "groups": actual["groups"],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except BaseException:
                for relative in changed:
                    _remove(root, relative)
                    shutil.copytree(backup / relative, root / relative)
                raise
            return {**actual, "updated": kinds, "restart_required": True,
                    "backup": backup.relative_to(root).as_posix(),
                    "resource_revision": new_revision}
