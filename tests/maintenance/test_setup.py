from __future__ import annotations

import sys

import pytest

import setup as deployment_setup


def _record_steps(monkeypatch):
    steps: list[str] = []
    monkeypatch.setattr(
        deployment_setup,
        "check_environment",
        lambda **_kwargs: steps.append("check") or True,
    )
    monkeypatch.setattr(
        deployment_setup, "setup_venv", lambda: steps.append("venv") or True
    )
    monkeypatch.setattr(deployment_setup, "init_env", lambda: steps.append("env"))
    monkeypatch.setattr(
        deployment_setup, "install_dependencies", lambda: steps.append("install") or True
    )
    monkeypatch.setattr(
        deployment_setup, "build_frontend", lambda: steps.append("build") or True
    )
    return steps


def test_no_arguments_performs_complete_deployment(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py"])

    assert deployment_setup.main() == 0
    assert steps == ["check", "venv", "env", "install", "build"]


def test_check_does_not_deploy(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--check"])

    assert deployment_setup.main() == 0
    assert steps == ["check"]


def test_check_mode_never_bootstraps_node(monkeypatch) -> None:
    calls: list[bool] = []

    def check_environment(*, bootstrap_node: bool = True) -> bool:
        calls.append(bootstrap_node)
        return True

    monkeypatch.setattr(deployment_setup, "check_environment", check_environment)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--check"])

    assert deployment_setup.main() == 0
    assert calls == [False]


def test_build_frontend_runs_check_then_build(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--build-frontend"])

    assert deployment_setup.main() == 0
    assert steps == ["check", "build"]


def test_init_env_only(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--init-env"])

    assert deployment_setup.main() == 0
    assert steps == ["env"]


def test_check_cannot_be_combined_with_deployment_steps(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["setup.py", "--check", "--build-frontend"]
    )

    with pytest.raises(SystemExit) as raised:
        deployment_setup.main()

    assert raised.value.code == 2
    assert steps == []


def test_frontend_prefers_installed_pnpm(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_setup.shutil,
        "which",
        lambda name: "C:/tools/pnpm.cmd" if name == "pnpm" else None,
    )

    assert deployment_setup._pnpm_command() == ["C:/tools/pnpm.cmd"]


def test_frontend_uses_pinned_pnpm_through_npm(monkeypatch) -> None:
    commands = {
        "node": "C:/Program Files/nodejs/node.exe",
        "npm": "C:/Program Files/nodejs/npm.cmd",
    }
    monkeypatch.setattr(
        deployment_setup.shutil, "which", lambda name: commands.get(name)
    )

    assert deployment_setup._pnpm_command() == [
        "C:/Program Files/nodejs/npm.cmd",
        "exec",
        "--yes",
        "--package=pnpm@11.9.0",
        "--",
        "pnpm",
    ]


def test_existing_local_node_runtime_is_reused(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / ".runtime"
    executable_root = runtime / "node" / "bin"
    executable_root.mkdir(parents=True)
    node = executable_root / "node"
    npm = executable_root / "npm"
    node.write_text("node", encoding="utf-8")
    npm.write_text("npm", encoding="utf-8")

    monkeypatch.setattr(deployment_setup, "FRONTEND_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(deployment_setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(deployment_setup, "_check_node", lambda: None)
    monkeypatch.setattr(
        deployment_setup,
        "_install_local_node_runtime",
        lambda: pytest.fail("已有本地工具链时不得重复下载"),
    )
    monkeypatch.setenv("PATH", "")

    assert deployment_setup._ensure_node() == (str(node), str(npm))
    assert str(executable_root) in deployment_setup.os.environ["PATH"].split(
        deployment_setup.os.pathsep
    )


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "node-v22.0.0-win-x64.zip"),
        ("Windows", "ARM64", "node-v22.0.0-win-arm64.zip"),
        ("Linux", "x86_64", "node-v22.0.0-linux-x64.tar.xz"),
        ("Linux", "aarch64", "node-v22.0.0-linux-arm64.tar.xz"),
    ],
)
def test_node_archive_selection(
    monkeypatch, system: str, machine: str, expected: str
) -> None:
    monkeypatch.setattr(deployment_setup.platform, "system", lambda: system)
    monkeypatch.setattr(deployment_setup.platform, "machine", lambda: machine)

    assert deployment_setup._node_archive_name("v22.0.0") == expected


@pytest.mark.parametrize(
    "path",
    ["../escape", "folder/../../escape", "/absolute/path", "C:/absolute/path"],
)
def test_node_archive_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(RuntimeError, match="不安全路径"):
        deployment_setup._validate_archive_path(path)
