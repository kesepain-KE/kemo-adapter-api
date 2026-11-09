import os
from pathlib import Path

import pytest

from core.yaml_config import (
    MAX_CONFIG_BYTES,
    dump_yaml,
    load_startup_environment,
    parse_yaml,
    read_yaml,
    startup_values,
)


def test_yaml_preserves_identifiers_and_round_trips_configuration() -> None:
    value = parse_yaml(
        "enabled: true\n"
        "disabled: false\n"
        "subject_id: on\n"
        "date: 2026-11-07\n"
        "timeout: 15\n"
        "headers:\n  X-Client: kemo\n"
        "prompt: |\n  第一行\n  second line\n"
    )
    assert value == {
        "enabled": True,
        "disabled": False,
        "subject_id": "on",
        "date": "2026-11-07",
        "timeout": 15,
        "headers": {"X-Client": "kemo"},
        "prompt": "第一行\nsecond line\n",
    }
    assert parse_yaml(dump_yaml(value)) == value


@pytest.mark.parametrize("text", [
    "enabled: true\nenabled: false\n",
    "1: value\n",
    "- item\n",
    "{invalid",
    "value: !!python/object/apply:os.system [echo unsafe]\n",
])
def test_yaml_rejects_ambiguous_or_unsafe_documents_without_echoing_values(text: str) -> None:
    with pytest.raises(ValueError) as error:
        parse_yaml(text)
    assert "unsafe" not in str(error.value)


def test_startup_yaml_supports_scalars_and_host_lists_with_process_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "PORT: 7531\nWEB_ACCESS_LOG: false\nWEB_TOKEN: null\n"
        "WEB_ALLOWED_HOSTS:\n  - localhost\n  - gateway.example.com\n",
        encoding="utf-8",
    )
    assert startup_values(path) == {
        "PORT": "7531",
        "WEB_ACCESS_LOG": "false",
        "WEB_TOKEN": "",
        "WEB_ALLOWED_HOSTS": "localhost,gateway.example.com",
    }
    monkeypatch.setattr(os, "environ", {"PORT": "9753"})
    load_startup_environment(path)
    assert os.environ["PORT"] == "9753"
    assert os.environ["WEB_ACCESS_LOG"] == "false"
    assert os.environ["WEB_ALLOWED_HOSTS"] == "localhost,gateway.example.com"


@pytest.mark.parametrize("text", ["host: localhost\n", "PORT: [7531]\n"])
def test_startup_yaml_rejects_invalid_keys_and_value_shapes(tmp_path: Path, text: str) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        startup_values(path)


def test_yaml_file_size_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        read_yaml(path)
