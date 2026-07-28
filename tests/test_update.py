from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from update import backup as update_backup
from update import deps as update_deps
from update import frontend as update_frontend
from update import git as update_git
from update import version as update_version
from update.git import GitDiff, GitSyncState


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_update_entry():
    spec = importlib.util.spec_from_file_location(
        "kemo_update_entry", PROJECT_ROOT / "update.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_stops_before_transition_when_backup_fails(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.0")
    remote = SimpleNamespace(version="0.6.1")

    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git, "get_remote_diff", lambda _root: GitDiff(["setup.py"])
    )
    monkeypatch.setattr(
        update_entry.git,
        "get_sync_state",
        lambda _root: GitSyncState("behind", 0, 1),
    )
    monkeypatch.setattr(
        update_entry.git, "get_fetch_commit", lambda _root: "b" * 40
    )
    monkeypatch.setattr(
        update_entry.git, "get_protected_remote_diff", lambda _root: GitDiff([])
    )
    monkeypatch.setattr(update_entry.backup, "create", lambda _root: (False, "磁盘空间不足"))

    def unexpected_transition(*_args, **_kwargs):
        raise AssertionError("备份失败后不得改变 Git HEAD")

    monkeypatch.setattr(
        update_entry.git,
        "fast_forward_to_fetch_head",
        unexpected_transition,
    )

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


def test_git_runner_decodes_utf8_independently_of_windows_code_page(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        raw_stdout = "更新：开发目录/中文文件.md".encode("utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout=raw_stdout.decode(
                kwargs["encoding"], errors=kwargs.get("errors", "strict")
            ),
            stderr="",
        )

    monkeypatch.setattr(update_git.subprocess, "run", run)

    result = update_git.run_git(["status", "--short"], PROJECT_ROOT, timeout=7)

    assert result.stdout == "更新：开发目录/中文文件.md"
    assert captured["command"] == [
        "git",
        "-c",
        "i18n.logOutputEncoding=UTF-8",
        "-c",
        "core.quotePath=false",
        "status",
        "--short",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["text"] is True
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["LANG"] == "C"
    assert kwargs["timeout"] == 7


def test_remote_version_uses_shared_git_utf8_boundary(monkeypatch) -> None:
    calls: list[tuple[list[str], Path, int]] = []

    def run_git(args, project_root, timeout=30):
        calls.append((args, project_root, timeout))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"version":"0.6.2","protocol_version":"1.1",'
                '"notes":"修复 Windows 中文路径"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(update_version, "run_git", run_git)

    remote = update_version.read_remote(PROJECT_ROOT)

    assert remote == update_version.VersionInfo(
        "0.6.2", "1.1", "修复 Windows 中文路径"
    )
    assert calls == [(["show", "FETCH_HEAD:version.json"], PROJECT_ROOT, 10)]


def test_apply_rejects_remote_changes_to_protected_paths(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.0")
    remote = SimpleNamespace(version="0.6.1")

    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git,
        "get_sync_state",
        lambda _root: GitSyncState("behind", 0, 1),
    )
    monkeypatch.setattr(
        update_entry.git,
        "get_protected_remote_diff",
        lambda _root: GitDiff(["api/keys.json"]),
    )

    def unexpected_backup(*_args, **_kwargs):
        raise AssertionError("受保护路径冲突时不得开始备份或拉取")

    monkeypatch.setattr(update_entry.backup, "create", unexpected_backup)

    assert update_entry._apply(yes=True) == 4


def test_apply_up_to_date_never_implies_repair(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.1")
    remote = SimpleNamespace(version="0.6.1")
    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git,
        "get_sync_state",
        lambda _root: GitSyncState("up_to_date", 0, 0),
    )

    def unexpected_mutation(*_args, **_kwargs):
        raise AssertionError("--apply --yes 在无更新时不得进入修复或备份")

    monkeypatch.setattr(update_entry.backup, "create", unexpected_mutation)
    monkeypatch.setattr(
        update_entry.git, "hard_reset_to_fetch_head", unexpected_mutation
    )

    assert update_entry._apply(yes=True) == 0


@pytest.mark.parametrize(
    "state",
    [
        GitSyncState("ahead", 2, 0),
        GitSyncState("diverged", 1, 3),
    ],
)
def test_apply_rejects_non_fast_forward_history(monkeypatch, state) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.1")
    remote = SimpleNamespace(version="0.6.2")
    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(update_entry.git, "get_sync_state", lambda _root: state)

    def unexpected_backup(*_args, **_kwargs):
        raise AssertionError("非快进历史不得进入备份和源码变更阶段")

    monkeypatch.setattr(update_entry.backup, "create", unexpected_backup)

    assert update_entry._apply(yes=True) == 4


def test_apply_passes_precaptured_diff_and_exact_fetch_commit(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.1")
    remote = SimpleNamespace(version="0.6.2")
    diff = GitDiff(["requirements.txt", "web/frontend/src/App.tsx"])
    target = "b" * 40
    captured: dict[str, object] = {}

    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git,
        "get_sync_state",
        lambda _root: GitSyncState("behind", 0, 2),
    )
    monkeypatch.setattr(
        update_entry.git, "get_protected_remote_diff", lambda _root: GitDiff([])
    )
    monkeypatch.setattr(update_entry.git, "get_remote_diff", lambda _root: diff)
    monkeypatch.setattr(update_entry.git, "get_fetch_commit", lambda _root: target)

    def do_update(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(update_entry, "_do_update", do_update)

    assert update_entry._apply(yes=True) == 0
    assert captured["args"] == (local, remote)
    assert captured["kwargs"] == {
        "is_repair": False,
        "yes": True,
        "diff": diff,
        "target_commit": target,
    }


def test_repair_rejects_protected_paths_before_backup(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.1")
    remote = SimpleNamespace(version="0.6.1")
    monkeypatch.setattr(update_entry, "_check", lambda: (0, local, remote))
    monkeypatch.setattr(
        update_entry.git,
        "get_protected_remote_diff",
        lambda _root: GitDiff(["providers/example/secrets.json"]),
    )

    def unexpected_backup(*_args, **_kwargs):
        raise AssertionError("受保护路径冲突时修复也不得开始")

    monkeypatch.setattr(update_entry.backup, "create", unexpected_backup)

    assert update_entry._repair(yes=True) == 4


def test_repair_creates_recovery_ref_before_exact_reset(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.1")
    remote = SimpleNamespace(version="0.6.1", notes="")
    before = "a" * 40
    target = "b" * 40
    events: list[tuple[str, str]] = []
    commits = iter([before, target])

    monkeypatch.setattr(
        update_entry.backup, "create", lambda _root: (True, "backup-ok")
    )
    monkeypatch.setattr(update_entry.git, "has_local_changes", lambda _root: False)
    monkeypatch.setattr(
        update_entry.git, "get_stash_label", lambda: "kemo-update-test"
    )
    monkeypatch.setattr(
        update_entry.git, "get_current_commit", lambda _root: next(commits)
    )

    def recovery(_root, *, label, commit):
        assert label == "kemo-update-test"
        events.append(("recovery", commit))
        return "refs/kemo-update/recovery/kemo-update-test"

    def reset(_root, expected_commit=None):
        events.append(("reset", expected_commit))
        return True

    monkeypatch.setattr(update_entry.git, "create_recovery_ref", recovery)
    monkeypatch.setattr(update_entry.git, "hard_reset_to_fetch_head", reset)
    monkeypatch.setattr(update_entry.deps, "install_requirements", lambda _root: True)
    monkeypatch.setattr(
        update_entry.frontend,
        "build_frontend",
        lambda _root: (True, "frontend-ok"),
    )

    assert update_entry._do_update(
        local,
        remote,
        is_repair=True,
        yes=True,
        diff=GitDiff([]),
        target_commit=target,
    ) == 0
    assert events == [("recovery", before), ("reset", target)]


def test_repair_ref_failure_prevents_hard_reset(monkeypatch) -> None:
    update_entry = _load_update_entry()
    local = SimpleNamespace(version="0.6.1")
    remote = SimpleNamespace(version="0.6.1", notes="")
    monkeypatch.setattr(
        update_entry.backup, "create", lambda _root: (True, "backup-ok")
    )
    monkeypatch.setattr(update_entry.git, "has_local_changes", lambda _root: False)
    monkeypatch.setattr(
        update_entry.git, "get_stash_label", lambda: "kemo-update-test"
    )
    monkeypatch.setattr(
        update_entry.git, "get_current_commit", lambda _root: "a" * 40
    )
    monkeypatch.setattr(
        update_entry.git, "create_recovery_ref", lambda *_args, **_kwargs: None
    )

    def unexpected_reset(*_args, **_kwargs):
        raise AssertionError("恢复引用失败后不得 reset --hard")

    monkeypatch.setattr(
        update_entry.git, "hard_reset_to_fetch_head", unexpected_reset
    )

    assert update_entry._do_update(
        local,
        remote,
        is_repair=True,
        yes=True,
        diff=GitDiff([]),
        target_commit="b" * 40,
    ) == 2


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("0 0\n", GitSyncState("up_to_date", 0, 0)),
        ("0 3\n", GitSyncState("behind", 0, 3)),
        ("2 0\n", GitSyncState("ahead", 2, 0)),
        ("2 3\n", GitSyncState("diverged", 2, 3)),
    ],
)
def test_git_sync_state_classifies_all_relationships(
    monkeypatch, output, expected
) -> None:
    monkeypatch.setattr(
        update_git,
        "_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=output,
            stderr="",
        ),
    )
    assert update_git.get_sync_state(PROJECT_ROOT) == expected


def test_stash_includes_untracked_source_files(monkeypatch) -> None:
    captured: list[list[str]] = []

    def git_command(args, *_args, **_kwargs):
        captured.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="Saved working directory and index state",
            stderr="",
        )

    monkeypatch.setattr(update_git, "_git", git_command)

    assert update_git.stash_local(PROJECT_ROOT, "kemo-update-test") is True
    assert captured == [
        [
            "stash",
            "push",
            "--include-untracked",
            "-m",
            "kemo-update-test",
        ]
    ]


def test_backup_restore_rejects_path_traversal(tmp_path) -> None:
    backup_root = tmp_path / ".backup"
    backup_root.mkdir()
    (tmp_path / "outside").mkdir()

    ok, message = update_backup.restore(tmp_path, "../outside")

    assert ok is False
    assert "格式无效" in message


def test_failed_backup_removes_incomplete_staging_directory(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    monkeypatch.setattr(update_backup, "_timestamp", lambda: "20260728-120000")

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(update_backup.shutil, "copy2", fail_copy)

    ok, message = update_backup.create(tmp_path)

    assert ok is False
    assert "simulated copy failure" in message
    assert not (tmp_path / ".backup" / "20260728-120000").exists()
    assert not list((tmp_path / ".backup").glob(".creating-*"))


def test_update_cli_exposes_repair_and_backup_restore_but_not_git_rollback() -> None:
    update_entry = _load_update_entry()
    parser = update_entry.build_parser()

    assert parser.parse_args(["--repair"]).repair is True
    assert parser.parse_args(["--list-backups"]).list_backups is True
    assert parser.parse_args(["--restore-backup", "latest"]).restore_backup == "latest"
    with pytest.raises(SystemExit):
        parser.parse_args(["--rollback"])
    with pytest.raises(SystemExit):
        update_entry.main(["--yes"])


def test_no_argument_entry_opens_beginner_menu_and_defaults_to_update(
    monkeypatch,
) -> None:
    update_entry = _load_update_entry()
    calls: list[bool] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(
        update_entry,
        "_apply",
        lambda yes=False: calls.append(yes) or 7,
    )

    assert update_entry.main([]) == 7
    assert calls == [False]


def test_beginner_menu_can_check_without_mutating_and_return_to_menu(
    monkeypatch,
) -> None:
    update_entry = _load_update_entry()
    answers = iter(["2", "", "0"])
    checks: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        update_entry,
        "_check_cmd",
        lambda: checks.append("check") or 0,
    )

    assert update_entry.main([]) == 0
    assert checks == ["check"]


def test_beginner_menu_restores_backup_by_number(monkeypatch) -> None:
    update_entry = _load_update_entry()
    answers = iter(["3", "2", "", "0"])
    restored: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        update_entry.backup,
        "list_backups",
        lambda _root: ["20260728-130000", "20260728-120000"],
    )
    monkeypatch.setattr(
        update_entry,
        "_restore_backup",
        lambda backup_id: restored.append(backup_id) or 0,
    )

    assert update_entry.main([]) == 0
    assert restored == ["20260728-120000"]


def test_beginner_menu_eof_exits_without_starting_update(monkeypatch) -> None:
    update_entry = _load_update_entry()

    def eof(_prompt: str) -> str:
        raise EOFError

    def unexpected_update(*_args, **_kwargs):
        raise AssertionError("无输入终端不得默认执行更新")

    monkeypatch.setattr("builtins.input", eof)
    monkeypatch.setattr(update_entry, "_apply", unexpected_update)

    assert update_entry.main([]) == 0


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("1", True), ("是", True), ("2", False), ("否", False)],
)
def test_confirm_accepts_beginner_friendly_numeric_input(
    monkeypatch,
    answer: str,
    expected: bool,
) -> None:
    update_entry = _load_update_entry()
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert update_entry._confirm("继续？") is expected


def test_frontend_update_reuses_one_stop_deployment(monkeypatch, tmp_path) -> None:
    frontend_root = tmp_path / "web" / "frontend"
    frontend_root.mkdir(parents=True)
    (frontend_root / "package.json").write_text("{}", encoding="utf-8")
    setup_script = tmp_path / "setup.py"
    setup_script.write_text("", encoding="utf-8")
    calls: list[tuple[list[str], Path, bool, int]] = []

    def run(command, *, cwd, check, timeout):
        calls.append((command, cwd, check, timeout))
        output = frontend_root / "dist" / "index.html"
        output.parent.mkdir(parents=True)
        output.write_text("built", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_frontend.subprocess, "run", run)

    ok, message = update_frontend.build_frontend(tmp_path)

    assert ok is True
    assert "setup.py" in message
    assert calls == [
        (
            [update_frontend.sys.executable, str(setup_script), "--build-frontend"],
            tmp_path,
            False,
            900,
        )
    ]


def test_dependency_update_does_not_decode_unused_process_output(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"\xff", stderr=b"\xfe")

    monkeypatch.setattr(update_deps.subprocess, "run", run)

    assert update_deps.install_requirements(tmp_path) is True
    assert "text" not in captured["kwargs"]
    assert "encoding" not in captured["kwargs"]
