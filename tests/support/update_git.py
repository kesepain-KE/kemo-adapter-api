"""Disposable Git repositories for updater integration tests."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


GIT = shutil.which("git")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT or "git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.name", "Kemo Test")
    git(seed, "config", "user.email", "kemo@example.invalid")
    (seed / "version.yaml").write_text('{"version":"0.6.0","protocol_version":"1.0"}\n', encoding="utf-8")
    (seed / "README.txt").write_text("initial\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "初始版本")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(seed, "remote", "add", "origin", remote.as_uri())
    git(seed, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    local = tmp_path / "local"
    git(tmp_path, "clone", str(remote), str(local))
    git(local, "config", "user.name", "Kemo Test")
    git(local, "config", "user.email", "kemo@example.invalid")
    return seed, local, git(local, "rev-parse", "HEAD")
