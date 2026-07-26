from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.provider_contract import ProviderPackage
from .provider import ExampleProvider


def create_provider(settings: Mapping[str, Any]) -> ProviderPackage:
    """网关发现 Provider 包时唯一调用的工厂。"""
    return ExampleProvider.from_settings(settings)
