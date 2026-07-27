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
