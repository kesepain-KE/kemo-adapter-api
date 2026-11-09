"""Credential placement shared by adapters and the outbound transport."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from gateway.domain import Document


class AuthScheme(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    location: Literal["header", "query", "none"]
    name: str = ""
    prefix: str = ""

    @model_validator(mode="after")
    def valid_placement(self):
        if self.location == "none":
            if self.id != "none" or self.name or self.prefix:
                raise ValueError("无鉴权 scheme 必须使用 none 且不包含参数")
        elif not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.name):
            raise ValueError("鉴权参数名称无效")
        elif self.location == "header" and self.name.lower() in {"host", "content-length", "cookie"}:
            raise ValueError("鉴权不能覆盖 Host 或传输头")
        if "\r" in self.prefix or "\n" in self.prefix:
            raise ValueError("鉴权前缀不能含换行")
        return self


BEARER = AuthScheme(id="bearer", location="header", name="Authorization", prefix="Bearer ")
API_KEY = AuthScheme(id="api-key", location="header", name="x-api-key")
AZURE_KEY = AuthScheme(id="azure-key", location="header", name="api-key")
ANTHROPIC_KEY = AuthScheme(id="anthropic-key", location="header", name="x-api-key")
GEMINI_QUERY = AuthScheme(id="gemini-query", location="query", name="key")
GEMINI_KEY = AuthScheme(id="gemini-key", location="header", name="x-goog-api-key")
NO_AUTH = AuthScheme(id="none", location="none")
STANDARD_AUTH = (BEARER, API_KEY, AZURE_KEY, NO_AUTH)


def legacy_scheme(adapter_id: str, auth_type: str) -> str:
    # Preserve the actual old wire header, including custom Chat relays.
    if auth_type == "api-key":
        return {"anthropic-messages": "anthropic-key", "gemini": "gemini-query"}.get(adapter_id, "api-key")
    return auth_type


class RequestHeaders(dict):
    """HTTP headers plus credential query parameters, never ordinary config."""

    def __init__(self, values=(), *, query=None, secrets=()):
        super().__init__(values)
        self.query = query or {}
        self.secrets = tuple(secrets)
