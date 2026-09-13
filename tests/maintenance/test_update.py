from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from update import backup as update_backup
from update import checker as update_checker
from update import cli as update_cli
from update import deps as update_deps
from update import frontend as update_frontend
from update import git as update_git
from update import integrity as update_integrity
from update import plan as update_plan
from update import transaction as update_transaction
from update import ui as update_ui
from update import version as update_version
from update._utils import UpdateError, git_control_dir, redact_text
from update.git import GitDiff, GitSyncState
from update.lock import UpdateLock
from update.plan import UpdatePlan


from tests.support.paths import PROJECT_ROOT


def _load_root_entry():
    spec = importlib.util.spec_from_file_location(
        "kemo_update_entry", PROJECT_ROOT / "update.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _healthy() -> update_integrity.IntegrityResult:
    return update_integrity.IntegrityResult(True, "ok")


def test_root_update_is_only_a_compatibility_entrypoint() -> None:
    source = (PROJECT_ROOT / "update.py").read_text(encoding="utf-8")
    entry = _load_root_entry()

    assert "from update.cli import main" in source
    assert "def _apply" not in source
    assert entry.main is update_cli.main


def test_update_package_and_module_entrypoints_share_cli_main(monkeypatch) -> None:
    import update
    import update.__main__ as module_entry

    assert module_entry.main is update_cli.main
    monkeypatch.setattr(update_cli, "main", lambda argv=None: 17 if argv == ["--check"] else 18)
    assert update.main(["--check"]) == 17


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "api/keys.json",
        "providers/deepseek/config.json",
        "storage/daily/2026-08-23.sqlite3",
        "storage/executions/executions.sqlite3",
        "core/runtime/gateway.pid.json",
        ".backup/20260823-120000/.env",
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


@pytest.mark.parametrize(
    "raw",
    [
        "https://operator:super-secret@example.invalid:bad/repo.git",
        "ssh://operator:super-secret@example.invalid/repo.git",
        "git+ssh://operator:super-secret@example.invalid/repo.git?token=another-secret",
        "https://operator:super-secret@[broken/repo.git?session_secret=another-secret",
    ],
)
def test_update_redaction_never_leaks_credentials_for_ssh_or_malformed_urls(
    raw: str,
) -> None:
    safe = redact_text(raw)

    assert "super-secret" not in safe
    assert "another-secret" not in safe
    assert "***@" in safe or "token=***" in safe or "access_token=***" in safe


def test_git_control_dir_supports_worktree_git_file(tmp_path: Path) -> None:
    control = tmp_path / "git-metadata" / "worktree"
    control.mkdir(parents=True)
    (tmp_path / ".git").write_text(
        f"gitdir: {control.as_posix()}\n", encoding="utf-8"
    )

    assert git_control_dir(tmp_path) == control.resolve()


def test_update_marker_and_lock_stay_outside_git_status(tmp_path: Path) -> None:
    initialized = update_git._git(["init"], tmp_path)
    assert initialized.returncode == 0

    with UpdateLock(tmp_path), update_transaction._maintenance_marker(tmp_path):
        assert UpdateLock(tmp_path).path == tmp_path / ".git" / "kemo-update.lock"
        assert (tmp_path / ".git" / ".update.maintenance").is_file()
        assert not (tmp_path / ".update.maintenance").exists()
        assert update_git.has_local_changes(tmp_path) is False

    assert not (tmp_path / ".git" / ".update.maintenance").exists()


def test_no_argument_menu_errors_are_caught(monkeypatch, capsys) -> None:
    def fail(_root: Path) -> int:
        raise UpdateError("交互菜单测试错误")

    monkeypatch.setattr(update_cli, "interactive_menu", fail)

    assert update_cli.main([], project_root=PROJECT_ROOT) == 1
    assert "交互菜单测试错误" in capsys.readouterr().err


def test_repair_dry_run_still_rejects_protected_paths(monkeypatch) -> None:
    current = update_version.VersionInfo("0.7.8", "1.0", "")
    called: list[Path] = []
    monkeypatch.setattr(update_checker, "check", lambda _root: (0, current, current))
    monkeypatch.setattr(
        update_checker,
        "reject_protected_remote_changes",
        lambda root: called.append(root) or True,
    )

    assert update_cli.repair(PROJECT_ROOT, yes=True, dry_run=True) == 4
    assert called == [PROJECT_ROOT]


def test_update_plan_reports_protocol_compatibility() -> None:
    local = update_version.VersionInfo("0.7.8", "1.0", "")
    remote = update_version.VersionInfo("0.8.0", "2.0", "")
    inspection = UpdatePlan(
        PROJECT_ROOT,
        local,
        remote,
        GitSyncState("behind", 0, 1),
        "a" * 40,
        GitDiff(["update.py"]),
        GitDiff([]),
        _healthy(),
        "直连",
        False,
        "协议主版本不兼容",
    )

    rendered = update_plan.render(inspection)
    assert any(line.startswith("协议状态：不兼容") for line in rendered)
    assert inspection.safe_to_apply is False


def test_remote_version_uses_shared_git_utf8_boundary(monkeypatch) -> None:
    calls: list[tuple[list[str], Path, int]] = []

    def run_git(args, project_root, timeout=30):
        calls.append((args, project_root, timeout))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"version":"0.7.6","protocol_version":"1.0",'
                '"notes":"修复 Windows 中文路径"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(update_version, "run_git", run_git)
    remote = update_version.read_remote(PROJECT_ROOT)

    assert remote == update_version.VersionInfo(
        "0.7.6", "1.0", "修复 Windows 中文路径"
    )
    assert calls == [(["show", "FETCH_HEAD:version.json"], PROJECT_ROOT, 10)]


def test_apply_stops_before_transition_when_backup_fails(monkeypatch) -> None:
    local = update_version.VersionInfo("0.7.5", "1.0", "")
    remote = update_version.VersionInfo("0.7.6", "1.0", "")
    monkeypatch.setattr(update_integrity, "check_source_state", lambda _root: _healthy())
    monkeypatch.setattr(
        update_backup,
        "create_snapshot",
        lambda _root: (False, "磁盘空间不足", None),
    )

    def unexpected_transition(*_args, **_kwargs):
        raise AssertionError("备份失败后不得改变 Git HEAD")

    monkeypatch.setattr(
        update_git, "fast_forward_to_fetch_head", unexpected_transition
    )

    assert update_transaction.perform_update(
        PROJECT_ROOT,
        local,
        remote,
        is_repair=False,
        yes=True,
        diff=GitDiff(["setup.py"]),
        target_commit="b" * 40,
    ) == 2


def test_apply_rejects_remote_changes_to_protected_paths(monkeypatch) -> None:
    local = update_version.VersionInfo("0.7.5", "1.0", "")
    remote = update_version.VersionInfo("0.7.6", "1.0", "")
    monkeypatch.setattr(update_checker, "check", lambda _root: (0, local, remote))
    monkeypatch.setattr(
        update_integrity, "check_source_state", lambda _root: _healthy()
    )
    monkeypatch.setattr(
        update_git,
        "get_sync_state",
        lambda _root: GitSyncState("behind", 0, 1),
    )
    monkeypatch.setattr(
        update_git,
        "get_protected_remote_diff",
        lambda _root: GitDiff(["api/keys.json"]),
    )

    def unexpected_update(*_args, **_kwargs):
        raise AssertionError("受保护路径不得进入事务")

    monkeypatch.setattr(update_cli, "perform_update", unexpected_update)
    assert update_cli.apply(PROJECT_ROOT, yes=True) == 4


def test_apply_up_to_date_never_implies_repair(monkeypatch) -> None:
    current = update_version.VersionInfo("0.7.5", "1.0", "")
    monkeypatch.setattr(update_checker, "check", lambda _root: (0, current, current))
    monkeypatch.setattr(
        update_integrity, "check_source_state", lambda _root: _healthy()
    )
    monkeypatch.setattr(
        update_git,
        "get_sync_state",
        lambda _root: GitSyncState("up_to_date", 0, 0),
    )

    def unexpected_update(*_args, **_kwargs):
        raise AssertionError("无更新时不得进入事务")

    monkeypatch.setattr(update_cli, "perform_update", unexpected_update)
    assert update_cli.apply(PROJECT_ROOT, yes=True) == 0


@pytest.mark.parametrize(
    "state",
    [GitSyncState("ahead", 2, 0), GitSyncState("diverged", 1, 3)],
)
def test_apply_rejects_non_fast_forward_history(monkeypatch, state) -> None:
    local = update_version.VersionInfo("0.7.5", "1.0", "")
    remote = update_version.VersionInfo("0.7.6", "1.0", "")
    monkeypatch.setattr(update_checker, "check", lambda _root: (0, local, remote))
    monkeypatch.setattr(
        update_integrity, "check_source_state", lambda _root: _healthy()
    )
    monkeypatch.setattr(update_git, "get_sync_state", lambda _root: state)
    assert update_cli.apply(PROJECT_ROOT, yes=True) == 4


def test_apply_passes_precaptured_diff_and_exact_fetch_commit(monkeypatch) -> None:
    local = update_version.VersionInfo("0.7.5", "1.0", "")
    remote = update_version.VersionInfo("0.7.6", "1.0", "")
    diff = GitDiff(["requirements.txt", "web/frontend/src/App.tsx"])
    target = "b" * 40
    captured: dict[str, object] = {}
    monkeypatch.setattr(update_checker, "check", lambda _root: (0, local, remote))
    monkeypatch.setattr(
        update_integrity, "check_source_state", lambda _root: _healthy()
    )
    monkeypatch.setattr(
        update_git,
        "get_sync_state",
        lambda _root: GitSyncState("behind", 0, 2),
    )
    monkeypatch.setattr(
        update_git, "get_protected_remote_diff", lambda _root: GitDiff([])
    )
    monkeypatch.setattr(update_git, "get_remote_diff", lambda _root: diff)
    monkeypatch.setattr(update_git, "get_fetch_commit", lambda _root: target)

    def perform(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(update_cli, "perform_update", perform)
    assert update_cli.apply(PROJECT_ROOT, yes=True) == 0
    assert captured["args"] == (PROJECT_ROOT, local, remote)
    assert captured["kwargs"] == {
        "is_repair": False,
        "yes": True,
        "diff": diff,
        "target_commit": target,
    }


def test_repair_rejects_protected_paths_before_transaction(monkeypatch) -> None:
    current = update_version.VersionInfo("0.7.5", "1.0", "")
    monkeypatch.setattr(update_checker, "check", lambda _root: (0, current, current))
    monkeypatch.setattr(
        update_git,
        "get_protected_remote_diff",
        lambda _root: GitDiff(["providers/example/secrets.json"]),
    )
    assert update_cli.repair(PROJECT_ROOT, yes=True) == 4


def test_repair_creates_recovery_ref_before_exact_reset(monkeypatch) -> None:
    local = update_version.VersionInfo("0.7.5", "1.0", "")
    remote = update_version.VersionInfo("0.7.6", "1.0", "")
    before = "a" * 40
    target = "b" * 40
    events: list[tuple[str, str]] = []
    commits = iter([before, target, target])
    monkeypatch.setattr(update_integrity, "check_source_state", lambda _root: _healthy())
    monkeypatch.setattr(
        update_integrity,
        "validate_installed_source",
        lambda *_args, **_kwargs: _healthy(),
    )
    monkeypatch.setattr(
        update_backup,
        "create_snapshot",
        lambda _root: (True, "backup-ok", "20260823-120000"),
    )
    monkeypatch.setattr(update_git, "get_current_commit", lambda _root: next(commits))
    monkeypatch.setattr(update_git, "get_stash_label", lambda: "kemo-update-test")

    def recovery(_root, *, label, commit):
        assert label == "kemo-update-test"
        events.append(("recovery", commit))
        return "refs/kemo-update/recovery/kemo-update-test"

    def reset(_root, expected_commit=None):
        events.append(("reset", expected_commit))
        return True

    monkeypatch.setattr(update_git, "create_recovery_ref", recovery)
    monkeypatch.setattr(update_git, "abort_in_progress_operations", lambda _root: None)
    monkeypatch.setattr(update_git, "hard_reset_to_fetch_head", reset)
    monkeypatch.setattr(update_transaction, "_install_deps", lambda _root: True)
    monkeypatch.setattr(update_transaction, "_build_frontend", lambda _root: True)

    assert update_transaction.perform_update(
        PROJECT_ROOT,
        local,
        remote,
        is_repair=True,
        yes=True,
        diff=GitDiff([]),
        target_commit=target,
    ) == 0
    assert events == [("recovery", before), ("reset", target)]


def test_repair_ref_failure_prevents_hard_reset(monkeypatch) -> None:
    current = update_version.VersionInfo("0.7.5", "1.0", "")
    monkeypatch.setattr(update_integrity, "check_source_state", lambda _root: _healthy())
    monkeypatch.setattr(
        update_backup,
        "create_snapshot",
        lambda _root: (True, "backup-ok", "20260823-120000"),
    )
    monkeypatch.setattr(update_git, "get_current_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(update_git, "get_stash_label", lambda: "kemo-update-test")
    monkeypatch.setattr(
        update_git, "create_recovery_ref", lambda *_args, **_kwargs: None
    )

    def unexpected_reset(*_args, **_kwargs):
        raise AssertionError("恢复引用失败后不得 reset --hard")

    monkeypatch.setattr(update_git, "hard_reset_to_fetch_head", unexpected_reset)
    assert update_transaction.perform_update(
        PROJECT_ROOT,
        current,
        current,
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
            returncode=0, stdout=output, stderr=""
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
        ["stash", "push", "--include-untracked", "-m", "kemo-update-test"]
    ]


def test_backup_restore_rejects_path_traversal(tmp_path) -> None:
    (tmp_path / ".backup").mkdir()
    (tmp_path / "outside").mkdir()
    ok, message = update_backup.restore(tmp_path, "../outside")
    assert ok is False
    assert "格式无效" in message


def test_backup_excludes_private_and_runtime_data(tmp_path) -> None:
    (tmp_path / ".env").write_text("SECRET=x", encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "keys.json").write_text("{}", encoding="utf-8")
    (tmp_path / "providers" / "demo").mkdir(parents=True)
    (tmp_path / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "providers" / "demo" / "secrets.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")

    ok, _, backup_id = update_backup.create_snapshot(tmp_path)

    assert ok and backup_id
    snapshot = tmp_path / ".backup" / backup_id
    assert (snapshot / "source.py").is_file()
    assert (snapshot / "providers" / "__init__.py").is_file()
    assert not (snapshot / ".env").exists()
    assert not (snapshot / "api" / "keys.json").exists()
    assert not (snapshot / "providers" / "demo").exists()


def test_failed_backup_removes_incomplete_staging_directory(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    monkeypatch.setattr(update_backup, "_timestamp", lambda: "20260823-120000")

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(update_backup.shutil, "copy2", fail_copy)
    ok, message, backup_id = update_backup.create_snapshot(tmp_path)
    assert ok is False
    assert backup_id is None
    assert "simulated copy failure" in message
    assert not list((tmp_path / ".backup").glob(".creating-*"))


def test_conflict_marker_scanner_reports_source_but_ignores_runtime(tmp_path) -> None:
    marker = "<<<" + "<<<< Updated upstream\n"
    (tmp_path / "start_web.py").write_text(marker + "value = 1\n", encoding="utf-8")
    runtime = tmp_path / "storage" / "daily"
    runtime.mkdir(parents=True)
    (runtime / "note.txt").write_text(marker, encoding="utf-8")

    assert update_integrity.find_conflict_markers(tmp_path) == ["start_web.py"]


def test_update_cli_exposes_repair_and_backup_restore_but_not_git_rollback() -> None:
    parser = update_cli.build_parser()
    assert parser.parse_args(["--repair"]).repair is True
    assert parser.parse_args(["--list-backups"]).list_backups is True
    assert parser.parse_args(["--restore-backup", "latest"]).restore_backup == "latest"
    with pytest.raises(SystemExit):
        parser.parse_args(["--rollback"])
    with pytest.raises(SystemExit):
        update_cli.main(["--yes"])


def test_no_argument_entry_opens_beginner_menu_and_defaults_to_update(
    monkeypatch,
) -> None:
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(
        update_cli,
        "apply",
        lambda root, yes=False: calls.append((root, yes)) or 7,
    )
    assert update_cli.main([], project_root=PROJECT_ROOT) == 7
    assert calls == [(PROJECT_ROOT, False)]


def test_beginner_menu_can_check_without_mutating_and_return_to_menu(
    monkeypatch,
) -> None:
    answers = iter(["2", "", "0"])
    checks: list[Path] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        update_cli,
        "check_command",
        lambda root: checks.append(root) or 0,
    )
    assert update_cli.main([], project_root=PROJECT_ROOT) == 0
    assert checks == [PROJECT_ROOT]


def test_beginner_menu_eof_exits_without_starting_update(monkeypatch) -> None:
    def eof(_prompt: str) -> str:
        raise EOFError

    def unexpected_update(*_args, **_kwargs):
        raise AssertionError("无输入终端不得默认执行更新")

    monkeypatch.setattr("builtins.input", eof)
    monkeypatch.setattr(update_cli, "apply", unexpected_update)
    assert update_cli.main([], project_root=PROJECT_ROOT) == 0


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("1", True), ("是", True), ("2", False), ("否", False)],
)
def test_confirm_accepts_beginner_friendly_numeric_input(
    monkeypatch, answer: str, expected: bool
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert update_ui.confirm("继续？") is expected


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
