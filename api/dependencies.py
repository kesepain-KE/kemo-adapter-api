from __future__ import annotations

from fastapi import Request

from core.executor import GatewayExecutor
from core.registry import ProviderRegistry


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_executor(request: Request) -> GatewayExecutor:
    return request.app.state.executor
