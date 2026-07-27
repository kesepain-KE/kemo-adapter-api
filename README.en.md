# Kemo Provider Gateway

<p align="center">
  <img src="kemo-adapter-api.png" alt="Kemo Gateway Logo" width="200">
</p>

<p align="center">
  <strong>A unified multi-provider model gateway for agents and knowledge graphs</strong>
</p>

<p align="center">
  Normalize vendor-specific requests, streaming events, tool calls, capabilities, errors,<br>
  and token accounting into the stable Kemo Provider Protocol.
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <a href="https://github.com/kesepain-KE/kemo-adapter-api"><img src="https://img.shields.io/badge/gateway-0.6.0-blue" alt="Gateway version 0.6.0"></a>
  <img src="https://img.shields.io/badge/Kemo%20Protocol-1.0-7c5cff" alt="Kemo Protocol 1.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache License 2.0"></a>
</p>

---

## What this project does

Kemo Gateway sits between agent runtimes, knowledge graphs, and model vendors. Clients use one stable
protocol while every vendor-specific behavior is isolated inside a dedicated
`providers/<provider_id>/` package.

```text
kemo-agent / kemo-graph / other Kemo clients
                       │
                       ▼
              Kemo Provider Protocol
                       │
                       ▼
        Provider Registry + Gateway Core
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     Provider A   Provider B   Provider C
```

Adding a vendor should not introduce vendor-name branches into the gateway core. Authentication,
request mapping, stream parsing, tool calls, error mapping, token semantics, and reachability probes
belong to that vendor's Provider Package.

## Highlights

- **Provider isolation** — each vendor owns its protocol, configuration, secrets, capabilities, usage,
  errors, and probing behavior.
- **Unified model execution** — non-streaming and SSE LLM responses, multi-turn input, reasoning levels,
  and tool calls.
- **Built for kemo-graph** — dedicated Embedding and Rerank request/response contracts.
- **Model discovery** — returns the models the current key can actually invoke, not an unrestricted
  registry dump.
- **Capability discovery** — exposes task type, modalities, tools, streaming, reasoning, and task-specific
  limits.
- **Per-key allowlists** — allow every model, deny every model, or allow only selected models.
- **Live control** — provider settings, gateway keys, the highest-priority system prompt, and
  provider/model switches can be hot-reloaded.
- **Provider-owned probes** — the core consumes a normalized `ProviderProbeResult` and never guesses a
  vendor's test protocol.
- **Web console** — manages providers, models, keys, statistics, call logs, versions, and restarts.
- **Read-only agent awareness** — `GET /status` uses an independent `STATUS_TOKEN` and returns a redacted
  gateway snapshot.

## Public API

The public surface contains model, retrieval, and read-only status APIs. Web administration APIs are
private and documented separately.

| Method and path | Purpose |
| --- | --- |
| `GET /model/models` | List the Kemo models available to the current key |
| `GET /model/models/{model}/capabilities` | Read the declared capabilities of one model |
| `GET /v1/models` | Model-discovery compatibility endpoint |
| `POST /model/responses` | Create a non-streaming or SSE LLM response |
| `GET /model/responses/{response_id}` | Retrieve a response |
| `POST /model/responses/{response_id}/cancel` | Cancel a response |
| `POST /model/embeddings` | Generate query or document embeddings in batches |
| `POST /model/rerank` | Rerank candidate documents |
| `GET /status` | Read a gateway status snapshot for an external agent |

`GET /v1/models` is a discovery-only compatibility endpoint. The gateway does not expose
`/v1/chat/completions` or `/chat/completions`; inference must use the Kemo-native
`POST /model/responses` contract.

See [api.md](api.md) for request fields, authentication, SSE, idempotency, Embedding, Rerank, and error
contracts.

## Public model names

Every public model name follows this rule:

```text
<provider_id>-<vendor_model_name>
```

If the provider ID is `deepseek` and the upstream model is `deepseek-v4-flash`, the public name is:

```text
deepseek-deepseek-v4-flash
```

Vendor model names may contain additional hyphens. The registry stores exact ownership; the gateway
does not guess ownership by splitting on arbitrary hyphens and does not accept the legacy
`provider_id/model` format.

## Quick start

### Requirements

- Python 3.11 or newer
- Node.js
- pnpm

### 1. Initialize the project

Run from the project root:

```powershell
python setup.py --install-dependencies --build-frontend --init-env
```

This installs Python dependencies, builds the Web console, and creates `.env` from `.env.example` only
when `.env` does not already exist.

### 2. Configure a gateway key

For a normal deployment, create the hot-reloadable key file from the example:

```powershell
Copy-Item api/keys.json.example api/keys.json
```

Replace the sample token and configure `scopes` and `allowed_models`. A `null` allowlist permits every
model, an empty list denies every model, and a non-empty list is a strict model allowlist. The real
`api/keys.json` is ignored by Git.

For initial bootstrap or emergency access, a single `GATEWAY_API_KEY` may be placed in `.env` instead.
Environment variables are read only at process startup and require a restart after changes.

### 3. Install a Provider

Real deployment-specific `providers/*` packages are not committed by default. Create a local provider
from `template/provider/`, or let an agent follow [agent_control.md](agent_control.md) and
[ADD_DIY/README.md](ADD_DIY/README.md) to build and verify one.

Adding a Provider directory or changing Python, manifests, or dependencies requires a restart. Existing
Provider `config.json` and `secrets.json` files can be hot-reloaded.

### 4. Start the gateway

```powershell
python start_web.py
```

Default endpoints:

- Web console: `http://127.0.0.1:7531/admin`
- Kemo model catalog: `http://127.0.0.1:7531/model/models`
- OpenAPI: `http://127.0.0.1:7531/docs`

`HOST`, `PORT`, `LOG_LEVEL`, and the externally advertised `GATEWAY_BASE_URL` are loaded from `.env`.
When the gateway is published behind a reverse proxy or domain, set `GATEWAY_BASE_URL` to that external
address. It is displayed and copied by the console; it does not change the listener or route layout.

## Authentication boundaries

The gateway deliberately uses three independent credential classes:

| Credential | Purpose | Configuration |
| --- | --- | --- |
| Gateway invocation key | Models, Embedding, Rerank, and Asset APIs | `api/keys.json` or startup settings |
| Web credentials | `/admin` and protected management APIs | `WEB_TOKEN`, username, and password in `.env` |
| Status token | Read-only `GET /status` access | `STATUS_TOKEN` in `.env` |

When both a Web token and username/password are configured, the token check runs first and the password
check runs second. Both session stages expire after two hours. If all three Web credential fields are
empty, the console enters passwordless owner mode; always configure management credentials on an
untrusted network.

`STATUS_TOKEN` must not match a model invocation key or Web token. The status API never returns raw
gateway keys, Provider secrets, request bodies, raw vendor responses, or stack traces.

## Hot reload versus restart

| Change | Restart required |
| --- | --- |
| `api/runtime.json` or `api/keys.json` | No |
| Provider `config.json` or `secrets.json` | No |
| Highest-priority system prompt or provider/model switches | No |
| `.env` variables | Yes |
| Python, Provider manifests, dependencies, or protocol models | Yes |
| Added or removed Provider directories | Yes |
| Web frontend source or build output | Yes |

Graceful restart commands:

```powershell
python restart.py --reason "update configuration"
python restart.py --status
```

The restart controller enters Drain and waits for in-flight requests. The Web console adds confirmation,
elapsed-time feedback, and status polling around the same operation.

## Provider development

`template/provider/` is the only authoritative Provider template. A Provider must implement or explicitly
declare:

- Kemo-to-vendor request mapping;
- non-streaming result and streaming event conversion;
- tool calls and parallel tool calls;
- real semantics for tokens, cached tokens, reasoning tokens, and media units;
- normalized and redacted error mapping;
- verified capabilities for every model;
- a minimal vendor-owned reachability probe;
- redacted golden fixtures and contract tests.

Capabilities must not be inferred from marketing pages, and the core must not invent missing vendor usage.
Follow these documents:

- [Agent operation index](agent_control.md)
- [Agent DIY entry point](ADD_DIY/README.md)
- [Provider creation rules](ADD_DIY/provider-package.md)
- [Release verification checklist](ADD_DIY/verification.md)
- [Provider template guide](template/provider/README.md)

## Repository layout

```text
api/                    Public APIs, authentication, routes, and SSE
core/                   Execution core, unified models, registry, and runtime control
providers/              Deployment-local Provider packages (not committed by default)
storage/                Daily SQLite statistics and redacted call logs
template/provider/      The single Provider creation template
ADD_DIY/                Agent procedures for creating and modifying gateway components
web/backend/            Private FastAPI management APIs
web/frontend/           React 19 + TypeScript + Vite console
tests/                  Gateway and Provider-boundary tests
agent_control.md        Main agent operation index
api.md                  Complete public API documentation
start_web.py            Gateway and Web console entry point
restart.py              Graceful restart controller
update.py               Update module
version.json            Gateway and Kemo Protocol versions
```

## Development checks

```powershell
python -m compileall core api web/backend template/provider
python -m pytest -q
Set-Location web/frontend
pnpm run build
```

Before release, also run `git diff --check` and verify that `.env`, `api/keys.json`, Provider secrets,
runtime databases, logs, caches, and real vendor responses are not staged.

## Project status

The current gateway version is `0.6.0`; the Kemo Protocol version is `1.0`. LLM, Embedding, Rerank,
model discovery, capability discovery, usage statistics, call logs, per-key model allowlists, the Web
console, read-only status awareness, and the Provider creation template are implemented.

Work continues on more production Provider packages, cross-process execution and response persistence,
the complete Asset API, Provider State services, and stream recovery.

## Related projects

- [kemo-agent](https://github.com/kesepain-KE/kemo-agent) — the primary agent client of Kemo Gateway.
- `kemo-graph` — the intended consumer of the Embedding and Rerank APIs.
- [votx-agent](https://github.com/kesepain-KE/votx-agent) — an independently maintained agent project.

## Maintainer and license

Maintainer: [@kesepain](https://github.com/kesepain-KE)

Licensed under the [Apache License 2.0](LICENSE).
