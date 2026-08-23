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


class ProviderKeyItem(AdminModel):
    key_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    api_key: str = Field(min_length=1, max_length=8192)
    enabled: bool = True


class ProviderKeysUpdate(AdminModel):
    expected_revision: str
    keys: list[ProviderKeyItem] = Field(min_length=1, max_length=128)


class ProviderKeyAppend(AdminModel):
    """Append one upstream secret without requiring the client to echo the pool."""

    expected_revision: str
    api_key: str = Field(min_length=1, max_length=8192)


class ProviderKeyDelete(AdminModel):
    """Delete one upstream key while retaining at least one pool entry."""

    expected_revision: str


class KeyModelPolicyUpdate(AdminModel):
    expected_revision: str
    allowed_models: list[str] | None = None


class RestartRequestBody(AdminModel):
    reason: str = Field(default="web console restart", min_length=1, max_length=500)
    force: bool = False
