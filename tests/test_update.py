from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(update_entry.backup, "create", lambda _root: (False, "磁盘空间不足"))

    def unexpected_pull(*_args, **_kwargs):
        raise AssertionError("备份失败后不得拉取远程代码")

    monkeypatch.setattr(update_entry.git, "pull", unexpected_pull)

    assert update_entry._apply(yes=True) == 2
