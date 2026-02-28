#!/usr/bin/env python3
"""Verify 2 complete client-driven compaction cycles with real Claude Code.

Drives a conversation through:
  Cycle 1: IDLE → checkpoint → WAL_ACTIVE → (Claude compacts) → swap → IDLE
  Cycle 2: IDLE → checkpoint → WAL_ACTIVE → (Claude compacts) → swap → IDLE

The proxy pre-computes checkpoints at DBPROXY_CHECKPOINT_THRESHOLD (default 25%).
Claude Code triggers compaction at CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (default 35%).
The proxy intercepts the compact request and returns the pre-computed checkpoint.

Proxy must be running (Docker container or direct).
Claude Code must be running in a tmux session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import ssl
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DASHBOARD_HOST = os.environ.get("E2E_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("E2E_DASHBOARD_PORT", "8443"))
TMUX_SESSION = os.environ.get("E2E_TMUX_SESSION", "ct")
LOG_FILE = os.environ.get("E2E_LOG_FILE", os.path.expanduser(
    "~/.local/share/claude-db-proxy/logs/dbproxy.jsonl"
))
# Fallback to old location if new one doesn't exist
if not os.path.exists(LOG_FILE):
    for alt in ["/tmp/proxy.log", "logs/dbproxy.jsonl"]:
        if os.path.exists(alt):
            LOG_FILE = alt
            break

BASE_URL = f"https://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
TARGET_SWAPS = int(os.environ.get("E2E_TARGET_SWAPS", "2"))
MAX_ROUNDS = int(os.environ.get("E2E_MAX_ROUNDS", "120"))
ROUND_TIMEOUT = int(os.environ.get("E2E_ROUND_TIMEOUT", "180"))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg: str) -> None:
    log(f"FAIL: {msg}")
    sys.exit(1)


def api_get(path: str) -> dict:
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, context=SSL_CTX, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log(f"WARNING: GET {path} failed: {exc}")
        return {}


def tmux_send(text: str) -> None:
    if len(text) < 800:
        subprocess.run(
            ["tmux", "send-keys", "-t", TMUX_SESSION, "-l", text],
            check=True,
        )
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            subprocess.run(["tmux", "load-buffer", tmp], check=True)
            subprocess.run(["tmux", "paste-buffer", "-t", TMUX_SESSION], check=True)
        finally:
            os.unlink(tmp)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-m"], check=True)


def _is_busy(screen: str) -> bool:
    """Detect if Claude Code is currently processing."""
    # Spinner patterns in Claude Code:
    #   ✽ Thinking...    (old style)
    #   ⏳ ...           (old style)
    #   * Architecting…  (new style: asterisk + verb + ellipsis)
    #   queued           (message queued)
    #   Churned          (context compaction)
    busy_markers = ("✽", "⏳", "Thinking", "Churned", "queued")
    if any(s in screen for s in busy_markers):
        return True
    # New spinner: "* <Verb>…" pattern (asterisk + space + capitalized word + ellipsis)
    for line in screen.split("\n"):
        stripped = line.strip()
        if stripped.startswith("* ") and ("…" in stripped or "..." in stripped):
            return True
    return False


def wait_idle(timeout: float = 120) -> bool:
    """Wait for Claude Code to finish processing (spinner disappears)."""
    start = time.time()
    started = False
    while time.time() - start < min(15, timeout):
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
            capture_output=True, text=True,
        )
        if _is_busy(r.stdout):
            started = True
            break
        time.sleep(1)
    if not started:
        time.sleep(3)
    while time.time() - start < timeout:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
            capture_output=True, text=True,
        )
        if not _is_busy(r.stdout) and ("bypass permissions" in r.stdout or ">" in r.stdout):
            return True
        time.sleep(2)
    return False


def log_events() -> list[dict]:
    """Read all structured log events from the proxy log file."""
    out = []
    try:
        with open(LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    pass
    except FileNotFoundError:
        log(f"WARNING: Log file not found: {LOG_FILE}")
    return out


def count_swaps() -> int:
    """Count swap_executed events — emitted when handle_client_compact calls execute_swap."""
    return sum(1 for e in log_events() if e.get("event") == "swap_executed")


def count_client_compacts() -> int:
    """Count client compact interception events."""
    return sum(
        1 for e in log_events()
        if e.get("event") == "client_compact_intercepted"
    )


def latest_tokens() -> int:
    for e in reversed(log_events()):
        if e.get("event") == "tokens_updated" and e.get("total", 0) > 500:
            return e.get("total", 0)
    return 0


def latest_phase() -> str | None:
    for e in reversed(log_events()):
        if e.get("event") == "request_received" and "haiku" not in e.get("model", ""):
            return e.get("phase")
    return None


PROMPT_TEMPLATES = [
    "Write a detailed 1500-word technical analysis #{n} of microservice architecture patterns including service mesh, event sourcing, CQRS, and saga patterns. Focus on {focus}. Cover tradeoffs, failure modes, and when to use each pattern. Be extremely thorough and do not reference any prior responses.",
    "Write a comprehensive 1500-word comparison #{n} of database indexing strategies: B-tree, hash, GIN, GiST, and BRIN indexes. Focus on {focus}. Include concrete examples of queries each optimizes for, storage overhead, and maintenance costs. This is a fresh request — write the full analysis.",
    "Write a detailed 1500-word explanation #{n} of distributed consensus algorithms: Raft, Paxos, and PBFT. Focus on {focus}. Cover leader election, log replication, membership changes, and Byzantine fault tolerance. Write the complete analysis from scratch.",
    "Write a thorough 1500-word analysis #{n} of memory management strategies in systems programming: stack vs heap allocation, garbage collection algorithms, reference counting, and arena allocators. Focus on {focus}. Provide the complete analysis.",
    "Write a comprehensive 1500-word guide #{n} to TLS 1.3 handshake protocol. Focus on {focus}. Cover cipher suites, key exchange mechanisms, certificate verification, 0-RTT resumption, and compare with TLS 1.2. Write the full guide.",
    "Write a detailed 1500-word analysis #{n} of container orchestration internals: how Kubernetes scheduling works, pod lifecycle, CNI networking, CSI storage, and the control plane reconciliation loop. Focus on {focus}. Write the complete analysis.",
    "Write a 1500-word deep dive #{n} into Linux kernel networking: the packet receive path from NIC interrupt through NAPI, sk_buff, netfilter hooks, and socket delivery. Focus on {focus}. Provide the full deep dive.",
    "Write a comprehensive 1500-word explanation #{n} of modern CPU cache architecture: L1/L2/L3 cache hierarchies, cache coherence protocols (MESI, MOESI), false sharing, and prefetching strategies. Focus on {focus}. Write the complete explanation.",
]

FOCUS_AREAS = [
    "real-world production incidents and post-mortems",
    "performance benchmarks and quantitative comparisons",
    "historical evolution and design decisions",
    "security implications and attack vectors",
    "debugging techniques and observability",
    "cloud-native deployments and scaling challenges",
    "academic research and theoretical foundations",
    "open-source implementations and code architecture",
    "failure recovery and fault tolerance mechanisms",
    "emerging trends and future directions",
]


def get_prompt(idx: int) -> str:
    """Generate a unique prompt for each round to prevent 'already answered' responses."""
    template = PROMPT_TEMPLATES[idx % len(PROMPT_TEMPLATES)]
    focus = FOCUS_AREAS[idx % len(FOCUS_AREAS)]
    return template.format(n=idx + 1, focus=focus)


def run() -> None:
    log("=" * 60)
    log("Double-Buffer Proxy: 2-Cycle Verification")
    log("  Architecture: Client-driven compaction")
    log("  Proxy pre-computes checkpoints, Claude Code drives compact")
    log(f"  Log file: {LOG_FILE}")
    log(f"  Dashboard: {BASE_URL}")
    log("=" * 60)

    health = api_get("/health")
    if health.get("status") != "ok":
        fail(f"Proxy unhealthy: {health}")
    log(f"Proxy OK — {health.get('conversations', 0)} active conversations")

    # Seed conversation
    log("Seeding conversation...")
    tmux_send("What is 2+2? Reply with just the number.")
    wait_idle(timeout=30)
    log(f"  tokens={latest_tokens()}")

    prompt_idx = 0

    for rnd in range(MAX_ROUNDS):
        swaps = count_swaps()
        compacts = count_client_compacts()
        tokens = latest_tokens()
        phase = latest_phase()
        log(f"Round {rnd+1}  swaps={swaps}/{TARGET_SWAPS}  compacts={compacts}  tokens={tokens}  phase={phase}")

        if swaps >= TARGET_SWAPS:
            log(f"Reached {TARGET_SWAPS} swaps!")
            break

        tmux_send(get_prompt(prompt_idx))
        prompt_idx += 1
        if not wait_idle(timeout=ROUND_TIMEOUT):
            log("  WARNING: Timed out waiting for Claude to finish")
            # Check if Claude is still alive
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
                capture_output=True, text=True,
            )
            if "error" in r.stdout.lower() or "fatal" in r.stdout.lower():
                log(f"  Screen contents: {r.stdout[-500:]}")
                fail("Claude appears to have errored out")

    # Final counts
    swaps = count_swaps()
    compacts = count_client_compacts()

    # Post-swap verification: can Claude still respond?
    log("Verifying Claude still works post-swap...")
    tmux_send("What is 7+7? Reply with just the number.")
    post_ok = wait_idle(timeout=30)

    # Collect phase transitions for report
    transitions = [
        e for e in log_events()
        if e.get("event") == "phase_transition"
    ]
    client_compact_events = [
        e for e in log_events()
        if e.get("event") == "client_compact_intercepted"
    ]

    log("=" * 60)
    log("PHASE TRANSITIONS:")
    for t in transitions:
        log(f"  {t.get('from_phase')} → {t.get('to_phase')}  ({t.get('trigger', '')})")
    log("")
    log("CLIENT COMPACT EVENTS:")
    for c in client_compact_events:
        log(f"  action={c.get('action')}  conv_id={c.get('conv_id', '?')}")
    log("=" * 60)
    log("RESULTS")
    log(f"  swaps_completed:    {swaps}/{TARGET_SWAPS}  {'PASS' if swaps >= TARGET_SWAPS else 'FAIL'}")
    log(f"  client_compacts:    {compacts}")
    log(f"  post_swap_alive:    {'PASS' if post_ok else 'FAIL'}")
    log(f"  final_tokens:       {latest_tokens()}")
    log(f"  final_phase:        {latest_phase()}")
    log("=" * 60)

    if swaps >= TARGET_SWAPS and post_ok:
        log("OVERALL: PASS")
    else:
        log("OVERALL: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    run()
