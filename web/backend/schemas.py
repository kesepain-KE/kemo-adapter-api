from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebTokenAuth(AdminModel):
    token: str = Field(min_length=1, max_length=4096)


class WebPasswordAuth(AdminModel):
    username: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=4096)


class GatewayRuntimeUpdate(AdminModel):
    expected_revision: str
    enabled: bool


class LiveControlUpdate(AdminModel):
    expected_revision: str
    highest_priority_system_prompt: str = Field(max_length=100_000)
    disabled_providers: list[str] = Field(default_factory=list)
    disabled_models: list[str] = Field(default_factory=list)


class ProviderApiUpdate(AdminModel):
    expected_revision: str
    config: dict[str, Any]
    api_key: str | None = Field(default=None, min_length=1)


class KeyModelPolicyUpdate(AdminModel):
    expected_revision: str
    allowed_models: list[str] | None = None


class RestartRequestBody(AdminModel):
    reason: str = Field(default="web console restart", min_length=1, max_length=500)
    force: bool = False
