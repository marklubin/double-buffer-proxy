# Claude DB Proxy

Double-buffer context window management for Claude Code. Transparently extends Claude's effective context by checkpointing and swapping conversation history before the window fills up.

```
                              ┌─────────────────────────────────────────────┐
                              │            Docker / Podman Container        │
                              │                                             │
  ┌──────────────┐  CONNECT   │  ┌───────────────┐      ┌───────────────┐  │
  │              │  tunnel    │  │   CONNECT      │ TCP  │  Main Proxy   │  │     ┌─────────────────┐
  │  Claude Code │───────────────│   Redirector   │─────▶│  :443         │──────▶│ api.anthropic.com│
  │              │            │  │   :8080        │      │               │  │     └─────────────────┘
  │  HTTPS_PROXY │            │  └───────────────┘      │  - TLS term   │  │
  │  =:8080      │            │         │               │  - Buffer mgmt│  │
  └──────────────┘            │         │ passthrough    │  - Dashboard  │  │
                              │         ▼               │  - Health API │  │
                              │  ┌───────────────┐      └───────────────┘  │
                              │  │ claude.ai,     │                         │
                              │  │ platform.      │──────▶ real destination │
                              │  │ claude.com,    │                         │
                              │  │ MCP servers    │                         │
                              │  └───────────────┘                         │
                              └─────────────────────────────────────────────┘
```

**How it works:** Claude Code natively supports `HTTPS_PROXY`. The proxy sets this env var scoped to the `claude` process only — no system-wide DNS overrides, no `/etc/hosts` changes, no root required. A tiny CONNECT redirector intercepts `api.anthropic.com` traffic and routes it to the main proxy; all other traffic (OAuth, MCP, telemetry) passes through untouched.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/marklubin/double-buffer-proxy/main/install.sh | sh
```

The installer:
- Detects Docker or Podman (prompts to install if neither found)
- Pulls the container image (or builds from source)
- Generates TLS certificates
- Installs `claude-db-proxy` to `~/.local/bin/`
- Configures the Claude Code status line to show `DB_PROXY_ON`

Then add the alias to your shell config:

```sh
# Add to ~/.zshrc or ~/.bashrc
alias claude="claude-db-proxy"
```

## Usage

```sh
claude-db-proxy                  # start proxy + launch Claude Code
claude-db-proxy status           # check if proxy is running
claude-db-proxy logs             # tail structured JSON logs
claude-db-proxy dashboard        # print dashboard URL
claude-db-proxy stop             # stop the proxy container
claude-db-proxy help             # all subcommands
```

Or use `claude` directly if you added the alias.

## How the Double Buffer Works

The proxy pre-computes conversation checkpoints in the background. When Claude Code naturally asks to compact its context, the proxy intercepts the request and returns the pre-computed checkpoint instantly — saving an API call and ~30 seconds of latency.

```
Context Window Utilization
│
│  0%─────────70%──────────────────80%───────100%
│  │           │                    │          │
│  │   IDLE    │  Proxy checkpoints │  Claude  │
│  │           │  in background     │  auto-   │
│  │           │                    │  compacts│
│  ▼           ▼                    ▼          ▼
│
│  Proxy:     Background checkpoint at 70% (pre-compute summary)
│  Claude:    Drives compaction at 80% (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80)
│  Intercept: Compact request → return pre-computed checkpoint instantly
│
│  Phase Transitions:
│
│  IDLE ──▶ CHECKPOINT_PENDING ──▶ CHECKPOINTING ──▶ WAL_ACTIVE
│                                                        │
│                                                        ▼
│                   Claude sends compact ──▶ SWAP_READY ──▶ IDLE (reset)
│                                            (proxy intercepts)
```

| Phase | What happens |
|-------|-------------|
| **IDLE** | Normal operation. Proxy tracks token usage per request. |
| **CHECKPOINT_PENDING** | Utilization crossed checkpoint threshold. Preparing background summary. |
| **CHECKPOINTING** | Calling the Anthropic compaction API in the background to pre-compute a summary. |
| **WAL_ACTIVE** | Checkpoint complete. New messages are recorded in a write-ahead log (WAL). |
| **SWAP_READY** | Utilization crossed swap threshold. Pre-computed checkpoint ready to serve. |

**Client-driven compaction:** The proxy never initiates compaction itself — Claude Code drives the process. When Claude sends a compact request (detected by the prompt "create a detailed summary of the conversation"), the proxy either returns the pre-computed checkpoint (if available) or forwards the request to the API natively. Either way, Claude Code rebuilds its internal state correctly because it initiated the compaction.

After the swap, Claude continues with full knowledge of the conversation via the compressed summary plus a WAL section containing recent messages. The cycle repeats as the window fills again.

## Dashboard

Real-time web UI for monitoring all active conversations.

```
https://localhost:8443/dashboard
```

The dashboard shows:
- **Active conversations** with model, token count, and utilization bar
- **Phase indicator** (color-coded) for each conversation
- **Message history** with checkpoint/WAL boundaries highlighted
- **Live event log** of phase transitions and API errors
- **WebSocket updates** — no polling, state pushes on every token change

## Monitoring & Management

### Health Check

```sh
curl -sk https://localhost:8443/health
```

```json
{"status": "ok", "conversations": 3, "passthrough": false}
```

### Logs

Structured JSON logs with hourly rotation (7-day retention):

```sh
# Via wrapper
claude-db-proxy logs

# Direct
tail -f ~/.local/share/claude-db-proxy/logs/dbproxy.jsonl
```

Each log line is a JSON object:

```json
{"conv_id": "97ef3a60", "from_phase": "IDLE", "to_phase": "CHECKPOINT_PENDING", "trigger": "utilization=60.2%", "event": "phase_transition", "level": "info", "timestamp": "2026-02-27T15:22:52Z"}
```

Key events to watch:
| Event | Meaning |
|-------|---------|
| `phase_transition` | Buffer state changed |
| `checkpoint_started` | Compaction API call in progress |
| `checkpoint_completed` | Summary generated successfully |
| `swap_executed` | Context window reset with summary |
| `connect_redirect` | API request routed through proxy |
| `connect_passthrough` | Non-API request passed through |

### Container Management

```sh
claude-db-proxy status              # health + running state
claude-db-proxy stop                # stop container
claude-db-proxy start               # start without launching claude
docker logs claude-db-proxy         # container stdout/stderr
```

### Reset a Conversation

```sh
# Reset all
curl -sk -X POST https://localhost:8443/v1/_reset

# Reset specific conversation
curl -sk -X POST https://localhost:8443/v1/_reset \
  -H 'Content-Type: application/json' \
  -d '{"conv_id": "97ef3a60"}'
```

## Configuration

All settings via environment variables (prefix `DBPROXY_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DBPROXY_CHECKPOINT_THRESHOLD` | `0.70` | Pre-compute checkpoint at this % of context window |
| `DBPROXY_SWAP_THRESHOLD` | `0.80` | Mark checkpoint ready to serve at this % |
| `DBPROXY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DBPROXY_PASSTHROUGH` | `false` | Disable buffer logic (pure proxy mode) |
| `DBPROXY_CONVERSATION_TTL_SECONDS` | `7200` | Inactive conversation cleanup (2 hours) |
| `DBPROXY_COMPACT_TRIGGER_TOKENS` | `50000` | Min tokens for compaction API (API minimum: 50k) |

Set them when launching:

```sh
DBPROXY_CHECKPOINT_THRESHOLD=0.50 DBPROXY_SWAP_THRESHOLD=0.70 claude-db-proxy
```

### Wrapper-Specific Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DBPROXY_PROXY_PORT` | `8080` | CONNECT redirector port |
| `DBPROXY_DASHBOARD_PORT` | `8443` | Dashboard/proxy port |
| `DBPROXY_DATA_DIR` | `~/.local/share/claude-db-proxy` | Data directory |

## Architecture

```
┌─ Host Machine ──────────────────────────────────────────────────────────┐
│                                                                         │
│  claude-db-proxy (wrapper)                                              │
│    │                                                                    │
│    ├── sets HTTPS_PROXY=http://127.0.0.1:8080    (scoped to process)   │
│    ├── sets NODE_EXTRA_CA_CERTS=~/.../ca.pem     (scoped to process)   │
│    ├── sets DBPROXY_ACTIVE=1                     (for status line)     │
│    └── exec claude "$@"                                                 │
│                                                                         │
│  ┌─ Container (Docker/Podman) ────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  CONNECT Redirector (:8080)                                         │ │
│  │    Accepts CONNECT tunnels from Node.js                             │ │
│  │    api.anthropic.com:443 ──▶ 127.0.0.1:443 (local proxy)          │ │
│  │    everything else ──▶ real destination (passthrough)               │ │
│  │                                                                     │ │
│  │  Main Proxy (:443)                                                  │ │
│  │    TLS termination (self-signed cert for api.anthropic.com)         │ │
│  │    Double-buffer context management                                 │ │
│  │    Dashboard + WebSocket (/dashboard)                               │ │
│  │    Health endpoint (/health)                                        │ │
│  │    SQLite persistence (data/dbproxy.sqlite)                         │ │
│  │    Structured logging (logs/dbproxy.jsonl)                          │ │
│  │                                                                     │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  Volumes (persisted on host):                                           │
│    ~/.local/share/claude-db-proxy/certs/   ── TLS certificates         │
│    ~/.local/share/claude-db-proxy/data/    ── SQLite database          │
│    ~/.local/share/claude-db-proxy/logs/    ── JSON log files           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why This Design

| Concern | Solution |
|---------|----------|
| **Process isolation** | `HTTPS_PROXY` is set only on the `claude` process — nothing else on the system is affected |
| **No root required** | No `/etc/hosts` changes, no port 443 on host, no system CA modifications |
| **Cross-platform** | Docker/Podman + env vars work on Linux, macOS, and WSL |
| **Auth passthrough** | OAuth (`platform.claude.com`), MCP servers, and telemetry bypass the proxy entirely |
| **TLS trust** | `NODE_EXTRA_CA_CERTS` tells Node.js to trust the proxy's self-signed cert |
| **Persistence** | SQLite WAL mode + bind mounts survive container restarts |

## Development

```sh
# Clone and install dev dependencies
git clone https://github.com/marklubin/double-buffer-proxy
cd double-buffer-proxy
uv sync --dev

# Run tests (151 tests)
uv run pytest tests/ -x -v

# Run proxy locally (without container)
uv run -m dbproxy --host 0.0.0.0 --log-level DEBUG

# Run CONNECT redirector locally
uv run -m dbproxy.connect_redirector

# Build container
docker compose build

# Start container
docker compose up -d

# Launch Claude through dev wrapper
scripts/dbproxy-claude
```

## Versioning

Releases are tagged 1-to-1 with Claude Code versions. When Claude Code `2.1.61` is released, this project releases `2.1.61` after automated testing confirms compatibility.

A GitHub Action runs daily, checks for new Claude Code versions via npm, runs the full test suite, builds the container image, and publishes to `ghcr.io`.

## Uninstall

```sh
claude-db-proxy uninstall
```

Or manually:

```sh
docker rm -f claude-db-proxy           # stop container
rm ~/.local/bin/claude-db-proxy        # remove wrapper
rm -rf ~/.local/share/claude-db-proxy  # remove data
# Remove 'alias claude="claude-db-proxy"' from your shell config
# Remove statusLine from ~/.claude/settings.json
```
