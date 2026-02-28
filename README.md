# Double Buffer Proxy

Stop losing context mid-task. This proxy sits between Claude Code and the Anthropic API, pre-computing conversation summaries in the background so compaction is instant instead of a 30-second stall.

![Claude Code with DB_PROXY_ON status](docs/claude.png)

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/marklubin/double-buffer-proxy/main/install.sh | sh
```

Then alias it so every `claude` session goes through the proxy:

```sh
# Add to ~/.zshrc or ~/.bashrc
alias claude="claude-db-proxy"
```

That's it. Run `claude` as normal — the proxy handles everything transparently.

## What It Does

Claude Code compacts its context when the window fills up. Normally this means a blocking API call that takes ~30 seconds while Claude generates a summary. During that time, you wait.

This proxy pre-computes that summary at 70% utilization. When Claude asks to compact at 80%, the proxy returns the pre-computed checkpoint instantly. No API call, no wait.

```
0%────────────70%────────────80%────────100%
│              │              │           │
│    Normal    │  Proxy       │  Claude   │
│   operation  │  checkpoints │  compacts │
│              │  (background)│  (instant)│
```

If no checkpoint is ready when Claude asks, the request passes through to the API normally — the proxy never blocks or degrades the experience.

## Dashboard

Real-time monitoring of all active conversations at `https://localhost:8443/dashboard`.

![Dashboard showing conversation state and event log](docs/dash.png)

## Commands

```sh
claude                   # start proxy + launch Claude Code
claude proxy-help        # proxy-specific help
claude proxy-update      # update proxy image + wrapper
claude-db-proxy status   # check if proxy is running
claude-db-proxy stop     # stop the proxy container
claude-db-proxy logs     # tail structured JSON logs
claude-db-proxy dashboard  # print dashboard URL
```

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DBPROXY_CHECKPOINT_THRESHOLD` | `0.70` | Pre-compute checkpoint at this % of context window |
| `DBPROXY_SWAP_THRESHOLD` | `0.80` | Mark checkpoint ready to serve at this % |
| `DBPROXY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DBPROXY_PASSTHROUGH` | `false` | Disable buffer logic (pure proxy mode) |
| `DBPROXY_PROXY_PORT` | `8080` | CONNECT redirector port |
| `DBPROXY_DASHBOARD_PORT` | `8443` | Dashboard/proxy port |

```sh
DBPROXY_CHECKPOINT_THRESHOLD=0.60 claude-db-proxy
```

## How It Works

The proxy uses Claude Code's native `HTTPS_PROXY` support. A CONNECT redirector intercepts `api.anthropic.com` traffic and routes it to the proxy; all other traffic (OAuth, MCP servers, telemetry) passes through untouched. No system-wide DNS changes, no `/etc/hosts`, no root required.

```
┌─ Host ──────────────────────────────────────────────────────────────────┐
│                                                                         │
│  claude-db-proxy (wrapper)                                              │
│    ├── HTTPS_PROXY=http://127.0.0.1:8080    (scoped to process)        │
│    ├── NODE_EXTRA_CA_CERTS=~/.../ca.pem     (scoped to process)        │
│    └── exec claude "$@"                                                 │
│                                                                         │
│  ┌─ Container (Docker/Podman) ────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  CONNECT Redirector (:8080)                                         │ │
│  │    api.anthropic.com → local proxy                                  │ │
│  │    everything else   → real destination                             │ │
│  │                                                                     │ │
│  │  Main Proxy (:443)                                                  │ │
│  │    TLS termination · double-buffer logic · dashboard · health API   │ │
│  │                                                                     │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### Buffer Lifecycle

| Phase | What happens |
|-------|-------------|
| **IDLE** | Normal operation. Proxy tracks token usage. |
| **CHECKPOINTING** | Background API call to pre-compute a conversation summary. |
| **WAL_ACTIVE** | Checkpoint ready. New messages recorded in write-ahead log. |
| **SWAP_READY** | Pre-computed checkpoint ready to serve on next compact request. |

The proxy never initiates compaction — Claude Code drives the process. When Claude sends a compact request, the proxy returns the pre-computed checkpoint if available, or forwards to the API if not.

## Development

```sh
git clone https://github.com/marklubin/double-buffer-proxy
cd double-buffer-proxy
uv sync --dev

uv run pytest tests/ -x -v          # 151 tests
uv run -m dbproxy --log-level DEBUG  # run proxy locally
docker compose build && docker compose up -d  # container
```

## Uninstall

```sh
claude-db-proxy uninstall
```
