"""Validate connection data once when building a runtime generation."""
from __future__ import annotations

import jsonschema

from gateway.domain import ConfigurationError, Upstream
from gateway.transport import validate_url


def validate_connection(manifest, upstream: Upstream):
    validate_url(upstream.base_url, upstream.allow_private_network)
    if upstream.proxy:
        validate_url(upstream.proxy, True, field="proxy", proxy=True)
    scheme = next((value for value in manifest.auth_schemes if value.id == upstream.auth_scheme), None)
    if scheme is None:
        raise ConfigurationError("AUTH_SCHEME_UNAVAILABLE", "auth_scheme", "所选适配器不支持此鉴权方式")
    header_names = {key.lower() for key in upstream.headers}
    for auth in manifest.auth_schemes:
        if auth.location == "header" and auth.name.lower() in header_names:
            raise ConfigurationError("AUTH_HEADER_FORBIDDEN", "headers", "认证头只能通过加密凭据注入")
    if any("\r" in key + value or "\n" in key + value for key, value in manifest.fixed_headers.items()):
        raise ConfigurationError("INVALID_HEADER", "headers", "固定请求头不能包含换行")
    extra_body = upstream.profile.get("extra_body", {})
    extra_query = upstream.profile.get("extra_query", {})
    if not isinstance(extra_body, dict):
        raise ConfigurationError("INVALID_EXTRA_BODY", "profile.extra_body", "extra_body 必须是对象")
    if not isinstance(extra_query, dict) or any(
        not isinstance(key, str) or not isinstance(value, (str, int, float, bool)) or value is None
        for key, value in extra_query.items()
    ):
        raise ConfigurationError("INVALID_EXTRA_QUERY", "profile.extra_query", "extra_query 必须是简单查询参数对象")
    protected = set(manifest.body_fields) | {"model", "messages", "input", "stream", "tools", "tool_choice"}
    conflicts = sorted(protected & extra_body.keys())
    if conflicts:
        raise ConfigurationError("EXTRA_BODY_CONFLICT", "profile.extra_body",
                                 "extra_body 与适配器字段冲突: " + ", ".join(conflicts), keys=conflicts)
    auth_names = {"key", "api_key", "api-key", "access_token", "token"}
    auth_names.update(auth.name.lower() for auth in manifest.auth_schemes if auth.location == "query")
    conflicts = sorted(key for key in extra_query if key.lower() in auth_names or key.lower() in {"alt", "stream"})
    if conflicts:
        raise ConfigurationError("EXTRA_QUERY_CONFLICT", "profile.extra_query",
                                 "extra_query 不能覆盖鉴权或协议参数: " + ", ".join(conflicts), keys=conflicts)
    jsonschema.validate({key: value for key, value in upstream.profile.items()
                         if key not in {"extra_body", "extra_query"}}, manifest.config_schema)
