from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from update import git as update_git


GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git executable is required")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT or "git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Kemo Test")
    _git(seed, "config", "user.email", "kemo@example.invalid")
    (seed / "version.json").write_text(
        '{"version":"0.6.0","protocol_version":"1.0"}\n',
        encoding="utf-8",
    )
    (seed / "README.txt").write_text("initial\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "初始版本")

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(seed, "remote", "add", "origin", remote.as_uri())
    _git(seed, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.name", "Kemo Test")
    _git(local, "config", "user.email", "kemo@example.invalid")
    return seed, local, _git(local, "rev-parse", "HEAD")


def test_fetch_fast_forwards_to_exact_utf8_commit(tmp_path: Path) -> None:
    seed, local, old_commit = _repository(tmp_path)
    (seed / "README.txt").write_text("更新内容\n", encoding="utf-8")
    _git(seed, "add", "README.txt")
    _git(seed, "commit", "-m", "修复中文更新说明")
    _git(seed, "push", "origin", "main")
    expected = _git(seed, "rev-parse", "HEAD")

    ok, _ = update_git.fetch(local)

    assert ok
    assert update_git.get_sync_state(local).relation == "behind"
    assert update_git.get_fetch_commit(local) == expected
    assert "修复中文更新说明" in update_git.get_commit_log(local)[0]
    assert update_git.fast_forward_to_fetch_head(local, expected)
    assert update_git.get_current_commit(local) == expected
    assert update_git.get_current_commit(local) != old_commit


def test_recovery_ref_keeps_old_commit_before_repair_reset(tmp_path: Path) -> None:
    seed, local, old_commit = _repository(tmp_path)
    (seed / "README.txt").write_text("远程修复\n", encoding="utf-8")
    _git(seed, "add", "README.txt")
    _git(seed, "commit", "-m", "远程修复提交")
    _git(seed, "push", "origin", "main")
    expected = _git(seed, "rev-parse", "HEAD")
    ok, _ = update_git.fetch(local)
    assert ok

    reference = update_git.create_recovery_ref(
        local, label="kemo-update-integration", commit=old_commit
    )
    assert reference is not None
    assert update_git.hard_reset_to_fetch_head(local, expected)
    assert update_git.get_current_commit(local) == expected
    assert _git(local, "rev-parse", reference) == old_commit


def test_ahead_and_diverged_histories_leave_head_unchanged(tmp_path: Path) -> None:
    seed, local, _ = _repository(tmp_path)
    (local / "local.txt").write_text("本地提交\n", encoding="utf-8")
    _git(local, "add", "local.txt")
    _git(local, "commit", "-m", "本地独有提交")
    local_head = _git(local, "rev-parse", "HEAD")

    ok, _ = update_git.fetch(local)

    assert ok
    state = update_git.get_sync_state(local)
    assert state.relation == "ahead"
    assert update_git.get_current_commit(local) == local_head

    (seed / "remote.txt").write_text("远端提交\n", encoding="utf-8")
    _git(seed, "add", "remote.txt")
    _git(seed, "commit", "-m", "远端独有提交")
    _git(seed, "push", "origin", "main")
    ok, _ = update_git.fetch(local)

    assert ok
    assert update_git.get_sync_state(local).relation == "diverged"
    assert not update_git.fast_forward_to_fetch_head(
        local, update_git.get_fetch_commit(local)
    )
    assert update_git.get_current_commit(local) == local_head
