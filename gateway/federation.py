"""Request-local federation hop limit and local gateway address checks."""
from __future__ import annotations

from contextvars import ContextVar
import ipaddress
import json
from urllib.parse import urlsplit

from gateway.domain import ConfigurationError


HOPS_HEADER = "X-Kemo-Federation-Hops"
MAX_HOPS = 2
current_hops = ContextVar("kemo_federation_hops", default=0)


class FederationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(("/model/", "/assets")):
            return await self.app(scope, receive, send)
        values = [value for name, value in scope["headers"] if name.lower() == HOPS_HEADER.lower().encode()]
        raw = values[0] if values else b"0"
        if len(values) > 1 or raw not in (b"0", b"1", b"2"):
            body = json.dumps({
                "protocol_version": "2.0", "object": "kemo.error",
                "error": {"type": "federation", "code": "PROTOCOL_ERROR",
                          "message": "网关联邦跳数无效或超过上限", "retryable": False, "details": {}},
            }, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 400,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        token = current_hops.set(int(raw))
        try:
            await self.app(scope, receive, send)
        finally:
            current_hops.reset(token)


def validate_self_target(base_url, gateway_urls):
    target = urlsplit(base_url)

    def host(value):
        name = value.hostname.lower()
        if name in {"localhost", "localhost.localdomain"}:
            return "loopback"
        try:
            address = ipaddress.ip_address(name)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if address.is_loopback or address.is_unspecified:
                return "loopback"
        except ValueError:
            pass
        return name

    def port(value):
        return value.port or (443 if value.scheme == "https" else 80)

    for url in gateway_urls:
        local = urlsplit(url)
        if host(target) == host(local) and port(target) == port(local):
            raise ConfigurationError("UPSTREAM_SELF_REFERENCE", "base_url", "上游不能指向当前网关自身")
