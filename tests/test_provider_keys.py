from __future__ import annotations

import asyncio

import pytest

from core.models import ErrorObject
from core.provider_contract import ProviderException
from core.provider_contract import ModelCapabilities, ProviderPackage, ProviderResult
from core.provider_keys import (
    ProviderKeyRouter,
    RoutedProviderPackage,
    is_key_specific_failure,
    normalize_provider_keys,
    preview_provider_key,
)


class FakeClient:
    def __init__(self, key: str, failures: int = 0) -> None:
        self.key = key
        self.failures = failures
        self.calls = 0

    async def create(self, payload: object) -> dict[str, object]:
        del payload
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise ProviderException(ErrorObject(
                type="provider_error", code="RATE_LIMITED", message="quota exhausted",
                retryable=True, provider_status=429,
            ))
        return {"key": self.key}

    async def close(self) -> None:
        return None


class FakeStreamClient:
    def __init__(self, key: str, *, fail: bool) -> None:
        self.key = key
        self.fail = fail

    async def stream(self, payload: object):
        del payload
        if self.fail:
            raise ProviderException(ErrorObject(
                type="provider_error", code="RATE_LIMITED", message="quota exhausted",
                retryable=True, provider_status=429,
            ))
        yield {"key": self.key, "delta": "ok"}

    async def close(self) -> None:
        return None


class CancellableClient:
    def __init__(self, key: str) -> None:
        self.key = key
        self.cancelled: list[str] = []

    async def create(self, payload: object) -> dict[str, object]:
        del payload
        return {"id": f"resp-{self.key}"}

    async def stream(self, payload: object):
        del payload
        yield {"id": f"stream-{self.key}", "delta": "ok"}

    async def cancel(self, provider_response_id: str) -> None:
        self.cancelled.append(provider_response_id)

    async def close(self) -> None:
        return None


class FakePackage(ProviderPackage):
    provider_id = "fake"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"fake-model"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        del model
        raise NotImplementedError


class ReloadableFakePackage(FakePackage):
    def __init__(self, secret: str) -> None:
        self.secret = secret

    async def execute(self, request: object, context: object) -> ProviderResult:
        del request, context
        return ProviderResult(status="completed", metadata={"key": self.secret})

    async def close(self) -> None:
        return None


class BlockingClient:
    def __init__(self, key: str) -> None:
        self.key = key
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def create(self, payload: object) -> dict[str, object]:
        del payload
        self.started.set()
        await self.release.wait()
        if self.closed:
            raise AssertionError("client closed before the in-flight call completed")
        return {"key": self.key}

    async def close(self) -> None:
        self.closed = True


class ConcurrentClient:
    def __init__(self, key: str, release: asyncio.Event) -> None:
        self.key = key
        self.release = release
        self.calls = 0

    async def create(self, payload: object) -> dict[str, object]:
        del payload
        self.calls += 1
        await self.release.wait()
        return {"key": self.key}

    async def close(self) -> None:
        return None


class BlockingPackage(FakePackage):
    def __init__(self, key: str) -> None:
        self.key = key
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def execute(self, request: object, context: object) -> ProviderResult:
        del request, context
        self.started.set()
        await self.release.wait()
        if self.closed:
            raise AssertionError("package closed before the in-flight call completed")
        return ProviderResult(status="completed", metadata={"key": self.key})

    async def close(self) -> None:
        self.closed = True


class ConcurrentPackage(FakePackage):
    def __init__(self, key: str, release: asyncio.Event) -> None:
        self.key = key
        self.release = release
        self.calls = 0

    async def execute(self, request: object, context: object) -> ProviderResult:
        del request, context
        self.calls += 1
        await self.release.wait()
        return ProviderResult(status="completed", metadata={"key": self.key})

    async def close(self) -> None:
        return None


class FakeStreamPackage(FakePackage):
    def __init__(self, key: str, *, fail: bool) -> None:
        self.key = key
        self.fail = fail

    def stream(self, request: object, context: object):
        del request, context

        async def generate():
            if self.fail:
                raise ProviderException(ErrorObject(
                    type="provider_error", code="RATE_LIMITED", message="quota exhausted",
                    retryable=True, provider_status=429,
                ))
            yield {"key": self.key, "delta": "ok"}

        return generate()


class CancellablePackage(FakePackage):
    def __init__(self, key: str) -> None:
        self.key = key
        self.cancelled: list[str] = []

    async def execute(self, request: object, context: object) -> ProviderResult:
        del request, context
        return ProviderResult(
            status="completed",
            provider_response_id=f"resp-{self.key}",
        )

    async def cancel(self, provider_response_id: str | None, context: object) -> None:
        del context
        if provider_response_id:
            self.cancelled.append(provider_response_id)

    async def close(self) -> None:
        return None


def test_normalize_provider_keys_keeps_legacy_compatibility() -> None:
    assert normalize_provider_keys({"api_key": "legacy", "api_key_id": "old"}) == [("old", "legacy", True)]
    assert normalize_provider_keys({"api_keys": {"a": {"api_key": "A"}, "b": {"key": "B", "enabled": False}}}) == [
        ("a", "A", True), ("b", "B", False)
    ]


def test_explicit_empty_pool_does_not_resurrect_legacy_key() -> None:
    assert normalize_provider_keys(
        {"api_keys": [], "api_key": "stale-legacy-secret"}
    ) == []


def test_normalize_provider_keys_rejects_string_enabled_flags() -> None:
    try:
        normalize_provider_keys({"api_keys": [{"key_id": "a", "api_key": "A", "enabled": "false"}]})
    except ValueError as exc:
        assert "布尔值" in str(exc)
    else:
        raise AssertionError("string enabled flags must not be coerced to true")


def test_router_fails_over_on_quota_error_without_exposing_secret() -> None:
    clients: dict[str, FakeClient] = {}

    def factory(secret: str) -> FakeClient:
        client = FakeClient(secret, failures=1 if secret == "A" else 0)
        clients[secret] = client
        return client

    router = ProviderKeyRouter.build(
        "fake", {"api_keys": [{"key_id": "a", "api_key": "A"}, {"key_id": "b", "api_key": "B"}]}, factory
    )

    async def scenario() -> None:
        result = await router.call("create", {})
        assert result == {"key": "B"}
        statuses = router.statuses()
        assert statuses[0]["status"] == "cooldown"
        assert all("api_key" not in item and item["key_id"] in {"a", "b"} for item in statuses)
        await router.close()

    asyncio.run(scenario())


def test_router_cancel_uses_client_that_created_response() -> None:
    clients: dict[str, CancellableClient] = {}

    def factory(secret: str) -> CancellableClient:
        clients[secret] = CancellableClient(secret)
        return clients[secret]

    router = ProviderKeyRouter.build(
        "fake",
        {"api_keys": [
            {"key_id": "a", "api_key": "A"},
            {"key_id": "b", "api_key": "B"},
        ]},
        factory,
    )

    async def scenario() -> None:
        await router.call("create", {})
        # The successful A call advances the cursor to B.  Cancellation must
        # still target A because A created resp-A.
        await router.cancel("resp-A")
        assert clients["A"].cancelled == ["resp-A"]
        assert clients["B"].cancelled == []
        await router.close()

    asyncio.run(scenario())


def test_router_cancel_tracks_response_id_seen_during_stream() -> None:
    clients: dict[str, CancellableClient] = {}

    def factory(secret: str) -> CancellableClient:
        clients[secret] = CancellableClient(secret)
        return clients[secret]

    router = ProviderKeyRouter.build(
        "fake",
        {"api_keys": [
            {"key_id": "a", "api_key": "A"},
            {"key_id": "b", "api_key": "B"},
        ]},
        factory,
    )

    async def scenario() -> None:
        events = [item async for item in router.stream({})]
        assert events == [{"id": "stream-A", "delta": "ok"}]
        await router.cancel("stream-A")
        assert clients["A"].cancelled == ["stream-A"]
        assert clients["B"].cancelled == []
        await router.close()

    asyncio.run(scenario())


def test_generic_router_cancel_uses_package_that_created_response() -> None:
    packages = {"A": CancellablePackage("A"), "B": CancellablePackage("B")}
    router = RoutedProviderPackage(
        "fake",
        [("a", packages["A"]), ("b", packages["B"])],
        key_secrets={"a": "A", "b": "B"},
        configured_keys=[("a", True), ("b", True)],
    )

    async def scenario() -> None:
        await router.execute({}, {})
        await router.cancel("resp-A", {})
        assert packages["A"].cancelled == ["resp-A"]
        assert packages["B"].cancelled == []
        await router.close()

    asyncio.run(scenario())


def test_stream_router_hides_first_key_failure_and_raises_after_all_keys_fail() -> None:
    async def collect(stream):
        return [item async for item in stream]

    async def scenario() -> None:
        router = ProviderKeyRouter.build(
            "fake",
            {"api_keys": [{"key_id": "a", "api_key": "A"}, {"key_id": "b", "api_key": "B"}]},
            lambda secret: FakeStreamClient(secret, fail=secret == "A"),
        )
        assert await collect(router.stream({})) == [{"key": "B", "delta": "ok"}]
        await router.close()

        all_failed = ProviderKeyRouter.build(
            "fake",
            {"api_keys": [{"key_id": "a", "api_key": "A"}, {"key_id": "b", "api_key": "B"}]},
            lambda secret: FakeStreamClient(secret, fail=True),
        )
        try:
            try:
                await collect(all_failed.stream({}))
            except ProviderException as exc:
                assert exc.error.code == "RATE_LIMITED"
            else:
                raise AssertionError("all failed stream must raise the final ProviderException")
        finally:
            await all_failed.close()

    asyncio.run(scenario())


def test_generic_stream_router_hides_first_key_failure_and_raises_after_all_keys_fail() -> None:
    async def collect(stream):
        return [item async for item in stream]

    async def scenario() -> None:
        package = RoutedProviderPackage(
            "fake",
            [
                ("a", FakeStreamPackage("A", fail=True)),
                ("b", FakeStreamPackage("B", fail=False)),
            ],
            key_secrets={"a": "A", "b": "B"},
            configured_keys=[("a", True), ("b", True)],
        )
        assert await collect(package.stream({}, {})) == [{"key": "B", "delta": "ok"}]
        await package.close()

        all_failed = RoutedProviderPackage(
            "fake",
            [
                ("a", FakeStreamPackage("A", fail=True)),
                ("b", FakeStreamPackage("B", fail=True)),
            ],
            key_secrets={"a": "A", "b": "B"},
            configured_keys=[("a", True), ("b", True)],
        )
        try:
            try:
                await collect(all_failed.stream({}, {}))
            except ProviderException as exc:
                assert exc.error.code == "RATE_LIMITED"
            else:
                raise AssertionError("all failed generic stream must raise the final ProviderException")
        finally:
            await all_failed.close()

    asyncio.run(scenario())


def test_context_length_limit_is_not_misclassified_as_key_failure() -> None:
    error = ProviderException(ErrorObject(
        type="validation",
        code="CONTEXT_LENGTH_EXCEEDED",
        message="context length limit exceeded",
        provider_status=400,
    ))
    assert is_key_specific_failure(error)[0] is False


def test_generic_permission_denied_does_not_fail_over_without_key_evidence() -> None:
    error = ProviderException(ErrorObject(
        type="provider_error",
        code="PERMISSION_DENIED",
        message="This model is not available to the current account.",
        provider_status=403,
    ))
    assert is_key_specific_failure(error)[0] is False


def test_explicit_403_key_failure_can_fail_over() -> None:
    error = ProviderException(ErrorObject(
        type="authentication_error",
        code="PERMISSION_DENIED",
        message="Provider rejected the API key.",
        provider_status=403,
        details={"key_failure": True},
    ))
    assert is_key_specific_failure(error)[0] is True


def test_key_preview_keeps_only_edges_and_never_returns_short_secret() -> None:
    secret = "sk-live-0123456789abcdef"
    preview = preview_provider_key(secret)
    assert preview == f"{secret[:5]}…{secret[-5:]}"
    assert secret not in preview
    assert preview_provider_key("short") == "sh…rt"
    assert preview_provider_key("x") == "•••"
    assert preview_provider_key("") is None


def test_generic_router_exposes_preview_and_disabled_keys_without_secret() -> None:
    secret = "generic-upstream-secret-012345"
    package = RoutedProviderPackage(
        "fake",
        [("primary", FakePackage())],
        key_secrets={"primary": secret, "backup": "backup-secret"},
        configured_keys=[("primary", True), ("backup", False)],
    )

    statuses = package.key_statuses()
    assert [item["key_id"] for item in statuses] == ["primary", "backup"]
    assert statuses[0]["key_preview"] == f"{secret[:5]}…{secret[-5:]}"
    assert statuses[1]["status"] == "disabled"
    serialized = repr(statuses)
    assert secret not in serialized
    assert "backup-secret" not in serialized


def test_generic_router_rebuilds_packages_when_key_pool_changes() -> None:
    initial = ReloadableFakePackage("initial-secret")

    def build(secret: str, settings: object) -> ReloadableFakePackage:
        del settings
        return ReloadableFakePackage(secret)

    package = RoutedProviderPackage(
        "fake",
        [("primary", initial)],
        key_secrets={"primary": "initial-secret"},
        configured_keys=[("primary", True)],
        package_factory=build,
    )

    async def scenario() -> None:
        await package.reload_config(
            {"api_keys": [{"key_id": "backup", "api_key": "new-upstream-secret", "enabled": True}]}
        )
        result = await package.execute(object(), object())
        assert result.metadata["key"] == "new-upstream-secret"
        statuses = package.key_statuses()
        assert [item["key_id"] for item in statuses] == ["backup"]
        assert statuses[0]["key_preview"] == "new-u…ecret"
        await package.close()

    asyncio.run(scenario())


def test_router_close_drains_in_flight_client_before_closing() -> None:
    async def scenario() -> None:
        client = BlockingClient("A")
        router = ProviderKeyRouter("fake", [("a", client)])
        task = asyncio.create_task(router.call("create", {}))
        await client.started.wait()

        await router.close()
        assert client.closed is False

        client.release.set()
        assert await task == {"key": "A"}
        assert client.closed is True

    asyncio.run(scenario())


def test_router_reserves_round_robin_key_before_concurrent_call_waits() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        clients: dict[str, ConcurrentClient] = {}

        def factory(secret: str) -> ConcurrentClient:
            clients[secret] = ConcurrentClient(secret, release)
            return clients[secret]

        router = ProviderKeyRouter.build(
            "fake",
            {
                "api_keys": [
                    {"key_id": "a", "api_key": "A"},
                    {"key_id": "b", "api_key": "B"},
                ]
            },
            factory,
        )
        first = asyncio.create_task(router.call("create", {}))
        second = asyncio.create_task(router.call("create", {}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert clients["A"].calls == 1
        assert clients["B"].calls == 1

        release.set()
        assert {result["key"] for result in await asyncio.gather(first, second)} == {
            "A",
            "B",
        }
        await router.close()

    asyncio.run(scenario())


def test_generic_router_close_drains_in_flight_package_before_closing() -> None:
    async def scenario() -> None:
        package = BlockingPackage("A")
        router = RoutedProviderPackage(
            "fake",
            [("a", package)],
            key_secrets={"a": "A"},
            configured_keys=[("a", True)],
        )
        task = asyncio.create_task(router.execute({}, {}))
        await package.started.wait()

        await router.close()
        assert package.closed is False

        package.release.set()
        result = await task
        assert result.metadata["key"] == "A"
        assert package.closed is True

    asyncio.run(scenario())


def test_generic_router_reserves_round_robin_package_for_concurrent_calls() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        first_package = ConcurrentPackage("A", release)
        second_package = ConcurrentPackage("B", release)
        router = RoutedProviderPackage(
            "fake",
            [("a", first_package), ("b", second_package)],
            key_secrets={"a": "A", "b": "B"},
            configured_keys=[("a", True), ("b", True)],
        )

        first = asyncio.create_task(router.execute({}, {}))
        second = asyncio.create_task(router.execute({}, {}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert first_package.calls == 1
        assert second_package.calls == 1

        release.set()
        results = await asyncio.gather(first, second)
        assert {result.metadata["key"] for result in results} == {"A", "B"}
        await router.close()

    asyncio.run(scenario())


def test_generic_router_replacing_same_key_resets_health_state() -> None:
    initial = ReloadableFakePackage("old-secret")

    def build(secret: str, settings: object) -> ReloadableFakePackage:
        del settings
        return ReloadableFakePackage(secret)

    package = RoutedProviderPackage(
        "fake",
        [("primary", initial)],
        key_secrets={"primary": "old-secret"},
        configured_keys=[("primary", True)],
        package_factory=build,
    )
    old_state = package._states["primary"]
    old_state.status = "exhausted"
    old_state.calls = 9
    old_state.successes = 3
    old_state.failures = 6
    old_state.last_error_code = "AUTHENTICATION_ERROR"
    old_state.last_used_at = 123.0
    old_state.cooldown_until = 9999999999.0

    async def scenario() -> None:
        await package.reload_config(
            {"api_keys": [{"key_id": "primary", "api_key": "new-secret", "enabled": True}]}
        )
        new_state = package._states["primary"]
        assert new_state is not old_state
        assert new_state.api_key == "new-secret"
        assert new_state.status == "healthy"
        assert new_state.cooldown_until == 0.0
        assert new_state.calls == 0
        assert new_state.successes == 0
        assert new_state.failures == 0
        assert new_state.last_error_code is None
        assert new_state.last_used_at is None
        await package.close()

    asyncio.run(scenario())


def test_generic_router_rejects_all_disabled_reload() -> None:
    package = RoutedProviderPackage(
        "fake",
        [("primary", ReloadableFakePackage("secret"))],
        key_secrets={"primary": "secret"},
        configured_keys=[("primary", True)],
        package_factory=lambda secret, settings: ReloadableFakePackage(secret),
    )

    async def scenario() -> None:
        with pytest.raises(ValueError, match="没有启用的 api_keys"):
            await package.reload_config(
                {
                    "api_keys": [
                        {"key_id": "primary", "api_key": "secret", "enabled": False}
                    ]
                }
            )
        assert package.key_statuses()[0]["status"] == "healthy"
        await package.close()

    asyncio.run(scenario())
