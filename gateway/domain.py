from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.models import ModelCapabilities


TokenizerName = Literal[
    "auto", "deepseek", "openai", "qwen", "glm", "minimax", "mistral", "hunyuan",
    "ernie", "doubao", "stepfun", "yi", "mimo", "sensenova", "baichuan", "internlm",
    "cohere", "gemini", "grok", "ai21-j2", "llama", "claude-legacy", "kimi",
]


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigurationError(ValueError):
    def __init__(self, code: str, field: str, message: str, **details):
        super().__init__(message)
        self.code, self.field, self.details = code, field, details


class Credential(Document):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    encrypted: str
    preview: str
    enabled: bool = True
    priority: int = Field(default=0, ge=-1000, le=1000)


class Upstream(Document):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=120)
    adapter_id: str
    base_url: str = Field(max_length=2048)
    timeout: float = Field(default=120, gt=0, le=7200)
    enabled: bool = True
    allow_private_network: bool = False
    auth_scheme: str = "bearer"
    headers: dict[str, str] = Field(default_factory=dict)
    credentials: tuple[Credential, ...] = ()
    profile: dict[str, Any] = Field(default_factory=dict)
    proxy: str | None = Field(default=None, max_length=2048)
    note: str = ""
    template_id: str | None = None
    tokenizer: TokenizerName = "auto"
    discovered_models: tuple[str, ...] = ()
    discovered_at: str | None = None

    @field_validator("headers")
    @classmethod
    def check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        forbidden = {"authorization", "api-key", "x-api-key", "x-goog-api-key", "proxy-authorization",
                     "host", "content-length", "cookie"}
        if any(k.lower() in forbidden or "\n" in k + v or "\r" in k + v for k, v in value.items()):
            raise ValueError("凭据请使用密钥字段；不允许覆盖认证、Host 或传输头")
        return value

    @model_validator(mode="after")
    def unique_credentials(self):
        if len({k.id for k in self.credentials}) != len(self.credentials):
            raise ValueError("上游密钥 ID 重复")
        return self


class PublicModel(Document):
    id: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\s]+$")
    display_name: str = ""
    task: Literal["llm", "embedding", "rerank"] = "llm"
    enabled: bool = True
    verification: Literal["verified", "declared", "unknown", "unsupported"] = "declared"
    evidence: str = "operator declaration"
    verified_at: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def order_reasoning_efforts(cls, value: dict[str, Any]) -> dict[str, Any]:
        reasoning = value.get("reasoning")
        if isinstance(reasoning, dict) and isinstance(reasoning.get("efforts"), list):
            order = ("minimal", "low", "medium", "high", "xhigh", "max")
            efforts = sorted(reasoning["efforts"], key=lambda effort: (
                order.index(effort) if effort in order else len(order)
            ))
            return {**value, "reasoning": {**reasoning, "efforts": efforts}}
        return value

    def declaration(self) -> ModelCapabilities:
        return ModelCapabilities.model_validate({
            **self.capabilities,
            "protocol_version": "2.0",
            "model": self.id,
            "provider_id": "gateway",
            "provider_model": self.id,
            "task": self.task,
        })


class Price(Document):
    input_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cached_input_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class Deployment(Document):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    upstream_id: str
    upstream_model: str = Field(min_length=1, max_length=300)
    public_model: str
    priority: int = Field(default=0, ge=-1000, le=1000)
    weight: int = Field(default=1, ge=1, le=10000)
    enabled: bool = True
    rpm: int = Field(default=0, ge=0)
    price: Price = Field(default_factory=Price)


class RuntimeOptions(Document):
    enabled: bool = True
    system_prompt: str = Field(default="", max_length=100000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    cooldown_seconds: int = Field(default=30, ge=1, le=3600)


class ExtensionState(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    kind: Literal["protocol_adapters", "provider_templates"]
    enabled: bool = True
    digest: str = ""


class Catalog(Document):
    upstreams: tuple[Upstream, ...] = ()
    models: tuple[PublicModel, ...] = ()
    deployments: tuple[Deployment, ...] = ()
    extensions: tuple[ExtensionState, ...] = ()
    settings: RuntimeOptions = Field(default_factory=RuntimeOptions)

    @model_validator(mode="after")
    def references(self):
        for items in (self.upstreams, self.models, self.deployments):
            if len({item.id for item in items}) != len(items):
                raise ValueError("目录中存在重复 ID")
        upstreams = {item.id for item in self.upstreams}
        models = {item.id for item in self.models}
        if len({(item.kind, item.id) for item in self.extensions}) != len(self.extensions):
            raise ValueError("扩展 ID 重复")
        identities = set()
        for d in self.deployments:
            if d.upstream_id not in upstreams or d.public_model not in models:
                raise ValueError(f"Deployment {d.id} 引用了不存在的上游或公开模型")
            identity = (d.public_model, d.upstream_id, d.upstream_model)
            if identity in identities:
                raise ValueError("同一公开模型不能重复绑定相同的上游模型")
            identities.add(identity)
        for model in self.models:
            model.declaration()
        return self
