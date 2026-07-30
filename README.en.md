# Kemo Provider Gateway

<p align="center">
  <img src="kemo-adapter-api.png" alt="Kemo Gateway Logo" width="200">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <strong>A unified multi-provider model gateway for agents and knowledge graphs.</strong>
</p>

<p align="center">
  Normalize vendor-specific requests, streaming events, tool calls, capabilities, errors,<br>
  and token accounting into the stable Kemo Provider Protocol.
</p>

<p align="center">
  <a href="https://github.com/kesepain-KE/kemo-adapter-api"><img src="https://img.shields.io/badge/gateway-0.7.2-blue" alt="Gateway version 0.7.2"></a>
  <img src="https://img.shields.io/badge/Kemo%20Protocol-1.0-7c5cff" alt="Kemo Protocol 1.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache License 2.0"></a>
</p>

---

## Every vendor has its own protocol. That is the problem.

Each model vendor — request format, streaming events, tool calls, capability declarations, error semantics, token accounting. Every layer means another round of adaptation.

When an agent runtime talks to vendors directly, every new provider requires rewriting the same protocol translation layer. Worse, authentication, key management, and reachability probes are scattered across the codebase, making maintenance harder over time.

Kemo Gateway is built for this.

It is not another model aggregation interface. It is a translation layer: the agent expresses itself through one stable protocol, and the gateway translates that into each vendor's native language. When the vendor responds, the gateway translates back.

Every vendor-specific behavior lives inside a dedicated `providers/<provider_id>/` package. The core code contains no vendor-name branches.

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

---

## What it offers

| Scenario | What the gateway brings |
|----------|------------------------|
| Multi-vendor unification | One stable Kemo protocol on top; vendor differences are isolated in dedicated Provider Packages |
| Model discovery and capability query | Returns the models a key can actually invoke, with real capability declarations |
| Native multimodal Assets | Image, audio, video, and file input plus generated media use strict Content Blocks, authenticated Assets, and SSE contracts |
| Embedding and Rerank | Dedicated vectorization and reranking contracts for knowledge graph scenarios |
| Per-key access control | Every gateway key can allow all, deny all, or allow only selected models |
| Hot-reload at runtime | Provider settings, gateway keys, system prompts, and provider/model toggles take effect without restart |
| Provider-owned probes | Reachability testing is owned by each Provider; the core consumes a normalized result only |
| Recoverable streaming | SQLite WAL stores idempotency records, terminal responses, and SSE events for heartbeats, reconnects, and safe replay |
| Web console | Manage providers, models, keys, statistics, call logs, versions, and restarts from a browser |
| Agent status awareness | `GET /status` uses an independent `STATUS_TOKEN` for read-only gateway snapshots |

These are not isolated features. They share one goal: let the agent focus on understanding the user, without caring which vendor is behind the request.

---

## Public API

The public surface contains model, retrieval, and read-only status APIs. Administration APIs are private.

| Method and path | Purpose |
| --- | --- |
| `GET /model/models` | List the Kemo models available to the current key |
| `GET /model/models/{model}/capabilities` | Read the declared capabilities of one model |
| `GET /v1/models` | Model-discovery compatibility endpoint |
| `POST /model/responses` | Create a text or multimodal JSON/SSE response |
| `GET /model/responses/{response_id}` | Retrieve a response |
| `POST /model/responses/{response_id}/cancel` | Cancel a response |
| `POST /assets` | Stream-upload a multimodal input Asset |
| `GET /assets/{asset_id}` | Read Asset metadata and status |
| `GET /assets/{asset_id}/content` | Authenticated download with Range support |
| `DELETE /assets/{asset_id}` | Delete a temporary Asset owned by the current subject |
| `POST /model/embeddings` | Generate query or document embeddings in batches |
| `POST /model/rerank` | Rerank candidate documents |
| `GET /status` | Read a gateway status snapshot for an external agent |

`GET /v1/models` is a discovery-only compatibility endpoint. The gateway does not expose `/v1/chat/completions` or `/chat/completions`; inference must use `POST /model/responses`.

See [api.md](api.md) for request fields, authentication, SSE, idempotency, Embedding, Rerank, and error contracts.

Production streams emit an SSE comment heartbeat every 15 seconds by default and persist execution records and emitted
events in `storage/executions/executions.sqlite3`. A client disconnect does not cancel the Provider execution in the
current process; during the default 24-hour retention window, the same request and `Last-Event-ID` resume at the next
event. A gateway restart never re-runs the upstream request: unfinished work becomes
`incomplete/gateway_restarted`. The core also enforces a 900-second fallback timeout, a 64-execution single-process
limit, and consistent retry semantics. See [api.md](api.md) for the exact boundaries and environment variables.

### Public model names

Every public model name follows the rule `<provider_id>-<vendor_model_name>`. For example, `deepseek-deepseek-v4-flash`. The registry stores exact ownership; the gateway does not guess ownership by splitting on arbitrary hyphens and does not accept the legacy `provider_id/model` format.

---

## Quick start

### Requirements

- Python 3.11 or newer
- Network access to the configured Python and frontend package registries

The deployment module installs Python dependencies automatically and uses the pinned pnpm version through
npm when pnpm is not installed. When Node.js is unavailable on Windows or Linux, it downloads an LTS
release from the official Node.js distribution service, verifies its SHA-256 checksum, and installs it
under `web/frontend/.runtime/` without administrator privileges or system-wide changes.

### 1. Initialize the project

```powershell
python setup.py
```

Running without arguments performs a complete deployment: it installs Python dependencies, rebuilds the
Web console, and creates `.env` from `.env.example` only when `.env` does not already exist. Use
`python setup.py --check` to validate an existing deployment without installing or building anything.

### 2. Configure a gateway key

For a normal deployment, create the hot-reloadable key file from the example:

```powershell
Copy-Item api/keys.json.example api/keys.json
```

Replace the sample token and configure `scopes` and `allowed_models`. A `null` allowlist permits every model, an empty list denies every model, and a non-empty list is a strict model allowlist. The real `api/keys.json` is ignored by Git.

For initial bootstrap or emergency access, a single `GATEWAY_API_KEY` may be placed in `.env` instead. Environment variables are read only at process startup and require a restart after changes.

### 3. Install a Provider

Real deployment-specific `providers/*` packages are not committed by default. Create a local provider from `template/provider/`, or let an agent follow [agent_control.md](agent_control.md) and [ADD_DIY/README.md](ADD_DIY/README.md) to build and verify one.

Adding a Provider directory or changing Python, manifests, or dependencies requires a restart. Existing Provider `config.json` and `secrets.json` files can be hot-reloaded.

### 4. Start the gateway

```powershell
python start_web.py
```

Default endpoints:

- Web console: `http://127.0.0.1:7531/admin`
- Kemo model catalog: `http://127.0.0.1:7531/model/models`
- OpenAPI: disabled by default; enable `API_DOCS_ENABLED=true` only in a trusted development environment

`HOST`, `PORT`, `LOG_LEVEL`, and the externally advertised `GATEWAY_BASE_URL` are loaded from `.env`. When the gateway is published behind a reverse proxy or domain, set `GATEWAY_BASE_URL` to that external address. It is displayed and copied by the console; it does not change the listener or route layout.

---

## Authentication boundaries

The gateway deliberately uses three independent credential classes:

| Credential | Purpose | Configuration |
| --- | --- | --- |
| Gateway invocation key | Models, Embedding, Rerank | `api/keys.json` or startup settings |
| Web credentials | `/admin` and protected management APIs | `WEB_TOKEN`, username, and password in `.env` |
| Status token | Read-only `GET /status` access | `STATUS_TOKEN` in `.env` |

When both a Web token and username/password are configured, the token check runs first and the password check runs second. Both session stages expire after two hours. The Web token is submitted through the login form only and must never be placed in a URL. Successful login creates an opaque server-side session carried by an `HttpOnly`, `SameSite=Strict` cookie; state-changing requests also require a CSRF token.

When all three Web credentials are empty, the gateway enters no-login owner mode. LAN addresses and `0.0.0.0` binds are allowed so a trusted local network can use the console directly. Every client that can reach the management console has owner privileges in this mode. Public deployments must therefore configure both `WEB_TOKEN` and the `WEB_USERNAME`/`WEB_PASSWORD` pair, terminate HTTPS at a trusted reverse proxy, publish an `https://` `GATEWAY_BASE_URL`, configure `WEB_ALLOWED_HOSTS`, and enforce network access controls. The API-key page returns masked identifiers only; full gateway keys and Provider header secrets are never sent back to the browser.

`STATUS_TOKEN` must not match a model invocation key or Web token. The status API never returns raw gateway keys, Provider secrets, request bodies, raw vendor responses, or stack traces.

---

## Connect the kemo-agent status extension

kemo-agent `v0.6.0` includes `global_expand/kemo_gateway_status/`. The extension is inactive by default. It reads `GET /status` with a dedicated status token only after explicit user authorization, and it never calls restart, key-management, or Provider-configuration administration APIs.

### 1. Configure a status token on the gateway

Add a new dedicated token to the gateway `.env`:

```dotenv
STATUS_TOKEN=replace-with-a-dedicated-random-token
```

Environment variables are read at startup, so restart the gateway after changing this value. It must not match `WEB_TOKEN`, `GATEWAY_API_KEY`, or any model invocation key in `api/keys.json`; otherwise `/status` refuses to start.

### 2. Ask the main agent to activate the extension

Explicitly ask kemo-agent to activate the Kemo gateway status extension and provide the gateway root URL and status token. The main agent performs the equivalent structured call:

```text
expand_call(
  scope="global",
  module="kemo_gateway_status",
  command="activate",
  params={
    "base_url": "http://127.0.0.1:7531",
    "status_token": "<dedicated STATUS_TOKEN>"
  }
)
```

The extension validates the endpoint and response contract before it saves local configuration or enables prompt injection. It generates a concise status summary, a strict allow-list JSON snapshot, and a `1600×900` PNG chart covering runtime phase, version, Providers and models, success rate, latency, cache hit rate, and token usage.

When the gateway is published behind a reverse proxy, FRP tunnel, or domain, `base_url` must be the exact external root URL reachable from kemo-agent. The status client refuses HTTP redirects so that the token cannot be forwarded to a host other than the configured origin.

### 3. Refresh, inspect, or deactivate

- `refresh` collects a new snapshot immediately and may target a statistics date;
- `configuration_status` reads local activation state without a network request or token disclosure;
- `deactivate` removes the local kemo-agent credentials, snapshots, and chart without stopping or modifying the gateway.

See [api.md](api.md#智能体全局感知接口) for the complete status fields and error semantics.

---

## Hot reload versus restart

| Change | Restart required |
| --- | --- |
| `api/runtime.json` or `api/keys.json` | No |
| Provider `config.json` or `secrets.json` | No |
| Highest-priority system prompt or provider/model switches | No |
| `.env` variables | Yes |
| Python, Provider manifests, dependencies, or protocol models | Yes |
| Adding or removing a Provider directory | Yes |
| Web front-end source or build output | Yes |

Graceful restart:

```powershell
python restart.py --reason "update configuration"
python restart.py --status
```

The restart module drains in-flight requests before restarting. Before stopping the old process, an isolated Python process preflights the new environment, frontend artifact, and backend imports. The replacement then validates the new `.env` HOST/PORT through health checks before reporting success; if startup fails, it makes a best-effort rollback to the old startup environment. The web console also provides confirmation, progress feedback, and status polling. When authentication settings are unchanged, the two-hour Web session is handed off securely so a graceful restart does not immediately log the browser out.

### Self-update

```powershell
python update.py
```

Enter a menu number to check and install updates, inspect status, restore a backup, or repair tracked source. No command suffix is required.
Normal updates are verified fast-forwards only. Local-ahead or diverged histories are never overwritten, and an up-to-date checkout never implies a source reset. The updater pins the exact inspected remote commit and creates a cold backup under `.backup/` before changing Git HEAD. It rejects the entire operation when a remote commit touches `.env`, API keys, Providers, statistics, Assets, runtime state, or the private developer directory. Front-end changes reuse the cross-platform `setup.py` toolchain to rebuild on Windows or Linux.

Choose source repair only when tracked source is damaged or the normal updater explicitly cannot continue. Repair creates a Git recovery reference first and preserves deployment environment variables, keys, Providers, and statistics.

---

## Provider development

The only authoritative template is `template/provider/`. A Provider must at minimum implement or declare:

- **Contract** — abstract methods in `ProviderPackage` (`core/provider_contract.py`)
- **Protocol** — `protocol.py` maps KemoRequest ↔ vendor request
- **Streaming** — `streaming.py` translates vendor SSE events into envelope-free `ProviderEvent` values
- **Capabilities** — `capabilities.py` declares supported tasks, modalities, tools, and reasoning levels
- **Probing** — `probe.py` implements connectivity and model reachability probes
- **Contract verification** — `test_contract.py` contains test cases that validate the implementation against the ProviderPackage interface

There is no second authoritative reference beyond `template/provider/`. After implementing a provider package, run the contract tests before putting it into service.

Full Kemo mode uses `POST /model/responses` for conversation, vision, ASR, TTS, speech conversion, image
generation/editing, video understanding, and video generation. `metadata.capability`, input/output modalities,
and `extensions.operations` must all agree. Large media goes through `/assets`; Providers read input or register
output through `RequestContext.assets`, while public responses expose only an Asset ID, verified MIME type, and
SHA-256—not a local gateway path. Vendor endpoints, formats, and usage accounting remain entirely inside each
Provider package.

See [ADD_DIY/provider-package.md](ADD_DIY/provider-package.md) for the creation workflow.

---

## A gateway is more than connectors

Kemo Gateway does not try to be an all-encompassing platform.

It aims to be a stable protocol bridge:

- Adding a new vendor does not require changing core code;
- Switching models does not require changing the caller's code;
- When a vendor updates its API, only that Provider Package needs updating;
- Keys and configuration can change at runtime without interrupting service.

The agent on top can keep talking through the same protocol. The vendor differences, upgrades, and swaps — they stay behind this translation layer.
