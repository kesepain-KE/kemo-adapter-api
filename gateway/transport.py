from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import socket
import re
from urllib.parse import urlsplit

import httpx

from core.models import ErrorObject
from core.provider_contract import ProviderException
from gateway.tracing import current_trace
from gateway.domain import ConfigurationError


def upstream_error(status: int | None = None, code="UPSTREAM_ERROR", *, retryable: bool | None = None) -> ProviderException:
    wire_code = ("RATE_LIMITED" if status == 429 else
                 "PROVIDER_TIMEOUT" if code == "UPSTREAM_TIMEOUT" else
                 "PROVIDER_UNAVAILABLE" if status in {500, 502, 503, 504} or code in {
                     "NO_HEALTHY_DEPLOYMENT", "NO_UPSTREAM_CREDENTIAL", "UPSTREAM_UNAVAILABLE",
                 } else
                 "PROVIDER_BAD_RESPONSE")
    message = {"NO_UPSTREAM_CREDENTIAL": "没有可用的上游凭据",
               "NO_HEALTHY_DEPLOYMENT": "没有可用的模型路由"}.get(
                   code, "上游请求失败" if status else "上游连接失败",
               )
    return ProviderException(ErrorObject(
        type="rate_limit_error" if status == 429 else "upstream_error",
        code=wire_code, message=message,
        retryable=(status in {408, 429, 500, 502, 503, 504} or status is None) if retryable is None else retryable,
        provider_status=status,
        details={"reason": code},
    ))


class ResolutionError(ValueError):
    pass


def validate_url(base_url: str, allow_private: bool, *, field="base_url", proxy=False):
    placeholders = re.findall(r"\{([^{}]+)\}", base_url)
    if placeholders:
        raise ConfigurationError("TEMPLATE_PLACEHOLDER_UNRESOLVED", field,
                                 "请替换地址占位符: " + ", ".join(placeholders), placeholders=placeholders)
    try:
        parsed = urlsplit(base_url)
        parsed.port
    except ValueError:
        raise ConfigurationError("INVALID_URL", field, "HTTP(S) 地址格式无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ConfigurationError("INVALID_URL", field, "地址必须是无凭据、查询参数和 fragment 的 HTTP(S) 地址")
    if proxy and parsed.path not in {"", "/"}:
        raise ConfigurationError("INVALID_PROXY", field, "代理地址不能包含路径")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"} and not allow_private:
            raise ConfigurationError("PRIVATE_NETWORK_DISABLED", field, "本地服务需显式允许私网") from None
    else:
        check_address(address, allow_private, field=field)
    return parsed


def check_address(address, allow_private, *, field="base_url"):
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (address.is_link_local or address.is_multicast or address.is_unspecified
            or (address.is_reserved and not address.is_loopback) or str(address) == "100.100.100.200"
            or (not address.is_global and not allow_private)):
        raise ConfigurationError("UNSAFE_UPSTREAM_ADDRESS", field,
                                 "上游 IP 不在允许的网络范围；本地服务需显式允许私网")


async def safe_addresses(base_url: str, allow_private: bool) -> tuple[str, ...]:
    parsed = validate_url(base_url, allow_private)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        async with asyncio.timeout(10):
            records = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname, port, type=socket.SOCK_STREAM,
            )
    except (OSError, ValueError, TimeoutError):
        raise ResolutionError("无法解析上游地址") from None
    addresses = tuple(dict.fromkeys(row[4][0] for row in records))
    if not addresses:
        raise ResolutionError("上游地址没有可用的 IP")
    for text in addresses:
        check_address(ipaddress.ip_address(text), allow_private)
    return addresses


class Transport:
    def __init__(self, *, http_transport: httpx.AsyncBaseTransport | None = None,
                 json_max_bytes: int = 32 * 1024 * 1024):
        self.client = httpx.AsyncClient(
            transport=http_transport or httpx.AsyncHTTPTransport(retries=0),
            trust_env=False, follow_redirects=False,
        )
        self._test_transport = http_transport
        self.json_max_bytes = json_max_bytes
        self._proxy_clients = {}

    @asynccontextmanager
    async def response(self, upstream, path, headers, *, body=None, method="POST", files=None):
        base_url = upstream.base_url.rstrip("/")
        if not path.startswith("/") or "#" in path or ".." in path:
            raise ValueError("适配器端点无效")
        url = httpx.URL(base_url + path)
        profile = upstream.profile
        if body is not None:
            extra_body = profile.get("extra_body", {})
            if set(extra_body) & set(body):
                raise ConfigurationError("EXTRA_BODY_CONFLICT", "profile.extra_body", "extra_body 与请求字段冲突")
            body = {**body, **extra_body}
        extra_query = dict(profile.get("extra_query", {}))
        if set(extra_query) & set(url.params):
            raise ConfigurationError("EXTRA_QUERY_CONFLICT", "profile.extra_query", "extra_query 与请求参数冲突")
        url = url.copy_merge_params(extra_query).copy_merge_params(getattr(headers, "query", {}))
        trace = current_trace.get()
        if trace is not None and not trace.capture_details:
            trace = None
        exchange = None
        if trace is not None:
            exchange = {"method": method, "path": path, "request": body, "status": None, "response": ""}
            if files is not None:
                exchange["files"] = [{"field": name, "filename": value[0], "mime_type": value[2]}
                                     for name, value in files]
            trace.exchanges.append(exchange)
        phase = "resolve"
        try:
            client = self.client
            if self._test_transport is None:
                async with asyncio.timeout(min(upstream.timeout, 15)):
                    addresses = await safe_addresses(base_url, upstream.allow_private_network)
                # Connect to the checked address, retaining TLS SNI and the HTTP Host.
                # DNS is not consulted a second time by httpcore.
                host_header = url.netloc.decode()
                extensions = {"sni_hostname": url.host}
                url = url.copy_with(host=addresses[0])
                proxy = getattr(upstream, "proxy", None)
                if proxy:
                    # Validate the target and pin its checked IP even through a
                    # proxy. The proxy is an explicitly configured egress hop;
                    # it may be local but never metadata/link-local.
                    await safe_addresses(proxy, True)
                    cache_key = (urlsplit(base_url).netloc, proxy)
                    if cache_key not in self._proxy_clients:
                        self._proxy_clients[cache_key] = httpx.AsyncClient(
                            proxy=proxy, trust_env=False, follow_redirects=False,
                        )
                    client = self._proxy_clients[cache_key]
            else:
                host_header, extensions = url.netloc.decode(), {}
            payload = {"json": body}
            request_headers = {**headers, "Host": host_header}
            if files is not None:
                request_headers = {key: value for key, value in request_headers.items() if key.lower() != "content-type"}
                payload = {"data": {key: value if isinstance(value, str) else json.dumps(value)
                                    for key, value in (body or {}).items()}, "files": files}
            phase = "response_headers"
            async with client.stream(
                method, url, headers=request_headers, **payload,
                extensions=extensions, timeout=httpx.Timeout(upstream.timeout, connect=15.0),
            ) as response:
                if exchange is not None:
                    exchange["status"] = response.status_code
                phase = "response_body"
                if response.status_code >= 300:
                    error = upstream_error(response.status_code)
                    content = bytearray()
                    complete = True
                    try:
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) >= (8 * 1024 * 1024 if trace is not None else 4096):
                                complete = False
                                break
                    except Exception:
                        # A broken error body must not replace the HTTP status.
                        complete = False
                    error.error.details = {
                        **(error.error.details or {}),
                        "upstream_body": content[:4096].decode("utf-8", errors="replace"),
                    }
                    if exchange is not None:
                        exchange["response"] = content.decode("utf-8", errors="replace")
                        exchange["complete"] = complete
                    raise error
                yield response
        except (httpx.HTTPError, TimeoutError, ResolutionError) as exc:
            reason = ("UPSTREAM_TIMEOUT" if isinstance(exc, (httpx.TimeoutException, TimeoutError))
                      else "UPSTREAM_UNAVAILABLE" if isinstance(exc, (ResolutionError, httpx.ConnectError))
                      else "UPSTREAM_ERROR")
            error = upstream_error(code=reason)
            # Exception messages can contain authenticated URLs. Keep only safe metadata.
            error.error.details.update(transport_exception=type(exc).__name__, transport_phase=phase)
            raise error from None

    async def json(self, upstream, path, headers, *, body=None, method="POST", files=None):
        async with self.response(upstream, path, headers, body=body, method=method, files=files) as response:
            content = bytearray()
            trace = current_trace.get()
            if trace is not None and not trace.capture_details:
                trace = None
            if trace is not None:
                exchange = trace.exchanges[-1]
                exchange["complete"] = False
            try:
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.json_max_bytes:
                        raise upstream_error(code="UPSTREAM_RESPONSE_TOO_LARGE")
                if trace is not None:
                    exchange["complete"] = True
            finally:
                if trace is not None:
                    exchange["response"] = content.decode("utf-8", errors="replace")
            try:
                value = json.loads(content)
            except (ValueError, UnicodeError):
                raise upstream_error(code="UPSTREAM_INVALID_JSON") from None
            if not isinstance(value, dict):
                raise upstream_error(code="UPSTREAM_INVALID_JSON")
            return value

    async def events(self, upstream, path, headers, body):
        async with self.response(upstream, path, headers, body=body) as response:
            buffer = b""
            trace = current_trace.get()
            if trace is not None and not trace.capture_details:
                trace = None
            chunks = []
            if trace is not None:
                exchange = trace.exchanges[-1]
                exchange["complete"] = False
                trace.stream_bodies.append((exchange, chunks))
            try:
                async for chunk in response.aiter_bytes():
                    if trace is not None:
                        chunks.append(chunk)
                    buffer += chunk
                    if len(buffer) > 1024 * 1024:
                        raise upstream_error(code="UPSTREAM_EVENT_TOO_LARGE")
                    while b"\n\n" in buffer or b"\r\n\r\n" in buffer:
                        separator = b"\r\n\r\n" if b"\r\n\r\n" in buffer and (
                            b"\n\n" not in buffer or buffer.index(b"\r\n\r\n") < buffer.index(b"\n\n")
                        ) else b"\n\n"
                        frame, buffer = buffer.split(separator, 1)
                        lines = frame.decode("utf-8").splitlines()
                        data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
                        if not data or data == "[DONE]":
                            continue
                        try:
                            value = json.loads(data)
                        except ValueError:
                            raise upstream_error(code="UPSTREAM_INVALID_EVENT") from None
                        if not isinstance(value, dict):
                            raise upstream_error(code="UPSTREAM_INVALID_EVENT")
                        yield value
                if buffer.strip() and not buffer.lstrip().startswith(b":"):
                    raise upstream_error(code="UPSTREAM_TRUNCATED_EVENT")
                if trace is not None:
                    exchange["complete"] = True
            finally:
                if trace is not None:
                    exchange["response"] = b"".join(chunks).decode("utf-8", errors="replace")

    async def close(self):
        await asyncio.gather(*(client.aclose() for client in self._proxy_clients.values()))
        await self.client.aclose()
