from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from update import git as update_git
from update.git import GitDiff


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_update_entry():
    spec = importlib.util.spec_from_file_location(
        "kemo_update_entry", PROJECT_ROOT / "update.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_stops_before_pull_when_backup_fails(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.0")
    remote = SimpleNamespace(version="0.6.1")

    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git, "get_remote_diff", lambda _root: GitDiff(["setup.py"])
    )
    monkeypatch.setattr(
        update_entry.git, "get_protected_remote_diff", lambda _root: GitDiff([])
    )
    monkeypatch.setattr(update_entry.backup, "create", lambda _root: (False, "磁盘空间不足"))

    def unexpected_pull(*_args, **_kwargs):
        raise AssertionError("备份失败后不得拉取远程代码")

    monkeypatch.setattr(update_entry.git, "pull", unexpected_pull)

    assert update_entry._apply(yes=True) == 2


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "api/keys.json",
        "providers/deepseek/config.json",
        "storage/daily/2026-07-28.sqlite3",
        "core/runtime/gateway.pid.json",
        ".backup/20260728-120000/.env",
        "开发目录/obsidian/index.md",
        "restart.py.bak.1",
        "gateway.log",
        "gateway.pid",
    ],
)
def test_local_data_paths_are_protected(path: str) -> None:
    assert update_git._is_protected(path)


def test_provider_namespace_file_remains_updateable() -> None:
    assert not update_git._is_protected("providers/__init__.py")


def test_remote_diff_separates_source_from_protected_data(monkeypatch) -> None:
    monkeypatch.setattr(
        update_git,
        "get_remote_files",
        lambda _root: ["update.py", "api/keys.json", "providers/__init__.py"],
    )

    assert update_git.get_remote_diff(PROJECT_ROOT).files == [
        "update.py",
        "providers/__init__.py",
    ]
    assert update_git.get_protected_remote_diff(PROJECT_ROOT).files == [
        "api/keys.json"
    ]


def test_apply_rejects_remote_changes_to_protected_paths(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.0")
    remote = SimpleNamespace(version="0.6.1")

    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git,
        "get_protected_remote_diff",
        lambda _root: GitDiff(["api/keys.json"]),
    )

    def unexpected_backup(*_args, **_kwargs):
        raise AssertionError("受保护路径冲突时不得开始备份或拉取")

    monkeypatch.setattr(update_entry.backup, "create", unexpected_backup)

    assert update_entry._apply(yes=True) == 4
