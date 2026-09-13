"""测试自身的边界：套件不能漏收，共享支持层不能依赖测试用例。"""
from __future__ import annotations

import ast
import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import runner
from tests.suites import DEFAULT_SUITES, SUITES, suite_files
from tests.support.paths import PROJECT_ROOT


def test_default_suite_covers_every_source_test_and_the_provider_template() -> None:
    expected = {
        path for path in (PROJECT_ROOT / "tests").rglob("test_*.py")
        if path.relative_to(PROJECT_ROOT / "tests").parts[0] not in {"integration", "local_providers"}
    }
    expected.add(PROJECT_ROOT / "template/provider/test_contract.py")
    assert set(suite_files(["all"])) == expected
    assert set(suite_files(list(DEFAULT_SUITES))) == expected
    assert not any(path.is_relative_to(PROJECT_ROOT / "providers") for path in expected)


def test_pytest_default_paths_match_the_single_entry() -> None:
    config = configparser.ConfigParser()
    config.read(PROJECT_ROOT / "pytest.ini", encoding="utf-8")
    assert set(config["pytest"]["testpaths"].split()) == set(SUITES["all"].paths)


def test_suite_combinations_never_duplicate_test_files() -> None:
    assert suite_files(["all", "version", "update", "all"]) == suite_files(["all"])


def test_missing_source_test_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        suite_files(["api"], root=tmp_path)


def test_test_modules_do_not_import_other_test_modules() -> None:
    for path in (PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
                local = bool(node.level)
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                local = False
            else:
                continue
            for name in names:
                if name.startswith("tests.") or local:
                    assert not any(part.startswith("test_") for part in name.split(".")), f"{path.name}: {name}"


def test_support_layer_has_no_pytest_or_test_case_dependency_and_no_cycles() -> None:
    graph: dict[str, set[str]] = {}
    for path in (PROJECT_ROOT / "tests/support").glob("*.py"):
        module = f"tests.support.{path.stem}"
        dependencies: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                assert not name.startswith("pytest"), f"{module}: {name}"
                if name.startswith("tests."):
                    assert name.startswith("tests.support."), f"{module}: {name}"
                    dependencies.add(name)
        graph[module] = dependencies

    def visit(module: str, ancestors: frozenset[str]) -> None:
        assert module not in ancestors, f"共享测试支持层出现循环：{module}"
        for dependency in graph.get(module, set()):
            visit(dependency, ancestors | {module})

    for module in graph:
        visit(module, frozenset())


def test_list_suites_needs_no_pytest_or_test_imports(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda _: pytest.fail("列表不应加载 pytest"))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("列表不应启动进程"))
    assert runner.main(["--list"]) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in SUITES)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 4, 5])
def test_entry_preserves_pytest_exit_codes_and_passes_arguments(monkeypatch, exit_code: int) -> None:
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setenv("KEMO_RUN_RESTART_E2E", "1")
    assert runner.main(["--suite", "protocol", "--", "-q", "-k", "tool"]) == exit_code
    assert captured["command"][:3] == [runner.sys.executable, "-m", "pytest"]
    assert captured["command"][-3:] == ["-q", "-k", "tool"]
    assert captured["cwd"] == PROJECT_ROOT
    assert captured["check"] is False
    assert captured["env"]["KEMO_RUN_RESTART_E2E"] == "0"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "shell" not in captured


def test_restart_e2e_requires_explicit_suite_selection(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: captured.update(k) or SimpleNamespace(returncode=0))
    assert runner.main(["--suite", "restart-e2e", "--collect-only", "-q"]) == 0
    assert captured["env"]["KEMO_RUN_RESTART_E2E"] == "1"


def test_missing_pytest_has_a_non_successful_exit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda _: None)
    assert runner.main([]) == 2
    assert "pytest" in capsys.readouterr().err


def test_unknown_suite_fails_before_starting_tests() -> None:
    with pytest.raises(SystemExit) as result:
        runner.main(["--suite", "not-a-suite"])
    assert result.value.code == 2


def test_user_interrupt_is_not_reported_as_success(monkeypatch) -> None:
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.subprocess, "run", interrupt)
    assert runner.main(["--suite", "version"]) == 130
