<p align="center">
  <img src="docs/logo.svg" alt="synix" width="420"/>
</p>

<p align="center">
  Pre-computes conversation summaries so Claude Code compaction is instant instead of a 30-second stall.
</p>

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/marklubin/double-buffer-proxy/main/install.sh | sh
```

```sh
# Add to ~/.zshrc or ~/.bashrc
alias claude="synix-proxy"
```

That's it. Run `claude` as normal.

## What It Does

<p align="center">
  <img src="docs/diagram.svg" alt="Claude Code → Synix → Anthropic API" width="680"/>
</p>

A local proxy that sits between Claude Code and the Anthropic API on your machine. It pre-computes a conversation checkpoint at 70% context utilization. When Claude auto-compacts at 80%, the proxy returns the checkpoint instantly — no API call, no wait.

**Runs entirely on your machine.** No third-party servers, no data leaves your network. The proxy runs in a local Docker/Podman container and only communicates with `api.anthropic.com` — the same endpoint Claude Code already talks to. Your API keys and conversation data never touch anything else.

![Claude Code with SYNIX_ON status](docs/claude.png)

## Dashboard

Real-time monitoring of all active conversations at `https://localhost:8443/dashboard`.

![Dashboard showing conversation state and event log](docs/dash.png)

## Commands

```sh
claude                     # start proxy + launch Claude Code
claude proxy-help          # proxy-specific help
claude proxy-update        # update proxy image + wrapper
synix-proxy status         # check if proxy is running
synix-proxy stop           # stop the proxy container
synix-proxy logs           # tail structured JSON logs
synix-proxy dashboard      # print dashboard URL
synix-proxy report-bug     # open GitHub issue form
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNIX_CHECKPOINT_THRESHOLD` | `0.70` | Pre-compute checkpoint at this % of context window |
| `SYNIX_SWAP_THRESHOLD` | `0.80` | Mark checkpoint ready to serve at this % |
| `SYNIX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SYNIX_PASSTHROUGH` | `false` | Disable buffer logic (pure proxy mode) |
| `SYNIX_PROXY_PORT` | `47200` | CONNECT redirector port |
| `SYNIX_DASHBOARD_PORT` | `8443` | Dashboard/proxy port |

```sh
SYNIX_CHECKPOINT_THRESHOLD=0.60 synix-proxy
```

## How It Works

The proxy uses Claude Code's native `HTTPS_PROXY` support, scoped to the `claude` process only. A CONNECT redirector intercepts `api.anthropic.com` traffic and routes it to the proxy; everything else (OAuth, MCP servers, telemetry) passes through untouched. No `/etc/hosts`, no system DNS changes, no root required.

### Buffer Lifecycle

```
0%────────────70%────────────80%────────100%
│              │              │           │
│    Normal    │  Proxy       │  Claude   │
│   operation  │  checkpoints │  compacts │
│              │  (background)│  (instant)│
```

| Phase | What happens |
|-------|-------------|
| **IDLE** | Normal operation. Proxy tracks token usage. |
| **CHECKPOINTING** | Background API call to pre-compute a conversation summary. |
| **WAL_ACTIVE** | Checkpoint ready. New messages recorded in write-ahead log. |
| **SWAP_READY** | Pre-computed checkpoint ready to serve on next compact request. |

The proxy never initiates compaction — Claude Code drives the process. When Claude sends a compact request, the proxy returns the pre-computed checkpoint if available, or forwards to the API if not. The proxy never blocks or degrades the experience.

## FAQ

**Why do I see multiple conversations in the dashboard?**

Each Claude Code session gets its own conversation in the proxy. If you open multiple terminals, or restart Claude Code, each gets tracked separately. The proxy identifies conversations by a session UUID embedded in Claude Code's API requests. Old conversations are automatically cleaned up after 2 hours of inactivity.

**How does conversation deduplication work?**

The proxy fingerprints each conversation using the `metadata.user_id` field that Claude Code sends with every API request. This contains a session UUID that uniquely identifies each Claude Code process. If the same session reconnects (e.g., after a brief network interruption), the proxy picks up where it left off with the same checkpoint state. Different sessions — even in the same project directory — are tracked independently.

**What if the proxy is down when Claude compacts?**

If the proxy container stops or the health check fails, Claude Code falls back to its native compaction behavior automatically. The proxy never blocks — if anything goes wrong, Claude Code talks directly to the Anthropic API as if the proxy wasn't there.

**Does this work with Claude Max / OAuth?**

Yes. The proxy forwards OAuth `Authorization: Bearer` tokens transparently. It works with both API keys (`x-api-key`) and OAuth sessions.

**Can I use this with multiple models?**

Yes. The proxy tracks context windows per-model (Opus, Sonnet, Haiku all have their own context window sizes configured). Each conversation is tracked with the model it's using.

**Found a bug?**

```sh
synix-proxy report-bug    # opens GitHub issue form
```

Or file directly at [github.com/marklubin/double-buffer-proxy/issues](https://github.com/marklubin/double-buffer-proxy/issues).

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
synix-proxy uninstall
```
