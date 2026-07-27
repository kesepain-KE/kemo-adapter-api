from __future__ import annotations

import sys

import pytest

import setup as deployment_setup


def _record_steps(monkeypatch):
    steps: list[str] = []
    monkeypatch.setattr(
        deployment_setup, "install_dependencies", lambda: steps.append("install")
    )
    monkeypatch.setattr(deployment_setup, "build_frontend", lambda: steps.append("build"))
    monkeypatch.setattr(deployment_setup, "initialize_env", lambda: steps.append("env"))

    def check() -> bool:
        steps.append("check")
        return True

    monkeypatch.setattr(deployment_setup, "check_environment", check)
    return steps


def test_no_arguments_performs_complete_deployment(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py"])

    assert deployment_setup.main() == 0
    assert steps == ["install", "build", "env", "check"]


def test_check_does_not_install_or_build(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--check"])

    assert deployment_setup.main() == 0
    assert steps == ["check"]


def test_explicit_action_only_runs_requested_step(monkeypatch) -> None:
    steps = _record_steps(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--build-frontend"])

    assert deployment_setup.main() == 0
    assert steps == ["build", "check"]


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

    assert deployment_setup._frontend_package_command() == ["C:/tools/pnpm.cmd"]


def test_frontend_uses_pinned_pnpm_through_npm(monkeypatch) -> None:
    commands = {
        "node": "C:/Program Files/nodejs/node.exe",
        "npm": "C:/Program Files/nodejs/npm.cmd",
    }
    monkeypatch.setattr(
        deployment_setup.shutil, "which", lambda name: commands.get(name)
    )

    assert deployment_setup._frontend_package_command() == [
        "C:/Program Files/nodejs/npm.cmd",
        "exec",
        "--yes",
        "--package=pnpm@11.9.0",
        "--",
        "pnpm",
    ]


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
