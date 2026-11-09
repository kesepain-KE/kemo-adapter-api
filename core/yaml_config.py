"""YAML configuration at filesystem boundaries; HTTP and runtime records stay JSON."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


MAX_CONFIG_BYTES = 1024 * 1024


class ConfigLoader(yaml.SafeLoader):
    """Keep identifiers such as 'on' and dates as strings, and reject duplicate keys."""

    yaml_implicit_resolvers = {
        key: [
            (tag, pattern) for tag, pattern in resolvers
            if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
        ]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("YAML configuration requires unique string keys")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"),
    list("tTfF"),
)


def parse_yaml(text: str) -> dict[str, Any]:
    try:
        value = yaml.load(text, Loader=ConfigLoader)
    except yaml.YAMLError:
        raise ValueError("Invalid YAML configuration") from None
    if not isinstance(value, dict):
        raise ValueError("YAML configuration must be a mapping")
    return value


def read_yaml(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"Configuration exceeds {MAX_CONFIG_BYTES} bytes: {path.name}")
    return parse_yaml(path.read_text(encoding="utf-8-sig"))


def dump_yaml(value: dict[str, Any]) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def startup_values(path: Path) -> dict[str, str]:
    """Convert startup YAML scalars to process values for existing deployment overrides."""
    if not path.exists():
        return {}
    result = {}
    for name, value in read_yaml(path).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("Startup configuration keys must use UPPER_SNAKE_CASE")
        if value is None:
            result[name] = ""
        elif isinstance(value, bool):
            result[name] = str(value).lower()
        elif isinstance(value, (str, int, float)):
            result[name] = str(value)
        elif name == "WEB_ALLOWED_HOSTS" and isinstance(value, list) and all(
            isinstance(item, str) for item in value
        ):
            result[name] = ",".join(value)
        else:
            raise ValueError(f"Invalid startup configuration type: {name}")
    return result


def load_startup_environment(path: Path, *, override: bool = False) -> None:
    for name, value in startup_values(path).items():
        if override or name not in os.environ:
            os.environ[name] = value
