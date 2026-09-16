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
  <a href="https://github.com/kesepain-KE/kemo-adapter-api"><img src="https://img.shields.io/badge/gateway-0.8.1-blue" alt="Gateway version 0.8.1"></a>
  <img src="https://img.shields.io/badge/Kemo%20Protocol-1.0-7c5cff" alt="Kemo Protocol 1.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache License 2.0"></a>
</p>

---

## 0.8.1 SSE persistence and disk I/O stability

This release reduces high-frequency SQLite writes without changing the public Kemo protocol, event payloads, or resume contract:

- **Bounded in-memory batches:** each response owns an isolated queue of unpublished SSE events. A batch commits after 50 ms, 32 events, or immediately at a terminal boundary.
- **Publish after commit:** events become visible to subscribers only after SQLite `COMMIT` succeeds, so `Last-Event-ID`, exact replay, and restart recovery continue to use durable boundaries only.
- **Persistent SQLite connection:** the gateway reuses one lock-protected WAL connection for its lifetime, removing per-event connections, PRAGMA initialization, and `SELECT MAX(sequence)`. Sequence reservation stays in memory while database primary-key and event-ID uniqueness constraints remain the final guard.
- **Atomic terminal persistence:** pending events, the terminal event, and the final response row commit in one transaction. The redundant terminal `save()` is removed, and terminal or shutdown paths flush immediately.
- **Cancellation-safe consistency:** once a SQLite worker begins committing, cancellation is delayed until database and in-memory replay state agree. Background persistence failures wake subscribers instead of leaving an SSE stream waiting forever.

In a local synthetic run with 1,698 events, response transactions fell from 1,700 to 55, process write operations from 19,940 to 2,252, and written bytes from about 50.0 MiB to 4.96 MiB. These figures demonstrate removal of transaction amplification; identical percentages are not promised across hardware, filesystems, or concurrent workloads.

Gateway and console versions are **`0.8.1`**; the Kemo Protocol remains **`1.0`**. Existing databases, gateway keys, and Provider configuration require no migration. Restart the gateway after upgrading, and rebuild the frontend to synchronize its package version.

## 0.8.0 guided workflows, unified tests, and bounded statistics caching

This release improves maintainability and the management console without changing the public Kemo protocol:

- **Task-specific agent guidance:** separate recipes for creating Providers, adding models, updating capabilities, single-model multimodal support, reasoning levels, and key rotation. Existing implementations must not be overwritten with templates.
- **Offline template checks:** validate model catalogs, manifests, capabilities, and reasoning mappings, with paired request examples for nine operations. Passing a template test does not prove real upstream support.
- **One test entry point:** `python -m tests` runs source and template suites grouped by responsibility. Local Provider and real restart tests are opt-in; shared builders no longer import test cases.
- **Bounded statistics read cache:** daily/hourly metrics, trends, rankings, key totals, and log pages reuse short-lived results, capped at 128 entries and 8 MiB of serialized payload with a 30-second TTL. Writes invalidate cached results immediately; invocation and SSE persistence remains transactional.
- **Console and safe previews:** clearer cards, read-only gateway configuration, and backend-generated first-five/last-three credential masks. Short credentials remain fully hidden; complete credentials and arbitrary private JSON are not returned by these views.

Existing updater plan previews, backups, safe fast-forward checks, Provider hot configuration, and in-flight request protection remain available.
Gateway and console versions are **`0.8.0`**; the Kemo Protocol remains **`1.0`**.
Existing key files require no migration. Source and frontend changes require a rebuild and restart.

Gateway statistics writes invalidate the cache immediately. External database edits may remain unseen until the remaining 30-second TTL expires.
This is not an authentication cache or a model token-cache metric, and no fixed disk IOPS improvement is promised.
See [statistics storage boundaries](storage/README.md) and [release verification](ADD_DIY/release.md).

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

The call-log hour picker always displays the total number of calls in each period. Card colors are driven only by terminal outcomes: successful calls contribute green, failed calls contribute red, and both gradients are blended when both outcomes are present. Cancelled or non-terminal calls are not misclassified as either result.

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
limit, and consistent retry semantics. Non-terminal events commit in bounded per-response batches of up to 50 ms or
32 events; only committed events are published to SSE clients, while terminal events flush atomically without waiting.
See [api.md](api.md) for the exact boundaries and environment variables.

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

The single-token `GATEWAY_API_KEY` remains available as a commented quick-start entry in `.env.example`. It is useful
for connecting one agent during first-time setup or emergency recovery. Routine management and restart-free rotation
should use `api/keys.json`; do not maintain both sources long term. Environment variables are read only at process
startup and require a restart after changes.

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

## Short guide for small automation agents

Use this decision table before editing. Do not copy a template over an existing Provider.

| Goal | Edit only | Restart? |
| --- | --- | --- |
| Create a new Provider | Copy `template/provider/` to `providers/<provider_id>/`, then replace every example file | Yes |
| Add a model to an existing Provider | The Provider's model registry, `capabilities.py`, `manifest.json`, protocol mapping, and tests | Yes |
| Change an upstream Provider key | `providers/<provider_id>/secrets.json` → `api_keys` | No |
| Change a gateway caller key or model allowlist | `api/keys.json` | No |
| Change a Provider URL or timeout | `providers/<provider_id>/config.json` | No |
| Change Python, dependencies, a manifest, or the web build | The affected source files | Yes |

The safe order is always: read the current Provider and tests, make the smallest change, run the
contract tests, then report what changed and whether a restart is required. Never put an upstream key in
`.env`, `config.json`, source code, Markdown, logs, or a commit.

The canonical upstream key file is:

```json
{
  "api_keys": [
    {"key_id": "primary", "api_key": "provider-key-a", "enabled": true},
    {"key_id": "backup-1", "api_key": "provider-key-b", "enabled": true}
  ]
}
```

Keep at least one enabled entry. The gateway selects keys in configured order and reserves the next cursor position
before starting an upstream attempt; concurrent requests are therefore distributed in approximate start order, not a
promise of strict per-request round-robin fairness. Only a 401/402/429, or an explicitly key-specific 403, that identifies the
current credential as the cause is handled before output is produced; an ordinary model-permission or parameter 403
does not trigger failover. The gateway tries the next enabled key and reports an error only after all eligible keys
fail. Invalid request parameters, unknown models, or output that has already started are not silently replayed.

For a new model, use the full public name `<provider_id>-<vendor_model_name>` and keep the same complete
key in the Provider registry, `MODEL_CAPABILITIES`, and `manifest.json`. Declare only abilities verified by
tests. The standard Kemo reasoning levels are `minimal`, `low`, `medium`, `high`, and `max`; `xhigh` is a
compatibility value only when the Provider explicitly maps it to a real upstream setting.

---

## Authentication boundaries

The gateway deliberately uses three independent credential classes:

| Credential | Purpose | Configuration |
| --- | --- | --- |
| Gateway invocation key | Models, Embedding, Rerank | `api/keys.json` (legacy `.env` compatibility only) |
| Web credentials | `/admin` and protected management APIs | `WEB_TOKEN`, username, and password in `.env` |
| Status token | Read-only `GET /status` access | `STATUS_TOKEN` in `.env` |

When both a Web token and username/password are configured, the token check runs first and the password check runs second. Both session stages expire after two hours. The Web token is submitted through the login form only and must never be placed in a URL. Successful login creates an opaque server-side session carried by an `HttpOnly`, `SameSite=Strict` cookie; state-changing requests also require a CSRF token.

When all three Web credentials are empty, the gateway enters no-login owner mode. LAN addresses and `0.0.0.0` binds are allowed so a trusted local network can use the console directly. Every client that can reach the management console has owner privileges in this mode. In loopback mode (`HOST=127.0.0.1` or `::1`), a direct request to `127.0.0.1`, `localhost`, or `::1` deliberately bypasses stale Web credentials left in `.env`; that bypass is disabled when a public `GATEWAY_BASE_URL` is configured or a reverse proxy supplies an external `Host`, `X-Forwarded-Host`, or `Forwarded` host. Public deployments must configure both `WEB_TOKEN` and the `WEB_USERNAME`/`WEB_PASSWORD` pair, terminate HTTPS at a trusted reverse proxy, publish an `https://` `GATEWAY_BASE_URL`, configure `WEB_ALLOWED_HOSTS`, and enforce network access controls. The proxy must preserve the external Host or forward it correctly instead of rewriting every request as a loopback Host. The API-key list returns masked values by default; an owner may use the session-, origin-, and CSRF-protected reveal action to retrieve one gateway invocation key briefly. Provider upstream keys and Provider header secrets are never sent to the browser.

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

### Read-only gateway configuration

System Settings → Gateway Configuration shows the settings loaded by the current process: network, Web access policy, concurrency/timeouts, SSE, and media limits.
This page has no save or secret-reveal action. `GET /admin/api/system/gateway-config` requires management permissions and has no write endpoint.
The backend generates credential previews using the first five and last three characters, separated by `…`.
Empty values and credentials of ten characters or fewer remain `***`. Multiple keys receive separate previews.
Provider previews read only standard key fields, never arbitrary private JSON or headers; this endpoint does not return complete credentials.
Refreshing does not reload `.env`; environment changes still require a restart. Launcher logging, browser opening, and restart-script parameters are outside this view.

### Self-update

```powershell
python update.py
```

Enter a menu number to check and install updates, inspect status, restore a backup, repair tracked source, preview a plan, or inspect the current state. No command suffix is required.
The repository-root `update.py` is the only recommended entrypoint. Its implementation is split by responsibility under the `update/` package, and `python -m update` invokes the same application instead of maintaining a second updater.
Automation and remote terminals can use explicit commands:

```powershell
python update.py --check                 # read-only remote/version check
python update.py --apply --dry-run       # preview without changing the worktree, config, or runtime data (refreshes Git check refs)
python update.py --apply --yes           # apply after external confirmation
python update.py --status                # version, commit, and source state
python update.py --list-backups          # list cold backups
python update.py --restore-backup latest # restore the newest source backup
```

`--repo-url` and `--branch` apply only to the current operation and never rewrite the local `origin`.
`--remote-version-url` is a read-only fallback for checking a version when Git is unavailable; it
does not authorize source writes. Git output is decoded through a UTF-8 boundary on both Windows and
Linux. Command diagnostics are redacted so tokens, passwords, and credential-bearing URLs are not
printed.
Normal updates are verified fast-forwards only. Local-ahead or diverged histories are never overwritten, and an up-to-date checkout never implies a source reset. The updater pins the exact inspected remote commit and creates a cold backup under `.backup/` before changing Git HEAD. It rejects the entire operation when a remote commit touches `.env`, API keys, Providers, statistics, Assets, runtime state, or the private developer directory. Front-end changes reuse the cross-platform `setup.py` toolchain to rebuild on Windows or Linux.
The updater keeps backup identifiers unique even when two operations start within the same second. During a write it publishes a short-lived `.update.maintenance` marker and holds a cross-platform lock so two updater processes cannot write concurrently.
Before reporting success, the updater requires a clean Git index, no conflict markers, successful Python compilation, a valid frontend artifact, and a successful `start_web.py --preflight`. Any failure restores the previous commit and the original local changes, so source containing `<<<<<<<` is never handed to the launcher.

Choose source repair only when tracked source is damaged or the normal updater explicitly cannot continue. Repair creates a Git recovery reference first and preserves deployment environment variables, keys, Providers, and statistics. It realigns tracked source to the verified remote commit; the previous source remains in `.backup/` but is not reapplied over the repaired copy.

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

When a reasoning model requires its reasoning text or encrypted vendor state to be replayed during a tool
continuation, the Provider must declare that requirement truthfully through
`ReasoningCapabilities.persisted_state` and preserve the complete response → Kemo reasoning item → next vendor
request round trip. Persisted state must stay bound to the original Provider and model; it must never be injected
as ordinary message text or reused across Providers or models.

Full Kemo mode uses `POST /model/responses` for conversation, vision, ASR, TTS, speech conversion, image
generation/editing, video understanding, and video generation. `metadata.capability`, input/output modalities,
and `extensions.operations` must all agree. Large media goes through `/assets`; Providers read input or register
output through `RequestContext.assets`, while public responses expose only an Asset ID, verified MIME type, and
SHA-256—not a local gateway path. Vendor endpoints, formats, and usage accounting remain entirely inside each
Provider package.

### Guided recipes for automation agents

Start with the [task selector](ADD_DIY/tasks.md) and fill in the target directory and scope before editing.
The detailed recipes are in Chinese and designed for agents with limited context or reasoning capacity:

- [Create a provider](ADD_DIY/create-provider.md): stage safely, implement in order, retain contract tests, then register.
- [Add models or update capabilities](ADD_DIY/model-maintenance.md): synchronize the catalog, modify one model, map reasoning levels.
- [Single-model multimodality](ADD_DIY/multimodal.md): nine operations, input/output media, Assets, and negative tests.
- [Keys and allowlists](ADD_DIY/keys-and-secrets.md): distinguish upstream pools from gateway caller tokens and preserve unrelated entries.
- [Executable teaching examples](template/examples/README.md): paired requests/capabilities, simulated five-level mapping, and incremental updates.

The provider skeleton does not advertise unverified advanced features or invent numerical limits.
Catalog and teaching-example tests run offline without real keys; passing them does not prove upstream availability.
Prefer all five reasoning levels when verified mappings exist, but expose only verified levels rather than inventing support.
See the [provider reference](ADD_DIY/provider-package.md) and [task-specific verification matrix](ADD_DIY/verification.md).

### Single test entry point

Run `python -m tests` from the repository root for the source suite, or `python -m tests --list` to list groups.
Use `python -m tests --suite templates -q` for templates and
`python -m tests --suite protocol --suite providers -q` for protocol and key-routing tests.
Local unpublished providers and real process-replacement tests are opt-in. Shared test builders live in
`tests/support/`; test modules do not import other test modules. See [tests/README.md](tests/README.md).

---

## A gateway is more than connectors

Kemo Gateway does not try to be an all-encompassing platform.

It aims to be a stable protocol bridge:

- Adding a new vendor does not require changing core code;
- Switching models does not require changing the caller's code;
- When a vendor updates its API, only that Provider Package needs updating;
- Keys and configuration can change at runtime without interrupting service.

The agent on top can keep talking through the same protocol. The vendor differences, upgrades, and swaps — they stay behind this translation layer.

### One Provider, multiple upstream keys

Upstream keys are stored explicitly in `providers/<provider_id>/secrets.json`; they are not moved into
`.env` or `PROVIDER_SETTINGS_JSON`. The canonical format is an ordered `api_keys` array. The console only
shows key IDs, masked previews, health state, and counters; it never returns the complete key.

Enabled keys are selected in configured order from a cursor reserved before each upstream attempt; concurrent
requests are therefore distributed in approximate start order, but are not guaranteed to be perfectly even. Before any stream output is emitted, a
clear credential/quota/rate-limit failure continues through the remaining eligible keys without exposing the
intermediate error to the caller. An ordinary model-permission or parameter error must not trigger failover. If every enabled and eligible key fails, the
gateway returns one final sanitized error. Key cooldown and health state are runtime routing state; the
24-hour/7-day/30-day statistics views do not define a time-based key schedule. Upstream-key counters are
in-memory routing diagnostics and reset after a restart or Provider rebuild; persisted gateway-key rankings
refer to caller tokens, not upstream Provider keys.
