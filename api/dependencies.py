from __future__ import annotations

from fastapi import Request

from core.executor import GatewayExecutor
from core.registry import ProviderRegistry
from core.retrieval_executor import RetrievalExecutor


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_executor(request: Request) -> GatewayExecutor:
    return request.app.state.executor


def get_retrieval_executor(request: Request) -> RetrievalExecutor:
    return request.app.state.retrieval_executor
